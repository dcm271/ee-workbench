"""Hardware watchdog limits, the flight recorder, safety (crash marker,
reconnect, outputs off), production test and resuming a sequence."""
import json
import os
import sqlite3
import time

import numpy as np
import pytest

from otterbench import production, safety, watchdog
from otterbench.instruments import make_driver, model_for
from otterbench.sequencer import Bench, Plan, Step, run_plan
from otterbench.session import SessionWriter
from otterbench.sim import open_sim


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "otterbench"


def _drv(res):
    t = open_sim(res)
    d = make_driver(model_for(res), t, res)
    d.identify()
    return d


# ---------------------------------------------------------------- watchdog
def test_rule_round_trip_with_new_fields():
    r = watchdog.Rule("PPK2", "current", ">", 0.05, 10, "capture", "Logic", hw=True,
                      rearm_s=30)
    back = watchdog.Rule.from_dict(json.loads(json.dumps(r.to_dict())))
    assert back == r and back.hw and back.rearm_s == 30
    assert "[+HW]" in r.describe() and "re-arms" in r.describe()


def test_auto_rearm():
    wd = watchdog.Watchdog([watchdog.Rule("PPK2", "current", ">", 1.0, rearm_s=5)])
    t = np.array([0.0])
    assert wd.feed("PPK2", {"current": (t, np.array([2.0]))})
    rule = wd.rules[0]
    assert rule.tripped
    assert wd.auto_rearm(rule.tripped_at + 1) == []
    assert wd.auto_rearm(rule.tripped_at + 6) == [rule] and not rule.tripped


def test_hw_plan_on_a_663xx_simulator():
    d = _drv("SIM::66321D")
    d.set_current(2.0)
    job, text = watchdog.hw_plan(watchdog.Rule("x", "current", ">", 0.5, hw=True), "supply")
    assert "0.5" in text
    job(d)
    assert d.read_setpoints().current == pytest.approx(0.5, abs=1e-3)
    job, _ = watchdog.hw_plan(watchdog.Rule("x", "voltage", ">", 4.2, hw=True), "supply")
    job(d)
    assert d.read_setpoints().ovp == pytest.approx(4.2, abs=0.01)


def test_hw_plan_on_a_scpi_supply_channel():
    d = _drv("SIM::DP832")
    names = [s.name for s in d.d.settings]
    job, _ = watchdog.hw_plan(watchdog.Rule("x", "voltage2", ">", 5.5, hw=True), "scpi", names)
    job(d)
    assert d.values["ovp2"] == pytest.approx(5.5) and d.values["ovp_on2"] is True
    job, _ = watchdog.hw_plan(watchdog.Rule("x", "current3", ">", 0.2, hw=True), "scpi", names)
    job(d)
    assert d.values["iset3"] == pytest.approx(0.2) and d.values["ocp_on3"] is True


def test_hw_plan_refuses_what_it_cannot_do():
    for rule, kind in ((watchdog.Rule("x", "current", "<", 1), "supply"),
                       (watchdog.Rule("x", "power", ">", 1), "supply"),
                       (watchdog.Rule("x", "current", ">", 1), "ppk2"),
                       (watchdog.Rule("x", "voltage", ">", 1), "scpi")):
        job, why = watchdog.hw_plan(rule, kind, [])
        assert job is None and why


# ---------------------------------------------------------------- flight recorder
def test_gated_session_keeps_only_around_events(tmp_path):
    w = SessionWriter()
    path = str(tmp_path / "g.otter")
    w.start(path, gate=(1.0, 0.5))
    w._shift = 0.0
    w.stream("k", "x", "m", "r", ["v"])
    for i in range(1000):
        t = i * 0.01
        w.add("k", t, [t])
        if i == 500:
            w.trigger(5.0, "event A")
    w.note("always kept", 0.5)
    w.stop()
    db = sqlite3.connect(path)
    ts = [r[0] for r in db.execute("SELECT t FROM s_1")]
    assert min(ts) == pytest.approx(4.0) and max(ts) == pytest.approx(5.5)
    assert len(ts) == 151
    assert db.execute("SELECT why FROM events").fetchall() == [("event A",)]
    assert db.execute("SELECT count(*) FROM notes").fetchone()[0] == 1
    assert "pre_s" in dict(db.execute("SELECT key, value FROM meta"))["gate"]


def test_ungated_trigger_is_a_no_op(tmp_path):
    w = SessionWriter()
    w.start(str(tmp_path / "a.otter"))
    assert w.trigger(1.0) is False
    w.stop()


