"""Driver for the Nordic Semiconductor Power Profiler Kit II (PPK2).

The PPK2 is a USB CDC serial device (VID 0x1915, PID 0xC00A), not a VISA
instrument. Commands are single bytes, optionally followed by arguments;
measurements stream continuously at 100 kS/s as 4-byte little-endian words:

    bits  0-13  ADC value (multiply by 4 before calibrating)
    bits 14-16  measurement range 0..4 (shunt R0..R4)
    bits 18-23  sample counter (+1 per sample, mod 64)
    bits 24-31  logic inputs D0..D7

Calibration and conversion follow Nordic's nRF Connect Power Profiler
(pc-nrfconnect-ppk), including its spike filter for range switches. The
device reports per-unit calibration as text ("R0: 998.95...", ..., "END")
in reply to GET_META_DATA.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

SAMPLE_RATE = 100_000
DT = 1.0 / SAMPLE_RATE
VDD_MIN_MV, VDD_MAX_MV = 800, 5000
ADC_MULT = 1.8 / 163840
USB_VID, USB_PID = 0x1915, 0xC00A


class Cmd:
    AVERAGE_START = 0x06
    AVERAGE_STOP = 0x07
    DEVICE_RUNNING_SET = 0x0C     # DUT power: 0x0C 0x01 on / 0x0C 0x00 off
    REGULATOR_SET = 0x0D          # 0x0D mV_hi mV_lo
    SET_POWER_MODE = 0x11         # 0x11 0x01 ampere meter / 0x11 0x02 source meter
    GET_META_DATA = 0x19
    RESET = 0x20


MODES = {"ampere": 1, "source": 2}
MODE_NAMES = {v: k for k, v in MODES.items()}

# Uncalibrated defaults (pc-nrfconnect-ppk), used for anything the device omits.
DEFAULT_MODIFIERS = {
    "R": [1031.64, 101.65, 10.15, 0.94, 0.043],
    "GS": [1.0] * 5, "GI": [1.0] * 5, "O": [0.0] * 5,
    "S": [0.0] * 5, "I": [0.0] * 5, "UG": [1.0] * 5,
}

_META_ROW = re.compile(r"^\s*([A-Za-z]+?)(\d)?\s*:\s*(\S+)\s*$")


def voltage_bytes(mv: int) -> bytes:
    """REGULATOR_SET argument: the source voltage in mV, big-endian.
    (Nordic's code spells this as 3 + (mV-768)//256, (mV-768)%256.)"""
    mv = int(round(mv))
    if not VDD_MIN_MV <= mv <= VDD_MAX_MV:
        raise ValueError(f"PPK2 source voltage must be {VDD_MIN_MV}..{VDD_MAX_MV} mV")
    return bytes([mv >> 8, mv & 0xFF])


# ---------------------------------------------------------------- calibration
@dataclass
class Calibration:
    modifiers: dict = field(default_factory=lambda: {k: list(v) for k, v in
                                                     DEFAULT_MODIFIERS.items()})
    info: dict = field(default_factory=dict)       # HW, mode, VDD, IA, Calibrated ...

    @classmethod
    def parse(cls, text: str) -> "Calibration":
        cal = cls()
        for line in text.splitlines():
            m = _META_ROW.match(line)
            if not m:
                continue
            key, idx, value = m.group(1), m.group(2), m.group(3)
            k = key.upper()
            if idx is not None and k in cal.modifiers:
                try:
                    cal.modifiers[k][int(idx)] = float(value)
                except (ValueError, IndexError):
                    pass
            elif idx is None:
                cal.info[key] = value
        return cal

    def _arrays(self):
        if not hasattr(self, "_np"):
            self._np = {k: np.asarray(v, dtype=float) for k, v in self.modifiers.items()}
        return self._np

    def amps(self, adc: np.ndarray, rng: np.ndarray, vdd_mv: float) -> np.ndarray:
        """Calibrated current in A. `adc` is the 14-bit field already x4."""
        m = self._arrays()
        r = np.minimum(rng, 4)
        x = (adc - m["O"][r]) * (ADC_MULT / m["R"][r])
        return m["UG"][r] * (x * (m["GS"][r] * x + m["GI"][r])
                             + (m["S"][r] * (vdd_mv / 1000.0) + m["I"][r]))

    @property
    def calibrated_flag(self) -> str:
        return self.info.get("Calibrated", "?")


def decode_words(data: bytes):
    """Raw stream bytes (a multiple of 4) -> (adc*4, range, counter, logic)."""
    w = np.frombuffer(data, dtype="<u4")
    adc = (w & 0x3FFF).astype(np.float64) * 4
    rng = ((w >> 14) & 0x7).astype(np.intp)
    cnt = ((w >> 18) & 0x3F).astype(np.int16)
    logic = (w >> 24).astype(np.uint8)
    return adc, rng, cnt, logic


# ---------------------------------------------------------------- spike filter
class SpikeFilter:
    """nRF Connect PPK's range-switch spike filter, vectorised.

    The reference runs two exponential averages over every sample (alpha 0.18,
    and 0.06 for range 4). For the 3 samples starting at each range change the
    output is replaced by the average; the first 2 samples in range 4 after a
    change are left out of both averages. An exponential average only "sees"
    the last few hundred samples, so instead of looping in Python we evaluate
    it with a finite window (weight lost beyond it: ~2e-11) at just the samples
    that need it. State carries across chunks.
    """
    ALPHA, ALPHA4, SAMPLES = 0.18, 0.06, 3
    WINDOW = 400

    def __init__(self):
        self.reset()
        k = np.arange(self.WINDOW)[::-1]          # oldest first
        self._w = self.ALPHA * (1 - self.ALPHA) ** k
        self._w4 = self.ALPHA4 * (1 - self.ALPHA4) ** k
        # The oldest weight absorbs the tail, as if the average started there.
        self._w[0] = (1 - self.ALPHA) ** (self.WINDOW - 1)
        self._w4[0] = (1 - self.ALPHA4) ** (self.WINDOW - 1)

    def reset(self):
        self.hist = None            # last WINDOW values included in the averages
        self.prev_range = None
        self.since_change = 1 << 30

    def __call__(self, amps: np.ndarray, rng: np.ndarray) -> np.ndarray:
        n = amps.size
        if n == 0:
            return amps
        if self.prev_range is None:
            self.prev_range = int(rng[0])
        prev = np.concatenate(([self.prev_range], rng[:-1]))
        change = rng != prev
        idx = np.arange(n)
        last = np.where(change, idx, -1)
        np.maximum.accumulate(last, out=last)
        since = np.where(last >= 0, idx - last, self.since_change + 1 + idx)
        filt = since < self.SAMPLES
        excluded = filt & (rng == 4) & (since < 2)
        if self.hist is None:
            first = amps[~excluded][:1]
            self.hist = np.full(self.WINDOW, first[0] if first.size else amps[0])
        out = amps.copy()
        if filt.any():
            inc = ~excluded
            series = np.concatenate((self.hist, amps[inc]))
            # position (in `series`) of the newest included sample at or before i
            pos = self.WINDOW - 1 + np.cumsum(inc)
            fi = np.flatnonzero(filt)
            win = series[pos[fi][:, None] - np.arange(self.WINDOW)[::-1]]
            r4 = rng[fi] == 4
            out[fi] = np.where(r4, win @ self._w4, win @ self._w)
            tail = series[-self.WINDOW:]
        else:
            tail = np.concatenate((self.hist, amps))[-self.WINDOW:]
        self.hist = tail.copy()
        self.prev_range = int(rng[-1])
        self.since_change = int(since[-1])
        return out


# ---------------------------------------------------------------- data types
@dataclass
class PpkCaps:
    idn: str = ""
    model: str = "PPK2"
    note: str = ""
    info: dict = field(default_factory=dict)


@dataclass
class PpkState:
    mode: str | None = None          # "source" | "ampere" | None (unknown)
    voltage_mv: int = 3000           # source voltage, or the DUT's supply in ampere mode
    dut_power: bool | None = None    # None: unknown (the PPK2 can't report it)
    measuring: bool = False
    spike_filter: bool = True


@dataclass
class PpkReading:
    t: float                         # app time of the newest sample
    t0: float                        # app time of current[0]
    current: np.ndarray              # A, one per 10 µs
    logic: np.ndarray                # uint8, D0..D7
    lost: int = 0                    # samples missing (counter gaps) in this block
    errors: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return int(self.current.size)


# ---------------------------------------------------------------- driver
class PPK2:
    kind = "ppk2"
    family = "PPK2"

    def __init__(self, transport):
        self.t = transport
        self.caps = PpkCaps()
        self.state = PpkState()
        self.cal = Calibration()
        self.spike = SpikeFilter()
        self.log = None
        self._rest = b""
        self._last_cnt: int | None = None
        self._t_anchor: float | None = None   # app time of sample 0 of the run
        self._n_run = 0                       # samples since the anchor (incl. lost)
        self.lost_total = 0

    @property
    def cfg(self) -> PpkState:            # what the worker re-reads after a command
        return PpkState(**vars(self.state))

    def _send(self, *data: int):
        if self.log:
            self.log("> " + " ".join(f"{b:02X}" for b in data))
        self.t.write(bytes(data))

    def errors(self) -> list[str]:
        return []

    # ---- connect ------------------------------------------------------------
    def identify(self) -> PpkCaps:
        # It may still be streaming from an earlier session: stop and drain.
        self._send(Cmd.AVERAGE_STOP)
        self.t.drain(quiet_s=0.15, max_s=3.0)
        self._send(Cmd.GET_META_DATA)
        text = self.t.read_until(b"END", timeout_s=3.0).decode("latin1", "replace")
        if "END" not in text or ":" not in text:
            raise RuntimeError(
                "No metadata from the PPK2. If it shows two serial ports, use the first "
                "one (USB interface 1); if another program (e.g. nRF Connect Power "
                "Profiler) has it open, close that first.")
        self.cal = Calibration.parse(text)
        info = self.cal.info
        try:
            self.state.voltage_mv = int(float(info.get("VDD", 3000)))
        except ValueError:
            pass
        try:
            self.state.mode = MODE_NAMES.get(int(info.get("mode", 0)))
        except ValueError:
            self.state.mode = None
        hw = info.get("HW", "?")
        self.caps = PpkCaps(
            idn=f"Nordic Power Profiler Kit II  (HW {hw})",
            note=f"Calibration read from the device (Calibrated: {self.cal.calibrated_flag})",
            info=dict(info))
        self.start()
        return self.caps

    # ---- settings ---------------------------------------------------------------
    def set_mode(self, mode: str):
        self._send(Cmd.SET_POWER_MODE, MODES[mode])
        self.state.mode = mode
        if mode == "source":
            # The regulator output follows REGULATOR_SET; make sure it matches.
            self.set_voltage(self.state.voltage_mv)

    def set_voltage(self, mv: int):
        """Source voltage (source mode). In ampere mode this only records the
        DUT's supply voltage, used for the calibration's voltage term and
        for energy figures."""
        b = voltage_bytes(mv)
        if self.state.mode == "source":
            self._send(Cmd.REGULATOR_SET, *b)
        self.state.voltage_mv = int(round(mv))

    def set_dut_power(self, on: bool):
        self._send(Cmd.DEVICE_RUNNING_SET, 1 if on else 0)
        self.state.dut_power = bool(on)

    def set_output(self, on: bool):           # "all outputs off" uses this name
        self.set_dut_power(on)

    def set_spike_filter(self, on: bool):
        self.state.spike_filter = bool(on)
        self.spike.reset()

    def start(self):
        self._rest = b""
        self._last_cnt = None
        self._t_anchor = None
        self._n_run = 0
        self.spike.reset()
        self.t.reset_input()
        self._send(Cmd.AVERAGE_START)
        self.state.measuring = True

    def stop(self):
        self._send(Cmd.AVERAGE_STOP)
        self.state.measuring = False
        self.t.drain(quiet_s=0.05, max_s=1.0)
        self._rest = b""

    def close(self):
        try:
            self._send(Cmd.AVERAGE_STOP)
        except Exception:
            pass

    # ---- streaming ----------------------------------------------------------------
    def poll(self, t: float, _cfg=None) -> PpkReading:
        """Everything that arrived since the last poll. `t` is the app time now;
        samples are timed from the device's 100 kHz clock, anchored to app time
        at the start of a run (and re-anchored if the two drift apart, e.g.
        after the host fell behind and the OS buffer overflowed)."""
        empty = PpkReading(t, t, np.empty(0), np.empty(0, np.uint8))
        if not self.state.measuring:
            return empty
        data = self._rest + self.t.read_available()
        n = len(data) // 4
        self._rest = data[n * 4:]
        if n == 0:
            return empty
        adc, rng, cnt, logic = decode_words(data[: n * 4])
        # Counter gaps = samples lost on the way (it can't see multiples of 64).
        prev = self._last_cnt if self._last_cnt is not None else (int(cnt[0]) - 1) % 64
        steps = np.diff(np.concatenate(([prev], cnt.astype(np.int32)))) % 64
        gaps = np.where(steps == 0, 64, steps) - 1
        lost = int(gaps.sum())
        self._last_cnt = int(cnt[-1])
        self.lost_total += lost
        amps = self.cal.amps(adc, rng, self.state.voltage_mv)
        if self.state.spike_filter:
            amps = self.spike(amps, rng)
        span = (n + lost) * DT
        if self._t_anchor is None or abs(self._t_anchor + (self._n_run + n + lost) * DT - t) > 0.25:
            self._t_anchor = t - span
            self._n_run = 0
        t0 = self._t_anchor + (self._n_run + gaps[0]) * DT
        self._n_run += n + lost
        # Samples after a gap are not evenly spaced; keep it simple and honest:
        # time them evenly within the block (gaps are rare) and report `lost`.
        return PpkReading(t=self._t_anchor + self._n_run * DT, t0=t0, current=amps,
                          logic=logic, lost=lost)
