"""Calibration log, checks against a reference, and the tool windows that run
on the simulators."""
import json
import os
import time
from datetime import date

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from otterbench import calibration as cal  # noqa: E402
from otterbench import report  # noqa: E402

IDN = "KEITHLEY INSTRUMENTS,MODEL DMM6500,04412345,1.7.0"


def test_recognising_an_instrument():
    assert cal.serial_of(IDN) == "04412345"
    assert cal.serial_of("HEWLETT-PACKARD,66321D,0,A.01") == ""
    assert cal.serial_of("B&K Precision 393B  [x]  serial 12AB34  fw 1.0") == "12AB34"
    assert cal.serial_of("Serial console on /dev/ttyUSB0, 115200 baud") == ""
    assert cal.key_of(IDN, "TCPIP::1") == cal.key_of(IDN.replace("1.7.0", "1.8"), "USB::2")
    assert cal.key_of("HP,66321D,0,A", "GPIB0::5::INSTR") == "GPIB0::5::INSTR"


def test_due_dates():
    e = cal.Entry(calibrated="2026-01-31", months=1)
    assert e.due() == date(2026, 2, 28)
    e = cal.Entry(calibrated="2024-02-29", months=12)
    assert e.due() == date(2025, 2, 28)
    e = cal.Entry(calibrated="2026-01-15", months=12)
    assert e.status(date(2026, 6, 1)) == cal.OK
    assert e.status(date(2026, 12, 20)) == cal.SOON
    assert e.status(date(2027, 1, 15)) == cal.SOON            # the last good day
    assert e.status(date(2027, 1, 16)) == cal.OVERDUE
    assert "OVERDUE" in e.describe(date(2027, 2, 1))
    assert cal.Entry().status() == cal.NONE
    assert cal.Entry(months=0).status() == cal.NOT_NEEDED
    assert cal.Entry(calibrated="soon").status() == cal.NONE


def test_log_round_trip_and_history(tmp_path):
    log = cal.Log(tmp_path / "c.json")
    e = log.get(IDN, "USB::1", "Ref meter")
    e.record("2025-01-10", "Lab A", "C-1")
    e.record("2026-01-12", "Lab B", "C-2")
    with pytest.raises(ValueError):
        e.record("tomorrow")
    log.save()
    log = cal.Log(tmp_path / "c.json")
    assert log.find(IDN, "TCPIP::moved").certificate == "C-2"
    assert log.entries[0].history[0]["certificate"] == "C-1"
    assert log.find("HP,66321D,0,A", "GPIB0::5") is None
    assert log.describe("x", "y") == "no calibration record"


