import math
import os
import time

import pytest

from ee_workbench import config
from ee_workbench.instruments import make_driver, model_for, resolve_model
from ee_workbench.instruments.bk393 import (BK393, BK393B, OVERLOAD, appa_frame,
                                            decode_393, decode_display, parse_393,
                                            parse_appa, split_unit)
from ee_workbench.signals import signals_of
from ee_workbench.sim.sbk393 import SimBK393, SimBK393B, frame393, line
from ee_workbench.transport import describe, serial_path


def dec(hexs):
    return decode_393(bytes.fromhex(hexs), 1.0)


# ---------------------------------------------------------------- 393 frames
def test_393_frames_from_the_decoder_source():
    # sample digits from inflex/BK-393: "D3D6", least significant first = 0x6D3D = 27965
    r = dec("f0 11 02 00 44 33 44 36 00 05")
    assert r.function == "DCV" and r.unit == "V" and r.decimals == 4
    assert r.value == pytest.approx(0x6D3D / 1e4) and r.display() == "+2.7965 V"
    r = dec("f0 11 04 02 44 33 44 36 20 05")          # 60 kΩ range, sign bit
    assert r.value == pytest.approx(-27965.0) and r.native == "kΩ" and r.unit == "Ω"
    assert r.display() == "-27.965 kΩ"


def test_393_status_bits():
    r = decode_393(frame393(0x02, 1, 1234, negative=True), 0)
    assert r.value == pytest.approx(-1.234)
    r = decode_393(frame393(0x02, 0, 0, overload=True), 0)
    assert r.overload and r.value == OVERLOAD and r.display() == "OL"
    r = decode_393(frame393(0x02, 0, 0, overload=True, negative=True), 0)
    assert r.value == -OVERLOAD and r.display() == "-OL"
    assert decode_393(frame393(0x02, 0, 70000), 0).value == pytest.approx(7.0)   # 17th bit


@pytest.mark.parametrize("func,rng,count,value,unit", [
    (0x03, 0, 12345, 0.12345, "V"), (0x01, 3, 7500, 750.0, "V"),
    (0x04, 0, 10000, 100.0, "Ω"), (0x04, 5, 47000, 47e6, "Ω"),
    (0x05, 0, 1000, 1e-9, "F"), (0x05, 6, 6000, 6e-3, "F"),
    (0x06, 2, 10000, 1e3, "Hz"), (0x06, 6, 1000, 1e6, "Hz"),
    (0x07, 1, 6000, 600e-6, "A"), (0x11, 0, 12345, 12.345e-3, "A"),
    (0x12, 1, 1500, 1.5, "A"), (0x09, 1, 251, 25.1, "°C"), (0x0F, 0, 6500, 0.65, "V"),
    (0x16, 0, 500, 50.0, "%"), (0x1E, 0, 1234, 12.34e-9, "S"),
])
def test_393_scales(func, rng, count, value, unit):
    r = decode_393(frame393(func, rng, count), 0)
    assert r.value == pytest.approx(value) and r.unit == unit and not r.text


def test_393_unknown_range_is_not_guessed():
    r = decode_393(frame393(0x09, 5, 251), 0)
    assert "unknown range" in r.text and math.isnan(r.value)


def test_393_parser_resyncs():
    good = frame393(0x02, 0, 1000)
    bad_end = good[:-1] + b"\x06"
    buf = bytearray(b"\xf0\x11\x33" + bad_end + good + good[:4])
    assert parse_393(buf) == [good]
    assert bytes(buf) == good[:4]
    buf += good[4:]
    assert parse_393(buf) == [good] and not buf


def test_393_driver_uses_the_newest_frame():
    t = SimBK393()
    t.PERIOD = 1e9
    t._next = float("inf")
    d = BK393(t)
    t._out += frame393(0x02, 0, 1) + frame393(0x02, 0, 2)
    assert d.poll(0.0).value == pytest.approx(2e-4)
    t._out += frame393(0x02, 0, 3)[:5]
    d.FRAME_S = 0.05
    with pytest.raises(TimeoutError, match="RS232 key"):
        d.poll(0.0)
    t._out += frame393(0x02, 0, 3)[5:]              # the rest of a split frame
    assert d.poll(0.0).value == pytest.approx(3e-4)


