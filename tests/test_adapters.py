"""Instruments behind adapters: byte-level simulators, and recorded frames."""
import math
import struct

import pytest

from ee_workbench.gui.main_window import parse_target
from ee_workbench.instruments import adapters as ad
from ee_workbench.instruments import definition_for, make_driver, model_for
from ee_workbench.instruments import scpi_generic as g
from ee_workbench.sim import open_sim, sadapters
from ee_workbench.transport import TransportError, TransportTimeout, describe


def drv(res):
    t = open_sim(res)
    d = make_driver(model_for(res), t, res)
    d.identify()
    return d


@pytest.mark.parametrize("did", sorted(k for k, d in g.definitions().items() if d.protocol))
def test_self_test(did):
    assert g.self_test(definition_for(did)) == []


# ---- resources --------------------------------------------------------------
def test_resources():
    assert parse_target("RD60XX::/dev/ttyUSB0@9600") == ("SCPI:RD60XX", "RD60XX::/dev/ttyUSB0@9600")
    assert parse_target("KA3005P@/dev/ttyUSB1") == ("SCPI:KA3005P", "KA3005P::/dev/ttyUSB1")
    assert parse_target("W1::")[0] == "SCPI:W1"
    assert parse_target("PX100@SIM::PX100") == ("SCPI:PX100", "SIM::PX100")
    assert model_for("rd60xx::/dev/ttyUSB0") == "SCPI:RD60XX"
    assert model_for("DP832::/dev/ttyUSB0") == "auto"           # real SCPI: a VISA resource
    assert ad.parse_resource("RD60XX::/dev/ttyUSB0@9600") == ("RD60XX", "/dev/ttyUSB0", 9600)
    assert describe("RD60XX::/dev/ttyUSB0@9600") == "RD60XX ttyUSB0"


def test_serial_speed_and_path(monkeypatch):
    seen = []

    class Fake:
        def __init__(self, path, baud=0, wait_s=0):
            seen.append((path, baud))
    monkeypatch.setattr("ee_workbench.transport.SerialStream", Fake)
    from ee_workbench.transport import open_transport
    open_transport("RD60XX::/dev/ttyUSB0", 9600, "SCPI:RD60XX")
    open_transport("RD60XX::/dev/ttyUSB0@19200", 9600, "SCPI:RD60XX")
    open_transport("ASRL/dev/ttyUSB2::INSTR", 9600, "SCPI:KA3005P")
    open_transport("/dev/ttyUSB3", 9600, "SCPI:PX100")
    assert seen == [("/dev/ttyUSB0", 115200), ("/dev/ttyUSB0", 19200),
                    ("/dev/ttyUSB2", 9600), ("/dev/ttyUSB3", 9600)]


# ---- Riden ------------------------------------------------------------------
def test_modbus_crc_and_frame():
    # the Modbus specification's own example frame
    assert ad.crc16(bytes.fromhex("0207")) == 0x1241
    link = sadapters.SimRiden()
    a = ad.RidenAdapter(link)
    a.query("VSET?")
    assert link.sent[0] == bytes.fromhex("010300000004") + struct.pack(
        "<H", ad.crc16(bytes.fromhex("010300000004")))


@pytest.mark.parametrize("mid,v,i,vc,ic", [(60062, 12.34, 1.234, 1234, 1234),
                                           (60121, 12.34, 1.23, 1234, 123),
                                           (60065, 12.345, 1.2345, 12345, 12345),
                                           (60241, 12.34, 20.0, 1234, 2000)])
def test_riden_scales_by_model(mid, v, i, vc, ic):
    link = sadapters.SimRiden(mid)
    a = ad.RidenAdapter(link)
    a.write(f"VSET {v}")
    a.write(f"ISET {i}")
    assert link.r[8] == vc and link.r[9] == ic
    assert float(a.query("VSET?")) == pytest.approx(v)
    assert float(a.query("ISET?")) == pytest.approx(i)


def test_riden_6012p_current_range():
    link = sadapters.SimRiden(60127)
    a = ad.RidenAdapter(link)
    a.write("ISET 2")
    assert link.r[9] == 20000                 # 6 A range: 0.1 mA steps
    link.r[20] = 1
    a._block = None
    a.write("ISET 2")
    assert link.r[9] == 2000


