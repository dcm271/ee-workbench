"""Generic SCPI definitions, oscilloscopes, and several units of one model."""
import os
import time

import numpy as np
import pytest

from otterbench.instruments import (all_models, make_driver, model_for, resolve_model,
                                      scopes)
from otterbench.instruments import scpi_generic as g
from otterbench.sequencer import Bench, Plan, Step, run_plan
from otterbench.sim import open_sim
from otterbench.signals import name_matches


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    g.definitions(reload=True)
    yield tmp_path / "otterbench"
    g.definitions(reload=True)


def _drv(res):
    t = open_sim(res)
    d = make_driver(model_for(res), t, res)
    d.identify()
    return d


# ---------------------------------------------------------------- definitions
def test_every_builtin_definition_loads():
    defs = g.definitions(reload=True)
    assert not g.PROBLEMS
    assert {"34461A", "DP832", "DL3021", "SPD3303X", "SCPI-DMM"} <= set(defs)
    for d in defs.values():
        assert d.type in g.TYPES and d.readings


@pytest.mark.parametrize("did", sorted(g.definitions(reload=True)))
def test_definition_round_trips_on_its_simulator(did):
    d = _drv(f"SIM::{did}")
    r = d.poll(0.0)
    assert set(r.values) == {x.name for x in d.d.readings}
    for s in d.d.settings:
        if s.kind == "choice":
            for o in s.options:
                d.set(s.name, o.label)
                assert d.values[s.name] == o.label
        elif s.kind == "bool":
            d.set(s.name, True)
            assert d.values[s.name] is True
        else:
            v = s.values[0] if s.values else (s.min or 0) + 0.5
            d.set(s.name, v)
            assert d.values[s.name] == pytest.approx(v)
    d.poll(0.0)
    assert d.errors() == []
    d.set_output(False)
    assert not any(d.values[s.name] for s in d.d.settings if s.output)


def test_multichannel_expansion_and_values():
    d = _drv("SIM::DP832")
    assert [s.name for s in d.d.settings if s.output] == ["out1", "out2", "out3"]
    d.set("vset2", 5.0)
    d.set("out2", True)
    r = d.poll(0.0)
    assert r.values["voltage2"] == pytest.approx(5.0, abs=0.01)
    assert r.values["voltage1"] == pytest.approx(0.0, abs=0.01)


def test_meter_unit_follows_function():
    d = _drv("SIM::34461A")
    d.set("function", "DC I")
    r = d.poll(0.0)
    assert r.units["value"] == "A" and r.values["value"] == pytest.approx(0.015, rel=1e-3)


def test_query_from_uses_the_options_read_command():
    d = _drv("SIM::DM3058")
    d.set("function", "2W ohm")
    assert d.poll(0.0).values["value"] == pytest.approx(220, rel=1e-3)


def test_out_of_range_setting_is_refused():
    d = _drv("SIM::DL3021")
    with pytest.raises(ValueError):
        d.set("cc", 1000)
    with pytest.raises(ValueError):
        d.set("function", "XX")
    with pytest.raises(ValueError):
        d.set("nope", 1)


def test_bad_definitions_are_explained():
    with pytest.raises(g.DefinitionError, match="id"):
        g.parse_definition({"reading": [{"name": "v", "query": "X?"}]})
    with pytest.raises(g.DefinitionError, match="reading"):
        g.parse_definition({"id": "X"})
    with pytest.raises(g.DefinitionError, match="unknown key"):
        g.parse_definition({"id": "X", "reading": [{"name": "v", "query": "X?", "uint": "V"}]})
    with pytest.raises(g.DefinitionError, match="regex"):
        g.parse_definition({"id": "X", "reading": [{"name": "v", "query": "X?"}],
                            "setting": [{"name": "f", "kind": "choice", "options": [
                                {"label": "a", "set": "A", "reply": "("}]}]})


def test_user_definition_overrides_and_bad_file_is_reported(cfgdir):
    folder = cfgdir / "instruments"
    folder.mkdir(parents=True)
    (folder / "mine.toml").write_text(
        'id = "MYMETER"\nname = "My meter"\ntype = "meter"\nidn = ["MYM-1"]\n'
        '[[reading]]\nname = "value"\nquery = "MEAS?"\nunit = "V"\nsim = "2.5"\n', encoding="utf-8")
    (folder / "broken.toml").write_text("id = \n", encoding="utf-8")
    defs = g.definitions(reload=True)
    assert "MYMETER" in defs and not defs["MYMETER"].builtin
    assert any("broken.toml" in p for p in g.PROBLEMS)
    assert resolve_model("mymeter") == "SCPI:MYMETER"
    assert "SCPI:MYMETER" in all_models()
    assert g.match_idn("ACME,MYM-1,123,1.0").id == "MYMETER"
    assert _drv("SIM::MYMETER").poll(0).values["value"] == 2.5