def test_damaged_log_is_never_overwritten(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{oops")
    log = cal.Log(p)
    assert "damaged" in log.error and log.entries == []
    log.get(IDN, "USB::1")
    log.save()
    assert (tmp_path / "c.json.damaged").read_text() == "{oops"
    assert len(json.loads(p.read_text())["instruments"]) == 1


def test_judge():
    s = cal.CheckSettings("a: x", "b: x", percent=0.1, floor=0.001)
    assert cal.judge(10.0, 10.010, s)["result"] == "PASS"
    assert cal.judge(10.0, 10.012, s)["result"] == "FAIL"
    assert cal.judge(0.0, 0.0005, s)["result"] == "PASS"
    assert cal.judge(1.0, float("nan"), s)["result"] == "FAIL"
    assert cal.verdict([]) == ""
    for bad in (dict(dut=""), dict(ref="a: x"), dict(percent=0, floor=0),
                dict(source="s", levels="-1"), dict(source="s", levels="x")):
        assert cal.CheckSettings(**{**dict(dut="a: x", ref="b: x"), **bad}).problem(), bad


def test_reports_show_status():
    html = report.sequence_report("T", [], [], None, "", [("Meter", IDN, "CALIBRATION OVERDUE")],
                                  "Finished")
    assert "CALIBRATION OVERDUE" in html
    e = cal.Entry(key="k", name="Meter", calibrated="2020-01-01")
    assert "overdue" in report.calibration_report([e])


# ---- windows ------------------------------------------------------------------
def spin(cond, s=30.0):
    end = time.monotonic() + s
    while not cond() and time.monotonic() < end:
        _APP.processEvents()
        time.sleep(0.01)
    assert cond()


@pytest.fixture
def main(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from otterbench.config import Config
    from otterbench.gui.main_window import MainWindow
    made = []

    def make(*targets):
        w = MainWindow(list(targets), Config(), None)
        made.append(w)
        spin(lambda: len([p for p in w.panels() if p.connected]) == len(targets))
        return w
    yield make
    for w in made:
        w.close()


def done(w):
    spin(lambda: w.run_btn.isEnabled() and not w.runner.running() and w.status.text()
         not in ("", "Running…"))


def test_check_window(main, monkeypatch):
    from otterbench.gui import calibration_window as cw
    m = main(("auto", "SIM::34461A"), ("auto", "SIM::DMM6500"))
    w = m.show_calibration()
    names = w.dut.names()
    assert any("34461A" in n for n in names) and any("DMM6500" in n for n in names)
    warned = []
    monkeypatch.setattr(w, "warn", warned.append)
    w.run()
    assert "own reference" in warned[-1]
    w.dut.choose(next(n for n in names if "34461A" in n))
    w.ref.choose(next(n for n in names if "DMM6500" in n))
    w.average.setValue(0)
    w.settle.setValue(0)
    w.percent.setValue(100)
    w.floor.setValue(1000)
    w.run()
    done(w)
    assert w.status.text() == "Finished" and len(w.rows) == 1 and w.rows[0]["result"] == "PASS"
    w.keep()
    assert "Simulated" in warned[-1]
    # as if they were real
    monkeypatch.setattr(cw.cal, "simulated", lambda r: False)
    w.add_open()
    assert w.table.rowCount() == 2
    w.keep()
    assert any(e.checks for e in cal.Log().entries)
    p = m.panels()[0]
    assert "no calibration record" in p.calibration()
    report.check_report("x", w.rows, "PASS", w.settings(), w.run_insts)


def test_regulator_window(main, monkeypatch):
    m = main(("auto", "SIM::66321D"), ("auto", "SIM::DL3021"))
    w = m.show_regulator()
    assert w.source.name() == w.load.name() == ""      # nothing is chosen for you
    w.source.setCurrentIndex(1)
    w.load.setCurrentIndex(1)
    assert "66321D" in w.source.name() and "DL3021" in w.load.name()
    monkeypatch.setattr(w, "confirm", lambda t: True)
    warned = []
    monkeypatch.setattr(w, "warn", warned.append)
    w.vin.setText("6, 9")
    w.iload.setText("0, 0.5")
    w.vout_nominal.setValue(5)
    w.vout_max.setValue(5.5)
    w.settle.setValue(0.1)
    w.average.setValue(0)
    w.min_efficiency.setValue(1)
    w.run()
    assert not warned
    done(w)
    assert w.status.text() == "Finished", w.log_view.toPlainText()
    assert len(w.rows) == 4 and w.table.rowCount() == 4
    html = report.regulator_report("x", w.rows, reg_analyse(w), w.settings(), w.inst_list(), "ok")
    assert html.count("<svg") == 3
    m.save_bench_state()
    m.prefs.clear()
    m._load_bench_state()
    assert m.prefs["regulator"]["vin"] == "6, 9"


def reg_analyse(w):
    from otterbench import regulator
    return regulator.analyse(w.rows, w.run_with)


def test_battery_window_runs_and_esc_stops_it(main, monkeypatch):
    m = main(("auto", "SIM::PX100"))
    w = m.show_battery()
    assert w.load.name() == ""                    # nothing is chosen for you
    w.load.setCurrentIndex(1)
    assert w.load.tab() == m.panels()[0].display_name()
    monkeypatch.setattr(w, "confirm", lambda t: True)
    warned = []
    monkeypatch.setattr(w, "warn", warned.append)
    w.run()
    assert "charger" in warned[-1].lower()
    w.start_with.setCurrentIndex(1)
    w.interval.setValue(0.2)
    w.ir_delay.setValue(0.1)
    w.cutoff_v.setValue(0.5)
    w.run()
    spin(lambda: len(w.samples) > 3)
    from otterbench.gui import runner
    assert runner.busy() == "Battery tester"
    m.all_outputs_off()
    done(w)
    assert w.status.text() == "Stopped" and not runner.RUNNERS
    p = m.panels()[0]
    spin(lambda: p.last_reading.values["current"] < 0.01)
    report.battery_report("x", w.samples, w.cycles, w.settings(), w.inst_list(), "Stopped", "log")


def test_chemistry_preset(main):
    w = main(("auto", "SIM::PX100")).show_battery()
    w.cells.setValue(3)
    assert w.charge_v.value() == pytest.approx(12.6) and w.cutoff_v.value() == pytest.approx(9)


def test_session_keeps_the_status(tmp_path):
    from otterbench.session import SessionReader, recorder
    p = str(tmp_path / "s.otter")
    recorder.start(p)
    recorder.stream("k", "Meter", "DMM", "USB::1", ["v"], lambda: "in calibration (x)")
    recorder.add("k", 1.0, [2.0])
    recorder.add("k", 2.0, [2.5])
    recorder.stop()
    s = SessionReader(p)
    assert s.meta["cal:1"] == "in calibration (x)"
    s.close()
    assert "in calibration (x)" in report.session_report(p)