def test_riden_output_and_limits():
    d = drv("SIM::RD60XX")
    assert "RD6006" in d.caps.idn
    with pytest.raises(ValueError):
        d.set("iset", 12)                     # an RD6006 stops at 6 A
    d.set("vset", 5)
    d.set("out", True)
    r = d.poll(0)
    assert r.values["voltage"] == pytest.approx(5, abs=0.02)
    assert r.values["current"] == pytest.approx(0.1, abs=2e-3)
    assert r.values["power"] == pytest.approx(0.5, abs=0.02)
    d.set_output(False)
    assert d.poll(0).values["voltage"] == 0


def test_riden_unknown_model_and_errors():
    with pytest.raises(TransportError, match="Unknown Riden"):
        ad.RidenScale(50000)
    link = sadapters.SimRiden()
    a = ad.RidenAdapter(link)
    a.query("*IDN?")
    with pytest.raises(TransportError, match="Modbus error 2"):
        a.regs(115, 10)

    class Dead(sadapters._Link):
        def on_bytes(self, data):
            pass
    a = ad.RidenAdapter(Dead())
    a.REPLY_S = 0.01
    with pytest.raises(TransportTimeout):
        a.query("*IDN?")


def test_riden_retries_a_garbled_answer():
    class Noisy(sadapters.SimRiden):
        n = 0

        def on_bytes(self, f):
            super().on_bytes(f)
            self.n += 1
            if self.n == 2:
                self._out[4] ^= 0xFF
    a = ad.RidenAdapter(Noisy())
    a.query("*IDN?")
    assert float(a.query("VSET?")) == 5.0


# ---- Korad ------------------------------------------------------------------
def test_korad_bytes():
    link = sadapters.SimKorad()
    a = ad.KoradAdapter(link)
    a.GAP = 0
    a.write("VSET 3.3")
    a.write("ISET 0.25")
    a.write("OUT ON")
    assert link.sent[-3:] == [b"VSET1:03.30", b"ISET1:0.250", b"OUT1"]
    assert a.query("ISET?") == "0.25"          # its stray sixth byte is dropped
    assert a.query("VSET?") == "3.3"
    assert a.query("OUT?") == "1" and a.query("CV?") == "1" and a.query("OVP?") == "0"
    assert a.query("*IDN?").startswith("KORAD KA3005P")
    a.write("ISET 0.01")
    a._stat = None
    assert a.query("CV?") == "0"
    assert float(a.query("VOUT?")) == pytest.approx(0.5)


# ---- PX-100 -----------------------------------------------------------------
def test_px100_bytes():
    link = sadapters.SimPx100()
    a = ad.Px100Adapter(link)
    a.write("CURR 1.23")
    assert link.sent[-1] == bytes([0xB1, 0xB2, 2, 1, 0x17, 0xB6])     # the protocol's example
    a.write("INP ON")
    assert link.sent[-1] == bytes([0xB1, 0xB2, 1, 1, 0, 0xB6])
    assert a.query("INP?") == "1"
    v, i = float(a.query("VOLT?")), float(a.query("CURR?"))
    assert v == pytest.approx(12.6 - 0.123, abs=2e-3) and i == pytest.approx(1.23)
    assert float(a.query("POW?")) == pytest.approx(v * i, rel=1e-3)
    with pytest.raises(ValueError):
        a.write("CURR 99")
    a.write("TIMER 3700")
    assert a.query("TIMER?") == "3700"


# ---- temperature ------------------------------------------------------------
def test_center_packet():
    p = bytearray(45)
    p[0], p[44], p[1] = 2, 3, 0x80
    struct.pack_into(">hhhh", p, 7, 234, -55, 1370, 0)
    p[43], p[39] = 0b0100, 0b1000
    t = ad.CenterAdapter.parse(bytes(p))
    assert t[:3] == [23.4, -5.5, 1370] and math.isnan(t[3])
    link = sadapters.SimCenter()
    link.celsius = False
    a = ad.CenterAdapter(link)
    assert float(a.query("T1?")) == pytest.approx(23.4, abs=0.2)
    d = drv("SIM::CENTER309")
    r = d.poll(0)
    assert math.isnan(r.values["t3"]) and r.units["t1"] == "°C"


