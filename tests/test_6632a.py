from otterbench.instruments import detect
from otterbench.instruments.base import SupplyPoll
from otterbench.instruments.hp6632a import HP6632A
from otterbench.sim.s6632a import Sim6632A


def make():
    d = HP6632A(Sim6632A())
    d.identify()
    return d


def test_identify_and_unknown_setpoints():
    d = make()
    assert d.caps.model == "6632A" and d.caps.v_max == 20.475 and not d.caps.setpoint_readback
    sp = d.read_setpoints()
    assert sp.voltage is None and sp.output is None


def test_detect_from_id_query():
    t = Sim6632A()
    assert detect(t) == "6632A"
    assert HP6632A(t).errors() == []   # detect cleared the ERR 10 from '*IDN?'


def test_program_and_measure():
    d = make()
    d.set_voltage(5); d.set_current(1); d.set_output(True)
    r = d.poll(0, SupplyPoll())
    assert r.mode == "CV" and abs(r.voltage - 5) < 0.02 and abs(r.current - 0.5) < 0.01
    assert d.read_setpoints().voltage == 5
    d.set_current(0.2)
    assert d.poll(0, SupplyPoll()).mode == "CC"
    d.set_output(False)
    assert d.poll(0, SupplyPoll()).mode == "OFF"


def test_ocp_trip_reset_and_errors():
    d = make()
    d.set_voltage(5); d.set_current(0.1); d.set_ocp(True); d.set_output(True)
    assert "OCP" in d.poll(0, SupplyPoll()).flags
    d.set_ocp(False); d.clear_protection()
    assert d.poll(0, SupplyPoll()).flags == []
    d.write("VSET 99")
    r = d.poll(0, SupplyPoll())
    assert r.errors == ["ERR 42: Voltage programming error"]
