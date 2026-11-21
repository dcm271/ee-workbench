"""Driver for Saleae logic analyzers (Logic 8, Logic Pro 8, Logic Pro 16),
through the Logic 2 software's automation API.

Saleae doesn't document the USB protocol of its current analyzers; the only
supported way to drive them is to run Logic 2 with its automation server on
(Preferences ▸ Automation, or start it with `--automation`) and talk gRPC to
it on port 10430. The transport for that (`Logic2Link`, below) wraps the
`logic2-automation` package; `sim.slogic.SimLogic2` stands in for it.

A capture is started, waited for, exported with Logic 2's binary raw-data
export into a temporary folder, and read back here. Logic 2 writes those
files itself, so it has to run on this computer. The capture stays open in
Logic 2 until the next one (or until the tab closes), so protocol decoders can
be re-run on it and it can be saved as a .sal file.

Binary export format (support.saleae.com, "Binary export format - Logic 2"),
little-endian, one file per channel (digital_<ch>.bin / analog_<ch>.bin):
    "<SALEAE>"  int32 version (0 or 1)  int32 type (0 digital, 1 analog)
  digital v0: u32 initial_state, f64 begin, f64 end, u64 n, f64[n] transitions
  digital v1: u64 chunks, then per chunk: u32 initial_state, f64 sample_rate,
              f64 begin, f64 end, u64 n, f64[n] transitions
  analog  v0: f64 begin, u64 sample_rate, u64 downsample, u64 n, f32[n]
  analog  v1: u64 waveforms, then per waveform: f64 begin, f64 trigger_time,
              f64 sample_rate, i64 downsample, u64 n, f32[n]
"""
from __future__ import annotations

import csv
import math
import os
import shutil
import struct
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import numpy as np

DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 10430
MAGIC = b"<SALEAE>"

# device_type name -> (product name, digital channels, analog channels, thresholds)
DEVICES = {
    "LOGIC_PRO_16": ("Logic Pro 16", 16, 16, [1.2, 1.8, 3.3]),
    "LOGIC_PRO_8": ("Logic Pro 8", 8, 8, [1.2, 1.8, 3.3]),
    "LOGIC_8": ("Logic 8", 8, 8, []),
    "LOGIC_16": ("Logic 16", 16, 0, []),
    "LOGIC_4": ("Logic 4", 4, 1, []),
    "LOGIC": ("Logic", 8, 0, []),
}
# What Logic 2 offers; which of these a device accepts depends on the model and
# how many channels are on. Logic 2 rejects anything else with a clear message.
DIGITAL_RATES = {
    "Pro": [500e6, 250e6, 125e6, 100e6, 50e6, 25e6, 12.5e6, 10e6, 6.25e6, 5e6, 4e6, 2e6, 1e6],
    "Logic 8": [100e6, 50e6, 25e6, 20e6, 10e6, 5e6, 4e6, 2e6, 1e6],
}
ANALOG_RATES = {
    "Pro": [50e6, 12.5e6, 6.25e6, 3.125e6, 1.5625e6, 781.25e3, 125e3, 5e3],
    "Logic 8": [10e6, 5e6, 2.5e6, 1.25e6, 625e3, 125e3, 50e3, 10e3],
}
MAX_ANALOG_POINTS = 2_000_000          # per channel, after Logic 2's downsampling

MODES = ("timed", "trigger", "manual")
TRIGGER_TYPES = {"rising": "RISING", "falling": "FALLING",
                 "pulse_high": "PULSE_HIGH", "pulse_low": "PULSE_LOW"}

# Protocol decoders: the analyzer name in Logic 2 and the settings each one needs.
# Setting names must match Logic 2's analyzer settings exactly.
DECODERS = {
    "Async Serial": {"Input Channel": 0, "Bit Rate (Bits/s)": 115200},
    "I2C": {"SCL": 0, "SDA": 1},
    "SPI": {"Clock": 0, "MOSI": 1, "MISO": 2, "Enable": 3},
}


