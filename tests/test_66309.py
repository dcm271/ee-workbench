"""66309B: a 663xx with a second output."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from otterbench import hwcheck, watchdog                                # noqa: E402
from otterbench.instruments.agilent663xx import Agilent663xx            # noqa: E402
from otterbench.instruments.base import CaptureSpec, SupplyPoll         # noqa: E402
from otterbench.roles import ports                                      # noqa: E402
from otterbench.sequencer import Bench, Inst, SequenceError             # noqa: E402
from otterbench.signals import signals_of                               # noqa: E402
from otterbench.sim.s663xx import Sim66309, Sim663xx                    # noqa: E402


def make(coupling=True):
    d = Agilent663xx(Sim66309(coupling))
    d.identify()
    d.read_setpoints()
    return d


def test_identify():
    c = make().caps
    assert c.model == "66309B" and c.has_out2 and c.has_coupling
    assert (c.v2_max, c.i2_max) == pytest.approx((12.25, 1.52), abs=1e-3)
    assert not (c.has_dvm or c.has_resistance or c.has_ext_trigger)
    assert c.comp_modes == ["LOW", "HIGH", "H2"]
    assert [lab for lab, _ in c.current_ranges] == ["3 A", "20 mA"]
    assert c.ovp_is_trip and c.ovp_label == "OVP"


def test_the_66321d_is_as_it_was():
    d = Agilent663xx(Sim663xx())
    c = d.identify()
    assert not c.has_out2 and not c.ovp_is_trip and c.ovp_label == "Voltage limit"
    r = d.poll(0, SupplyPoll())
    assert r.voltage2 is None and r.output2 is None and r.coupled is None
    assert d.read_setpoints().voltage2 is None
    d.set_output(True)
    d.all_off()
    assert d.errors() == [] and d.read_setpoints().output is False
    with pytest.raises(ValueError):
        d.set_voltage2(1)


def test_coupled_switches_both():
    d = make()
    d.set_voltage(3), d.set_voltage2(5), d.set_current2(1), d.set_current(2)
    d.set_output(True)
    r = d.poll(0, SupplyPoll())
    assert r.output and r.output2 and r.coupled and r.mode == "CV" and r.mode2 == "CV"
    assert r.voltage2 == pytest.approx(5, abs=0.01) and r.current2 == pytest.approx(0.5, abs=0.01)
    d.set_current2(0.2)
    assert d.poll(0, SupplyPoll()).mode2 == "CC"
    assert d.errors() == []


def test_output_2_alone_is_refused_while_coupled():
    d = make()
    with pytest.raises(ValueError, match="coupled"):
        d.set_output2(True)
    s = d.read_setpoints()
    assert not s.output and not s.output2


def test_separate_outputs():
    d = make()
    d.set_coupled(False)
    d.set_voltage2(5), d.set_current2(1)
    d.set_output2(True)
    r = d.poll(0, SupplyPoll())
    assert r.output2 and not r.output and r.coupled is False and r.mode == "OFF"
    d.set_output(True), d.set_output(False)
    assert d.read_setpoints().output2 is True         # OUTP is output 1 only
    d.set_output2(False)
    assert d.read_setpoints().output2 is False


@pytest.mark.parametrize("coupled", [True, False])
def test_all_off_is_both_outputs(coupled):
    d = make()
    d.set_coupled(coupled)
    d.set_output(True)
    if not coupled:
        d.set_output2(True)
    s = d.read_setpoints()
    assert s.output and s.output2
    d.all_off()
    s = d.read_setpoints()
    assert s.output is False and s.output2 is False and d.errors() == []


def test_firmware_without_coupling():
    d = make(coupling=False)
    assert d.caps.has_out2 and not d.caps.has_coupling and "A.02.04" in d.caps.note
    d.set_output(True)
    s = d.read_setpoints()
    assert s.output and s.output2 and s.coupled
    with pytest.raises(ValueError):
        d.set_output2(True)
    with pytest.raises(ValueError):
        d.set_coupled(False)
    d.all_off()
    s = d.read_setpoints()
    assert not s.output and not s.output2 and d.errors() == []


def test_output_2_trip_is_reported():
    d = make()
    d.set_voltage2(5), d.set_current2(0.1), d.set_ocp(True), d.set_output(True)
    r = d.poll(0, SupplyPoll())
    assert "OCP2" in r.flags and "Output 2 over-current trip" in r.events
    assert r.output is False and r.output2 is False
    d.set_current2(1)
    d.clear_protection()
    assert d.poll(0, SupplyPoll()).flags == []


def test_ovp_is_a_trip():
    d = make()
    d.set_ovp(5), d.set_voltage(6), d.set_output(True)
    assert "OV" in d.poll(0, SupplyPoll()).flags


def test_capture_still_works_on_output_1():
    d = make()
    d.set_voltage(5), d.set_current(2), d.set_output(True)
    assert max(d.capture("CURR", 1024, 15.6e-6)) > 0.9
    d.arm(CaptureSpec(points=64))
    d.abort()
    d.poll(0, SupplyPoll(pulse_stats=True))
    assert d.errors() == []


def test_signals_and_faster_poll():
    d = make()
    d.set_voltage2(5), d.set_current2(1), d.set_output(True)
    s = signals_of("supply", d.poll(1.0, SupplyPoll()))
    assert s["power2"][1][0] == pytest.approx(2.5, abs=0.02)
    r = d.poll(1.0, SupplyPoll(out2=False))
    assert r.voltage2 is None and r.output2 is True       # state is always known
    assert "voltage2" not in signals_of("supply", r)


class _Inst(Inst):
    def __init__(self, name, drv, dual=True):
        self.name, self.kind, self.drv = name, "supply", drv
        self.caps = drv.caps
        self.measure_names = ["voltage", "current", "power"] + (
            ["voltage2", "current2", "power2"] if dual else [])
        self.set_names = ["voltage"] + (["voltage2", "current2", "output2"] if dual else [])
        self.profile_names = None

    def run(self, fn, timeout=30):
        return fn(self.drv)

    def reading_after(self, t, timeout):
        return self.drv.poll(time.monotonic(), SupplyPoll())


def test_sequencer_and_roles():
    d = make()
    i = _Inst("66309B", d)
    b = Bench([i], clock=time.monotonic)
    b.set("66309B", "coupled", "off")
    b.set("66309B", "voltage2", 5), b.set("66309B", "current2", 1)
    b.set("66309B", "output2", "on")
    assert b.measure("66309B", "voltage2") == pytest.approx(5, abs=0.01)
    assert b.measure("66309B", "power2") == pytest.approx(2.5, abs=0.02)
    assert d.read_setpoints().output is False
    assert [f.text for f in ports([i])] == ["66309B", "66309B output 2"]
    one = Agilent663xx(Sim663xx())
    one.identify()
    b = Bench([_Inst("66321D", one, dual=False)], clock=time.monotonic)
    with pytest.raises(SequenceError, match="one output"):
        b.set("66321D", "voltage2", 5)


def test_hardware_limits():
    d = make()
    names = ["voltage", "current2"]
    job, text = watchdog.hw_plan(watchdog.Rule("x", "current2", ">", 0.1, hw=True), "supply", names)
    d.set_current2(1.0)
    job(d)
    s = d.read_setpoints()
    assert s.current2 == pytest.approx(0.1, abs=1e-3) and s.ocp and "output 2" in text
    job, why = watchdog.hw_plan(watchdog.Rule("x", "voltage2", ">", 5, hw=True), "supply", names)
    assert job is None and "no over-voltage" in why
    job, why = watchdog.hw_plan(watchdog.Rule("x", "current2", ">", 5, hw=True), "supply", [])
    assert job is None and "one output" in why
    # a limit that depends on OVP switches OVP on
    d.set_ovp_tracking(False)
    job, _ = watchdog.hw_plan(watchdog.Rule("x", "voltage", ">", 4.2, hw=True), "supply")
    job(d)
    s = d.read_setpoints()
    assert s.ovp == pytest.approx(4.2, abs=0.01) and s.ovp_tracking is True


def test_hardware_check_sees_and_spares_output_2():
    d = make()
    d.set_coupled(False)
    d.set_output2(True)
    assert hwcheck.outputs_on(d) is True
    hwcheck.outputs_off(d)
    assert hwcheck.outputs_on(d) is False
    d.set_coupled(True)
    d.set_voltage2(9)
    seen = []
    real = d.set_output
    d.set_output = lambda on: (seen.append((on, d.read_setpoints().voltage2)), real(on))
    h = hwcheck.HardwareCheck(run=lambda fn: fn(d), caps=d.caps, sleep=lambda s: None)
    h._out_supply()
    assert (True, 0.0) in seen                       # it came on at 0 V
    assert d.read_setpoints().voltage2 == pytest.approx(9)


# ---------------------------------------------------------------- panel
@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _wait(app, cond, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize("touch_mode", [False, True])
def test_panel(qapp, monkeypatch, touch_mode):
    from otterbench.gui import touch
    from otterbench.gui.supply_panel import SupplyPanel
    monkeypatch.setattr(touch, "TOUCH", touch_mode)
    p = SupplyPanel("SIM::66309B", "auto", 9600)
    try:
        assert _wait(qapp, lambda: p.connected and p.last_reading is not None)
        # both outputs are on show; one button stands for the coupled pair
        assert not p.out2_col.isHidden() and p.out2_btn.isHidden()
        assert "1 + 2" in p.out_btn.text() and p.both_slot.indexOf(p.out_btn) >= 0
        assert p.ovt_chk.text() == "OVP"
        assert "voltage2" in p.measure_names() and "output2" in p.set_names()
        assert "power2" in p.quantities()
        assert len(p.csv_header()) == 18
        p.cmd(lambda d: (d.set_voltage2(5), d.set_current2(1), d.set_coupled(False)))
        assert _wait(qapp, lambda: p.sp.coupled is False)
        assert not p.out2_btn.isHidden() and p.out_slot.indexOf(p.out_btn) == 0
        assert p.out_btn.text().startswith("OUTPUT 1 ")
        p.set_output2(True)
        if touch_mode:                               # one tap only arms it
            assert "Tap again" in p.out2_btn.text()
            p.set_output2(True)
        assert _wait(qapp, lambda: p.sp.output2 is True)
        assert p.sp.output is False
        assert _wait(qapp, lambda: p.r_volt2.value.text().startswith("+5.00"))
        p.redraw()
        # ALL OUTPUTS OFF, with the outputs separate
        p.output_off()
        assert _wait(qapp, lambda: p.sp.output2 is False)
        # a trip of output 2 shows beside output 2
        p.cmd(lambda d: (d.set_current2(0.1), d.set_ocp(True), d.set_output2(True)))
        assert _wait(qapp, lambda: "OCP2" in p.flags2_label.text())
        assert "OCP2" not in p.flags_label.text() and "Clear trip" in p.flags_label.text()
        p.clr_btn.click()
        assert _wait(qapp, lambda: p.flags2_label.text() == "")
        # coupled while only output 2 is on: the button reads ON, and turns it off
        p.cmd(lambda d: (d.set_ocp(False), d.set_current2(1), d.set_output2(True)))
        assert _wait(qapp, lambda: p.sp.output2 is True)
        p.cmd(lambda d: d.set_coupled(True))
        assert _wait(qapp, lambda: p.sp.coupled is True)
        assert p.out_btn.isChecked() and "ONLY OUTPUT 2" in p.out_btn.text()
        p.out_btn.click()
        assert _wait(qapp, lambda: p.sp.output2 is False)
        p.cmd(lambda d: d.set_coupled(False))
        assert _wait(qapp, lambda: p.sp.coupled is False)
        # a change made on the front panel shows up
        p.worker.drv.t.coupled = True
        assert _wait(qapp, lambda: p.sp.coupled is True and "1 + 2" in p.out_btn.text())
    finally:
        p.shutdown()


def test_panel_of_a_single_output_supply(qapp):
    from otterbench.gui.supply_panel import SupplyPanel
    p = SupplyPanel("SIM::66321D", "663xx", 9600)
    try:
        assert _wait(qapp, lambda: p.connected)
        assert p.out2_col.isHidden() and p.meas2_chk.isHidden()
        assert p.out_slot.indexOf(p.out_btn) == 0 and p.prot_row.indexOf(p.clr_btn) >= 0
        assert p.out_btn.text().startswith("OUTPUT O") and len(p.csv_header()) == 14
        assert "voltage2" not in p.measure_names() and "output2" not in p.set_names()
    finally:
        p.shutdown()


def test_clear_does_not_stop_the_trend(qapp):
    """Clear forgets the lag; a redraw before the next reading must not fail."""
    from otterbench.gui.supply_panel import SupplyPanel
    p = SupplyPanel("SIM::66321D", "663xx", 9600)
    try:
        assert _wait(qapp, lambda: p.connected and p.last_reading is not None)
        p.clear_trend()
        assert p._view_edge() is None
        p.redraw()
    finally:
        p.shutdown()
