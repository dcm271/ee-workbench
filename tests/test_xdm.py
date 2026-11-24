"""OWON XDM1041 / XDM1241 / XDM2041."""
import os
import time

import pytest

from otterbench import config
from otterbench.instruments import make_driver, model_for, resolve_model
from otterbench.instruments import xdm
from otterbench.instruments.xdm import OwonXDM, XdmConfig, allowed, norm_range, ranges_for, scale
from otterbench.sim import open_sim
from otterbench.sim.sxdm import SimXDM
from otterbench.transport import TransportTimeout, describe, open_transport, serial_path


def connect(sim=None):
    d = OwonXDM(sim or SimXDM())
    d.SETTLE = d.GAP = 0.0
    d.identify()
    return d


def test_identify_and_first_reading():
    d = connect()
    assert d.caps.model == "XDM1041" and not d.caps.note
    assert d.cfg == XdmConfig("DCV", "AUTO", "Slow", False)
    r = d.poll(10.0)
    assert r.function == "DCV" and r.unit == "V" and r.value == pytest.approx(5, abs=0.01)
    assert r.flags == ["AUTO"] and r.t >= 10.0
    assert len(r.display()) == len("+4.9987 V")          # 5 V range: four decimals


@pytest.mark.parametrize("model", ["XDM1041", "XDM2041"])
@pytest.mark.parametrize("acks", [False, True])
def test_every_function_and_range_round_trips(model, acks):
    d = connect(SimXDM(model, acks=acks))
    assert ("4W Ω" in d.functions()) == (model == "XDM2041")
    for fn in d.functions():
        for rng in ranges_for(fn):
            for rate in ("Slow", "Fast"):
                d.apply(XdmConfig(fn, rng, rate))
                assert (d.cfg.function, d.cfg.range, d.cfg.rate) == (fn, rng, rate)
        assert d.poll(0.0).function == fn


def test_only_documented_commands_are_sent():
    sim = SimXDM()
    d = connect(sim)
    for fn in d.functions():
        for rng in ranges_for(fn):
            d.apply(XdmConfig(fn, rng, "Medium", dual=fn == "ACV"))
            d.poll(0.0)
    d.close()
    assert sim.cmds[-1] == "SYST:LOC" and all(allowed(c) for c in sim.cmds)
    assert "SYST:REM" not in sim.cmds            # would lock the meter's buttons


@pytest.mark.parametrize("cmd", ["INT:START", "int:record?", "INIT", "CAL:START", "*RST", "READ?",
                                 "MEAS1?;INT:START", "MEAS1?\nINT:START", "SENS:INT:START",
                                 "SYST:CAL", "", "CALIB"])
def test_console_refuses_undocumented_commands(cmd):
    sim = SimXDM()
    d = connect(sim)
    n = len(sim.cmds)
    with pytest.raises(ValueError):
        d.raw(cmd)
    assert len(sim.cmds) == n


def test_console_allows_documented_commands():
    d = connect()
    assert float(d.raw("MEAS1?")) == pytest.approx(5, abs=0.01)
    for c in ("CALC:FUNC NULL", "TEMP:RTD:UNIT C", "SENS:FUNC?", "SYST:BEEP:STAT OFF",
              "CONT:THRE 50", "CALC:AVER:MIN?", "meas2:show?"):
        assert allowed(c), c
    assert d.raw("AUTO", read=False) == ""
    assert d.raw("CALC:FUNC?") == "(no answer)"
    assert d.poll(0.0).function == "DCV"


def test_mv_auto_sequence():
    sim = SimXDM()
    d = connect(sim)
    sim.cmds.clear()
    d.apply(XdmConfig("DCV", "AUTO (mV)"))
    assert [c for c in sim.cmds if "?" not in c] == ["RANGE 1", "AUTO"]
    assert d.cfg.range == "AUTO (mV)"


