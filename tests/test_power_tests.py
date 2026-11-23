"""Regulator characterization and battery tester, on a simulated regulator and
a simulated battery with a clock that jumps."""
import math
import threading
from types import SimpleNamespace as NS

import pytest

from ee_workbench import battery as bat
from ee_workbench import regulator as reg
from ee_workbench import roles
from ee_workbench.sequencer import Bench, Inst, SequenceError, Stopped


class World:
    """The clock and what is wired up."""

    def __init__(self):
        self.t = 0.0
        self.insts = []
        self.hooks = []

    def step(self, dt):
        while dt > 0:
            d = min(dt, 0.25)
            for h in self.hooks:
                h(d)
            self.t += d
            dt -= d


class FakeInst(Inst):
    def __init__(self, w, name):
        self.w, self.name, self.stop = w, name, threading.Event()
        w.insts.append(self)

    def reading_after(self, t, timeout):
        self.w.step(0.05)
        v, i = self.vi()
        return NS(t=self.w.t, voltage=v, current=i, power=None, dvm=None,
                  values={"voltage": v, "current": i, "temp": getattr(self, "temp", 25.0)})

    def run(self, fn, timeout=30.0):
        return fn(self)


class Supply(FakeInst):
    kind = "supply"

    def __init__(self, w, name, sink=False):
        super().__init__(w, name)
        self.v = self.i = self.r = 0.0
        self.out = False
        self.caps = NS(has_resistance=sink)
        self.log = []

    def set_voltage(self, v):
        self.v = v

    def set_current(self, a):
        self.i = a

    def set_resistance(self, r):
        self.r = r

    def set_output(self, on):
        self.out = bool(on)
        self.log.append(self.out)


class Load(FakeInst):
    kind = "scpi"
    set_names = ["function", "cc", "input"]
    setting_kinds = {"function": "choice", "cc": "number", "input": "bool"}
    measure_names = ["voltage", "current"]

    def __init__(self, w, name="Load", cutoff=False):
        super().__init__(w, name)
        self.vals = {"function": "CV", "cc": 0.0, "input": False}
        if cutoff:
            self.set_names = ["cc", "input", "cutoff"]
        self.d = NS(setting=lambda n: NS(options=[NS(label="CV"), NS(label="CC")])
                    if n == "function" else None)

    def set(self, n, v):
        self.vals[n] = v

    @property
    def amps(self):
        return self.vals["cc"] if self.vals["input"] else 0.0


def bench_for(w, log=None):
    b = Bench(w.insts, lambda: w.t, log=(log if log is not None else []).append)

    def wait(s):
        if b.stop_event.is_set():
            raise Stopped()
        w.step(float(s))
    b.wait = wait
    return b


# ---- roles -------------------------------------------------------------------
def test_ports_of_real_definitions():
    from ee_workbench.instruments import definition_for

    def inst(key):
        d = definition_for(key)
        return NS(kind="scpi", name=d.id, set_names=[s.name for s in d.settings],
                  measure_names=[r.name for r in d.readings])
    got = {(f.text, f.role) for f in roles.ports(
        [inst("SCPI:" + k) for k in ("DP832", "DL3021", "SDL1020", "K2380", "PX100", "RD60XX",
                                     "KA3005P", "E36312A", "SPD3303X", "SCPI-SUPPLY",
                                     "34461A")])}
    assert {("DP832 output 1", "source"), ("DP832 output 3", "source"), ("DL3021", "load"),
            ("SDL1020", "load"), ("K2380", "load"), ("PX100", "load"), ("RD60XX", "source"),
            ("KA3005P", "source"), ("E36312A output 2", "source"),
            ("SPD3303X output 1", "source"), ("SCPI-SUPPLY", "source")} <= got
    assert not any(t.startswith("34461A") for t, _ in got)


def test_cc_mode_of_every_load_definition():
    from ee_workbench.instruments import definition_for
    for k in ("DL3021", "SDL1020", "K2380"):
        d = definition_for("SCPI:" + k)
        ld = roles.Load.__new__(roles.Load)
        ld.inst, ld.ch, ld.name = NS(run=lambda fn: fn(NS(d=d))), "", k
        assert ld._cc_mode().upper().startswith("C")


