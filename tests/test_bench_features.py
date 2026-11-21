"""Limits / pass-fail, profiles, GPIO, function generators, watchdog, events,
math channels, reports, setups and the remote API (no Qt, no hardware)."""
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from ee_workbench import events, mathchannels as mc, profile, report, setups, watchdog
from ee_workbench.instruments.fgen import FunctionGenerator, looks_like_fgen
from ee_workbench.instruments.gpio import GpioDriver, PinConfig
from ee_workbench.sequencer import (Bench, Inst, Plan, SequenceError, Step, LimitFailed,
                                    run_plan)
from ee_workbench.signals import signals_of, slug
from ee_workbench.sim.sfgen import SimFgen
from ee_workbench.sim.sgpio import SimGpio


# ---------------------------------------------------------------- fakes
class DrvInst(Inst):
    def __init__(self, name, kind, drv):
        self.name, self.kind, self.drv = name, kind, drv
        self.off = False

    def run(self, fn, timeout=30.0):
        return fn(self.drv)

    def reading_after(self, t, timeout):
        d = self.drv
        return SimpleNamespace(t=t + 0.001, voltage=d.v, current=d.v / 1000, dvm=None, power=None)

    def outputs_off(self):
        self.off = True


class Sup:
    def __init__(self):
        self.v, self.log = 0.0, []

    def set_voltage(self, v):
        self.v = v
        self.log.append(v)

    def set_current(self, a):
        pass

    def set_output(self, on):
        pass


def gpio_driver():
    d = GpioDriver(SimGpio())
    d.identify()
    d.configure([PinConfig(20, "out", "relay"), PinConfig(21, "in", "done"),
                 PinConfig(16, "out", "led", active_low=True), PinConfig(12, "in")])
    return d


@pytest.fixture
def bench():
    sup = Sup()
    g = gpio_driver()
    fg = FunctionGenerator(SimFgen())
    fg.identify()
    insts = [DrvInst("66321D", "supply", sup), DrvInst("GPIO", "gpio", g),
             DrvInst("33522B", "fgen", fg)]
    rows = []
    b = Bench(insts, time.monotonic, log=lambda s: None, on_row=rows.append)
    b.sup, b.g, b.fg, b.seen = sup, g, fg, rows
    return b


# ---------------------------------------------------------------- limits
def test_limits_pass_fail(bench):
    plan = Plan(steps=[Step("Set", "66321D", "voltage", "x"),
                       Step("Measure", "66321D", "voltage", lo="3.1", hi="3.5", label="V")],
                loop="sweep", start=3.0, stop=3.6, step=0.2)
    run_plan(plan, bench)
    res = [r["result"] for r in bench.rows]
    assert res == ["FAIL", "PASS", "PASS", "FAIL"]
    assert bench.passed == 2 and bench.failed == 2
    assert bench.rows[0]["failures"].startswith("V=3 (want 3.1 … 3.5")
    assert report.failing_columns(bench.rows[0]) == ["V"]


def test_stop_on_fail(bench):
    plan = Plan(steps=[Step("Set", "66321D", "voltage", "x"),
                       Step("Measure", "66321D", "voltage", hi="3.1")],
                loop="sweep", start=3.0, stop=3.6, step=0.2, stop_on_fail=True)
    with pytest.raises(LimitFailed):
        run_plan(plan, bench)
    assert len(bench.rows) == 2 and bench.rows[-1]["result"] == "FAIL"


def test_rows_without_limits_have_no_result(bench):
    run_plan(Plan(steps=[Step("Measure", "66321D", "voltage")]), bench)
    assert "result" not in bench.rows[0]


def test_expect_in_script(bench):
    assert bench.expect("a", 1.0, lo=0, hi=2)
    assert not bench.expect("b", float("nan"), hi=2)
    bench.record(a=1)
    assert bench.rows[-1]["result"] == "FAIL" and "b=nan" in bench.rows[-1]["failures"]