# ---------------------------------------------------------------- 393B (APPA)
def test_appa_framing():
    assert appa_frame(0x01).hex(" ") == "55 55 01 00 ab"
    f = appa_frame(0x01, b"\x01\x02")
    assert f[-1] == sum(f[:-1]) & 0xFF
    bad = bytearray(f)
    bad[-1] ^= 1
    buf = bytearray(b"\x55\x10" + bytes(bad) + f + f[:3])
    assert parse_appa(buf) == [(1, b"\x01\x02")] and bytes(buf) == f[:3]


def test_appa_display_decode():
    r = decode_display(bytes([0x02, 0x81]) + line(-49990, 4, 1) + line(0x700000, 0, 0), 0)
    assert r.function == "DCV" and r.display() == "-4.9990 V" and r.value == pytest.approx(-4.999)
    assert r.flags == ["AUTO"] and r.secondary == ""
    r = decode_display(bytes([0x01, 0x02]) + line(12345, 2, 2) + line(5999, 2, 0x11, 0x01), 0)
    assert r.value == pytest.approx(0.12345) and r.native == "mV"
    assert r.secondary == "Freq +59.99 Hz" and r.flags == ["Manual range"]
    r = decode_display(bytes([0x05, 0x80]) + line(0, 2, 0x0D, overload=True) + line(0, 0, 0), 0)
    assert r.overload and r.display() == "OL" and r.value == OVERLOAD
    r = decode_display(bytes([0x05, 0x80]) + line(0x70000D, 0, 0) + line(0, 0, 0), 0)
    assert r.text == "PROBE" and math.isnan(r.value) and r.display() == "PROBE"
    r = decode_display(bytes([0x02, 0x80]) + line(100, 2, 1, 0x1A) + line(0, 0, 0), 0)
    assert "HOLD" in r.flags


@pytest.mark.parametrize("legacy", [False, True])
def test_393b_driver(legacy):
    d = BK393B(SimBK393B(legacy=legacy))
    caps = d.identify()
    assert d.legacy is legacy and "SIM00001" in caps.idn
    r = d.poll(0.0)
    assert r.function == "DCV" and r.unit == "V" and abs(r.value - 5) < 0.01
    assert "AUTO" in r.flags


def test_393b_never_writes():
    t = SimBK393B()
    d = BK393B(t)
    d.identify()
    d.poll(0.0)
    assert {f[2] for f in t.sent} <= {0x00, 0x01}
    n = len(t.sent)
    for line_ in ("80", "a0", "8a 00", "05", "1a", "zz", ""):
        assert d.raw(line_).startswith("read commands only")
    with pytest.raises(ValueError):
        d.query(0x80)
    assert len(t.sent) == n
    assert bytes.fromhex(d.raw("00"))[:4] == b"393B"


def test_393b_silent_meter():
    t = SimBK393B()
    t.write = lambda data: None
    d = BK393B(t)
    d.REPLY_S = 0.05
    with pytest.raises(TimeoutError, match="No answer"):
        d.identify()


# ---------------------------------------------------------------- routing
def test_units():
    assert split_unit("kΩ") == ("Ω", 1e3) and split_unit("µA") == ("A", 1e-6)
    for plain in ("°C", "dBm", "%", "min", "Ω", ""):
        assert split_unit(plain) == (plain, 1.0)