# ---- regulator -----------------------------------------------------------------
def test_value_lists():
    assert reg.parse_list("5, 9;12 15") == [5, 9, 12, 15]
    assert reg.parse_list("4.5 … 6 step 0.5") == [4.5, 5, 5.5, 6]
    assert reg.parse_list("4.5-6/0.5") == [4.5, 5, 5.5, 6]
    assert reg.parse_list("0..2 x 5") == [0, 0.5, 1, 1.5, 2]
    assert reg.parse_list("2..1 step 0.5") == [2, 1.5, 1]
    for bad in ("", "abc", "1..2 step 0"):
        with pytest.raises(ValueError):
            reg.parse_list(bad)


def regulator(w, vnom=3.3, dropout=0.3, rout=0.02, runaway=None):
    src, load = Supply(w, "In"), Load(w)

    def solve():
        vin = src.v if src.out else 0.0
        vout = max(0.0, min(vnom, vin - dropout) - rout * load.amps)
        if runaway and vin > runaway:
            vout = vin
        iin = load.amps + 0.005 if vin else 0.0        # an LDO
        return vin, iin, vout
    src.vi = lambda: solve()[:2]
    load.vi = lambda: (solve()[2], load.amps)
    return src, load


SET = dict(source="In", load="Load", vin="5, 9", iload="0, 0.5, 1", vout_nominal=3.3,
           vout_max=3.6, settle=0.1, average=0.1)


def test_regulator_table_and_analysis():
    w = World()
    src, load = regulator(w)
    s = reg.RegSettings(**SET, min_efficiency=30, max_load_reg=1, max_line_reg=0.1, tolerance=2)
    t = reg.RegTest(s)
    t.run(bench_for(w))
    assert len(t.rows) == 6 and not any(r["note"] for r in t.rows)
    r = next(r for r in t.rows if r["vin_set"] == 5 and r["iload_set"] == 1)
    assert r["efficiency"] == pytest.approx(100 * 3.28 / (5 * 1.005))
    assert r["loss"] == pytest.approx(5 * 1.005 - 3.28)
    a = reg.analyse(t.rows, s)
    assert a["load"][0]["ohms"] == pytest.approx(0.02)
    assert a["load"][0]["percent"] == pytest.approx(100 * 0.02 / 3.3)
    assert a["line"][0]["mv_per_v"] == pytest.approx(0)
    assert a["best"]["vin_set"] == 5 and a["verdict"] == "PASS"
    assert load.vals["function"] == "CC"
    assert not load.vals["input"] and not src.out           # everything off
    s.min_efficiency = 60
    assert reg.analyse(t.rows, s)["verdict"] == "FAIL"
    s.min_efficiency = s.max_load_reg = s.max_line_reg = s.tolerance = 0
    assert reg.analyse(t.rows, s)["verdict"] == ""


def test_regulator_overvoltage_stops_everything():
    w = World()
    src, load = regulator(w, runaway=6)
    t = reg.RegTest(reg.RegSettings(**SET))
    with pytest.raises(SequenceError, match="above 3.6"):
        t.run(bench_for(w))
    assert not src.out and not load.vals["input"]
    assert t.rows[-1]["vout"] == 9


def test_more_out_than_in_is_flagged():
    w = World()
    src, load = regulator(w)
    src.vi = lambda: (5.0, 0.001)
    t = reg.RegTest(reg.RegSettings(**{**SET, "vin": "5"}))
    t.run(bench_for(w))
    assert "more power out" in t.rows[-1]["note"] and not t.rows[0]["note"]
    assert "best" not in reg.analyse(t.rows, t.s)


def test_regulator_dropout_skips_the_rest_and_cannot_pass():
    w = World()
    regulator(w)
    s = reg.RegSettings(**{**SET, "vin": "1.5, 5"}, tolerance=5)
    t = reg.RegTest(s)
    t.run(bench_for(w))
    assert [r["iload_set"] for r in t.rows if r["vin_set"] == 1.5] == [0, 0.5]
    assert all("output low" in r["note"] for r in t.rows if r["vin_set"] == 1.5)
    a = reg.analyse(t.rows, s)
    assert a["verdict"] == "FAIL" and any("points measured" in c for c, _ in a["checks"])


