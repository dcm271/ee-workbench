"""Accuracy specifications: parsing, the ± on readings, the sequencer's guard."""
import json
import math
import os
import time
import tomllib

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from otterbench import accuracy as acc  # noqa: E402
from otterbench import calibration as cal  # noqa: E402
from otterbench.instruments import scpi_generic as sg  # noqa: E402
from otterbench.sequencer import Bench, Inst, Plan, Step, run_plan  # noqa: E402


def test_parse():
    assert acc.parse("0.0035% + 50u").of(10) == pytest.approx(350e-6 + 50e-6)
    assert acc.parse("±0.0035 % + 50 u").of(-10) == pytest.approx(400e-6)
    assert acc.parse("100ppm + 2m").of(5) == pytest.approx(500e-6 + 2e-3)
    assert acc.parse("1% of reading").of(2) == pytest.approx(0.02)
    assert acc.parse("2m").of(5) == 2e-3 and acc.parse("3k").of(1) == 3000
    assert acc.parse("5e-3").of(1) == 5e-3 and acc.parse("0.5µ + 0.1%").of(0) == 5e-7
    s = acc.parse("100m: 0.005% + 3.5u; 1: 0.004% + 7u; 10: 0.0035% + 50u")
    assert s.of(0.05) == pytest.approx(6e-6) and s.of(0.5) == pytest.approx(27e-6)
    assert s.of(-5) == pytest.approx(225e-6)
    assert s.of(11) is not None and s.of(13) is None        # over range
    assert acc.parse("1: 1m; 5m").of(500) == 5e-3
    for v in (None, math.nan, math.inf):
        assert s.of(v) is None


@pytest.mark.parametrize("bad", ["", ";", "x", "0.1% 5u", "0.1%+", "-1%", "1: 1m; 1: 2m",
                                 "10: 1m; 1: 2m", "1m; 10: 2m", "a: 1m", "1:", "1 foo", "nan"])
def test_bad_specs(bad):
    with pytest.raises(acc.AccuracyError):
        acc.parse(bad)


def test_lines_and_pick():
    d = acc.parse_lines("DC V = 0.1% + 1m\n\n current = 1: 1m; 2m\n")
    assert list(d) == ["DC V", "current"] and acc.parse_lines(acc.lines(d)) == d
    for bad in ("DC V 1%", "= 1%", "DC V = what"):
        with pytest.raises(acc.AccuracyError):
            acc.parse_lines(bad)
    assert acc.pick(d, ["dc v", "value"]).of(1) == pytest.approx(2e-3)
    assert acc.pick(d, ["AC V", "value"]) is None and acc.pick({}, ["x"]) is None
    assert acc.pick({"x": "rubbish"}, ["x"]) is None


def test_marginal():
    assert acc.marginal(3.39, 3.2, 3.4, 0.02) and acc.marginal(3.21, 3.2, 3.4, 0.02)
    assert not acc.marginal(3.3, 3.2, 3.4, 0.02) and not acc.marginal(3.39, 3.2, 3.4, None)
    assert not acc.marginal(3.39, 3.2, None, 0.02) and not acc.marginal(math.nan, 0, 1, 1)


def test_definition_files(tmp_path):
    text = ('id="T1"\nname="T"\n[[reading]]\nname="value"\nquery="READ?"\nunit_from="f"\n'
            'accuracy="%s"\n[[setting]]\nname="f"\nkind="choice"\noptions=[{label="DC V",'
            'set="X",accuracy="%s"}]\n')
    p = tmp_path / "t.toml"
    p.write_text(text % ("1% + 1m", "0.1%"))
    d = sg.parse_definition(tomllib.loads(p.read_text()), str(p))
    assert d.readings[0].accuracy == "1% + 1m"
    assert d.settings[0].options[0].accuracy == "0.1%"
    for bad in (("1 parsec", "0.1%"), ("1%", "0.1 parsec")):
        p.write_text(text % bad)
        with pytest.raises(sg.DefinitionError, match="accuracy"):
            sg.parse_definition(tomllib.loads(p.read_text()), str(p))


def test_entries_keep_it(tmp_path):
    log = cal.Log(tmp_path / "c.json")
    e = log.get("A,B,SN1,1", "R", "meter")
    e.accuracy = {"DC V": "0.1%"}
    log.save()
    assert cal.Log(tmp_path / "c.json").entries[0].accuracy == {"DC V": "0.1%"}
    d = json.loads((tmp_path / "c.json").read_text())
    d["instruments"][0]["accuracy"] = "nonsense"
    (tmp_path / "c.json").write_text(json.dumps(d))
    assert cal.Log(tmp_path / "c.json").entries[0].accuracy == {}


# ---- the guard
class Meter(Inst):
    name, kind, measure_names = "Meter", "scpi", ["value"]
    value = 3.39

    def __init__(self):
        self.t = 0.0

    def uncertainty(self, what, value, r=None):
        assert r is not None                     # the reading it is about
        return 0.02

    def reading_after(self, t, timeout):
        self.t = t + 1
        from types import SimpleNamespace as NS
        return NS(t=self.t, values={"value": self.value})


def go(value, guard, **kw):
    m = Meter()
    m.value = value
    b = Bench([m], lambda: m.t)
    b.guard = guard
    run_plan(Plan([Step("Measure", "Meter", "value", "", "V", "3.2", "3.4")], guard=guard, **kw),
             b)
    return b.rows[0]


@pytest.mark.parametrize("value,guard,result,near", [
    (3.39, "off", "PASS", None), (3.39, "marginal", "PASS", "V"), (3.39, "strict", "FAIL", None),
    (3.3, "marginal", "PASS", ""), (3.3, "strict", "PASS", None), (3.41, "off", "FAIL", None),
    (3.41, "marginal", "FAIL", ""), (3.41, "strict", "FAIL", None)])