def test_front_panel_changes_are_followed():
    sim = SimXDM()
    d = connect(sim)
    d.poll(0.0)
    sim.function, sim.n, sim.auto = "2W Ω", 2, False
    r = d.poll(1.0)
    assert r.function == "2W Ω" and r.unit == "Ω" and r.cfg.range == "5 kΩ"
    assert r.native == "kΩ" and r.display().startswith("+0.99") and not r.flags


def test_function_changing_mid_reading_is_not_mislabelled():
    sim = SimXDM()
    d = connect(sim)
    real = sim._do
    flip = [True]

    def do(cmd):
        out = real(cmd)
        if cmd == "MEAS1?" and flip.pop() if flip else False:
            sim.function = "2W Ω"
        return out
    sim._do = do
    r = d.poll(0.0)
    assert r.function == "2W Ω" and r.value == pytest.approx(998.7, rel=0.01)


def test_overload_and_odd_answers():
    sim = SimXDM()
    d = connect(sim)
    for raw, text in (("1.000000E+09", "OL"), ("-1.000000E+09", "-OL"), ("overload", "OL")):
        sim.value = raw
        r = d.poll(0.0)
        assert r.display() == text and abs(r.value) > 9e37
    assert r.value > 0
    sim.value = "5.400000E+07"                    # 54 MΩ is still a reading
    assert not d.poll(0.0).overload
    sim.value = "NONe"
    r = d.poll(0.0)
    assert r.text == "NONe" and r.value != r.value


def test_late_answer_is_not_taken_for_the_next_one():
    """A query that goes unanswered for a while answers during the next one."""
    sim = SimXDM()
    d = connect(sim)
    d.OPTIONAL = 0.05
    real, held = sim._do, []

    def do(cmd):
        if cmd == "RATE?" and not held:
            held.append(1)
            return ""
        if cmd == "*IDN?" and held == [1]:
            held.append(2)
            return "S\r\n" + real(cmd)             # the late answer comes first
        return real(cmd)
    sim._do = do
    d._slow_at = 0.0
    assert d.poll(0.0).value == pytest.approx(5, abs=0.01)
    assert d.cfg.function == "DCV" and held == [1, 2]


def test_unanswered_queries_are_asked_once_per_function():
    sim = SimXDM()
    real = sim._do
    sim._do = lambda c: "" if c == "RANGE?" else real(c)
    d = OwonXDM(sim)
    d.SETTLE, d.OPTIONAL = 0.0, 0.05
    d.identify()
    for _ in range(3):
        d._slow_at = 0.0
        assert d.poll(0.0).display().endswith(" V")
    assert sim.cmds.count("RANGE?") == 1


def test_temperature_unit_and_second_display():
    sim = SimXDM()
    d = connect(sim)
    d.apply(XdmConfig("Diode"))
    assert d.poll(0.0).display().startswith("+0.58") and d.poll(0.0).native == "V"
    d.apply(XdmConfig("Temperature", "PT100"))
    r = d.poll(0.0)
    assert r.unit == "°C" and r.cfg.range == "PT100" and r.display().endswith(" °C")
    d.apply(XdmConfig("ACV", "AUTO", "Slow", True))
    assert d.poll(0.0).secondary == "50.001Hz" and d.cfg.dual


def test_wrong_instruments_are_explained():
    with pytest.raises(RuntimeError, match="XDM3051"):
        connect(SimXDM("XDM3051"))
    assert "Not a known" in connect(SimXDM("8045M")).caps.note
    mute = SimXDM()
    mute._do = lambda c: ""
    d = OwonXDM(mute)
    d.query = lambda c, s=None: (_ for _ in ()).throw(TransportTimeout("x"))
    with pytest.raises(TimeoutError, match="115200"):
        d.identify()


def test_bad_settings_are_refused():
    d = connect()
    with pytest.raises(ValueError):
        d.apply(XdmConfig("4W Ω"))
    with pytest.raises(ValueError):
        d.apply(XdmConfig("ACV", "50 mV"))