def test_regulator_stop_and_bad_settings():
    w = World()
    src, load = regulator(w)
    b = bench_for(w)
    with pytest.raises(Stopped):
        reg.RegTest(reg.RegSettings(**SET)).run(b, lambda r: b.stop_event.set())
    assert not src.out and not load.vals["input"]
    for bad in (dict(vin="x"), dict(source=""), dict(load=""), dict(vout_nominal=0),
                dict(vout_max=3.3), dict(iin_limit=0), dict(iload="-1")):
        assert reg.RegSettings(**{**SET, **bad}).problem(), bad
    with pytest.raises(SequenceError, match="isn't an open power supply"):
        reg.RegTest(reg.RegSettings(**{**SET, "source": "Nope"})).run(bench_for(w))


# ---- battery -----------------------------------------------------------------------
class Cell:
    """1 Ah, 3.0 V empty to 4.2 V full, 50 mΩ."""

    def __init__(self, w, soc=0.5, r=0.05, ah=1.0):
        self.w, self.soc, self.r, self.ah = w, soc, r, ah
        self.chg = self.load = None
        self.leak = 0.0
        w.hooks.append(self.advance)

    def ocv(self):
        return 3.0 + 1.2 * self.soc

    def solve(self):
        """(volts, charge current into it)."""
        i = 0.0
        if self.load is not None and isinstance(self.load, Load):
            i -= self.load.amps
        for s in {x for x in (self.chg, self.load) if isinstance(x, Supply)}:
            if s.out:
                x = (s.v - self.ocv()) / (self.r + s.r)
                i += min(x, s.i)
        return self.ocv() + i * self.r, i

    def advance(self, dt):
        self.soc = max(-0.1, self.soc + self.solve()[1] * dt / 3600 / self.ah)


def rig(soc=0.5, cutoff=False):
    w = World()
    c = Cell(w, soc)
    c.chg, c.load = Supply(w, "Chg"), Load(w, "Load", cutoff)
    c.chg.vi = lambda: (c.solve()[0], c.solve()[1] if c.chg.out else 0.0)
    c.load.vi = lambda: (c.solve()[0], c.load.amps)
    return w, c


BAT = dict(load="Load", charger="Chg", cutoff_v=3.1, charge_v=4.2, charge_a=0.5, taper_a=0.05,
           discharge_a=0.5, rest_s=60, interval=5, max_hours=20)


def test_two_cycles():
    w, c = rig()
    t = bat.BatTest(bat.BatSettings(**BAT, cycles=2))
    t.run(bench_for(w))
    assert len(t.cycles) == 2
    one, two = t.cycles
    assert two["discharge_ah"] == pytest.approx(0.9, abs=0.06)
    assert two["coulombic"] == pytest.approx(100, abs=2)
    for x in (one, two):
        assert x["ended"] == "cut-off"
        assert x["r_start"] == pytest.approx(0.05, rel=0.1)
        assert x["r_end"] == pytest.approx(0.05, rel=0.1)
    assert two["discharge_wh"] == pytest.approx(two["discharge_ah"] * 3.6, rel=0.05)
    assert not c.chg.out and not c.load.vals["input"]
    ph = {s["phase"] for s in t.samples}
    assert ph == {"charge", "rest", "discharge"}
    assert all(s["current"] <= 0 for s in t.samples if s["phase"] == "discharge")
    # never both at once
    assert min(s["voltage"] for s in t.samples) > 2.9


def test_charger_and_load_are_never_on_together():
    w, c = rig()
    seen = []
    w.hooks.append(lambda dt: seen.append(c.chg.out and c.load.vals["input"]))
    bat.BatTest(bat.BatSettings(**BAT, end_charged=True)).run(bench_for(w))
    assert not any(seen) and c.soc > 0.9