# ---------------------------------------------------------------- profiles
def test_profile_parse_and_points():
    p = profile.parse("sine 3.0..3.6 period=2 for=4 rate=10")
    t, v = p.points()
    assert len(t) == 41 and v[0] == pytest.approx(3.0) and v[10] == pytest.approx(3.6)
    assert v.min() >= 3.0 - 1e-12 and v.max() <= 3.6 + 1e-12
    t, v = profile.parse("ramp 0..5 for=10 rate=1").points()
    assert list(v) == pytest.approx([0, .5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5])
    t, v = profile.parse("steps 1.8..3.6 n=7 dwell=2").points()
    assert list(t) == [0, 2, 4, 6, 8, 10, 12] and v[-1] == pytest.approx(3.6)
    t, v = profile.parse("logsteps 10..1000 n=3 dwell=1").points()
    assert list(v) == pytest.approx([10, 100, 1000])
    p = profile.parse("list 3.0 3.3 3.6 dwell=0.5")
    assert p.length == pytest.approx(1.5)
    t, v = profile.parse("square 0..1 period=1 duty=25 cycles=2 rate=4").points()
    assert list(v) == [1, 0, 0, 0, 1, 0, 0, 0, 1]
    for bad in ("", "wobble 1..2", "sine 1..2", "sine 1..2 for=x", "ramp for=2",
                "list dwell=1", "sine 1..2 period=0 for=1"):
        with pytest.raises(profile.ProfileError):
            profile.parse(bad).points()


def test_profile_csv(tmp_path):
    f = tmp_path / "p.csv"
    f.write_text("time_s,volts\n# comment\n1,3.0\n1.5,3.3\n3,3.6\n")
    t, v = profile.parse(f"csv {f}").points()
    assert list(t) == [0, 0.5, 2] and list(v) == [3.0, 3.3, 3.6]


def test_profile_run_keeps_time_and_stops():
    p = profile.parse("ramp 0..1 for=0.2 rate=100")
    got = []
    t0 = time.monotonic()
    profile.run(p, lambda v: (got.append(v), time.sleep(0.025)), threading.Event())
    took = time.monotonic() - t0
    assert 0.19 < took < 0.35 and got[-1] == pytest.approx(1.0)
    assert len(got) < 21         # slow setter: late points were skipped
    ev = threading.Event()
    threading.Timer(0.05, ev.set).start()
    with pytest.raises(profile.ProfileStopped):
        profile.run(profile.parse("list 1 2 3 dwell=1"), lambda v: None, ev)


def test_bench_profile(bench):
    n = bench.profile("66321D", "voltage", "list 3.0 3.3 dwell=0.02")
    assert n == 2 and bench.sup.log == [3.0, 3.3]
    with pytest.raises(SequenceError, match="can't run a profile"):
        bench.profile("GPIO", "voltage", "list 1 dwell=0")
    run_plan(Plan(steps=[Step("Profile", "33522B", "frequency", "list 100 200 dwell=0.01")]),
             bench)
    assert bench.fg.state.channels[0].frequency == 200


# ---------------------------------------------------------------- GPIO
def test_gpio_loopback_and_active_low():
    d = gpio_driver()
    assert d.get_pin("done") == 0
    d.set_pin("relay", True)
    assert d.get_pin("done") == 1 and d.get_pin("GPIO21") == 1
    d.set_pin("led", True)                      # active low: drives 0 → GPIO12 reads 1
    assert d.t.phys[16] == 0 and d.get_pin(12) == 1
    d.set_output(False)
    assert d.get_pin("done") == 0 and d.state.outputs[20] is False
    with pytest.raises(ValueError, match="isn't set up as an output"):
        d.set_pin("done", True)
    with pytest.raises(ValueError, match="No pin called"):
        d.pin("nope")
    r = d.poll(1.0)
    assert set(r.levels) == {20, 21, 16, 12}
    assert signals_of("gpio", r)["gpio21"][1][0] == 0


def test_gpio_in_sequence(bench):
    plan = Plan(steps=[Step("Set", "GPIO", "relay", "on"),
                       Step("Wait for", "GPIO", "done", "1 timeout=1", label="t_done", hi="0.5"),
                       Step("Measure", "GPIO", "done", label="done", lo="1"),
                       Step("Set", "GPIO", "relay", "pulse 0.01")])
    run_plan(plan, bench)
    r = bench.rows[-1]
    assert r["done"] == 1 and r["result"] == "PASS" and r["t_done"] < 0.5
    assert bench.g.state.outputs[20] is True       # pulse restores
    bench.g.set_pin("relay", False)
    with pytest.raises(SequenceError, match="didn't go high"):
        bench.wait_for("GPIO", "done", 1, timeout=0.05)


def test_gpio_button_sim():
    clock = [0.1]
    s = SimGpio(clock=lambda: clock[0])
    d = GpioDriver(s)
    d.identify()
    d.configure([PinConfig(26, "in", pull="up")])
    assert d.get_pin(26) == 0
    clock[0] = 1.0
    assert d.get_pin(26) == 1


