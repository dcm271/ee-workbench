import time

import numpy as np

from otterbench.instruments.agilent663xx import Agilent663xx
from otterbench.instruments.base import CaptureSpec, SupplyPoll
from otterbench.sim.s663xx import Sim663xx


def make():
    d = Agilent663xx(Sim663xx())
    d.identify()
    d.read_setpoints()
    return d


def test_identify():
    d = make()
    assert d.caps.model == "66321D"
    c = d.caps
    assert c.has_dvm and c.has_resistance and c.has_auto_range    # done by the driver
    assert [lab for lab, _ in c.current_ranges] == ["3 A", "1 A", "20 mA"]
    assert c.has_trigger and c.has_ext_trigger and c.has_ovp_tracking and c.has_prot_delay
    assert c.comp_modes == ["LLOC", "HLOC", "LREM", "HREM"] and c.has_ri_dfi and c.has_port
    assert c.ovp_label == "Voltage limit" and c.info["Firmware"].startswith("A.03.01")


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
    assert "Over-current trip" in r.events          # latched event, reported once
    assert d.poll(0, SupplyPoll()).events == []
    d.clear_protection(); d.set_ocp(False); d.set_output(True)
    assert d.read_setpoints().output


def test_error_queue():
    d = make()
    d.write("VOLT 99")
    assert d.errors() and d.errors() == []


def run_capture(d, spec, fire=True, timeout=5.0):
    wait = d.arm(spec)
    if wait >= 0 and fire:
        time.sleep(wait)
        d.fire()
    t0 = time.monotonic()
    while not d.capture_done():
        assert time.monotonic() - t0 < timeout, "capture never completed"
        time.sleep(0.02)
    return d.fetch()


def test_settings_round_trip():
    d = make()
    d.set_ovp_tracking(False); d.set_prot_delay(0.5); d.set_window("RECT")
    d.set_comp_mode("LLOC"); d.set_ri_mode("LIVE"); d.set_dfi(True, "QUES")
    d.set_port_function("DIG"); d.set_port_data(3); d.set_sense_protect(True)
    d.set_current_range("1 A"); d.show_text("HELLO")
    s = d.read_setpoints()
    assert (s.ovp_tracking, s.prot_delay, s.window, s.comp_mode) == (False, 0.5, "RECT", "LLOC")
    assert (s.ri_mode, s.dfi, s.dfi_source) == ("LIVE", True, "QUES")
    assert (s.port_function, s.port_data, s.sense_protect) == ("DIG", 3, True)
    assert s.current_range == "1 A" and s.display_mode == "TEXT" and s.display_text == "HELLO"
    assert d.errors() == []


def test_saved_states():
    d = make()
    d.set_voltage(2.5); d.save_state(1); d.set_voltage(4.0)
    d.recall_state(1)
    assert d.read_setpoints().voltage == 2.5


def test_level_trigger_with_pretrigger():
    d = make()
    d.set_voltage(5); d.set_current(2); d.set_output(True)
    spec = CaptureSpec(what="CURR", points=512, tint=15.6e-6, trigger="LEVEL",
                       level=0.5, hyst=0.05, slope="POS", pre=128)
    res = run_capture(d, spec)
    y = np.asarray(res.data)
    assert y.size == 512
    # idle (~0.1 A) before the trigger point, burst (~1 A) after it
    assert y[:120].mean() < 0.3 and y[135:150].mean() > 0.8
    assert res.stats["max"] > 0.9 and d.read_setpoints().sweep_points == 2048


def test_segments():
    d = make()
    d.set_voltage(5); d.set_current(2); d.set_output(True)
    spec = CaptureSpec(what="CURR", points=256, trigger="LEVEL", level=0.5, count=4, pre=16)
    assert len(run_capture(d, spec).data) == 1024


def test_step_capture_restores_level():
    d = make()
    d.set_voltage(3.0); d.set_current(2); d.set_output(True)
    spec = CaptureSpec(what="VOLT", points=400, tint=15.6e-6, trigger="STEP", pre=100,
                       step={"VOLT": 4.0})
    res = run_capture(d, spec)
    y = np.asarray(res.data)
    assert abs(y[:90].mean() - 3.0) < 0.6 and y[-50:].mean() > 3.5
    assert res.restored == {"VOLT": 3.0} and d.read_setpoints().voltage == 3.0


def test_step_output_and_abort():
    d = make()
    d.step_output({"VOLT": 1.5, "CURR": 0.3})
    s = d.read_setpoints()
    assert (s.voltage, s.current) == (1.5, 0.3)
    d.arm(CaptureSpec(trigger="LEVEL", level=3.0))     # can never fire: output off
    time.sleep(0.1)
    assert not d.capture_done()
    d.abort()
    assert d.capture_done() and d.read_setpoints().sweep_points == 2048


def test_capture_validation():
    spec = Agilent663xx.validate(CaptureSpec(points=2048, count=5, pre=5000, tint=1e-6))
    assert spec.count == 1 and spec.pre == 2047 and spec.tint == 15.6e-6
    spec = Agilent663xx.validate(CaptureSpec(points=2048, count=5, trigger="LEVEL"))
    assert spec.count == 2