def parse_resource(resource: str) -> tuple[str, str, int]:
    """'SALEAE::F4241' / 'SALEAE::F4241@host:10430' / 'SALEAE::' ->
    (device_id or '' for the first one, host, port)."""
    r = resource.strip()
    if r.upper().startswith("SALEAE::"):
        r = r[8:]
    host, port = DEFAULT_HOST, DEFAULT_PORT
    if "@" in r:
        r, where = r.split("@", 1)
        h, _, p = where.rpartition(":")
        if h and p.isdigit():
            host, port = h, int(p)
        elif where:
            host = where
    return r.strip(), host, port


def fmt_rate(hz: float) -> str:
    for div, unit in ((1e6, "MS/s"), (1e3, "kS/s")):
        if hz >= div:
            return f"{hz / div:g} {unit}"
    return f"{hz:g} S/s"


# ---------------------------------------------------------------- binary export
@dataclass
class DigitalTrace:
    """One channel as edges: level[i] holds from t[i] until t[i+1]
    (the last one until `end`). t[0] is the start of the data."""
    channel: int
    t: np.ndarray
    level: np.ndarray            # uint8 0/1
    end: float

    def level_at(self, x: float) -> int:
        i = int(np.searchsorted(self.t, x, side="right")) - 1
        return int(self.level[max(i, 0)])


@dataclass
class AnalogTrace:
    channel: int
    t0: float
    dt: float
    v: np.ndarray                # float32, volts

    @property
    def t(self) -> np.ndarray:
        return self.t0 + np.arange(self.v.size) * self.dt


def _header(buf: bytes) -> tuple[int, int, int]:
    if len(buf) < 16 or buf[:8] != MAGIC:
        raise ValueError("not a Saleae binary export file")
    version, kind = struct.unpack_from("<ii", buf, 8)
    if version not in (0, 1):
        raise ValueError(f"unsupported Saleae binary export version {version}")
    return version, kind, 16


def parse_digital(buf: bytes, channel: int = 0) -> DigitalTrace:
    version, kind, off = _header(buf)
    if kind != 0:
        raise ValueError("not a digital channel file")
    if version == 0:
        nchunks = 1
    else:
        (nchunks,) = struct.unpack_from("<Q", buf, off)
        off += 8
    ts, levels, end = [], [], 0.0
    for _ in range(nchunks):
        (init,) = struct.unpack_from("<I", buf, off)
        off += 4
        if version == 1:
            off += 8                                  # sample_rate
        begin, end, n = struct.unpack_from("<ddQ", buf, off)
        off += 24
        tr = np.frombuffer(buf, "<f8", n, off)
        off += 8 * n
        lv = (np.arange(n + 1) + init) % 2            # state alternates at each transition
        ts.append(np.concatenate(([begin], tr)))
        levels.append(lv.astype(np.uint8))
    if not ts:
        return DigitalTrace(channel, np.zeros(1), np.zeros(1, np.uint8), 0.0)
    t = np.concatenate(ts)
    lv = np.concatenate(levels)
    # A chunk boundary isn't a transition unless the level changed there.
    keep = np.concatenate(([True], np.diff(lv) != 0))
    return DigitalTrace(channel, t[keep], lv[keep], float(end))


def parse_analog(buf: bytes, channel: int = 0) -> AnalogTrace:
    version, kind, off = _header(buf)
    if kind != 1:
        raise ValueError("not an analog channel file")
    if version == 0:
        begin, rate, down, n = struct.unpack_from("<dqqQ", buf, off)
        off += 32
        v = np.frombuffer(buf, "<f4", n, off)
        return AnalogTrace(channel, begin, down / rate, v)
    (nwf,) = struct.unpack_from("<Q", buf, off)
    off += 8
    parts, t0, dt = [], None, None
    for _ in range(nwf):
        begin, _trig, rate, down, n = struct.unpack_from("<dddqQ", buf, off)
        off += 40
        w = np.frombuffer(buf, "<f4", n, off)
        off += 4 * n
        if t0 is None:
            t0, dt = begin, down / rate
        elif dt and begin > t0 + (sum(p.size for p in parts)) * dt + dt / 2:
            # a gap (USB couldn't keep up): fill it so the time base stays uniform
            gap = int(round((begin - t0) / dt)) - sum(p.size for p in parts)
            parts.append(np.full(gap, np.nan, np.float32))
        parts.append(w)
    if t0 is None:
        return AnalogTrace(channel, 0.0, 1.0, np.empty(0, np.float32))
    return AnalogTrace(channel, t0, dt, np.concatenate(parts).astype(np.float32))


