import struct

import pytest

from otterbench.instruments import make_driver, model_for
from otterbench.instruments.base import SupplyPoll
from otterbench.instruments.dps150 import DPS150, frame, f32, parse_frames, Reg, START_SET
from otterbench.sim.sdps150 import SimDPS150, dframe
from otterbench.transport import describe, serial_path


def make():
    d = DPS150(SimDPS150())
    d.identify()
    return d


def test_frames_match_published_captures():
    assert frame(START_SET, Reg.VOLTAGE_SET, f32(10.0)).hex(" ") == "f1 b1 c1 04 00 00 20 41 26"
    assert frame(START_SET, Reg.CURRENT_SET, f32(1.0)).hex(" ") == "f1 b1 c2 04 00 00 80 3f 85"
    assert frame(START_SET, Reg.OUTPUT, b"\x01").hex(" ") == "f1 b1 db 01 01 dd"
    assert frame(0xA1, Reg.ALL, b"\x00").hex(" ") == "f1 a1 ff 01 00 00"


def test_parser_resyncs_and_keeps_partial_frames():
    good = bytes.fromhex("f0 a1 de 07 44 50 53 2d 31 35 30 8f")
    bad = bytes.fromhex("f0 a1 de 01 00 00")          # wrong checksum
    buf = bytearray(b"\x12\xf0" + bad + good + good[:5])
    out = parse_frames(buf)
    assert out == [(0xA1, 0xDE, b"DPS-150")]
    assert bytes(buf) == good[:5]
    buf += good[5:]
    assert parse_frames(buf) == [(0xA1, 0xDE, b"DPS-150")] and not buf


def test_identify():
    d = make()
    assert d.caps.model == "DPS-150" and "V1.0" in d.caps.idn
    assert d.caps.v_max == pytest.approx(SimDPS150().upper_v) and d.caps.i_max == 5.1
    sp = d.read_setpoints()
    assert sp.voltage == 5.0 and sp.current == 1.0 and sp.output is False and not sp.ocp


def test_program_and_measure():
    d = make()
    d.set_voltage(5); d.set_current(1); d.set_output(True)
    r = d.poll(0, SupplyPoll())
    assert r.mode == "CV" and abs(r.voltage - 5) < 1e-4 and abs(r.current - 0.5) < 1e-4
    d.set_current(0.2)
    r = d.poll(0, SupplyPoll())
    assert r.mode == "CC" and abs(r.voltage - 2) < 1e-4
    assert d.read_setpoints().current == d.read_setpoints().current == \
        struct.unpack("<f", f32(0.2))[0]
    d.set_output(False)
    assert d.poll(0, SupplyPoll()).mode == "OFF"


def test_voltage_clamped_to_input_limit():
    d = make()
    d.set_voltage(29)
    assert d.read_setpoints().voltage == d.caps.v_max


def test_ocp_tracks_current_limit_and_trips():
    d = make()
    d.set_voltage(5); d.set_current(0.2); d.set_ocp(True)
    assert d.read_setpoints().ocp
    d.set_current(0.3)                  # the OCP threshold follows
    assert d.read_setpoints().ocp and abs(d.st["ocp"] - 0.3) < 1e-6
    d.set_output(True)
    r = d.poll(0, SupplyPoll())
    assert r.flags == ["OCP"] and r.mode == "OFF" and r.output is False
    d.set_ocp(False); d.clear_protection(); d.set_output(True)
    r = d.poll(0, SupplyPoll())
    assert r.flags == [] and r.mode == "CC"


def test_ovp_trip_and_reset():
    d = make()
    d.set_voltage(12); d.set_current(2); d.set_ovp(10); d.set_output(True)
    assert "OV" in d.poll(0, SupplyPoll()).flags
    d.reset()
    sp = d.read_setpoints()
    assert sp.output is False and sp.voltage == 0 and sp.ovp > 30


def test_unsolicited_pushes_are_absorbed():
    t = SimDPS150()
    d = DPS150(t)
    d.identify()
    t._out += dframe(Reg.TEMPERATURE, f32(42.0)) + b"\x00junk"
    d.poll(0, SupplyPoll())
    assert d.st["temp"] in (42.0, 31.5)     # the ALL block may land after it
    assert d.st["vin"] == 24.0


def test_resource_routing():
    assert model_for("DPS150::/dev/ttyACM0") == "DPS150"
    assert model_for("SIM::DPS150") == "DPS150"
    assert serial_path("DPS150::/dev/ttyACM3") == "/dev/ttyACM3"
    assert describe("DPS150::/dev/ttyACM3").startswith("DPS-150")
    drv = make_driver("auto", SimDPS150(), "SIM::DPS150")
    assert isinstance(drv, DPS150)


def test_close_leaves_remote_mode():
    t = SimDPS150()
    d = DPS150(t)
    d.identify()
    d.close()
    assert t.sent[-1] == bytes.fromhex("f1 c1 00 01 00 01") and not t.connected


def test_console_hex():
    d = make()
    assert d.raw("a1 de 00") == "de: 44 50 53 2d 31 35 30"
    assert d.raw("f1 b1 db 01 01 dd").startswith("db: 01")
    assert d.read_setpoints().output is True
    assert d.raw("zz").startswith("hex bytes")