# ---------------------------------------------------------------- safety
def test_crash_marker(cfgdir):
    assert safety.previous_run() is None
    safety.write_marker([{"resource": "SIM::66321D", "model": "auto"}])
    assert safety.previous_run() is None             # our own process: not a crash
    p = safety.marker_path()
    d = json.loads(p.read_text(encoding="utf-8"))
    d["pid"] = 2 ** 22 + 12345                      # a process that isn't running
    p.write_text(json.dumps(d), encoding="utf-8")
    prev = safety.previous_run()
    assert prev["open"][0]["resource"] == "SIM::66321D"
    safety.clear_marker()
    assert not p.exists()


def test_damaged_marker_counts_as_a_crash(cfgdir):
    safety.marker_path().parent.mkdir(parents=True)
    safety.marker_path().write_text("{nope", encoding="utf-8")
    assert safety.previous_run()["damaged"]


def test_safety_settings_and_backoff():
    s = safety.Safety.from_dict({"off_on_quit": 1, "heartbeat_period": "0.5",
                                 "heartbeat_tab": "GPIO", "heartbeat_pin": "GPIO17",
                                 "junk": 3, "auto_reconnect": "yes"})
    assert s.off_on_quit is True and s.heartbeat and s.heartbeat_period == 0.5
    assert safety.Safety.from_dict("garbage") == safety.Safety()
    b = safety.Backoff()
    assert [b.next_delay("r") for _ in range(7)] == [3, 6, 12, 24, 48, 60, 60]
    b.reset("r")
    assert b.next_delay("r") == 3
    assert safety.device_path("PPK2::/dev/ttyACM0") == "/dev/ttyACM0"
    assert safety.device_path("GPIB0::5::INSTR") == ""


# ---------------------------------------------------------------- production
def test_results_db_history_and_yield(tmp_path):
    db = production.ResultsDB(tmp_path / "r.db")
    t = time.time()
    db.add("SN1", "Board", "dm", t, "FAIL", "i=0.3 (want ≤ 0.2)")
    db.add("SN1", "Board", "dm", t, "PASS")
    db.add("SN2", "Board", "dm", t, "PASS")
    db.add("SN3", "Other", "dm", t, "ERROR", "Stopped")
    y = db.yield_stats("Board")
    assert (y.tested, y.units, y.passed, y.failed) == (3, 2, 2, 1)
    assert y.first_pass_yield == pytest.approx(0.5) and y.final_yield == 1.0
    assert [u.serial for u in db.history("Board")] == ["SN2", "SN1", "SN1"]
    assert len(db.previous("SN1", "Board")) == 2
    assert db.products() == ["Board", "Other"]
    out = tmp_path / "r.csv"
    assert db.export_csv(str(out)) == 4
    assert out.read_text(encoding="utf-8").splitlines()[0].startswith("id,serial,product")
    with pytest.raises(ValueError):
        db.add("x", "", "", t, "MAYBE")


def test_verdict():
    assert production.verdict("Finished", 1, 0) == "PASS"
    assert production.verdict("Finished", 0, 0) == "PASS"
    assert production.verdict("Finished", 0, 1) == "FAIL"
    assert production.verdict("Stopped at the first fail (x)", 0, 1) == "FAIL"
    assert production.verdict("Stopped", 0, 0) == "ERROR"
    assert production.verdict("Failed: no reading", 0, 0) == "ERROR"


# ---------------------------------------------------------------- resume / extra
def test_run_plan_start_extra_and_on_pass():
    notes, rows, seen = [], [], []
    bench = Bench([], clock=time.monotonic, log=lambda *_: None, on_row=rows.append,
                  on_note=notes.append)
    plan = Plan(steps=[Step("Note", value="unit {serial} pass {i}")], loop="repeat", count=4)
    run_plan(plan, bench, start=2, extra={"serial": "SN9"}, on_pass=seen.append)
    assert [r["pass"] for r in rows] == [3, 4]
    assert rows[0]["serial"] == "SN9"
    assert notes == ["unit SN9 pass 3", "unit SN9 pass 4"]
    assert seen == [2, 3]


# ---------------------------------------------------------------- GUI (offscreen)
@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qt = pytest.importorskip("PyQt6.QtWidgets")
    yield qt.QApplication.instance() or qt.QApplication([])