# ---------------------------------------------------------------- function generator
@pytest.mark.parametrize("siglent", [False, True])
def test_fgen(siglent):
    sim = SimFgen(siglent=siglent)
    fg = FunctionGenerator(sim)
    caps = fg.identify()
    assert caps.dialect == ("siglent" if siglent else "scpi") and caps.nch == 2
    fg.set("function", "square", 2)
    fg.set("frequency", 12345.0, 2)
    fg.set("amplitude", 2.5, 2)
    fg.set("offset", 0.5, 2)
    fg.set("duty", 25, 2)
    fg.set("output", True, 2)
    c = FunctionGenerator(sim)
    c.identify()
    ch = c.state.channels[1]
    assert (ch.function, ch.frequency, ch.amplitude, ch.offset, ch.duty, ch.output) == \
        ("square", 12345.0, 2.5, 0.5, 25.0, True)
    c.set_output(False)
    assert not sim.ch[1].out and not sim.ch[2].out
    with pytest.raises(ValueError):
        c.set("frequency", -1)
    with pytest.raises(ValueError):
        c.select(3)
    assert not c.errors()


def test_fgen_detect():
    assert looks_like_fgen("Siglent Technologies,SDG1032X,SDG1X,1.01")
    assert looks_like_fgen("RIGOL TECHNOLOGIES,DG1062Z,DG1ZA,00.01")
    assert looks_like_fgen("Agilent Technologies,33220A,MY4,2.02")
    assert not looks_like_fgen("Agilent Technologies,66321D,0,A.02")


# ---------------------------------------------------------------- watchdog
def test_watchdog_hold_across_blocks():
    r = watchdog.Rule("PPK2", "current", ">", 0.05, hold_ms=10)
    w = watchdog.Watchdog([r])
    t = np.arange(0, 0.006, 1e-5)
    assert not w.feed("PPK2 · dut", {"current": (t, np.full(t.size, 0.06))})
    t2 = t + 0.006
    v = np.full(t.size, 0.06)
    tripped = w.feed("PPK2 · dut", {"current": (t2, v)})
    assert tripped == [r] and r.trip_t == pytest.approx(0.01, abs=2e-5)
    assert not w.feed("PPK2 · dut", {"current": (t2 + 1, v)})     # latched
    r.rearm()
    t3 = np.arange(0, 0.02, 1e-5)
    v3 = np.where((t3 > 0.005) & (t3 < 0.009), 0.1, 0.0)          # 4 ms burst: no trip
    assert not w.feed("PPK2", {"current": (t3, v3)})


def test_watchdog_slow_and_below():
    r = watchdog.Rule("66321D", "voltage", "<", 3.0)
    w = watchdog.Watchdog([r])
    assert not w.feed("66321D", {"voltage": (np.array([1.0]), np.array([3.3]))})
    assert w.feed("66321D", {"voltage": (np.array([1.2]), np.array([2.9]))}) == [r]
    r2 = watchdog.Rule("3456A", "value", ">", 1.0, hold_ms=500)
    w = watchdog.Watchdog([r2])
    assert not w.feed("3456A", {"value": (np.array([0.0]), np.array([2.0]))})
    assert not w.feed("3456A", {"value": (np.array([0.3]), np.array([2.0]))})
    assert w.feed("3456A", {"value": (np.array([0.6]), np.array([2.0]))})
    d = watchdog.Rule.from_dict(r2.to_dict())
    assert d == r2 and "tripped" not in r2.to_dict()


# ---------------------------------------------------------------- events
def test_find_events():
    t = np.arange(0, 1.0, 1e-5)
    i = np.full(t.size, 2e-6)
    for start, dur, amp in ((0.1, 0.002, 0.008), (0.3, 0.004, 0.012), (0.3045, 0.001, 0.01),
                            (0.7, 0.00002, 0.02)):
        i[(t >= start) & (t < start + dur)] = amp
    ev = events.find_events(t, i, 1e-3, min_gap=1e-3, min_len=100e-6)
    assert len(ev) == 2
    assert ev[0].t0 == pytest.approx(0.1) and ev[0].duration == pytest.approx(0.002, rel=0.01)
    assert ev[0].charge == pytest.approx(0.008 * 0.002, rel=0.02)
    assert ev[1].duration == pytest.approx(0.0055, rel=0.02) and ev[1].peak == 0.012
    s = events.summary(ev)
    assert s["count"] == 2 and s["interval_mean"] == pytest.approx(0.2)
    events.attach_bus(ev, [(0.0, "a"), (0.2005, "b"), (0.9, "c")], offset=0.1)
    assert ev[0].bus == ["a"] and ev[1].bus == ["b"]
    counts, edges = events.histogram([e.charge for e in ev], 5)
    assert counts.sum() == 2


