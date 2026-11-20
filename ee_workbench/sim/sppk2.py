"""Simulated Nordic PPK2: a byte stream like the real device's.

The DUT model is a BLE-style sensor: 3 µA asleep, a 2.5 ms radio event
(~6-9 mA with a TX peak) every 100 ms, a 0.3 ms 400 µA wake-up every 10 ms,
plus noise. D0 is high during radio events, D1 toggles at every wake-up.
The current is encoded through this unit's own calibration (offsets, gains,
voltage term), picking the shunt range the way the hardware does, and the
first sample after a range switch carries a spike, so decoding, calibration
and the spike filter are all exercised. In source mode the DUT runs only
while DUT power is on, and its current scales with the source voltage.
"""
from __future__ import annotations

import time
from contextlib import contextmanager

import numpy as np

from ..instruments.ppk2 import ADC_MULT, SAMPLE_RATE, Cmd

CAL = {
    "R": [1003.1, 101.2, 10.21, 0.957, 0.0512],
    "GS": [0.0] * 5,
    "GI": [1.0, 0.962, 0.951, 0.941, 0.947],
    "O": [120.5, 98.2, 88.7, 75.1, 110.4],
    "S": [-7e-8, -5.6e-7, -5.8e-6, -6.1e-5, 1.5e-3],
    "I": [1.8e-7, 1.3e-6, 8.4e-5, -1.5e-4, -6.0e-3],
    "UG": [1.0] * 5,
}
# Highest current each range is used for (the PPK2 switches automatically).
RANGE_MAX = [50e-6, 500e-6, 5e-3, 60e-3, 1.0]


def metadata(vdd_mv: int, mode: int) -> bytes:
    lines = ["Calibrated: 1"]
    for key in ("R", "GS", "GI", "O"):
        lines += [f"{key}{i}: {v:.20f}" for i, v in enumerate(CAL[key])]
    lines += [f"VDD: {vdd_mv}", "HW: 9173", f"mode: {mode}"]
    for key in ("S", "I", "UG"):
        lines += [f"{key}{i}: {v:.20f}" for i, v in enumerate(CAL[key])]
    lines += ["IA: 56", "END", ""]
    return "\n".join(lines).encode()


class SimPPK2:
    def __init__(self, clock=time.monotonic, seed: int = 1):
        self.clock = clock
        self.rng = np.random.default_rng(seed)
        self.timeout_s = 2.0
        self.vdd_mv = 3000
        self.mode = 2                 # 1 ampere, 2 source
        self.dut_on = False
        self.measuring = False
        self.n = 0                    # samples generated since power-up
        self._t_last = None
        self._out = bytearray()
        self.drop_next = 0            # test hook: lose this many samples
        self.sent: list[bytes] = []   # everything written, for tests

    @contextmanager
    def temp_timeout(self, seconds):
        old, self.timeout_s = self.timeout_s, seconds
        try:
            yield
        finally:
            self.timeout_s = old

    # ---- host -> device --------------------------------------------------------
    def write(self, data):
        if isinstance(data, str):
            data = bytes(int(x, 16) for x in data.split())
        self._generate()
        self.sent.append(bytes(data))
        i = 0
        while i < len(data):
            c = data[i]
            if c == Cmd.AVERAGE_START:
                self.measuring = True
                self._t_last = self.clock()      # start of this run
                self._run_n = 0                  # samples generated in it
            elif c == Cmd.AVERAGE_STOP:
                self.measuring = False
            elif c == Cmd.GET_META_DATA:
                self._out += metadata(self.vdd_mv, self.mode)
            elif c == Cmd.DEVICE_RUNNING_SET and i + 1 < len(data):
                self.dut_on = bool(data[i + 1])
                i += 1
            elif c == Cmd.REGULATOR_SET and i + 2 < len(data):
                self.vdd_mv = (data[i + 1] << 8) | data[i + 2]
                i += 2
            elif c == Cmd.SET_POWER_MODE and i + 1 < len(data):
                self.mode = data[i + 1]
                i += 1
            i += 1

    # ---- device -> host --------------------------------------------------------
    def _dut_current(self, t: np.ndarray) -> np.ndarray:
        i = np.full(t.shape, 3e-6)
        ph = np.mod(t, 0.1)
        radio = ph < 2.5e-3
        i[radio] = 6e-3 + 3e-3 * (ph[radio] > 1.0e-3) * (ph[radio] < 1.6e-3)
        tick = (np.mod(t, 0.01) < 0.3e-3) & ~radio
        i[tick] = 400e-6
        i *= self.vdd_mv / 3000.0
        return i + self.rng.normal(0, 0.15e-6, t.shape) + 0.002 * i * self.rng.standard_normal(t.shape)

    def _encode(self, amps: np.ndarray) -> np.ndarray:
        r = np.searchsorted(RANGE_MAX, np.abs(amps))
        r = np.minimum(r, 4)
        # spike on the first sample after each range switch
        sw = np.flatnonzero(np.diff(r) != 0) + 1
        amps = amps.copy()
        amps[sw] *= 3.0
        c = {k: np.asarray(v)[r] for k, v in CAL.items()}
        x = (amps / c["UG"] - (c["S"] * self.vdd_mv / 1000 + c["I"])) / c["GI"]
        adc = x * c["R"] / ADC_MULT + c["O"]
        raw = np.clip(np.round(adc / 4), 0, 0x3FFF).astype(np.uint32)
        return raw, r.astype(np.uint32)

    def _generate(self):
        if not self.measuring or self._t_last is None:
            return
        now = self.clock()
        due = int(round((now - self._t_last) * SAMPLE_RATE, 6))   # since the run started
        n = due - self._run_n
        if n <= 0:
            return
        self._run_n = due
        n = min(n, SAMPLE_RATE * 2)            # like a full OS buffer
        idx = self.n + np.arange(n)
        self.n += n
        t = idx / SAMPLE_RATE
        if self.mode == 2 and not self.dut_on:
            amps = self.rng.normal(0, 0.15e-6, n)
        else:
            amps = self._dut_current(t)
        raw, r = self._encode(amps)
        ph = np.mod(t, 0.1)
        logic = ((ph < 2.5e-3).astype(np.uint32)
                 | ((np.floor(t / 0.01).astype(np.int64) % 2).astype(np.uint32) << 1))
        cnt = (idx % 64).astype(np.uint32)
        words = raw | (r << 14) | (cnt << 18) | (logic << 24)
        if self.drop_next:
            words = words[self.drop_next:]
            self.drop_next = 0
        self._out += words.astype("<u4").tobytes()

    def read_available(self) -> bytes:
        self._generate()
        out, self._out = bytes(self._out), bytearray()
        return out

    def read_until(self, marker: bytes, timeout_s: float) -> bytes:
        return self.read_available()

    def drain(self, quiet_s: float, max_s: float) -> None:
        self.read_available()

    def reset_input(self):
        self._out.clear()

    def clear(self):
        self.reset_input()

    def read_stb(self) -> int:
        return 0

    def close(self):
        pass
