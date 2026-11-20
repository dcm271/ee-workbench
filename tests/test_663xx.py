from ee_workbench.instruments.agilent663xx import Agilent663xx
from ee_workbench.instruments.base import SupplyPoll
from ee_workbench.sim.s663xx import Sim663xx


def make():
    d = Agilent663xx(Sim663xx())
    d.identify()
    d.read_setpoints()
    return d


def test_identify():
    d = make()
    assert d.caps.model == "66321D"
    assert d.caps.has_dvm and d.caps.has_resistance and d.caps.has_auto_range


def test_poll_single_transaction_cv_then_cc():
    d = make()
    d.set_voltage(3.7); d.set_current(2.0); d.set_output(True)
    r = d.poll(0, SupplyPoll(pulse_stats=True, dvm=True))
    assert r.mode == "CV" and r.output and abs(r.voltage - 3.7) < 0.05
    assert r.i_max > 0.7 and r.i_min < 0.1 and r.i_high > r.i_low and r.dvm
    d.set_current(0.05)
    r = d.poll(0, SupplyPoll(measure_voltage=False))
    assert r.mode == "CC" and r.voltage is None and abs(r.current - 0.05) < 0.005
    assert d.errors() == []


def test_output_off_mode():
    d = make()
    r = d.poll(0, SupplyPoll())
    assert r.mode == "OFF" and r.output is False


def test_waveform_restores_sweep():
    d = make()
    d.set_voltage(5); d.set_current(2); d.set_output(True)
    d.set_sweep(256, 15.6e-6)
    w = d.capture("CURR", 4096, 15.6e-6)
    assert len(w) == 4096 and max(w) > 0.9
    assert d.read_setpoints().sweep_points == 256


def test_ocp_trip_and_clear():
    d = make()
    d.set_voltage(5); d.set_current(0.01); d.set_ocp(True); d.set_output(True)
    r = d.poll(0, SupplyPoll())
    assert "OCP" in r.flags and not d.read_setpoints().output
    d.clear_protection(); d.set_ocp(False); d.set_output(True)
    assert d.read_setpoints().output


def test_error_queue():
    d = make()
    d.write("VOLT 99")
    assert d.errors() and d.errors() == []