def test_number_parsing():
    assert g.parse_number("+1.234E-03\n") == pytest.approx(1.234e-3)
    assert g.parse_number("CH1:VOLT 3.30") == pytest.approx(1.0)  # first number wins
    assert np.isnan(g.parse_number("9.9E+37"))
    assert g.parse_bool("ON") and not g.parse_bool("0")


# ---------------------------------------------------------------- scopes
@pytest.mark.parametrize("res", ["SIM::SCOPE", "SIM::DSOX", "SIM::TBS", "SIM::SDS"])
def test_scope_dialects_decode_waveforms(res):
    d = _drv(res)
    assert d.caps.nch == 4
    d.arm()
    end = time.monotonic() + 5
    while not d.capture_done() and time.monotonic() < end:
        time.sleep(0.05)
    cap = d.fetch()
    assert sorted(cap.analog) == [1, 2]
    s1 = scopes.trace_stats(cap.analog[1])
    assert s1["vpp"] == pytest.approx(3.3, abs=0.15)
    assert s1["vavg"] == pytest.approx(1.65, abs=0.1)
    assert d.measure("vpp2") == pytest.approx(2.0, abs=0.15)
    d.set("ch3_on", "on")
    d.set("ch3_scale", 0.2)
    d.set("timebase", 5e-4)
    d.set("trigger_slope", "falling")
    d.set("trigger_level", 1.0)
    st = d.state
    assert st.channels[2].on and st.channels[2].scale == pytest.approx(0.2)
    assert st.tscale == pytest.approx(5e-4) and st.trig_slope == "falling"
    assert d.errors() == []


def test_scope_model_names():
    assert resolve_model("DS1054Z") == "SCOPE:RIGOL"
    assert resolve_model("MSO4") == "SCOPE:TEK"
    assert scopes.detect_idn("KEYSIGHT TECHNOLOGIES,DSOX1204G,X,1") == "SCOPE:KEYSIGHT"
    assert scopes.detect_idn("Siglent Technologies,SDS1104X-E,X,1") == "SCOPE:SIGLENT"
    assert model_for("SIM::TBS") == "SCOPE:TEK"


# ---------------------------------------------------------------- several of one model
def test_name_matching_for_several_units():
    assert name_matches("PPK2", "PPK2 (SIM::PPK2)")
    assert not name_matches("PPK2", "PPK2 #2 (SIM::PPK2#2)")
    assert name_matches("PPK2 #2", "PPK2 #2 (SIM::PPK2#2)")
    assert model_for("SIM::PPK2#2") == "PPK2"
    assert model_for("SIM::DP832#3") == "SCPI:DP832"


# ---------------------------------------------------------------- sequencer
class FakeInst:
    def __init__(self, name, drv, kind):
        self.name, self.d, self.kind = name, drv, kind
        if kind == "scpi":
            self.set_names = [s.name for s in drv.d.settings]
            self.setting_kinds = {s.name: s.kind for s in drv.d.settings}
            self.measure_names = [r.name for r in drv.d.readings]
            self.profile_names = []
        else:
            self.set_names = ["acquire", "timebase", "trigger_level"]
            self.setting_kinds = {"acquire": "choice"}
            self.measure_names = ["vpp1", "freq1"]
            self.profile_names = []

    def run(self, fn, timeout=30.0):
        return fn(self.d)

    def reading_after(self, t, timeout):
        return self.d.poll(t + 1)

    def outputs_off(self):
        self.d.set_output(False)


def test_sequence_drives_scpi_load_and_scope():
    load = FakeInst("DL3021 · load", _drv("SIM::DL3021"), "scpi")
    scope = FakeInst("Scope", _drv("SIM::SCOPE"), "scope")
    rows = []
    bench = Bench([load, scope], clock=time.monotonic, log=lambda *_: None,
                  on_row=rows.append)
    plan = Plan(steps=[Step("Set", "DL3021", "function", "CC"),
                       Step("Set", "DL3021", "cc", "0.5"),
                       Step("Set", "DL3021", "input", "on"),
                       Step("Measure", "DL3021", "current", "", "i", "0.45", "0.55"),
                       Step("Measure", "Scope", "vpp1", "", "vpp", "3", "3.6")])
    run_plan(plan, bench)
    assert rows[0]["result"] == "PASS"
    assert rows[0]["i"] == pytest.approx(0.5, abs=0.01)
    bench.outputs_off()
    assert load.d.values["input"] is False


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