def read_export(directory: str) -> tuple[dict[int, DigitalTrace], dict[int, AnalogTrace]]:
    digital, analog = {}, {}
    for name in os.listdir(directory):
        stem, ext = os.path.splitext(name)
        kind, _, ch = stem.rpartition("_")
        if ext != ".bin" or not ch.isdigit() or kind not in ("digital", "analog"):
            continue
        with open(os.path.join(directory, name), "rb") as fh:
            buf = fh.read()
        if kind == "digital":
            digital[int(ch)] = parse_digital(buf, int(ch))
        else:
            analog[int(ch)] = parse_analog(buf, int(ch))
    return dict(sorted(digital.items())), dict(sorted(analog.items()))


def read_table(path: str) -> tuple[list[str], list[list[str]]]:
    if not os.path.exists(path):
        return [], []
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    return (rows[0], rows[1:]) if rows else ([], [])


# ---------------------------------------------------------------- measurements
def digital_stats(tr: DigitalTrace, a: float | None = None, b: float | None = None) -> dict:
    """Edge count, frequency, duty cycle and pulse widths between a and b."""
    a = tr.t[0] if a is None else max(a, tr.t[0])
    b = tr.end if b is None else min(b, tr.end)
    out = {"edges": 0, "rising": 0, "freq": None, "period": None, "duty": None,
           "high_min": None, "high_max": None, "low_min": None, "low_max": None,
           "level": tr.level_at(a)}
    if b <= a:
        return out
    i0 = int(np.searchsorted(tr.t, a, side="right"))
    i1 = int(np.searchsorted(tr.t, b, side="right"))
    et, el = tr.t[i0:i1], tr.level[i0:i1]           # edges inside (a, b]
    out["edges"] = int(et.size)
    rises = et[el == 1]
    out["rising"] = int(rises.size)
    if rises.size >= 2:
        out["period"] = float((rises[-1] - rises[0]) / (rises.size - 1))
        out["freq"] = 1.0 / out["period"]
    # Duty cycle over the window: time high / total
    bounds = np.concatenate(([a], et, [b]))
    lv = np.concatenate(([tr.level_at(a)], el))
    dur = np.diff(bounds)
    out["duty"] = float(dur[lv == 1].sum() / (b - a))
    # Pulse widths: only pulses with both edges inside the window
    if et.size >= 2:
        w = np.diff(et)
        hi, lo = w[el[:-1] == 1], w[el[:-1] == 0]
        if hi.size:
            out["high_min"], out["high_max"] = float(hi.min()), float(hi.max())
        if lo.size:
            out["low_min"], out["low_max"] = float(lo.min()), float(lo.max())
    return out


def analog_stats(tr: AnalogTrace, a: float | None = None, b: float | None = None) -> dict | None:
    v = tr.v
    if a is not None or b is not None:
        i0 = 0 if a is None else max(0, int(math.ceil((a - tr.t0) / tr.dt)))
        i1 = v.size if b is None else min(v.size, int(math.floor((b - tr.t0) / tr.dt)) + 1)
        v = v[i0:i1]
    v = v[np.isfinite(v)]
    if not v.size:
        return None
    return {"min": float(v.min()), "max": float(v.max()), "mean": float(v.mean()),
            "rms": float(np.sqrt(np.mean(v.astype(np.float64) ** 2))),
            "pp": float(v.max() - v.min())}