def test_guard(value, guard, result, near):
    row = go(value, guard)
    assert (row["result"], row.get("marginal")) == (result, near)
    assert row["V ±"] == 0.02
    if guard == "strict" and value == 3.39:
        assert "V=3.39" in row["failures"]


def test_guard_is_kept_and_checked():
    assert Plan.from_dict(Plan(guard="strict").to_dict()).guard == "strict"
    assert Plan.from_dict({"guard": "maybe"}).guard == "off" and Plan.from_dict({}).guard == "off"


def test_the_pm_column_isnt_a_name():
    m = Meter()
    b = Bench([m], lambda: m.t)
    run_plan(Plan([Step("Measure", "Meter", "value", "", "Vout"),
                   Step("If", "", "", "vout > 3"), Step("Note", "", "", "yes"), Step("End")]), b)
    assert b.rows[0]["Vout"] == 3.39 and b.rows[0]["Vout ±"] == 0.02


def test_retry_forgets_the_marginal_try():
    m = Meter()
    vals = iter([3.5, 3.39, 3.3])
    m.reading_after = lambda t, timeout, f=m.reading_after: (
        setattr(m, "value", next(vals)), f(t, timeout))[1]
    b = Bench([m], lambda: m.t)
    b.guard = "marginal"
    run_plan(Plan([Step("Retry", "", "", "3"),
                   Step("Measure", "Meter", "value", "", "V", "3.2", "3.4"), Step("End")]), b)
    assert b.rows[0]["marginal"] == "V" and b.rows[0]["result"] == "PASS"


# ---- in the app
def until(cond, s=15.0):
    end = time.monotonic() + s
    while time.monotonic() < end and not cond():
        _APP.processEvents()
        time.sleep(0.005)
    return cond()


def test_tabs_and_sequence():
    from otterbench.config import Config, config_dir
    from otterbench.gui.calibration_window import EntryDialog
    from otterbench.gui.main_window import MainWindow
    m = MainWindow([("auto", "SIM::34461A"), ("auto", "SIM::3456A"), ("auto", "SIM::66321D")],
                   Config(), None)
    path = config_dir() / "calibration.json"
    try:
        assert until(lambda: len(m.panels()) == 3 and all(
            p.connected and p.last_reading is not None for p in m.panels()))
        dmm, hp, psu = m.panels()
        assert all(p.uncertainty("value", 1.0) is None for p in (dmm, hp))
        assert dmm._readouts["value"].pm is None
        log = cal.Log()
        e = log.get(dmm.identity(), dmm.resource, "dmm")
        d = EntryDialog(None, e, False)
        d.acc.setPlainText("DC V = 1 parsec")
        d._ok()
        assert "parsec" in d.err.text() and not d.result()
        d.acc.setPlainText("DC V = 1% + 1m\nvalue = 50%")
        d._ok()
        assert d.result()
        e.accuracy = d.specs
        log.get(hp.identity(), hp.resource, "hp").accuracy = {hp.cfg.function: "2m"}
        log.get(psu.identity(), psu.resource, "psu").accuracy = {"voltage": "5m"}
        log.save()
        assert dmm.uncertainty("value", 3.0) == pytest.approx(0.031)
        assert psu.uncertainty("voltage", 3.0) == 5e-3 and psu.uncertainty("current", 1) is None
        assert until(lambda: dmm._readouts["value"].pm is not None
                     and dmm._readouts["value"].pm.text().startswith("±"))
        assert until(lambda: hp.readout.pm is not None and "±" in hp.readout.pm.text())
        w = m.sequencer()
        name = dmm.display_name()
        w._set_plan(Plan([Step("Measure", name, "value", "", "V", "3.29", ""),
                          Step("Set", name, "function", "2W ohm"),
                          Step("Measure", name, "value", "", "R")], guard="marginal"))
        assert w.plan().guard == "marginal"
        w.run()
        assert until(lambda: not w.running() and w.run_btn.isEnabled())
        assert w.outcome == "Finished", w.logbox.toPlainText()
        row = w.rows[0]
        assert row["marginal"] == "V" and row["V ±"] == pytest.approx(0.034, rel=0.01)
        assert row["R ±"] == pytest.approx(500, rel=0.01)        # by the reading's name
        from otterbench import report
        html = report.sequence_report("t", w.rows, w.columns, w.plan().to_dict(), "", [], "ok")
        assert "marked" in html
    finally:
        path.unlink(missing_ok=True)
        m._confirm_quit = lambda: True
        m.close()


def test_it_is_quick_and_notices_changes(monkeypatch):
    """Asked for with every reading: must not read the disk each time."""
    from otterbench.config import config_dir
    path = config_dir() / "calibration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        reads = []
        real = acc._read
        monkeypatch.setattr(acc, "_read", lambda *a: (reads.append(a), real(*a))[1])
        acc.forget()
        for _ in range(1000):
            assert acc.from_log("A,B,SN9,1", "R") == {}
        assert len(reads) <= 2
        log = cal.Log()
        log.get("A,B,SN9,1", "R").accuracy = {"value": "1%"}
        log.save()                                 # seen at once
        assert acc.from_log("A,B,SN9,1", "R") == {"value": "1%"}
        path.unlink()                              # by another program: within CHECK_S
        monkeypatch.setattr(acc, "CHECK_S", 0.0)
        assert acc.from_log("A,B,SN9,1", "R") == {}
    finally:
        path.unlink(missing_ok=True)
        acc.forget()
