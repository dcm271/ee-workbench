"""Safety watchdog: rules that watch one instrument and act on another.

    if  PPK2 current  >  50 mA  for 10 ms   then  all outputs off
    if  66321D voltage  <  3.0 V  for 0 ms   then  turn off "DUT rail"

Each rule watches every reading of its source; a PPK2's are checked sample by
sample (10 µs), so a 10 ms hold means 1000 samples in a row over the limit.
Slower instruments are checked per reading: with a hold, two readings over
the limit at least that far apart trip it (a hold of 0 trips on the first).
A rule that trips stays tripped (latched) until it is re-armed (or, with
`rearm_s`, re-arms itself that many seconds later); the action runs once per
trip. The GUI does the actions, this module only decides.

Hardware limits (`hw`): a rule "supply voltage > L" or "supply current > L"
can also be pushed into the supply itself (OVP at L; current limit no higher
than L with over-current protection on), so the DUT stays protected even if
this program, the computer or the USB link dies. The supply acts at once,
without the rule's hold time. `hw_plan` works out what to send.
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field

import numpy as np

ACTIONS = ("all outputs off", "turn off", "stop sequence", "capture", "mark session",
           "note only")
SAFETY_ACTIONS = ("all outputs off", "turn off", "stop sequence")   # these show a dialog


@dataclass
class Rule:
    source: str = ""            # instrument name (display name, or part of it)
    quantity: str = "current"
    op: str = ">"               # ">" or "<"
    limit: float = 0.0
    hold_ms: float = 0.0
    action: str = "all outputs off"
    target: str = ""            # "turn off" / "capture": which instrument(s)
    enabled: bool = True
    hw: bool = False            # also set the supply's own OVP / current limit
    rearm_s: float = 0.0        # re-arm by itself this long after a trip (0: stay tripped)
    # state, not saved
    tripped: bool = field(default=False, compare=False)
    trip_t: float | None = field(default=None, compare=False)
    trip_value: float | None = field(default=None, compare=False)
    _since: float | None = field(default=None, compare=False, repr=False)
    tripped_at: float | None = field(default=None, compare=False, repr=False)

    def describe(self) -> str:
        hold = f" for {self.hold_ms:g} ms" if self.hold_ms else ""
        act = f"{self.action} {self.target}" if self.action in ("turn off", "capture") \
            and self.target else self.action
        extra = " [+HW]" if self.hw else ""
        if self.rearm_s:
            extra += f" (re-arms after {self.rearm_s:g} s)"
        return f"{self.source} {self.quantity} {self.op} {self.limit:g}{hold} → {act}{extra}"

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items()
                if k not in ("tripped", "trip_t", "trip_value", "_since", "tripped_at")}

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        r = cls(**{k: v for k, v in d.items() if k in ("source", "quantity", "op", "limit",
                                                         "hold_ms", "action", "target",
                                                         "enabled", "hw", "rearm_s")})
        r.limit = float(r.limit)
        r.hold_ms = max(0.0, float(r.hold_ms))
        r.rearm_s = max(0.0, float(r.rearm_s))
        r.hw = bool(r.hw)
        r.source, r.quantity, r.target = str(r.source), str(r.quantity), str(r.target)
        if r.op not in (">", "<"):
            raise ValueError(f"op must be > or <, not {r.op!r}")
        if r.action not in ACTIONS:
            raise ValueError(f"unknown action {r.action!r}")
        r.enabled = bool(r.enabled)
        return r

    def rearm(self):
        self.tripped, self.trip_t, self.trip_value, self._since = False, None, None, None
        self.tripped_at = None

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
        from .signals import name_matches
        return name_matches(rule.source, name)

    def feed(self, name: str, signals: dict) -> list[Rule]:
        """signals: {quantity: (t, v)} from one instrument. Returns rules that tripped now."""
        if not self.armed:
            return []
        out = []
        for r in self.rules:
            if r.quantity in signals and self.matches(r, name):
                t, v = signals[r.quantity]
                if r.feed(t, v):
                    r.tripped_at = time.monotonic()
                    out.append(r)
        return out

    def rearm(self):
        for r in self.rules:
            r.rearm()

    def auto_rearm(self, now: float | None = None) -> list[Rule]:
        """Re-arm rules whose `rearm_s` has passed since they tripped."""
        now = time.monotonic() if now is None else now
        out = []
        for r in self.rules:
            if r.tripped and r.rearm_s > 0 and r.tripped_at is not None \
                    and now - r.tripped_at >= r.rearm_s:
                r.rearm()
                out.append(r)
        return out

    def to_list(self) -> list[dict]:
        return [r.to_dict() for r in self.rules]

    def load(self, items: list[dict]) -> list[str]:
        """Replace the rules. A damaged entry is skipped (and reported) rather
        than losing every rule; returns what was skipped."""
        self.rules, bad = [], []
        for k, d in enumerate(items if isinstance(items, list) else []):
            try:
                self.rules.append(Rule.from_dict(d))
            except (TypeError, ValueError, AttributeError) as e:
                bad.append(f"rule {k + 1}: {e}")
        return bad


# ---------------------------------------------------------------- hardware limits
def hw_plan(rule: Rule, kind: str, settings: list[str] | None = None):
    """What to send so the instrument enforces `rule` itself.

    Returns (job, text): job(driver) runs on the instrument's I/O thread and
    returns a description of what it set; or (None, why-not)."""
    q = rule.quantity.lower()
    m = re.match(r"^(voltage|current)(\d*)$", q)
    if rule.op != ">" or not m:
        return None, "only “voltage >” and “current >” rules can be set in the instrument"
    what, ch = m.group(1), m.group(2)
    L = float(rule.limit)
    if L <= 0:
        return None, "the limit must be above 0"
    if kind == "supply":
        if ch:
            if ch != "2" or "current2" not in (settings or []):
                return None, "this supply has one output"
            if what == "voltage":
                return None, "output 2 has no over-voltage protection"

            def job(d):
                sp = d.read_setpoints()
                if sp.current2 is None or sp.current2 > L:
                    d.set_current2(L)
                d.set_ocp(True)
                return f"output 2 current limit ≤ {L:g} A, OCP on (both outputs)"
            return job, f"output 2 current limit no higher than {L:g} A, " \
                        "over-current protection on (both outputs)"
        if what == "voltage":
            def job(d):
                d.set_ovp(L)
                if getattr(getattr(d, "caps", None), "ovp_is_trip", False):
                    d.set_ovp_tracking(True)      # its switch
                return f"OVP {L:g} V"
            return job, f"over-voltage protection at {L:g} V"

        def job(d):
            sp = d.read_setpoints()
            if sp.current is None or sp.current > L:
                d.set_current(L)
            d.set_ocp(True)
            return f"current limit ≤ {L:g} A, OCP on"
        return job, f"current limit no higher than {L:g} A, over-current protection on"
    if kind == "scpi":
        names = set(settings or [])

        def name(base):
            for n in (f"{base}{ch}", base):
                if n in names:
                    return n
            return None
        if what == "voltage":
            ovp, ovp_on = name("ovp"), name("ovp_on")
            if not ovp:
                return None, "its definition has no ovp setting"

            def job(d):
                d.set(ovp, L)
                if ovp_on:
                    d.set(ovp_on, True)
                return f"{ovp} {L:g} V" + (f", {ovp_on} on" if ovp_on else "")
            return job, f"{ovp} at {L:g} V"
        iset, ocp, ocp_on = name("iset"), name("ocp"), name("ocp_on")
        if not iset:
            return None, "its definition has no iset setting"

        def job(d):
            cur = d.values.get(iset)
            if cur is None or float(cur) > L:
                d.set(iset, L)
            if ocp:
                d.set(ocp, L)
            if ocp_on:
                d.set(ocp_on, True)
            return f"{iset} ≤ {L:g} A" + (", OCP on" if ocp_on else "")
        return job, f"{iset} no higher than {L:g} A"
    return None, "only power supplies can enforce limits themselves"