def digital_envelope(tr: DigitalTrace, x0: float, x1: float, bins: int = 2000):
    """Points to draw a channel between x0 and x1. With few edges: the exact
    steps. With more edges than bins: one column per bin, full height where
    the bin has any transition, so a fast clock reads as a solid band and no
    glitch disappears at any zoom."""
    t, lv = tr.t, tr.level
    x0, x1 = max(x0, t[0]), min(x1, tr.end)
    if x1 <= x0:
        return np.empty(0), np.empty(0)
    i0 = max(0, int(np.searchsorted(t, x0, side="right")) - 1)
    i1 = int(np.searchsorted(t, x1, side="right"))
    if i1 - i0 <= bins:
        et = np.concatenate(([x0], t[i0 + 1:i1], [x1]))   # segment boundaries
        return np.repeat(et, 2)[1:-1], np.repeat(lv[i0:i1], 2).astype(float)
    edges = np.linspace(x0, x1, bins + 1)
    idx = np.searchsorted(t, edges, side="right")       # transitions up to each bin edge
    start_lv = lv[np.maximum(idx[:-1] - 1, 0)].astype(float)
    busy = np.diff(idx) > 0
    lo = np.where(busy, 0.0, start_lv)
    hi = np.where(busy, 1.0, start_lv)
    x = np.repeat(edges[:-1], 2)
    y = np.column_stack((lo, hi)).ravel()
    return np.append(x, x1), np.append(y, float(lv[idx[-1] - 1]))


# ---------------------------------------------------------------- data types
@dataclass
class LogicCaps:
    idn: str = ""
    model: str = "Saleae"
    note: str = ""
    device_id: str = ""
    digital: int = 8
    analog: int = 8
    thresholds: list = field(default_factory=list)
    digital_rates: list = field(default_factory=list)
    analog_rates: list = field(default_factory=list)
    info: dict = field(default_factory=dict)


@dataclass
class LogicConfig:
    digital: list = field(default_factory=lambda: [0, 1, 2, 3])
    analog: list = field(default_factory=list)
    digital_rate: float = 10e6
    analog_rate: float = 1.5625e6
    threshold: float | None = None
    mode: str = "timed"              # timed | trigger | manual
    duration: float = 0.1            # s, timed
    trig_channel: int = 0
    trig_type: str = "rising"        # rising | falling | pulse_high | pulse_low
    pulse_min: float | None = None   # s, pulse triggers
    pulse_max: float | None = None
    after_trigger: float = 0.05      # s recorded after the trigger
    pre_trigger: float = 0.01        # s kept before it (Logic 2 trims the rest)
    decoders: list = field(default_factory=list)   # [{"name", "label", "settings"}]


@dataclass
class LogicCapture:
    cfg: LogicConfig
    digital: dict                    # channel -> DigitalTrace
    analog: dict                     # channel -> AnalogTrace
    t_app: float = 0.0               # app time the capture finished
    wall: float = 0.0                # time.time() of the same moment
    elapsed: float = 0.0
    table_header: list = field(default_factory=list)
    table_rows: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def span(self) -> tuple[float, float]:
        lo = [d.t[0] for d in self.digital.values()] + [a.t0 for a in self.analog.values()]
        hi = [d.end for d in self.digital.values()] + \
             [a.t0 + a.v.size * a.dt for a in self.analog.values()]
        return (min(lo), max(hi)) if lo else (0.0, 0.0)


