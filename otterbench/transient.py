"""Load transient test: step an electronic load between two currents and look at
the output voltage on an oscilloscope. How far it dips or rises, how far it comes
back past its new level, how long until it stays inside a band.

The load's own slew rate decides how fast the step is; set it on the load. The
channel's volts/division and offset (AC coupling is fine) are left as they are:
dip and peak must fit on the screen.

Safe by order, like the regulator test: whatever happens the load goes off first,
then the supply.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from . import roles
from .bode import step125
from .sequencer import Bench, SequenceError, Unstopped

DIVS = 10                        # the fewest any of the scopes shows
COLUMNS = ["step", "n", "deviation", "rebound", "recovery", "shift", "before", "after", "note"]
UNITS = {"deviation": "mV", "rebound": "mV", "recovery": "µs", "shift": "mV", "before": "V",
         "after": "V"}


@dataclass
class TransSettings:
    load: str = ""               # port texts (roles.py)
    source: str = ""             # "": the board is powered by something else
    scope: str = ""
    ch: int = 1                  # the output voltage
    trig_ch: int = 0             # 0: triggers on the output itself; else this channel
    trig_level: float = 0.5      # V, with trig_ch
    trig_mv: float = 20.0        # otherwise: how far the output moves to trigger
    vin: float = 5.0
    iin_limit: float = 2.0
    i_low: float = 0.1
    i_high: float = 1.0
    vout_nominal: float = 3.3
    vout_max: float = 3.6        # above this (the load's reading) the test ends
    window: float = 500e-6       # s shown after the step
    settle: float = 0.5
    repeats: int = 3
    band: float = 1.0            # % of nominal: "recovered" is inside this
    # limits for PASS / FAIL (0: not judged)
    max_deviation: float = 0.0   # % of nominal
    max_rebound: float = 0.0     # % of nominal
    max_recovery: float = 0.0    # s

    def problem(self) -> str:
        if not self.load:
            return "Choose the electronic load"
        if not self.scope:
            return "Choose the oscilloscope"
        if self.trig_ch == self.ch:
            return "The trigger channel is the output's own channel: choose “the output itself”"
        if self.trig_ch == 0 and self.trig_mv <= 0:
            return "Give how far the output moves to trigger"
        if not 0 <= self.i_low < self.i_high:
            return "The high load current must be above the low one"
        if self.vout_nominal <= 0:
            return "Give the output voltage it should have"
        if self.vout_max <= self.vout_nominal:
            return "The highest allowed output must be above the nominal output"
        if self.source and (self.vin <= 0 or self.iin_limit <= 0):
            return "Give the input voltage and its current limit"
        if not 1e-6 <= self.window <= 10:
            return "The time shown must be 1 µs … 10 s"
        if not 1 <= self.repeats <= 100:
            return "Repeat 1 … 100 times"
        if self.band <= 0:
            return "The band must be above 0 %"
        return ""


@dataclass
class Step:
    up: bool
    n: int
    t: np.ndarray                # s; 0 = trigger
    v: np.ndarray
    deviation: float = math.nan  # V from the level before; always positive
    rebound: float = math.nan    # V past the level after it, the other way
    recovery: float = math.nan   # s, from leaving the old level until inside the band for good
    shift: float = math.nan      # V, level after - level before
    before: float = math.nan
    after: float = math.nan
    note: str = ""

    def row(self) -> dict:
        return {"step": "up" if self.up else "down", "n": self.n,
                "deviation": 1e3 * self.deviation, "rebound": 1e3 * self.rebound,
                "recovery": 1e6 * self.recovery, "shift": 1e3 * self.shift,
                "before": self.before, "after": self.after, "note": self.note}


def analyse(t, v, up: bool, band: float, n: int = 1) -> Step:
    """One trace, triggered at t = 0. band: volts."""
    t, v = np.asarray(t, float), np.asarray(v, float)
    s = Step(up, n, t, v)
    if t.size < 50 or t[0] >= 0 or t[-1] <= 0:
        s.note = "the trigger isn't on the screen"
        return s
    pre = v[t < 0.2 * t[0]]              # leaves out the last fifth before the trigger
    end = t >= 0.8 * t[-1]
    if pre.size < 10 or end.sum() < 10:
        s.note = "too few samples"
        return s
    s.before, s.after = float(np.median(pre)), float(np.median(v[end]))
    s.shift = s.after - s.before
    noise = float(np.std(pre))
    sign = -1.0 if up else 1.0
    live = np.flatnonzero(t >= 0.2 * t[0])
    d = sign * (v - s.before)            # positive = the way this step pushes it
    k = int(live[np.argmax(d[live])])
    s.deviation = float(max(d[k], 0.0))
    s.rebound = float(max(0.0, np.max(-sign * (v[k:] - s.after))))
    # still moving at the end: going on like that for another screen would be
    # more than half the band
    tail = v[end]
    h = tail.size // 2
    drift = abs(float(np.median(tail[h:]) - np.median(tail[:h])))
    moving = drift > 5 * 1.25 * noise * math.sqrt(2 / h) and \
        drift * t[-1] / (t[end][-1] - t[end][h]) > band / 2
    notes = []
    if s.deviation < 4 * noise:
        s.note = "no step seen above the noise"
        return s
    # where it left the old level: the last quiet sample before the extreme
    quiet = np.flatnonzero(d[:k + 1] < max(0.1 * s.deviation, 3 * noise))
    start = t[quiet[-1]] if quiet.size else t[live[0]]
    if 3 * noise > band:
        notes.append("noisier than the band: no recovery time")
    else:
        out = np.flatnonzero((np.abs(v - s.after) > band) & (t >= start))
        if moving:
            notes.append("not settled on the screen")
        elif not out.size:
            s.recovery = 0.0             # never left the band around the new level
        elif t[out[-1]] >= 0.8 * t[-1]:
            notes.append("not settled on the screen")
        else:
            s.recovery = float(t[min(out[-1] + 1, t.size - 1)] - start)
    if s.deviation > 0 and np.mean(d[live] >= d[k] - 1e-9) > 0.06 and d[k] > 8 * noise:
        notes.append("flat at the extreme: is the trace cut off by the screen?")
    s.note = ", ".join(notes)
    return s


def summary(steps: list[Step], s: TransSettings) -> dict:
    """The worst of each direction, and the verdict."""
    out: dict = {"worst": {}, "checks": []}
    for up in (True, False):
        g = [x for x in steps if x.up == up]
        if not g:
            continue
        name = "up" if up else "down"

        def worst(key):
            vals = [getattr(x, key) for x in g]
            return math.nan if any(math.isnan(x) for x in vals) else max(vals)
        w = {k: worst(k) for k in ("deviation", "rebound", "recovery")}
        w["shift"] = float(np.mean([x.shift for x in g]))
        out["worst"][name] = w
        what = "dip" if up else "peak"
        for key, lim, scale, unit, label in (
                ("deviation", s.max_deviation * s.vout_nominal / 100, 1e3, "mV", what),
                ("rebound", s.max_rebound * s.vout_nominal / 100, 1e3, "mV", "rebound"),
                ("recovery", s.max_recovery, 1e6, "µs", "recovery")):
            if lim > 0:
                x = w[key]
                out["checks"].append((
                    f"load step {name}: {label} " +
                    ("not measured" if math.isnan(x) else f"{scale * x:.4g} {unit}") +
                    f" (at most {scale * lim:.4g} {unit})", not math.isnan(x) and x <= lim))
    out["verdict"] = "" if not out["checks"] else \
        "PASS" if all(ok for _, ok in out["checks"]) else "FAIL"
    return out


@dataclass
class TransTest:
    s: TransSettings
    steps: list = field(default_factory=list)

    def run(self, bench: Bench, on_step=None):
        s = self.s
        if s.problem():
            raise SequenceError(s.problem())
        load = roles.find(bench, s.load, "load")
        src = roles.find(bench, s.source, "source") if s.source else None
        scope = bench.inst(s.scope)
        if scope.kind != "scope":
            raise SequenceError(f"{scope.name} isn't an oscilloscope")
        if src is not None and src.name == load.name:
            raise SequenceError("The supply and the load must be different instruments")
        nch = scope.run(lambda d: d.state.nch)
        if max(s.ch, s.trig_ch) > nch:
            raise SequenceError(f"{scope.name} has {nch} channels")
        old = scope.run(self._remember)
        try:
            load.prepare()
            load.amps(s.i_low)
            if src is not None:
                src.off()
                src.volts(s.vin)
                src.amps(s.iin_limit)
                src.on()
                bench.wait(s.settle)
            load.on()
            bench.wait(s.settle)
            self._check(load)
            tdiv = step125(s.window / (DIVS / 2))

            def prepare(d):
                d.set_channel(s.ch, on=True)
                if s.trig_ch:
                    d.set_channel(s.trig_ch, on=True)
                d.set_timebase(scale=tdiv, pos=0.0)
            scope.run(prepare)
            for n in range(1, s.repeats + 1):
                for up in (True, False):
                    st = self._step(bench, scope, load, up, n, tdiv)
                    self.steps.append(st)
                    bench.log(f"{'up' if up else 'down'} {n}: {1e3 * st.deviation:.4g} mV, "
                              f"recovery {1e6 * st.recovery:.4g} µs"
                              + (f"  ({st.note})" if st.note else ""))
                    if on_step:
                        on_step(st)
                    self._check(load)
        finally:
            with Unstopped(bench):
                todo = [("load", load.off)]
                if src is not None:
                    todo.append(("supply", src.off))
                todo.append((scope.name, lambda: scope.run(lambda d: self._restore(d, old))))
                for what, fn in todo:
                    try:
                        fn()
                    except Exception as e:      # noqa: BLE001 - try the others all the same
                        bench.log(f"{what}: switching off / putting back failed: {e}")
        return self.steps

    @staticmethod
    def _remember(d):
        d.read_state()
        st = d.state
        return st.tscale, st.tpos, st.trig_source, st.trig_level, st.trig_slope, st.sweep

    @staticmethod
    def _restore(d, old):
        d._armed = None
        tscale, tpos, src, level, slope, sweep = old
        d.set_timebase(scale=tscale, pos=tpos)
        d.set_trigger(source=src, level=level, slope=slope,
                      sweep="normal" if sweep == "normal" else "auto")
        d.run()

    def _check(self, load):
        s = self.s
        v, _ = load.read(0.0)
        if v > s.vout_max:
            raise SequenceError(f"Output {v:.3f} V is above {s.vout_max:g} V: stopped")
        if v < 0.5 * s.vout_nominal:
            raise SequenceError(f"Output {v:.3f} V is less than half of {s.vout_nominal:g} V: "
                                "stopped")

    def _step(self, bench, scope, load, up: bool, n: int, tdiv: float) -> Step:
        s = self.s
        target = s.i_high if up else s.i_low
        screen = 14 * tdiv               # the widest screen of them all
        if s.trig_ch:
            source, level, slope = s.trig_ch, s.trig_level, "rising" if up else "falling"
        else:
            def look(d):
                d.set_trigger(sweep="auto")
                d.run()
            scope.run(look)
            bench.wait(0.3 + 2 * screen)
            base = float(np.median(scope.run(lambda d: d.fetch_channel(s.ch), timeout=60.0).v))
            source, slope = s.ch, "falling" if up else "rising"
            level = base + (-1 if up else 1) * s.trig_mv / 1e3
        scope.run(lambda d: d.set_trigger(source=source, level=level, slope=slope,
                                          sweep="normal"))
        scope.run(lambda d: d.arm())
        bench.wait(0.4 + screen)         # it must have filled the time before the trigger
        load.amps(target)
        limit = time.monotonic() + 3.0 + 4 * screen
        while not scope.run(lambda d: d.capture_done() and d.status() == "stop"):
            if time.monotonic() > limit:
                scope.run(lambda d: d.abort())
                raise SequenceError(
                    "The scope didn't trigger on the load step: check the channel, its "
                    "volts/division and " + ("the trigger level" if s.trig_ch else
                                             "the trigger distance (less than the dip)"))
            bench.wait(0.02)

        def take(d):
            try:
                return d.fetch_channel(s.ch)
            finally:
                d._armed = None
        tr = scope.run(take, timeout=60.0)
        bench.wait(s.settle)
        return analyse(tr.t, tr.v, up, s.band * s.vout_nominal / 100, n)