def test_gui_scpi_scope_and_two_units(qapp, cfgdir):
    from otterbench.config import Config
    from otterbench.gui.main_window import MainWindow
    win = MainWindow([("auto", "SIM::DP832"), ("auto", "SIM::DP832#2"),
                      ("auto", "SIM::34461A"), ("auto", "SIM::SCOPE")], Config(), None)
    try:
        assert _wait(qapp, lambda: all(p.connected for p in win.panels()))
        sup1, sup2, dmm, scope = win.panels()
        assert (sup1.KIND, dmm.KIND, scope.KIND) == ("scpi", "scpi", "scope")
        assert "#2" in sup2.display_name() and "#" not in sup1.display_name()
        assert _wait(qapp, lambda: dmm.last_reading is not None)
        assert "voltage1" in sup1.quantities() and "out3" in sup1.set_names()
        assert "vpp1" in scope.measure_names()
        sup1._set("out1", True)
        assert _wait(qapp, lambda: sup1.values.get("out1") is True)
        win.all_outputs_off()
        assert _wait(qapp, lambda: sup1.values.get("out1") is False)
        scope.capture_now()
        assert _wait(qapp, lambda: scope.capture is not None)
        assert scope.meas.rowCount() == 2
        assert sup2.saved_settings().get("instance") == 2
    finally:
        win.close()
        _spin(qapp, 0.2)


def test_gui_auto_detect_reopens_as_the_right_tab(qapp, cfgdir, monkeypatch):
    """A resource opened as "auto" that answers *IDN? as a meter gets a meter tab."""
    from otterbench.config import Config
    from otterbench.gui.main_window import MainWindow
    from otterbench.instruments import __init__ as _  # noqa: F401
    import otterbench.instruments as inst
    monkeypatch.setattr(inst, "model_for",
                        lambda res, model="auto": model if model != "auto" else "auto")
    import otterbench.gui.main_window as mw
    import otterbench.worker as wk
    monkeypatch.setattr(mw, "model_for", inst.model_for)
    monkeypatch.setattr(wk, "model_for", inst.model_for)
    monkeypatch.setattr(inst, "SIM_MODELS", {})
    win = MainWindow([("auto", "SIM::K2000")], Config(), None)
    try:
        assert _wait(qapp, lambda: win.panels() and win.panels()[0].KIND == "scpi"
                     and win.panels()[0].connected)
        assert win.panels()[0].model == "SCPI:K2000"
    finally:
        win.close()
        _spin(qapp, 0.2)


def test_gui_two_ppk2s_align_through_a_shared_pin(qapp, cfgdir):
    from otterbench.config import Config
    from otterbench.gui.main_window import MainWindow
    from otterbench.instruments.ppk2 import PpkReading
    win = MainWindow([("auto", "SIM::PPK2"), ("auto", "SIM::PPK2#2")], Config(), None)
    try:
        assert _wait(qapp, lambda: all(p.connected for p in win.panels()))
        a, b = win.panels()
        assert a.display_name().startswith("PPK2") and "#2" in b.display_name()
        for p in (a, b):
            p.worker.poll_enabled = False
        _spin(qapp, 0.3)
        rng = np.random.default_rng(3)
        n = 200_000
        edges = np.sort(rng.uniform(0, 2, 60))
        t = 100.0 + np.arange(n) * 1e-5
        bits = ((np.searchsorted(edges, t - 100.0) % 2) << 5).astype(np.uint8)
        a.ring.clear()
        b.ring.clear()
        a.ring.push(PpkReading(t[-1], t[0], np.zeros(n, np.float32), bits))
        late = 0.0042                       # b's samples arrive time-stamped 4.2 ms late
        b.ring.push(PpkReading(t[-1] + late, t[0] + late, np.zeros(n, np.float32), bits))
        b._refresh_sync_choices()
        b.sync_combo.setCurrentIndex(b.sync_combo.findData(a.display_name()))
        b.sync_ch.setValue(5)
        b.align_now()
        assert b.time_shift == pytest.approx(-late, abs=2e-5)
        assert b.saved_settings()["sync"]["to"] == a.display_name()
    finally:
        win.close()
        _spin(qapp, 0.2)


def test_self_test_passes_for_every_builtin():
    for d in g.definitions(reload=True).values():
        assert g.self_test(d) == [], d.id


def test_self_test_finds_overlapping_replies(tmp_path):
    d = g.parse_definition({
        "id": "X", "type": "meter", "reading": [{"name": "value", "query": "READ?"}],
        "setting": [{"name": "f", "kind": "choice", "query": "F?", "options": [
            {"label": "AC", "set": "F AC", "reply": "V"},
            {"label": "DC", "set": "F DC", "reply": "VDC"}]}]})
    assert any("read back 'AC'" in p for p in g.self_test(d))


def test_check_instruments_cli(tmp_path, capsys):
    from otterbench.check_instruments import main
    bad = tmp_path / "bad.toml"
    bad.write_text('id = "B"\ntype = "meter"\n', encoding="utf-8")
    assert main([str(bad)]) == 1
    assert "at least one [[reading]]" in capsys.readouterr().out
    good = g.builtin_dir() / "DP832.toml"
    assert main([str(good)]) == 0