# ---------------------------------------------------------------- transport
class Logic2Link:
    """The Logic 2 automation server, behind the small interface the driver
    and the simulator share. Each method is one gRPC call (blocking)."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout_s: float = 3.0):
        try:
            from saleae import automation
        except ImportError as e:
            raise RuntimeError("The logic2-automation package is missing: "
                               ".venv/bin/pip install logic2-automation") from e
        self.a = automation
        self.timeout_s = timeout_s
        try:
            self.m = automation.Manager.connect(address=host, port=port,
                                                connect_timeout_seconds=timeout_s)
        except Exception as e:
            raise RuntimeError(
                f"Logic 2 isn't answering on {host}:{port} ({type(e).__name__}). Start "
                "Logic 2 and turn on Preferences ▸ Automation ▸ Enable automation server, "
                "or run it as  Logic-2.x.AppImage --automation") from e

    @contextmanager
    def temp_timeout(self, seconds):
        old, self.timeout_s = self.timeout_s, seconds
        try:
            yield
        finally:
            self.timeout_s = old

    def app_info(self) -> dict:
        i = self.m.get_app_info()
        v = i.api_version
        return {"app_version": i.app_version, "api": f"{v.major}.{v.minor}.{v.patch}"}

    def devices(self, simulation: bool = False) -> list[dict]:
        return [{"id": d.device_id, "type": d.device_type.name, "sim": d.is_simulation}
                for d in self.m.get_devices(include_simulation_devices=simulation)]

    def start(self, device_id: str, cfg: LogicConfig, buffer_mb: int | None = None):
        a = self.a
        dev = a.LogicDeviceConfiguration(
            enabled_digital_channels=list(cfg.digital),
            enabled_analog_channels=list(cfg.analog),
            digital_sample_rate=int(cfg.digital_rate) if cfg.digital else None,
            analog_sample_rate=int(cfg.analog_rate) if cfg.analog else None,
            digital_threshold_volts=cfg.threshold)
        if cfg.mode == "timed":
            mode = a.TimedCaptureMode(duration_seconds=cfg.duration)
        elif cfg.mode == "trigger":
            pulse = cfg.trig_type.startswith("pulse")
            mode = a.DigitalTriggerCaptureMode(
                trigger_type=getattr(a.DigitalTriggerType, TRIGGER_TYPES[cfg.trig_type]),
                trigger_channel_index=cfg.trig_channel,
                min_pulse_width_seconds=(cfg.pulse_min or 0.0) if pulse else None,
                max_pulse_width_seconds=cfg.pulse_max if pulse else None,
                after_trigger_seconds=cfg.after_trigger,
                trim_data_seconds=cfg.after_trigger + cfg.pre_trigger)
        else:
            mode = a.ManualCaptureMode()
        self.cap = self.m.start_capture(
            device_id=device_id, device_configuration=dev,
            capture_configuration=a.CaptureConfiguration(capture_mode=mode,
                                                         buffer_size_megabytes=buffer_mb))

    def wait(self):
        self.cap.wait()

    def stop(self):
        self.cap.stop()

    def close_capture(self):
        cap, self.cap = getattr(self, "cap", None), None
        if cap is not None:
            cap.close()

    def export_binary(self, directory: str, digital: list, analog: list, downsample: int):
        self.cap.export_raw_data_binary(directory, digital_channels=list(digital),
                                        analog_channels=list(analog),
                                        analog_downsample_ratio=max(1, int(downsample)))

    def export_csv(self, directory: str, digital: list, analog: list):
        self.cap.export_raw_data_csv(directory, digital_channels=list(digital),
                                     analog_channels=list(analog))

    def add_analyzer(self, name: str, label: str, settings: dict):
        return self.cap.add_analyzer(name, label=label, settings=settings)

    def remove_analyzer(self, handle):
        self.cap.remove_analyzer(handle)

    def export_table(self, path: str, handles: list):
        cfgs = [self.a.DataTableExportConfiguration(h, self.a.RadixType.HEXADECIMAL)
                for h in handles]
        self.cap.export_data_table(path, cfgs)

    def save(self, path: str):
        self.cap.save_capture(path)

    # Shapes the worker expects of every transport
    def clear(self):
        pass

    def read_stb(self) -> int:
        return 0

    def close(self):
        try:
            self.close_capture()
        except Exception:
            pass
        self.m.close()


def find_logic2(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> list[str]:
    """SALEAE::<id> for each analyzer a running Logic 2 can see. Cheap when it
    isn't running: a plain TCP connect first, so the resource list stays fast."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.2):
            pass
    except OSError:
        return []
    try:
        link = Logic2Link(host, port, timeout_s=1.0)
    except Exception:
        return []
    try:
        suffix = "" if (host, port) == (DEFAULT_HOST, DEFAULT_PORT) else f"@{host}:{port}"
        return [f"SALEAE::{d['id']}{suffix}" for d in link.devices()]
    except Exception:
        return []
    finally:
        try:
            link.m.close()
        except Exception:
            pass


