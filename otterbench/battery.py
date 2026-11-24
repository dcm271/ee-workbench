"""Battery tester: charge / rest / discharge cycles; capacity, energy and
internal resistance of every cycle.

Discharge: an electronic load in CC, or a two-quadrant 663xx (roles.SinkPort).
Charge:    a supply, CC / CV, until the current tapers off (leave it out to
           discharge only).

The limits are checked on every sample in software, and the outputs go off at
the end, on Stop and on any error. A program can't be the only protection of a
lithium battery: its own protection board, a fuse and someone nearby are.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import roles
from .sequencer import Bench, SequenceError, Unstopped

COLUMNS = ["t", "cycle", "phase", "voltage", "current", "ah", "wh", "temp"]
CYCLE_COLUMNS = ["cycle", "charge_ah", "discharge_ah", "discharge_wh", "coulombic", "r_start",
                 "r_end", "ocv", "ended"]
#                    charge/cell, cut-off/cell
CHEMISTRIES = {"Li-ion / LiPo": (4.2, 3.0), "LiFePO4": (3.65, 2.5), "NiMH": (None, 1.0),
               "Lead acid (6 cells)": (14.4, 10.5), "Other": (None, None)}


@dataclass
class BatSettings:
    load: str = ""
    charger: str = ""
    temp: str = ""               # "tab: reading" of a thermometer, or ""
    start: str = "charge"        # charge | discharge
    cycles: int = 1
    discharge_a: float = 0.5
    cutoff_v: float = 3.0
    charge_v: float = 4.2
    charge_a: float = 0.5
    taper_a: float = 0.05
    rest_s: float = 600.0
    interval: float = 1.0
    max_ah: float = 0.0          # end a phase here (0: off)
    max_hours: float = 12.0      # per phase
    temp_max: float = 50.0
    ir_delay: float = 1.0        # s after a current step at which R is taken
    end_charged: bool = False    # one more charge at the end (storage, next use)

    def problem(self) -> str:
        if not self.load and not self.charger:
            return "Choose a load, a charger, or both"
        if not self.load and (self.start == "discharge" or self.cycles > 1):
            return "Discharging needs a load"
        if self.load and self.discharge_a <= 0:
            return "The discharge current must be above 0"
        if self.load and self.cutoff_v <= 0:
            return "The cut-off voltage must be above 0"
        if self.charger:
            if self.charge_v <= self.cutoff_v:
                return "The charge voltage must be above the cut-off voltage"
            if not 0 < self.taper_a < self.charge_a:
                return "The end-of-charge current must be between 0 and the charge current"
        if self.start == "charge" and not self.charger:
            return "Starting with a charge needs a charger"
        if self.cycles > 1 and not self.charger:
            return "More than one cycle needs a charger"
        if self.interval < 0.2:
            return "Sample at most 5 times a second"
        if self.max_hours <= 0:
            return "Give each phase a longest time"
        return ""


@dataclass
class Phase:
    ah: float = 0.0
    wh: float = 0.0
    why: str = ""
    last_v: float = math.nan
    last_i: float = 0.0
    r_start: float = math.nan


@dataclass
class BatTest:
    s: BatSettings
    samples: list = field(default_factory=list)
    cycles: list = field(default_factory=list)

    def run(self, bench: Bench, on_sample=None, on_cycle=None):
        s = self.s
        if s.problem():
            raise SequenceError(s.problem())
        self.bench = bench
        self._emit, self._emit_cycle = on_sample, on_cycle
        self.load = roles.find(bench, s.load, "load", "sink") if s.load else None
        self.charger = roles.find(bench, s.charger, "source") if s.charger else None
        if self.load and self.charger and self.load.name == self.charger.name \
                and isinstance(self.load, roles.Load):
            raise SequenceError("The charger and the load must be different instruments")
        self.t0 = bench._clock()
        self.cycle = 0
        try:
            if self.charger:
                self.charger.off()
            if self.load:
                own = self.load.prepare(s.cutoff_v * 0.98)
                bench.log("The load's own cut-off is set too" if own else
                          "This load has no cut-off of its own: the computer ends the discharge")
            v, _ = self._meter().read()
            bench.log(f"Battery: {v:.3f} V")
            if v < 0.1:
                raise SequenceError(f"{v:.3f} V: no battery, or it is the wrong way round")
            if self.charger and v > s.charge_v * 1.03:
                raise SequenceError(f"The battery is at {v:.3f} V, above the charge voltage "
                                    f"{s.charge_v:g} V: is that the right battery?")
            for k in range(max(1, int(s.cycles))):
                self.cycle = k + 1
                rec = {c: math.nan for c in CYCLE_COLUMNS}
                rec["cycle"], rec["ended"] = self.cycle, ""
                if self.charger and (k > 0 or s.start == "charge"):
                    p = self._charge()
                    rec["charge_ah"], rec["ended"] = p.ah, p.why
                    self._rest()
                if self.load:
                    rec["ocv"], _ = self.load.read()
                    p = self._discharge(rec["ocv"])
                    rec.update(discharge_ah=p.ah, discharge_wh=p.wh, r_start=p.r_start,
                               ended=p.why)
                    # back up: the same step the other way
                    self.bench.wait(s.ir_delay)
                    v, _ = self.load.read()
                    if p.last_i > 0.01:
                        rec["r_end"] = (v - p.last_v) / p.last_i
                    if rec["charge_ah"] > 0:
                        rec["coulombic"] = 100 * p.ah / rec["charge_ah"]
                self.cycles.append(rec)
                if on_cycle:
                    on_cycle(rec)
                if self.load and (k + 1 < s.cycles or s.end_charged and self.charger):
                    self._rest()
            if s.end_charged and self.charger and self.load:
                self._charge()
        finally:
            with Unstopped(bench):
                for what, p in (("load", self.load), ("charger", self.charger)):
                    if p is not None:
                        try:
                            p.off()
                        except Exception as e:      # noqa: BLE001 - try the other one too
                            bench.log(f"{what} off failed: {e}")
        return self.cycles

    def _meter(self):
        return self.load or self.charger

    # ---- one sample
    def _temp(self) -> float:
        if not self.s.temp:
            return math.nan
        name, _, what = self.s.temp.partition(": ")
        t = self.bench.measure(name, what)
        if not math.isfinite(t):
            raise SequenceError(f"{name} reads no temperature: is the sensor connected?")
        if t > self.s.temp_max:
            raise SequenceError(f"The battery is at {t:.1f} °C, above {self.s.temp_max:g} °C: "
                                "stopped")
        return t

    def _sample(self, phase: str, port, p: Phase, last: tuple | None):
        """Read, integrate, record. Returns (t, v, i)."""
        v, i = port.read()
        t = self.bench._clock()
        if last is not None:
            h = (t - last[0]) / 3600
            p.ah += h * (i + last[2]) / 2
            p.wh += h * (i * v + last[2] * last[1]) / 2
        p.last_v, p.last_i = v, i
        row = {"t": t - self.t0, "cycle": self.cycle, "phase": phase, "voltage": v,
               "current": -i if phase == "discharge" else i, "ah": p.ah, "wh": p.wh,
               "temp": self._temp()}
        self.samples.append(row)
        if self._emit:
            self._emit(row)
        return t, v, i

    def _over(self, start: float, p: Phase) -> str:
        s = self.s
        if s.max_ah > 0 and p.ah >= s.max_ah:
            return f"capacity limit {s.max_ah:g} Ah"
        if self.bench._clock() - start > s.max_hours * 3600:
            return f"time limit {s.max_hours:g} h"
        return ""

    # ---- phases
    def _charge(self) -> Phase:
        s, c, p = self.s, self.charger, Phase()
        self.bench.log(f"Cycle {self.cycle}: charging to {s.charge_v:g} V at {s.charge_a:g} A")
        c.amps(s.charge_a)
        c.volts(s.charge_v)
        c.on()
        start, last, low = self.bench._clock(), None, 0
        self.bench.wait(max(1.0, s.ir_delay))
        while True:
            last = self._sample("charge", c, p, last)
            _, v, i = last
            if v > s.charge_v * 1.03:
                raise SequenceError(f"{v:.3f} V while charging to {s.charge_v:g} V: stopped")
            if i < -0.02:
                raise SequenceError("The battery is feeding the charger: stopped")
            # tapered only counts at the charge voltage: low current below it
            # means the charger isn't charging at all
            low = low + 1 if i < s.taper_a else 0
            if low >= 3 and v > 0.97 * s.charge_v:
                p.why = "charged"
            elif low >= 10:
                raise SequenceError(f"No charge current at {v:.3f} V: check the wiring and "
                                    "the charger")
            p.why = p.why or self._over(start, p)
            if p.why:
                break
            self.bench.wait(s.interval)
        c.off()
        self.bench.log(f"  {p.ah:.4f} Ah in ({p.why})")
        return p

    def _discharge(self, ocv: float) -> Phase:
        s, d, p = self.s, self.load, Phase()
        self.bench.log(f"Cycle {self.cycle}: discharging at {s.discharge_a:g} A to "
                       f"{s.cutoff_v:g} V")
        if ocv <= s.cutoff_v:
            p.why = "already empty"
            self.bench.log("  the battery is at the cut-off voltage already")
            return p
        d.amps(s.discharge_a)
        d.on()
        start, last, low = self.bench._clock(), None, 0
        self.bench.wait(s.ir_delay)
        none = 0
        while True:
            last = self._sample("discharge", d, p, last)
            _, v, i = last
            if math.isnan(p.r_start) and abs(i - s.discharge_a) < 0.1 * s.discharge_a:
                p.r_start = (ocv - v) / i
            # well below: at once; else two samples running (a lone spike isn't the end)
            low = low + 1 if v <= s.cutoff_v else 0
            if low >= 2 or v < 0.95 * s.cutoff_v:
                p.why = "cut-off"
            none = none + 1 if i < 0.1 * s.discharge_a else 0
            if not p.why and none >= 10:
                p.why = "no current (the load or the battery's protection switched off)"
            p.why = p.why or self._over(start, p)
            if p.why:
                break
            d.tick(i)
            self.bench.wait(s.interval)
        d.off()
        self.bench.log(f"  {p.ah:.4f} Ah, {p.wh:.4f} Wh out ({p.why})")
        return p

    def _rest(self):
        s = self.s
        if s.rest_s <= 0:
            return
        self.bench.log(f"Resting {s.rest_s:g} s")
        end = self.bench._clock() + s.rest_s
        p = Phase()
        while self.bench._clock() < end:
            v, _ = self._meter().read()
            row = {"t": self.bench._clock() - self.t0, "cycle": self.cycle, "phase": "rest",
                   "voltage": v, "current": 0.0, "ah": p.ah, "wh": p.wh, "temp": self._temp()}
            self.samples.append(row)
            if self._emit:
                self._emit(row)
            self.bench.wait(min(s.interval * 5, max(0.0, end - self.bench._clock())))


def estimate_hours(s: BatSettings, ah: float) -> float:
    """About how long a test takes with a battery of `ah`."""
    h = 0.0
    if s.load:
        h += s.cycles * (ah / s.discharge_a + s.rest_s / 3600)
    if s.charger:
        n = s.cycles - (s.start == "discharge") + bool(s.end_charged)
        h += n * (1.3 * ah / s.charge_a + s.rest_s / 3600)
    return h