def test_discharge_only_sets_the_loads_own_cutoff():
    w, c = rig(soc=0.9, cutoff=True)
    log = []
    t = bat.BatTest(bat.BatSettings(**{**BAT, "charger": "", "start": "discharge"}))
    t.run(bench_for(w, log))
    assert c.load.vals["cutoff"] == pytest.approx(3.1 * 0.98)
    assert t.cycles[0]["discharge_ah"] == pytest.approx(0.8, abs=0.05)
    assert math.isnan(t.cycles[0]["coulombic"])


def test_663xx_as_the_load():
    w = World()
    c = Cell(w, 0.8)
    s = Supply(w, "66321D", sink=True)
    c.chg = c.load = s
    s.vi = lambda: (c.solve()[0], c.solve()[1] if s.out else 0.0)
    t = bat.BatTest(bat.BatSettings(**{**BAT, "load": "66321D", "charger": "66321D",
                                       "interval": 1.0}, cycles=1))
    t.run(bench_for(w))
    d = [x["current"] for x in t.samples if x["phase"] == "discharge"]
    assert -0.51 < d[len(d) // 2] < -0.49                 # trimmed to what was asked
    assert min(d) > -0.6                                   # and no gross overshoot
    assert t.cycles[0]["ended"] == "cut-off" and not s.out and s.r == 0
    with pytest.raises(SequenceError, match="can sink about"):
        bat.BatTest(bat.BatSettings(**{**BAT, "load": "66321D", "charger": "",
                                       "start": "discharge", "discharge_a": 3})).run(bench_for(w))


def test_safety_limits():
    w, c = rig()
    c.chg.vi = lambda: (4.5, 0.5)                          # a charger gone mad
    with pytest.raises(SequenceError, match="while charging"):
        bat.BatTest(bat.BatSettings(**BAT)).run(bench_for(w))
    assert not c.chg.out

    w, c = rig()
    c.solve = lambda: (-3.7, 0.0)
    with pytest.raises(SequenceError, match="wrong way round"):
        bat.BatTest(bat.BatSettings(**BAT)).run(bench_for(w))
    assert c.chg.log and not any(c.chg.log)                # never switched on

    w, c = rig()
    th = FakeInst(w, "Thermo")
    th.kind, th.measure_names, th.vi, th.temp = "scpi", ["temp"], lambda: (0, 0), 25.0
    w.hooks.append(lambda dt: setattr(th, "temp", 25 + w.t / 60))
    with pytest.raises(SequenceError, match="above 45"):
        bat.BatTest(bat.BatSettings(**BAT, temp="Thermo: temp", temp_max=45)).run(bench_for(w))
    assert not c.chg.out and not c.load.vals["input"]

    w, c = rig()
    c.chg.vi = lambda: (c.solve()[0], 0.0)                 # no current flows
    with pytest.raises(SequenceError, match="No charge current"):
        bat.BatTest(bat.BatSettings(**BAT)).run(bench_for(w))


def test_phase_limits_and_stop():
    w, c = rig(soc=0.9)
    t = bat.BatTest(bat.BatSettings(**{**BAT, "charger": "", "start": "discharge"}, max_ah=0.1))
    t.run(bench_for(w))
    assert t.cycles[0]["ended"].startswith("capacity limit")
    assert t.cycles[0]["discharge_ah"] == pytest.approx(0.1, abs=0.002)
    w, c = rig(soc=0.9)
    b = bench_for(w)
    with pytest.raises(Stopped):
        bat.BatTest(bat.BatSettings(**{**BAT, "charger": "", "start": "discharge"})).run(
            b, lambda s: b.stop_event.set())
    assert not c.load.vals["input"]


def test_battery_settings():
    assert not bat.BatSettings(**BAT).problem()
    for bad in (dict(load="", charger=""), dict(charger="", start="charge"),
                dict(charger="", start="discharge", cycles=2), dict(taper_a=0.6),
                dict(charge_v=3.0), dict(discharge_a=0), dict(cutoff_v=0), dict(interval=0.1),
                dict(max_hours=0)):
        assert bat.BatSettings(**{**BAT, **bad}).problem(), bad
    assert bat.estimate_hours(bat.BatSettings(**BAT), 1.0) > 4