def test_events_hysteresis():
    t = np.arange(0, 0.01, 1e-5)
    i = np.where((t > 0.002) & (t < 0.006), 1e-3, 0.0)
    i[(t > 0.0035) & (t < 0.0036)] = 0.85e-3        # dips just under the threshold
    assert len(events.find_events(t, i, 0.9e-3)) == 2
    assert len(events.find_events(t, i, 0.9e-3, hysteresis=0.2)) == 1


# ---------------------------------------------------------------- math
def test_math_evaluate():
    t = np.linspace(0, 10, 101)
    series = {("_66321d_out", "power"): (t, np.full(t.size, 0.9)),
              ("_6632a_in", "power"): (t, np.full(t.size, 1.0)),
              ("ppk2", "current"): (t, t / 10)}
    g, y = mc.evaluate("_66321d_out.power / _6632a_in.power * 100", series)
    assert y[0] == pytest.approx(90)
    g, y = mc.evaluate("avg(ppk2.current, 2)", series)
    assert y[-1] == pytest.approx(0.9, abs=0.01)
    with pytest.raises(mc.MathError, match="Unknown signal"):
        mc.evaluate("scope.voltage", series)
    with pytest.raises(mc.MathError, match="Syntax"):
        mc.evaluate("ppk2.current +", series)
    with pytest.raises(mc.MathError):
        mc.evaluate("__import__('os')", series)
    assert mc.integrate(np.array([0, 1, 2.0]), np.array([1, 1, 1.0]))[-1] == 2.0
    assert slug("66321D · DUT rail") == "_66321d_dut_rail" and slug("PPK2") == "ppk2"
    assert slug("PPK2 (PPK2::/dev/ttyACM0)") == "ppk2" and slug("66321D (SIM::66321D)") == "_66321d"


def test_resample_averages_dense_signals():
    t = np.arange(0, 10, 1e-3)
    v = np.where((t % 0.1) < 0.0025, 6.7e-3, 0.0)          # 2.5 ms bursts every 100 ms
    grid = np.arange(1, 10, 0.05)
    y = mc.resample(t, v, grid)
    assert y.mean() == pytest.approx(v.mean(), rel=0.05)
    g, a = mc.evaluate("avg(p.current, 2)", {("p", "current"): (t, v)}, grid)
    assert np.nanstd(a[60:]) < 0.05 * v.mean()


def test_live_math():
    m = mc.LiveMath()
    m.channels = [mc.Channel("P", "sup.voltage * sup.current"),
                  mc.Channel("E", "sup.voltage * sup.current", integrate=True),
                  mc.Channel("bad", "nope.x")]
    for k in range(20):
        m.feed("sup", {"voltage": (np.array([k * 0.1]), np.array([2.0])),
                       "current": (np.array([k * 0.1]), np.array([0.5]))})
    out = m.tick(1.9)
    assert out["P"] == pytest.approx(1.0) and "bad" in m.errors
    out = m.tick(2.9)
    assert out["E"] == pytest.approx(1.0)
    t = np.arange(0, 0.05, 1e-5)
    m.feed("PPK2", {"current": (t, np.full(t.size, 1e-3))})
    assert "ppk2.current" in m.available()
    assert len(m.hist[("ppk2", "current")]) == 50


# ---------------------------------------------------------------- reports
def test_sequence_report_and_pdf(tmp_path, bench):
    plan = Plan(steps=[Step("Set", "66321D", "voltage", "x"),
                       Step("Measure", "66321D", "voltage", lo="3.1", label="V")],
                loop="sweep", start=3.0, stop=3.2, step=0.1)
    run_plan(plan, bench)
    cols = list(dict.fromkeys(k for r in bench.rows for k in r))
    html = report.sequence_report("Rail test", bench.rows, cols, plan.to_dict(), "log line",
                                  [("66321D", "Agilent 66321D")], "Finished")
    assert "<svg" in html and "FAIL" in html and "Rail test" in html
    assert "class='fail'" in html
    report.save(html, str(tmp_path / "r.html"))
    pytest.importorskip("weasyprint")
    report.save(html, str(tmp_path / "r.pdf"))
    assert (tmp_path / "r.pdf").read_bytes()[:4] == b"%PDF"


