"""Frequency response: sine fit, margins, the sweep on simulated instruments."""
import math
import os
import threading
import time

import numpy as np
import pytest

from ee_workbench import bode
from ee_workbench.clock import now
from ee_workbench.instruments import make_driver
from ee_workbench.sequencer import Bench, Inst, SequenceError, Stopped
from ee_workbench.sim import open_sim, shared


def P(f, g, ph):
    return bode.BodePoint(f, g, ph, 1, 1, 0)


def test_sine_fit_with_dc_noise_and_harmonic():
    t = np.linspace(0, 4.3e-3, 1000)
    rng = np.random.default_rng(1)
    v = 1.5 + 0.7 * np.cos(2 * np.pi * 1e3 * t + 0.4) + rng.normal(0, 0.02, t.size)
    f = bode.sine_fit(t, v, 1e3)
    assert f.amplitude == pytest.approx(0.7, abs=0.004)
    assert f.phase == pytest.approx(0.4, abs=0.01) and f.offset == pytest.approx(1.5, abs=0.003)
    assert f.residual == pytest.approx(0.02, rel=0.1)
    with pytest.raises(ValueError):
        bode.sine_fit(t[:4], v[:4], 1e3)


def test_point_gain_phase_with_different_sampling():
    a, b = np.linspace(0, 5e-3, 1000), np.linspace(1e-4, 4e-3, 777)
    p = bode.point_from(a, np.sin(2e3 * np.pi * a), b, 0.1 * np.sin(2e3 * np.pi * b - 2.0), 1e3)
    assert p.gain_db == pytest.approx(-20, abs=1e-6)
    assert p.phase == pytest.approx(math.degrees(-2.0), abs=1e-6)


def test_steps_and_wrap():
    assert [bode.step125(x) for x in (0.9, 1.0, 1.1, 3, 6)] == \
        pytest.approx([1, 1, 2, 5, 10])
    assert bode.step125(4e-5) == pytest.approx(5e-5)
    assert bode.wrap(190) == -170 and bode.wrap(-180) == 180.0 and bode.wrap(0) == 0


def test_analysis_of_a_loop_gain():
    f = np.logspace(1, 6, 201)
    h = 1000 / (1 + 1j * f / 100) / (1 + 1j * f / 50e3) / (1 + 1j * f / 400e3)
    pts = [P(a, 20 * math.log10(abs(x)), math.degrees(np.angle(x))) for a, x in zip(f, h)]
    a = bode.analyse(pts)
    assert a["f_high"] == pytest.approx(100, rel=0.02) and "f_low" not in a
    fc = a["f_unity"]
    hc = 1000 / (1 + 1j * fc / 100) / (1 + 1j * fc / 50e3) / (1 + 1j * fc / 400e3)
    assert abs(hc) == pytest.approx(1, rel=0.01)
    assert a["phase_margin"] == pytest.approx(180 + math.degrees(np.angle(hc)), abs=0.5)
    assert a["gain_margin"] > 0 and "phase margin" in bode.describe(a)
    assert bode.analyse([]) == {} and bode.describe({}) == ""


def test_band_pass():
    f = np.logspace(2, 4, 101)
    h = 1 / (1 + 1j * 5 * (f / 1e3 - 1e3 / f))
    a = bode.analyse([P(x, 20 * math.log10(abs(y)), math.degrees(np.angle(y)))
                      for x, y in zip(f, h)])
    assert a["peak_f"] == pytest.approx(1000, rel=0.03)
    assert a["bandwidth"] == pytest.approx(200, rel=0.05)


def test_reference_takes_out_the_probes():
    ref = [P(f, -1.0, -f / 1000) for f in (100, 1000, 10000)]
    out = bode.normalise([P(100, -4, -10), P(10000, -7, -170), P(50000, 0, 0)], ref)
    assert [round(p.gain_db, 6) for p in out] == [-3, -6]
    assert out[0].phase == pytest.approx(-9.9) and out[1].phase == pytest.approx(-160)


def test_bad_settings():
    ok = dict(fgen="a", scope="b")
    assert bode.BodeSettings(**ok).problem() == ""
    for bad in (dict(f_start=0), dict(f_stop=1), dict(ch_out=1), dict(amplitude=0),
                dict(per_decade=0), dict(cycles=0), dict(fgen=""), dict(scope="")):
        assert bode.BodeSettings(**{**ok, **bad}).problem()
    assert len(bode.BodeSettings(f_start=10, f_stop=1000, per_decade=5).frequencies()) == 11