def test_w1_files(tmp_path):
    for name, text in (("28-0000000000aa", "23125\n"), ("28-0000000000bb", "-1500\n")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "temperature").write_text(text)
    (tmp_path / "w1_bus_master1").mkdir()
    old = tmp_path / "28-0000000000cc"
    old.mkdir()
    (old / "w1_slave").write_text("2d 00 4b 46 ff ff 02 10 19 : crc=19 NO\n2d 00 t=22500\n")
    link = ad.W1Link(str(tmp_path))
    assert link.sensors() == ["28-0000000000aa", "28-0000000000bb", "28-0000000000cc"]
    assert link.read("28-0000000000aa") == 23.125 and link.read("28-0000000000bb") == -1.5
    assert math.isnan(link.read("28-0000000000cc"))       # CRC error
    a = ad.W1Adapter(link)
    assert float(a.query("T2?")) == -1.5
    assert a.query("T3?") == ad.BAD


def test_w1_channels_follow_the_resource():
    d = definition_for("SCPI:W1", "W1::28-00000000beef,28-00000000cafe,28-000000001234")
    assert [r.name for r in d.readings] == ["t1", "t2", "t3"]
    assert "beef" in d.readings[0].label
    assert [r.name for r in definition_for("W1").readings] == ["t_n"]      # not narrowed
    assert set(drv("SIM::W1").poll(0).values) == {"t1", "t2"}


def test_max31855_words():
    def w(x):
        return ad.Max31855Adapter.parse(x.to_bytes(4, "big"))
    # the data sheet's examples: +1600 °C, +25 °C, -250 °C; reference +127, -20 °C
    assert w(0b01100100000000 << 18)[0] == 1600
    assert w(0b00000001100100 << 18 | 0b011111110000 << 4) == (25, 127)
    assert w(0b11110000011000 << 18 | 0b111011000000 << 4) == (-250, -20)
    tc, cold = w(1 << 16 | 1 | 0b000110010000 << 4)
    assert math.isnan(tc) and cold == 25


def test_rtd_conversion():
    for t, r in ((0, 100.0), (100, 138.5055), (-100, 60.2558), (-200, 18.5201), (400, 247.0920)):
        assert ad.rtd_celsius(r, 100) == pytest.approx(t, abs=2e-3)
    d = drv("SIM::MAX31865")
    assert d.poll(0).values["t1"] == pytest.approx(100, abs=0.2)
    d.set("wires", "3 wires")
    d.t.query("T1?")
    assert d.t.link.reg0 & 0x10
    d.t.link.fault = 1
    assert math.isnan(d.poll(0).values["t1"])
    d.poll(0)


def test_spi_message_layout():
    """struct spi_ioc_transfer is 32 bytes; SPI_IOC_MESSAGE(1) encodes that."""
    assert struct.calcsize("=QQIIHBBBBBB") == 32
    assert ad.SpiLink.MESSAGE == (1 << 30 | 32 << 16 | ord("k") << 8 | 0)
    assert ad.SpiLink.WR_MODE == (1 << 30 | 1 << 16 | ord("k") << 8 | 1)


# ---- LCR --------------------------------------------------------------------
def test_et44_acknowledges_sets():
    d = drv("SIM::ET44")
    d.set("function", "R")
    r = d.poll(0)
    assert r.units["primary"] == "Ω" and r.units["secondary"] == ""
    d.set("second", "ESR")
    assert d.poll(0).units["secondary"] == "Ω"
    d.set("model", "Parallel")
    assert d.values["model"] == "Parallel"
    with pytest.raises(ValueError, match="rejected"):
        d.t.write("FOOBAR 42")


def test_one_answer_per_poll_and_second_unit():
    d = drv("SIM::E4980A")
    asked = []
    real = d.t.query
    d.t.query = lambda c: (asked.append(c), real(c))[1]
    d.set("function", "Cs-Rs")
    asked.clear()
    r = d.poll(0)
    assert asked == ["FETC?"]
    assert r.units == {"primary": "F", "secondary": "Ω"}
    assert r.values["primary"] == pytest.approx(1.0012e-9, rel=1e-3)
    assert r.values["secondary"] == pytest.approx(0.00123, rel=0.02)
    d.set("function", "Cp-D")
    assert d.poll(0).units["secondary"] == ""


def test_bad_protocol_keys_are_explained():
    base = {"id": "X", "reading": [{"name": "v", "query": "X?"}]}
    with pytest.raises(g.DefinitionError, match="protocol"):
        g.parse_definition({**base, "protocol": "nope"})
    with pytest.raises(g.DefinitionError, match="baud"):
        g.parse_definition({**base, "baud": "fast"})
    with pytest.raises(g.DefinitionError, match="field"):
        g.parse_definition({"id": "X", "reading": [{"name": "v", "query": "X?", "field": "a"}]})
