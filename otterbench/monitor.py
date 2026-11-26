"""The bench monitor: every reading of every open instrument, as signals
(`instrument.quantity`, the names math channels use), with a history, which
of them are live, and the nicknames the user gave them ("DUT 3V3 rail").

A supply's readings count as on only while its output is (output 2 has its
own switch); everything else while its readings keep coming.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .clock import now
from .signals import UNITS, signals_of, slug

STALE_S = 5.0                 # no reading for this long: not live
KEEP_S = 3600.0               # history kept per signal
MAX_POINTS = 40_000           # per signal; the oldest half goes when full

# which output switch each supply quantity follows
_SUPPLY_OUTPUT = {"voltage": "output", "current": "output", "power": "output",
                  "voltage2": "output2", "current2": "output2", "power2": "output2"}


@dataclass
class Signal:
    id: str                   # "_66309b.current2": instrument identifier . quantity
    instrument: str           # the tab's display name
    quantity: str
    unit: str
    kind: str
    t: np.ndarray = field(default_factory=lambda: np.empty(0))
    v: np.ndarray = field(default_factory=lambda: np.empty(0))
    n: int = 0
    on: bool | None = None    # the output it belongs to (None: has no output)
    seen: float = 0.0         # app time of the last reading

    def add(self, t: float, v: float):
        if self.n == len(self.t):
            if self.n >= MAX_POINTS:
                keep = self.n // 2
                self.t[:keep], self.v[:keep] = self.t[self.n - keep:self.n], self.v[self.n - keep:self.n]
                self.n = keep
            else:
                size = max(256, 2 * len(self.t))
                self.t = np.concatenate([self.t, np.full(size - len(self.t), np.nan)])
                self.v = np.concatenate([self.v, np.full(size - len(self.v), np.nan)])
        self.t[self.n], self.v[self.n] = t, v
        self.n += 1

    def data(self) -> tuple[np.ndarray, np.ndarray]:
        return self.t[:self.n], self.v[:self.n]

    def last(self) -> float | None:
        return float(self.v[self.n - 1]) if self.n else None

    def live(self, at: float | None = None) -> bool:
        """Readings are arriving, and its output (if it has one) is on."""
        fresh = (now() if at is None else at) - self.seen < STALE_S
        return fresh and self.on is not False

    @property
    def source(self) -> str:
        """"66309B · current2": the tab's name without an unnamed tab's
        "(resource)", and the quantity."""
        name = re.sub(r"\s*\([^()]*\)\s*$", "", self.instrument) or self.instrument
        return f"{name} · {self.quantity}"

    def window(self, t0: float, t1: float) -> np.ndarray:
        t, v = self.data()
        a, b = np.searchsorted(t, t0, "left"), np.searchsorted(t, t1, "right")
        y = v[a:b]
        return y[np.isfinite(y)]


def _unit(kind: str, quantity: str, r) -> str:
    if kind == "dmm":
        return getattr(r, "unit", "") or ""
    if kind == "scpi":
        return (getattr(r, "units", None) or {}).get(quantity, "") or ""
    if kind == "gpio":
        return ""
    return UNITS.get(quantity, "")


class Monitor:
    def __init__(self):
        self.signals: dict[str, Signal] = {}
        self.nicknames: dict[str, str] = {}

    def feed(self, name: str, kind: str, r, sig: dict | None = None):
        if sig is None:
            sig = signals_of(kind, r)
        inst = slug(name)
        for q, (t, v) in sig.items():
            t, v = np.asarray(t, float), np.asarray(v, float)
            if not v.size:
                continue
            key = f"{inst}.{q}"
            s = self.signals.get(key)
            if s is None:
                s = self.signals[key] = Signal(key, name, q, _unit(kind, q, r), kind)
            s.instrument = name
            if kind == "dmm":
                s.unit = _unit(kind, q, r) or s.unit
            # a block of fast samples (PPK2): one point, its mean
            ok = np.isfinite(v)
            s.add(float(t[-1]), float(v[ok].mean()) if ok.any() else np.nan)
            s.seen = now()
            out = _SUPPLY_OUTPUT.get(q) if kind == "supply" else None
            s.on = None if out is None else bool(getattr(r, out, None))
            if s.n and s.t[0] < s.t[s.n - 1] - KEEP_S:
                cut = int(np.searchsorted(s.t[:s.n], s.t[s.n - 1] - KEEP_S))
                s.t[:s.n - cut], s.v[:s.n - cut] = s.t[cut:s.n].copy(), s.v[cut:s.n].copy()
                s.n -= cut

    def keep_only(self, names: list[str]):
        """Forget the signals of instruments no longer open."""
        open_ = {slug(n) for n in names}
        for k in [k for k, s in self.signals.items() if slug(s.instrument) not in open_]:
            del self.signals[k]

    def nickname(self, key: str) -> str:
        return self.nicknames.get(key, "")

    def label(self, key: str) -> str:
        """The nickname, else "66309B · current2"."""
        s = self.signals.get(key)
        nick = self.nicknames.get(key)
        if nick:
            return nick
        return key if s is None else s.source

    def set_nickname(self, key: str, text: str):
        text = " ".join(text.split())
        if text:
            self.nicknames[key] = text
        else:
            self.nicknames.pop(key, None)

    def load_nicknames(self, d) -> None:
        if not isinstance(d, dict):
            d = {}
        self.nicknames = {str(k): " ".join(str(v).split()) for k, v in d.items()
                          if str(v).strip()}