# ---- the sweep ---------------------------------------------------------------
class DrvInst(Inst):
    setting_kinds: dict = {}
    set_names = measure_names = []

    def __init__(self, name, drv):
        self.name, self.kind, self.d, self.stop = name, drv.kind, drv, threading.Event()

    def run(self, fn, timeout=30.0):
        return fn(self.d)



def scope_sims():
    return ["SIM::SCOPE", "SIM::DSOX", "SIM::TBS", "SIM::SDS"]


@pytest.mark.parametrize("scope", scope_sims())
def test_sweep_matches_the_filter(scope):
    g, s = open_sim("SIM::FGEN"), open_sim(scope)
    gd, sd = make_driver("FGEN", g, "SIM::FGEN"), make_driver("auto", s, scope)
    gd.identify(), sd.identify()
    b = Bench([DrvInst("Gen", gd), DrvInst("Scope", sd)], now, log=lambda t: None)
    sweep = bode.Sweep(bode.BodeSettings("Gen", 1, "Scope", f_start=1e3, f_stop=1e5,
                                         per_decade=2, settle=0, amplitude=2, offset=0.5))
    got = []
    sweep.run(b, got.append)
    assert len(got) == 5
    for p in got:
        h = shared.filter_response(p.f)
        assert p.note == "", p
        assert p.gain_db == pytest.approx(20 * math.log10(abs(h)), abs=0.3), p
        assert bode.wrap(p.phase - math.degrees(np.angle(h))) == pytest.approx(0, abs=2.5), p
        assert p.vin == pytest.approx(1.0, rel=0.02)
    assert g.ch[1].out is False                       # off again at the end


def test_stop_switches_the_generator_off():
    g, s = open_sim("SIM::FGEN"), open_sim("SIM::SCOPE")
    gd, sd = make_driver("FGEN", g, "SIM::FGEN"), make_driver("auto", s, "SIM::SCOPE")
    gd.identify(), sd.identify()
    stop = threading.Event()
    b = Bench([DrvInst("Gen", gd), DrvInst("Scope", sd)], now, log=lambda t: None, stop=stop)
    with pytest.raises(Stopped):
        bode.Sweep(bode.BodeSettings("Gen", 1, "Scope", settle=0)).run(b, lambda p: stop.set())
    assert g.ch[1].out is False and b.stop_event is stop
    with pytest.raises(SequenceError, match="isn't an osc"):
        bode.Sweep(bode.BodeSettings("Gen", 1, "Gen")).run(
            Bench([DrvInst("Gen", gd)], now, log=lambda t: None))


# ---- the window ----------------------------------------------------------------
def test_window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    global _APP
    _APP = QApplication.instance() or QApplication([])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from ee_workbench.config import Config
    from ee_workbench.gui.main_window import MainWindow
    w = MainWindow([("FGEN", "SIM::FGEN"), ("auto", "SIM::SCOPE")], Config(), None)
    try:
        def spin(cond, s=25):
            end = time.monotonic() + s
            while not cond() and time.monotonic() < end:
                _APP.processEvents()
                time.sleep(0.01)
            assert cond()
        spin(lambda: len([p for p in w.panels() if p.connected]) == 2)
        b = w.show_bode()
        b.f_start.setValue(1e3)
        b.f_stop.setValue(1e4)
        b.per_decade.setValue(2)
        b.settle.setValue(0)
        warned = []
        monkeypatch.setattr(b, "warn", warned.append)
        b.run()
        assert not warned
        spin(lambda: b.status.text() not in ("", "Running…") and not b.runner.running()
             and b.run_btn.isEnabled())
        assert b.status.text() == "Finished", b.log_view.toPlainText()
        assert len(b.points) == 3
        b.use_reference()
        assert max(abs(p.gain_db) for p in b.shown()) < 1e-9
        b.clear_reference()
        from ee_workbench import report
        f, g, ph = bode.arrays(b.points)
        html = report.bode_report("x", f, g, ph, "s", b.settings(), b.inst_list())
        assert "33522B" in html and "<svg" in html
        # settings come back
        w.save_bench_state()
        w.prefs.clear()
        w._load_bench_state()
        assert w.prefs["bode"]["f_stop"] == 1e4
    finally:
        w.close()


_APP = None