def _spin(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def _wait(app, cond, seconds=8):
    end = time.monotonic() + seconds
    while not cond() and time.monotonic() < end:
        _spin(app, 0.05)
    return cond()


def test_gui_hw_limit_pushed_to_supply(qapp, cfgdir):
    from otterbench.config import Config
    from otterbench.gui.hub import hub
    from otterbench.gui.main_window import MainWindow
    win = MainWindow([("auto", "SIM::DP832")], Config(), None)
    old = hub.watchdog.rules
    try:
        p = win.panels()[0]
        assert _wait(qapp, lambda: p.connected)
        hub.watchdog.rules = [watchdog.Rule(p.display_name(), "voltage1", ">", 6.0, hw=True)]
        win.apply_hw_limits()
        assert _wait(qapp, lambda: p.values.get("ovp1") == pytest.approx(6.0))
        assert "ovp1" in win.hw_status[id(hub.watchdog.rules[0])]
    finally:
        hub.watchdog.rules = old
        win.close()
        _spin(qapp, 0.2)


def test_gui_outputs_off_on_quit_and_marker(qapp, cfgdir):
    from otterbench.config import Config
    from otterbench.gui.main_window import MainWindow
    from otterbench.instruments.scpi_generic import ScpiDriver
    win = MainWindow([("auto", "SIM::DL3021")], Config(), None)
    p = win.panels()[0]
    assert _wait(qapp, lambda: p.connected)
    assert json.loads(safety.marker_path().read_text(encoding="utf-8"))["open"][0]["resource"] == "SIM::DL3021"
    sent = []
    real = ScpiDriver.set_output
    ScpiDriver.set_output = lambda self, on: (sent.append(on), real(self, on))[1]
    try:
        win.safety.off_on_quit = True
        p._set("input", True)
        assert _wait(qapp, lambda: p.values.get("input") is True)
        win.close()
        _spin(qapp, 0.5)
        assert sent == [False]
        assert not safety.marker_path().exists()
    finally:
        ScpiDriver.set_output = real


def test_gui_off_after_crash(qapp, cfgdir):
    from otterbench.config import Config
    from otterbench.gui.main_window import MainWindow
    safety.marker_path().parent.mkdir(parents=True, exist_ok=True)
    safety.marker_path().write_text(json.dumps({"pid": 2 ** 22 + 999, "started": "x",
                                                "open": [{"resource": "SIM::E36312A",
                                                          "model": "SCPI:E36312A"}]}), encoding="utf-8")
    (cfgdir / "bench.json").write_text(json.dumps({"safety": {"off_after_crash": True}}), encoding="utf-8")
    from otterbench.instruments.scpi_generic import ScpiDriver
    sent = []
    real = ScpiDriver.set_output
    ScpiDriver.set_output = lambda self, on: (sent.append(on), real(self, on))[1]
    win = MainWindow([("auto", "SIM::E36312A")], Config(), None)
    try:
        assert _wait(qapp, lambda: sent == [False])
        assert "unexpectedly" in win.status.currentMessage() or sent == [False]
    finally:
        ScpiDriver.set_output = real
        win.close()
        _spin(qapp, 0.2)


def test_gui_production_run(qapp, cfgdir, tmp_path):
    from otterbench.config import Config
    from otterbench.gui.main_window import MainWindow
    win = MainWindow([("auto", "SIM::34461A")], Config(), None)
    try:
        p = win.panels()[0]
        assert _wait(qapp, lambda: p.connected)
        seq = win.sequencer()
        seq._set_plan(Plan(steps=[Step("Measure", p.display_name(), "value", "", "v",
                                       "3.0", "3.6")], off_at_end=False))
        win.prefs["production"] = {"report_dir": str(tmp_path / "rep"), "reports": True,
                                   "product": "Widget"}
        pw = win.show_production()
        pw.sn.setText("SN0001")
        pw.start()
        assert _wait(qapp, lambda: pw.banner.text().startswith(("PASS", "FAIL", "ERROR")), 15)
        assert pw.banner.text() == "PASS  SN0001"
        u = pw.db.history("Widget")[0]
        assert (u.serial, u.result) == ("SN0001", "PASS")
        assert u.report and os.path.exists(u.report)
        # a serial that doesn't match the format is refused before testing
        pw.pattern.setText(r"SN\d{4}")
        pw.sn.setText("XX12")
        pw.start()
        assert pw.banner.text() == "SERIAL?"
    finally:
        win.close()
        _spin(qapp, 0.2)