def test_text_helpers():
    assert norm_range("5KΩ") == "5 kΩ" and norm_range("500uA") == "500 µA"
    assert norm_range("50 mV") == "50 mV" and norm_range("KITS90") == ""
    assert xdm.decode(b"500\xa6\xb8\r") == "500Ω" and xdm.decode(b"23.4\xa1\xe6") == "23.4°C"
    assert xdm.decode(b"5\xaa\xccF") == "5µF" and xdm.decode(b"\xff\xfe1")      # never raises
    assert scale("V", -1.07e-4, "50 mV") == ("mV", 1e-3, 3)
    assert scale("V", 12.0, "5 V") == ("V", 1.0, 3)             # range just changed
    assert scale("Hz", 1000.0, "") == ("kHz", 1e3, 4) and scale("V", 0.0, "")[0] == "V"


def test_routing():
    for name in ("XDM1041", "xdm1241", "XDM2041", "XDM1141", "XDM"):
        assert resolve_model(name) == "XDM"
    assert config.norm_model("XDM1241") == "XDM"
    assert model_for("XDM::/dev/ttyUSB0") == "XDM"
    assert serial_path("XDM::/dev/ttyUSB3") == "/dev/ttyUSB3"
    assert describe("XDM::/dev/ttyUSB3") == "XDM ttyUSB3"
    for s in ("SIM::XDM1041", "SIM::XDM2041", "SIM::XDM"):
        d = make_driver("auto", open_sim(s), s)
        assert isinstance(d, OwonXDM)
    from otterbench.gui.main_window import parse_target
    assert parse_target("XDM1041@/dev/ttyUSB0") == ("XDM", "XDM::/dev/ttyUSB0")
    assert parse_target("XDM@ASRL/dev/ttyUSB0::INSTR") == ("XDM", "XDM::/dev/ttyUSB0")


def test_serial_settings(monkeypatch):
    import otterbench.transport as tr
    got = {}
    monkeypatch.setattr(tr, "SerialStream", lambda p, baud=9600, wait_s=0.05: got.update(p=p, baud=baud))
    open_transport("ASRL/dev/ttyUSB0::INSTR", 9600, "XDM")
    assert got == {"p": "/dev/ttyUSB0", "baud": 115200}


def test_panel():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    global _APP
    _APP = app = QApplication.instance() or QApplication([])
    from otterbench.config import Config
    from otterbench.gui.dmm_panel import XdmPanel
    from otterbench.gui.main_window import MainWindow
    win = MainWindow([("auto", "SIM::XDM1041")], Config(), None)

    def wait(cond, s=8):
        end = time.monotonic() + s
        while not cond() and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
        assert cond()
    try:
        p = win.panels()[0]
        assert isinstance(p, XdmPanel)
        wait(lambda: p.readings >= 2)
        assert p.readout.value.text().endswith(" V")
        assert win.tabs.tabText(0).startswith("XDM1041")
        assert not p.dual_chk.isEnabled() and p.func_combo.findText("4W Ω") < 0
        # change it from the tab
        p.func_combo.setCurrentText("2W Ω")
        p._func_changed()
        wait(lambda: p._last.function == "2W Ω")
        assert p.unit == "Ω" and p.range_combo.findText("50 MΩ") >= 0
        # change it on the meter
        p.worker.drv.t.function = "ACV"
        wait(lambda: p.func_combo.currentText() == "ACV")
        wait(lambda: p._last.function == "ACV")
        assert p.dual_chk.isEnabled()
        assert p.session_settings() == {"name": "", "poll_ms": 250}
        # config.toml may ask for a function
        p.restore_settings({"function": "2w ohm", "range": "5kohm", "rate": "fast"})
        p._apply_pending()
        wait(lambda: (p.cfg.function, p.cfg.range, p.cfg.rate) == ("2W Ω", "5 kΩ", "Fast"))
        p.redraw()
    finally:
        win.close()
        app.processEvents()


_APP = None