# ---------------------------------------------------------------- driver
class SaleaeLogic:
    kind = "logic"
    family = "Saleae"

    def __init__(self, transport, resource: str = ""):
        self.t = transport
        self.device_id, _, _ = parse_resource(resource)
        self.caps = LogicCaps()
        self.state = LogicConfig()
        self.log = None
        self.have_capture = False      # a finished capture is open in Logic 2
        self._run: dict | None = None  # the capture in progress

    @property
    def cfg(self) -> LogicConfig:
        return LogicConfig(**{k: (list(v) if isinstance(v, list) else v)
                              for k, v in vars(self.state).items()})

    def errors(self) -> list[str]:
        return []

    def _log(self, text: str):
        if self.log:
            self.log(text)

    # ---- connect ------------------------------------------------------------
    def identify(self) -> LogicCaps:
        info = self.t.app_info()
        sim = self.device_id.upper().startswith("SIM")
        devs = self.t.devices(simulation=True)
        real = [d for d in devs if not d["sim"]]
        if self.device_id and not sim:
            match = [d for d in devs if d["id"].upper() == self.device_id.upper()]
            if not match:
                seen = ", ".join(d["id"] for d in real) or "none"
                raise RuntimeError(f"Logic 2 doesn't see analyzer {self.device_id} "
                                   f"(connected: {seen}). Is it plugged in?")
            dev = match[0]
        elif sim:
            pool = [d for d in devs if d["sim"]]
            if not pool:
                raise RuntimeError("Logic 2 offers no simulated devices")
            dev = pool[0]
        else:
            if not real:
                raise RuntimeError("Logic 2 is running but sees no analyzer. Plug it in, "
                                   "check it appears in Logic 2, then try again.")
            dev = real[0]
        self.device_id = dev["id"]
        name, nd, na, thr = DEVICES.get(dev["type"], (dev["type"].title(), 8, 0, []))
        fam = "Pro" if "PRO" in dev["type"] else "Logic 8"
        self.caps = LogicCaps(
            idn=f"Saleae {name}  {dev['id']}" + ("  (simulated)" if dev["sim"] else ""),
            note=f"via Logic 2 {info['app_version']}, automation API {info['api']}",
            device_id=dev["id"], digital=nd, analog=na, thresholds=list(thr),
            digital_rates=list(DIGITAL_RATES[fam]), analog_rates=list(ANALOG_RATES[fam]),
            info={**info, "device_type": dev["type"]})
        s = self.state
        s.digital = [c for c in s.digital if c < nd] or [0]
        s.analog = [c for c in s.analog if c < na]
        if s.digital_rate not in self.caps.digital_rates:
            s.digital_rate = 10e6 if 10e6 in self.caps.digital_rates else self.caps.digital_rates[-1]
        if s.analog_rate not in self.caps.analog_rates:
            s.analog_rate = self.caps.analog_rates[4]
        if thr and s.threshold not in thr:
            s.threshold = 3.3
        if not thr:
            s.threshold = None
        return self.caps

    # ---- settings -------------------------------------------------------------
    def configure(self, cfg: LogicConfig):
        """Remember the settings for the next capture (nothing is sent until then)."""
        if not cfg.digital and not cfg.analog:
            raise ValueError("Turn on at least one channel")
        if cfg.mode == "trigger" and cfg.trig_channel not in cfg.digital:
            raise ValueError(f"The trigger channel D{cfg.trig_channel} isn't enabled")
        self.state = cfg

    # ---- capture (driven by the worker's arm / capture_done / fetch loop) --
    def arm(self, cfg: LogicConfig | None = None) -> float:
        if cfg is not None:
            self.configure(cfg)
        self.discard()
        cfg = self.cfg
        self._log(f"start {cfg.mode} capture: D{cfg.digital} A{cfg.analog} "
                  f"{fmt_rate(cfg.digital_rate)} / {fmt_rate(cfg.analog_rate)}")
        self.t.start(self.device_id, cfg)
        run = {"cfg": cfg, "t0": time.monotonic(), "done": threading.Event(),
               "error": None, "aborted": False}
        if cfg.mode != "manual":
            def waiter():
                try:
                    self.t.wait()
                except Exception as e:      # noqa: BLE001 - reported from capture_done
                    run["error"] = e
                run["done"].set()
            threading.Thread(target=waiter, name="logic2 wait", daemon=True).start()
        self._run = run
        return -1.0                         # nothing to fire: just wait

    def fire(self):
        pass

    def stop_capture(self):
        """End a manual capture (keeps the data)."""
        run = self._run
        if run and not run["done"].is_set():
            if run["cfg"].mode != "manual":
                raise RuntimeError("Only a manual capture can be stopped early; use Abort")
            self.t.stop()
            run["done"].set()

    def capture_done(self) -> bool:
        run = self._run
        if run is None:
            return True
        if run["done"].is_set() and run["error"] is not None:
            self._run = None
            self._discard_quietly()
            raise RuntimeError(f"Capture failed: {run['error']}")
        return run["done"].is_set()

    def abort(self):
        run, self._run = self._run, None
        if run is not None:
            run["aborted"] = True
            # wait() is still blocked in Logic 2: closing the capture releases it
            self._discard_quietly()

    def discard(self):
        """Close the capture Logic 2 is holding, if any."""
        if self.have_capture or self._run is not None:
            self.have_capture = False
            self._run = None
            self.t.close_capture()

    def _discard_quietly(self):
        try:
            self.have_capture = True
            self.discard()
        except Exception:
            pass

    def fetch(self, t_app: float = 0.0) -> LogicCapture:
        run, self._run = self._run, None
        if run is None:
            raise RuntimeError("No capture to fetch")
        self.have_capture = True
        cfg = run["cfg"]
        elapsed = time.monotonic() - run["t0"]
        # Keep each analog channel to a sensible number of points.
        down = 1
        if cfg.analog:
            down = max(1, math.ceil(cfg.analog_rate * elapsed * 1.1 / MAX_ANALOG_POINTS))
        tmp = tempfile.mkdtemp(prefix="eewb-logic2-")
        try:
            self.t.export_binary(tmp, cfg.digital, cfg.analog, down)
            digital, analog = read_export(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        res = LogicCapture(cfg, digital, analog, t_app=t_app, wall=time.time(), elapsed=elapsed)
        if cfg.decoders:
            try:
                res.table_header, res.table_rows = self.decode(cfg.decoders)
            except Exception as e:      # noqa: BLE001 - keep the waveform even if a decoder fails
                res.errors.append(f"Decoding failed: {e}")
        return res

    # ---- on the held capture ------------------------------------------------------
    def decode(self, decoders: list) -> tuple[list[str], list[list[str]]]:
        """Run protocol decoders on the current capture; returns Logic 2's data
        table (header, rows), all decoders merged and in time order."""
        if not self.have_capture:
            raise RuntimeError("Capture something first")
        handles = []
        tmp = tempfile.mkdtemp(prefix="eewb-logic2-")
        try:
            for d in decoders:
                self._log(f"decode {d['name']} {d.get('settings', {})}")
                handles.append(self.t.add_analyzer(d["name"], d.get("label") or d["name"],
                                                   dict(d.get("settings", {}))))
            path = os.path.join(tmp, "table.csv")
            self.t.export_table(path, handles)
            return read_table(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            for h in handles:
                try:
                    self.t.remove_analyzer(h)
                except Exception:
                    pass

    def save(self, path: str):
        if not self.have_capture:
            raise RuntimeError("Capture something first")
        self.t.save(path)

    def export_csv(self, directory: str):
        if not self.have_capture:
            raise RuntimeError("Capture something first")
        s = self.state
        self.t.export_csv(directory, s.digital, s.analog)

    def poll(self, t: float, _cfg=None):
        return None

    def close(self):
        try:
            self.abort()
            self.discard()
        except Exception:
            pass
