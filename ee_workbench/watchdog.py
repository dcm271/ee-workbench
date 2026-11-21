"""Safety watchdog: rules that watch one instrument and act on another.

    if  PPK2 current  >  50 mA  for 10 ms   then  all outputs off
    if  66321D voltage  <  3.0 V  for 0 ms   then  turn off "DUT rail"

Each rule watches every reading of its source; a PPK2's are checked sample by
sample (10 µs), so a 10 ms hold means 1000 samples in a row over the limit.
Slower instruments are checked per reading: with a hold, two readings over
the limit at least that far apart trip it (a hold of 0 trips on the first).
A rule that trips stays tripped (latched) until it is re-armed; the action
runs once. The GUI does the actions, this module only decides.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

ACTIONS = ("all outputs off", "turn off", "stop sequence", "note only")


@dataclass
class Rule:
    source: str = ""            # instrument name (display name, or part of it)
    quantity: str = "current"
    op: str = ">"               # ">" or "<"
    limit: float = 0.0
    hold_ms: float = 0.0
    action: str = "all outputs off"
    target: str = ""            # for "turn off": which instrument
    enabled: bool = True
    # state, not saved
    tripped: bool = field(default=False, compare=False)
    trip_t: float | None = field(default=None, compare=False)
    trip_value: float | None = field(default=None, compare=False)
    _since: float | None = field(default=None, compare=False, repr=False)

    def describe(self) -> str:
        hold = f" for {self.hold_ms:g} ms" if self.hold_ms else ""
        act = f"turn off {self.target}" if self.action == "turn off" else self.action
        return f"{self.source} {self.quantity} {self.op} {self.limit:g}{hold} → {act}"

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items()
                if k not in ("tripped", "trip_t", "trip_value", "_since")}

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        r = cls(**{k: v for k, v in d.items() if k in ("source", "quantity", "op", "limit",
                                                         "hold_ms", "action", "target",
                                                         "enabled")})
        r.limit = float(r.limit)
        r.hold_ms = float(r.hold_ms)
        return r

    def rearm(self):
        self.tripped, self.trip_t, self.trip_value, self._since = False, None, None, None

    def feed(self, t: np.ndarray, v: np.ndarray) -> bool:
        """Check readings (oldest first); True when this trips the rule."""
        if not self.enabled or self.tripped or not len(t):
            return False
        t = np.asarray(t, float)
        v = np.asarray(v, float)
        bad = (v > self.limit) if self.op == ">" else (v < self.limit)
        bad &= np.isfinite(v)
        if not bad.any():
            self._since = None
            return False
        hold = self.hold_ms / 1000.0
        idx = np.arange(len(t))
        prev_bad = np.concatenate(([self._since is not None], bad[:-1]))
        starts = bad & ~prev_bad
        last_start = np.maximum.accumulate(np.where(starts, idx, -1))
        carry = self._since if self._since is not None else 0.0
        start_t = np.where(last_start >= 0, t[np.maximum(last_start, 0)], carry)
        dur = t - start_t
        trip = bad & (dur >= hold - 1e-12)
        if trip.any():
            k = int(np.argmax(trip))
            self.tripped, self.trip_t, self.trip_value = True, float(t[k]), float(v[k])
            self._since = None
            return True
        self._since = float(start_t[-1]) if bad[-1] else None
        return False


class Watchdog:
    def __init__(self, rules: list[Rule] | None = None):
        self.rules: list[Rule] = list(rules or [])
        self.armed = True

    def matches(self, rule: Rule, name: str) -> bool:
        s = rule.source.strip().lower()
        return bool(s) and (s == name.lower() or s in name.lower())

    def feed(self, name: str, signals: dict) -> list[Rule]:
        """signals: {quantity: (t, v)} from one instrument. Returns rules that tripped now."""
        if not self.armed:
            return []
        out = []
        for r in self.rules:
            if r.quantity in signals and self.matches(r, name):
                t, v = signals[r.quantity]
                if r.feed(t, v):
                    out.append(r)
        return out

    def rearm(self):
        for r in self.rules:
            r.rearm()

    def to_list(self) -> list[dict]:
        return [r.to_dict() for r in self.rules]

    def load(self, items: list[dict]):
        self.rules = [Rule.from_dict(d) for d in items or []]