def test_resource_routing(monkeypatch):
    assert model_for("BK393::/dev/ttyUSB0") == "BK393"
    assert model_for("BK393B::/dev/ttyUSB0") == "BK393B"
    assert model_for("SIM::BK393B#2") == "BK393B"
    assert serial_path("BK393B::/dev/ttyUSB1") == "/dev/ttyUSB1"
    assert describe("BK393::/dev/ttyUSB9") == "B&K 393 ttyUSB9"
    assert isinstance(make_driver("auto", SimBK393(), "SIM::BK393"), BK393)
    assert isinstance(make_driver("auto", SimBK393B(), "SIM::BK393B"), BK393B)
    for name, key in (("393", "BK393"), ("393a", "BK393"), ("393B", "BK393B")):
        assert resolve_model(name) == key and config.norm_model(name.upper()) == key
    assert config.norm_model("BK393B") == "BK393B"


def test_parse_target():
    pytest.importorskip("PyQt6")
    from ee_workbench.gui.main_window import parse_target
    assert parse_target("393A@/dev/ttyUSB0") == ("BK393", "BK393::/dev/ttyUSB0")
    assert parse_target("393B@/dev/ttyUSB0") == ("BK393B", "BK393B::/dev/ttyUSB0")
    assert parse_target("bk393b@ASRL/dev/ttyUSB0::INSTR") == ("BK393B", "BK393B::/dev/ttyUSB0")
    assert parse_target("BK393@SIM::BK393") == ("BK393", "SIM::BK393")


def test_serial_settings(monkeypatch):
    from ee_workbench import transport
    seen = []
    monkeypatch.setattr(transport, "SerialStream", lambda p, baud: seen.append((p, baud)))
    transport.open_transport("BK393::/dev/ttyUSB0")
    transport.open_transport("ASRL/dev/ttyUSB1::INSTR", 115200, "BK393B")
    assert seen == [("/dev/ttyUSB0", 9600), ("/dev/ttyUSB1", 9600)]


def test_signals_hide_overload():
    r = decode_393(frame393(0x02, 0, 0, overload=True), 1.0)
    assert math.isnan(signals_of("dmm", r)["value"][1][0])


# ---------------------------------------------------------------- GUI (offscreen)
_APP = []           # keep the QApplication alive: other test files share its objects


@pytest.mark.parametrize("res", ["SIM::BK393", "SIM::BK393B"])
def test_panel(res, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qt = pytest.importorskip("PyQt6.QtWidgets")
    app = qt.QApplication.instance() or qt.QApplication([])
    _APP.append(app)
    from ee_workbench.config import Config
    from ee_workbench.gui.dmm_panel import HandheldDmmPanel
    from ee_workbench.gui.main_window import MainWindow
    win = MainWindow([("auto", res)], Config(), None)
    try:
        p = win.panels()[0]
        assert isinstance(p, HandheldDmmPanel)
        end = time.monotonic() + 8
        while p.readings < 2 and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
        assert p.readings >= 2 and p.readout.value.text().endswith(" V")
        assert p.info["Function"].text() == "DCV" and p.null_btn.isHidden()
        assert win.tabs.tabText(0).startswith(p.model.removeprefix("BK"))
        p.redraw()
        assert p.stat_labels["Mean"].text()[:4] in ("+4.9", "+5.0")
        p.restore_settings({"name": "rail", "poll_ms": 500})
        assert p.session_settings() == {"name": "rail", "poll_ms": 500}
        p.worker.poll_enabled = False
        app.processEvents()
        # a knob turn starts a new trend; overload leaves a gap
        p.on_reading(decode_393(frame393(0x04, 2, 10000), 1e6))
        assert p.unit == "Ω" and p.buf.n == 1 and p.readout.value.text() == "+10.000 kΩ"
        p.on_reading(decode_393(frame393(0x04, 2, 0, overload=True), 1e6 + 1))
        assert p.readout.value.text() == "OL" and p.ovld_label.text()
        p.redraw()
        hold = bytes([0x05, 0x80]) + line(100, 2, 0x0D, 0x1A) + line(0, 0, 0)
        p.on_reading(decode_display(hold, 1e6 + 2))
        assert p.readout.caption.text() == "Ω · HOLD" and p.buf.n == 1
    finally:
        win.close()
