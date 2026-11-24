"""What every logic analyzer shares, whatever drives it (Saleae through
Logic 2, or anything sigrok supports): settings, captured traces, the edge
measurements and the drawing envelope. The panel only deals in these."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

MODES = ("timed", "trigger", "manual")
TRIGGER_LABELS = {"rising": "Rising edge", "falling": "Falling edge", "either": "Any edge",
                  "high": "High level", "low": "Low level",
                  "pulse_high": "High pulse", "pulse_low": "Low pulse"}


def fmt_rate(hz: float) -> str:
    for div, unit in ((1e6, "MS/s"), (1e3, "kS/s")):
        if hz >= div:
            return f"{hz / div:g} {unit}"
    return f"{hz:g} S/s"



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
    channel_names: list = field(default_factory=list)       # "D0", ... as the device calls them
    trigger_types: list = field(default_factory=lambda: ["rising", "falling"])
    # Protocol decoders: {shown name: [(setting, default, kind)]}, kind "ch" (a
    # digital channel number), "int" or "str". "Other…" is always offered too.
    decoders: dict = field(default_factory=dict)
    capture_filter: str = ""                                 # e.g. "Logic 2 capture (*.sal)"
    capture_suffix: str = ""

    def channel_name(self, k: int) -> str:
        return self.channel_names[k] if k < len(self.channel_names) else f"D{k}"


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
    trig_type: str = "rising"        # a key of TRIGGER_LABELS the device supports
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
    t_armed: float | None = None     # app time it was started (rough position of t = 0)
    # app time of the capture's t = 0, once aligned against another instrument
    offset: float | None = None
    offset_note: str = ""
    # other instruments' data lined up with this capture, e.g. "ppk2":
    # {"name", "t" (capture time, s), "i" (A)}
    extra: dict = field(default_factory=dict)
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


