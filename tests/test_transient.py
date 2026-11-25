"""Load transient test: the analysis on made-up traces, the run on simulators."""
import math
import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from otterbench import report  # noqa: E402
from otterbench import transient as tr  # noqa: E402

T = np.linspace(-500e-6, 500e-6, 2001)


def dip(up=True, depth=0.1, tau=50e-6, shift=0.01, noise=1e-3, base=3.3, late=2e-6):
    s = -1 if up else 1
    v = np.full(T.size, base)
    on = T >= -late                       # the step starts a little before the trigger
    v[on] += s * (shift + (depth - shift) * np.exp(-(T[on] + late) / tau))
    return v + np.random.default_rng(3).normal(0, noise, T.size)


@pytest.mark.parametrize("up", [True, False])
def test_analysis(up):
    s = tr.analyse(T, dip(up), up, 0.02)
    assert s.deviation == pytest.approx(0.1, abs=0.006)
    assert s.before == pytest.approx(3.3, abs=1e-3)
    assert s.shift == pytest.approx(-0.01 if up else 0.01, abs=1e-3)
    assert s.recovery == pytest.approx(50e-6 * math.log(0.09 / 0.02), abs=8e-6)
    assert s.rebound < 0.006 and s.note == ""


def test_ac_coupled_is_the_same():
    a, b = tr.analyse(T, dip(), True, 0.02), tr.analyse(T, dip() - 3.3, True, 0.02)
    assert a.deviation == pytest.approx(b.deviation) and a.recovery == pytest.approx(b.recovery)


def test_rebound():
    v = np.full(T.size, 1.2)
    on = T >= 0
    v[on] -= 0.1 * np.exp(-T[on] / 60e-6) * np.sin(2 * np.pi * T[on] / 200e-6)
    s = tr.analyse(T, v, True, 0.005)
    peak = 0.1 * max(np.exp(-x / 60e-6) * -math.sin(2 * math.pi * x / 200e-6)
                     for x in np.linspace(100e-6, 200e-6, 200))
    assert s.rebound == pytest.approx(peak, rel=0.02)
    assert 0 < s.recovery < 300e-6


def test_what_cant_be_measured():
    assert "noise" in tr.analyse(T, np.random.default_rng(1).normal(3.3, 1e-3, T.size), True,
                                 0.02).note
    s = tr.analyse(T, dip(noise=0.01), True, 0.02)
    assert "noisier" in s.note and math.isnan(s.recovery) and s.deviation > 0.08
    s = tr.analyse(T, dip(tau=2e-3), True, 0.02)
    assert math.isnan(s.recovery) and "settled" in s.note
    assert "cut off" in tr.analyse(T, np.maximum(dip(tau=100e-6), 3.26), True, 0.02).note
    assert tr.analyse(T + 1, dip(), True, 0.02).note
    assert tr.analyse(T[:10], dip()[:10], True, 0.02).note
    assert tr.analyse(T, dip(depth=0.015), True, 0.02).recovery == 0.0


def test_settings():
    ok = dict(load="L", scope="S")
    assert tr.TransSettings(**ok).problem() == ""
    for bad in (dict(load=""), dict(scope=""), dict(trig_ch=1), dict(i_low=2), dict(i_low=-1),
                dict(vout_max=3), dict(window=0), dict(repeats=0), dict(band=0),
                dict(trig_mv=0), dict(source="X", vin=0), dict(vout_nominal=0)):
        assert tr.TransSettings(**{**ok, **bad}).problem(), bad


def test_verdict():
    steps = [tr.analyse(T, dip(u), u, 0.02) for u in (True, False)]
    s = tr.TransSettings(max_deviation=4, max_recovery=200e-6)
    assert tr.summary(steps, s)["verdict"] == "PASS"
    s.max_deviation = 2
    a = tr.summary(steps, s)
    assert a["verdict"] == "FAIL" and sum(not ok for _, ok in a["checks"]) == 2
    steps[0].recovery = math.nan                  # not measured isn't passed
    s.max_deviation = 0
    assert tr.summary(steps, s)["verdict"] == "FAIL"
    assert tr.summary(steps, tr.TransSettings())["verdict"] == ""
    assert tr.summary([], s)["verdict"] == ""
    html = report.transient_report("t", steps, a, {}, [], "Finished")
    assert "Load step down" in html and "FAIL" in html


def until(cond, s=40.0):
    end = time.monotonic() + s
    while time.monotonic() < end and not cond():
        _APP.processEvents()
        time.sleep(0.005)
    return cond()


@pytest.fixture
def app(monkeypatch):
    from otterbench.config import Config
    from otterbench.gui.main_window import MainWindow
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    m = MainWindow([("auto", "SIM::SCOPE"), ("auto", "SIM::DL3021"), ("auto", "SIM::DP832")],
                   Config(), None)
    assert until(lambda: len(m.panels()) == 3 and all(p.connected for p in m.panels()))
    yield m
    m._confirm_quit = lambda: True
    m.close()


def prepare(m):
    sc, load, src = m.panels()
    w = m.show_transient()
    for box in (w.load, w.source, w.scope):
        box.refresh()
    w.load.choose(load.display_name())
    w.source.setCurrentIndex(1)
    w.scope.choose(sc.display_name())
    w.ch.setCurrentIndex(3)
    w.vout_nominal.setValue(5.0)
    w.vout_max.setValue(5.5)
    w.band.setValue(0.2)
    w.repeats.setValue(1)
    w.settle.setValue(0.1)
    sc.cmd(lambda d: d.set_channel(4, on=True, scale=0.05, offset=-1.2))
    return sc, load, src, w


def off(m, w):
    return w.load.currentText() and not any(
        x.worker.drv.t.values.get("input") or x.worker.drv.t.values.get("out1")
        for x in m.panels()[1:])


def test_run_on_simulators(app):
    sc, load, src, w = prepare(app)
    w.max_deviation.setValue(5)
    before = sc.state.tscale
    w.run()
    assert until(lambda: not w.runner.running() and w.run_btn.isEnabled())
    assert w.outcome == "Finished", w.log_view.toPlainText()
    assert [s.up for s in w.steps] == [True, False] and not w.steps[0].note
    assert w.steps[0].deviation == pytest.approx(0.9 * 0.04, rel=0.3)
    assert "PASS" in w.result.text() and w.table.rowCount() == 2
    assert off(app, w)
    assert until(lambda: sc.state.tscale == before)
    s = w.settings()
    w.restore(s)
    assert w.settings() == s


def test_no_trigger_switches_everything_off(app):
    sc, load, src, w = prepare(app)
    w.ch.setCurrentIndex(0)                       # its square wave: triggers, shows no step
    w.trig_ch.setCurrentIndex(4)                  # CH4 …
    w.trig_level.setValue(1.2)                    # … goes the other way than it waits for
    w.run()
    assert until(lambda: not w.runner.running() and w.run_btn.isEnabled())
    assert w.outcome.startswith("Failed: The scope didn't trigger"), w.outcome
    assert off(app, w)


def test_overvoltage_and_stop(app):
    sc, load, src, w = prepare(app)
    w.vout_nominal.setValue(3.3)
    w.vout_max.setValue(3.6)
    w.run()
    assert until(lambda: not w.runner.running() and w.run_btn.isEnabled())
    assert "above 3.6" in w.outcome and off(app, w)
    w.vout_nominal.setValue(5.0)
    w.vout_max.setValue(5.5)
    w.run()
    until(lambda: False, 1.5)
    w.runner.stop()
    assert until(lambda: not w.runner.running() and w.run_btn.isEnabled())
    assert w.outcome == "Stopped" and off(app, w)