def test_session_report(tmp_path):
    from ee_workbench.session import SessionWriter
    w = SessionWriter()
    p = str(tmp_path / "s.eewb")
    w.start(p)
    w.stream("a", "66321D", "663xx", "SIM", ["voltage_V", "current_A"])
    for k in range(50):
        w.add("a", k * 0.1, [3.3, 0.01 * k])
    w.note("hello", 1.0)
    w.stream("seq", "Sequencer", "sequence", "", ["pass", "V", "result"])
    w.add("seq", 2.0, [1, 3.3, "PASS"])
    w.flush()
    w.stop()
    html = report.session_report(p)
    assert html.count("<svg") == 2 and "hello" in html and "Sequencer results" in html


def test_svg_plot_handles_edge_cases():
    assert "no data" in report.svg_plot([("a", np.array([]), np.array([]))])
    s = report.svg_plot([("a", np.array([1.0]), np.array([5.0]))])
    assert "<polyline" in s
    x = np.arange(100_000.0)
    s = report.svg_plot([("a", x, np.sin(x))])
    assert s.count(",") < 4000


# ---------------------------------------------------------------- setups
def test_setups(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    data = {"instruments": [{"resource": "SIM::PPK2", "model": "PPK2", "settings": {"a": 1}}],
            "sequence": {"steps": []}, "watchdog": [], "math": []}
    setups.save_setup("nRF52 sleep/test", data, "sleep current")
    assert [s[0] for s in setups.list_setups()] == ["nRF52 sleep/test"]
    d = setups.load_setup("nRF52 sleep/test")
    assert d["instruments"][0]["settings"] == {"a": 1} and d["description"] == "sleep current"
    out = tmp_path / "x.json"
    setups.export_setup("nRF52 sleep/test", str(out))
    setups.delete_setup("nRF52 sleep/test")
    assert setups.list_setups() == []
    assert setups.import_setup(str(out)) == "nRF52 sleep/test"
    with pytest.raises(ValueError):
        setups.load_setup("nope")


# ---------------------------------------------------------------- remote API
def test_api_roundtrip(tmp_path, monkeypatch, bench):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from ee_workbench.api import ApiServer
    from ee_workbench.client import BenchError, RemoteBench, connect
    srv = ApiServer(lambda: bench, port=0)
    srv.start()
    try:
        info = json.loads((tmp_path / "ee-workbench" / "api.json").read_text())
        assert (tmp_path / "ee-workbench" / "api.json").stat().st_mode & 0o777 == 0o600
        b = connect()
        assert b.instruments() == ["66321D", "GPIO", "33522B"]
        b.set("66321D", "voltage", 3.3)
        assert b.measure("66321D", "voltage") == pytest.approx(3.3)
        b.gpio("GPIO", "relay", "on")
        assert b.read_pin("GPIO", "done") == 1
        assert b.expect("v", 3.3, lo=3.0) is True
        with pytest.raises(BenchError, match="No open instrument"):
            b.set("scope", "voltage", 1)
        with pytest.raises(BenchError, match="no method"):
            b.call("__init__")
        with pytest.raises(BenchError, match="bad token"):
            RemoteBench(info["url"], "wrong").instruments()
    finally:
        srv.stop()
    assert not (tmp_path / "ee-workbench" / "api.json").exists()


# ---------------------------------------------------------------- open-sense detection
def test_sense_protect_from_a_sequence():
    from ee_workbench.instruments.agilent663xx import Agilent663xx
    from ee_workbench.instruments.hp6632a import HP6632A
    from ee_workbench.sim.s663xx import Sim663xx
    from ee_workbench.sim.s6632a import Sim6632A
    a = Agilent663xx(Sim663xx())
    a.identify()
    b = HP6632A(Sim6632A())
    b.identify()
    bench = Bench([DrvInst("66321D", "supply", a), DrvInst("6632A", "supply", b)],
                  time.monotonic, log=lambda s: None)
    bench.set("66321D", "sense_protect", "on")
    assert a.read_setpoints().sense_protect is True
    bench.set("66321D", "sense_protect", "off")
    assert a.read_setpoints().sense_protect is False
    with pytest.raises(SequenceError, match="no open-sense"):
        bench.set("6632A", "sense_protect", "on")
