import struct
import time

import numpy as np
import pytest

from otterbench.config import norm_model
from otterbench.instruments import make_driver, model_for
from otterbench.instruments.saleae import (DigitalTrace, LogicConfig, SaleaeLogic,
                                             analog_stats, digital_envelope, digital_stats,
                                             parse_analog, parse_digital, parse_resource,
                                             read_export)
from otterbench.sim.slogic import (UART_TEXT, SimLogic2, channel_edges, write_analog,
                                     write_digital)


# ---------------------------------------------------------------- file format
@pytest.mark.parametrize("version", [0, 1])
def test_digital_roundtrip(tmp_path, version):
    p = tmp_path / "digital_3.bin"
    write_digital(p, 1, -0.5, 2.0, np.array([0.1, 0.4, 1.0]), 10e6, version)
    tr = parse_digital(p.read_bytes(), 3)
    assert tr.channel == 3 and tr.end == 2.0
    np.testing.assert_allclose(tr.t, [-0.5, 0.1, 0.4, 1.0])
    assert list(tr.level) == [1, 0, 1, 0]
    assert tr.level_at(0.0) == 1 and tr.level_at(0.2) == 0 and tr.level_at(5) == 0


@pytest.mark.parametrize("version", [0, 1])
def test_analog_roundtrip(tmp_path, version):
    p = tmp_path / "analog_0.bin"
    v = np.linspace(0, 1, 1000)
    write_analog(p, 0.25, 1_000_000, 4, v, version)
    tr = parse_analog(p.read_bytes())
    assert tr.t0 == 0.25 and tr.dt == pytest.approx(4e-6)
    np.testing.assert_allclose(tr.v, v, rtol=1e-6)


def test_digital_v1_chunks_merge():
    """Two chunks; the second starts at the level the first ended on, so the
    chunk boundary must not be counted as an edge."""
    buf = b"<SALEAE>" + struct.pack("<iiQ", 1, 0, 2)
    buf += struct.pack("<IdddQ", 0, 1e6, 0.0, 1.0, 1) + struct.pack("<d", 0.5)
    buf += struct.pack("<IdddQ", 1, 1e6, 1.2, 2.0, 1) + struct.pack("<d", 1.5)
    tr = parse_digital(buf)
    np.testing.assert_allclose(tr.t, [0.0, 0.5, 1.5])
    assert list(tr.level) == [0, 1, 0] and tr.end == 2.0


def test_bad_files():
    with pytest.raises(ValueError):
        parse_digital(b"not a saleae file at all")
    with pytest.raises(ValueError):
        parse_digital(b"<SALEAE>" + struct.pack("<ii", 7, 0))
    with pytest.raises(ValueError):
        parse_analog(b"<SALEAE>" + struct.pack("<ii", 0, 0))


def test_read_export_ignores_other_files(tmp_path):
    write_digital(tmp_path / "digital_1.bin", 0, 0, 1, np.array([0.5]), 1e6)
    write_analog(tmp_path / "analog_2.bin", 0, 1000, 1, np.zeros(10))
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    d, a = read_export(str(tmp_path))
    assert list(d) == [1] and list(a) == [2]


# ---------------------------------------------------------------- measurements
def square(f, duty, a, b, rate=100e6):
    """DigitalTrace of a square wave starting low at a."""
    p = 1 / f
    k = np.arange(0, int((b - a) * f) + 2)
    rises, falls = a + k * p + p * (1 - duty), a + (k + 1) * p
    t = np.sort(np.concatenate((rises, falls)))
    t = t[t <= b]
    lv = (np.arange(t.size) + 1) % 2
    return DigitalTrace(0, np.concatenate(([a], t)), np.concatenate(([0], lv)).astype(np.uint8), b)


def test_digital_stats_square():
    tr = square(1000, 0.25, 0.0, 0.1)
    s = digital_stats(tr)
    assert s["freq"] == pytest.approx(1000, rel=1e-9)
    assert s["duty"] == pytest.approx(0.25, abs=0.005)
    assert s["high_min"] == pytest.approx(0.25e-3) and s["high_max"] == pytest.approx(0.25e-3)
    assert s["low_min"] == pytest.approx(0.75e-3)
    assert s["edges"] == 200


def test_digital_stats_window_and_idle():
    tr = square(1000, 0.5, 0.0, 0.1)
    s = digital_stats(tr, 0.0101, 0.0108)        # less than one full period: one rise
    assert s["freq"] is None and s["edges"] == 1 and s["rising"] == 1
    s = digital_stats(tr, 0.0101, 0.0149)        # whole periods inside
    assert s["freq"] == pytest.approx(1000) and s["edges"] == 9
    idle = DigitalTrace(0, np.array([0.0]), np.array([1], np.uint8), 1.0)
    s = digital_stats(idle)
    assert s["edges"] == 0 and s["duty"] == 1.0 and s["level"] == 1


def test_analog_stats_window():
    from otterbench.instruments.saleae import AnalogTrace
    tr = AnalogTrace(0, 0.0, 1e-3, np.arange(1000, dtype=np.float32))
    s = analog_stats(tr, 0.1, 0.2)
    assert s["min"] == 100 and s["max"] == 200 and s["mean"] == pytest.approx(150)
    assert analog_stats(tr, 5, 6) is None


def test_envelope_exact_and_binned():
    tr = DigitalTrace(0, np.array([0.0, 1.0, 2.0]), np.array([0, 1, 0], np.uint8), 3.0)
    x, y = digital_envelope(tr, -1, 10)
    np.testing.assert_allclose(x, [0, 1, 1, 2, 2, 3])
    np.testing.assert_allclose(y, [0, 0, 1, 1, 0, 0])
    fast = square(1e6, 0.5, 0.0, 0.01)              # 20000 edges into 100 bins
    x, y = digital_envelope(fast, 0, 0.01, bins=100)
    assert x.size == 201 and y.min() == 0 and y.max() == 1
    assert np.all(np.diff(x) >= 0)
    # a single glitch in a long quiet stretch survives binning
    t = np.concatenate(([0.0], np.linspace(0.5, 0.5 + 1e-6, 2001)))
    lv = np.concatenate(([0], (np.arange(2001) + 1) % 2)).astype(np.uint8)
    x, y = digital_envelope(DigitalTrace(0, t, lv, 1.0), 0, 1, bins=1000)
    assert y.max() == 1


# ---------------------------------------------------------------- resources
def test_parse_resource():
    assert parse_resource("SALEAE::F4241") == ("F4241", "127.0.0.1", 10430)
    assert parse_resource("SALEAE::") == ("", "127.0.0.1", 10430)
    assert parse_resource("SALEAE::ABC@lab-pc:10431") == ("ABC", "lab-pc", 10431)
    assert model_for("SALEAE::F4241") == "SALEAE"
    assert model_for("SIM::SALEAE") == "SALEAE"
    assert norm_model("saleae") == "SALEAE"


def test_parse_target():
    from otterbench.gui.main_window import parse_target
    assert parse_target("SALEAE@F4241") == ("SALEAE", "SALEAE::F4241")
    assert parse_target("SALEAE@") == ("SALEAE", "SALEAE::")
    assert parse_target("SALEAE::F4241@host:10430") == ("SALEAE", "SALEAE::F4241@host:10430")


# ---------------------------------------------------------------- driver + sim
@pytest.fixture
def drv():
    d = make_driver("auto", SimLogic2(speed=20), "SIM::SALEAE")
    d.identify()
    return d


def run(d, cfg, timeout=5.0):
    d.arm(cfg)
    end = time.monotonic() + timeout
    while not d.capture_done():
        assert time.monotonic() < end, "capture never finished"
        time.sleep(0.005)
    return d.fetch()


def test_identify(drv):
    assert isinstance(drv, SaleaeLogic)
    c = drv.caps
    assert c.digital == 16 and c.analog == 16 and c.thresholds == [1.2, 1.8, 3.3]
    assert "Logic Pro 16" in c.idn and drv.device_id == "SIM16"
    assert drv.cfg.threshold == 3.3 and drv.cfg.digital_rate in c.digital_rates


def test_unknown_device():
    d = SaleaeLogic(SimLogic2(), "SALEAE::NOPE")
    with pytest.raises(RuntimeError, match="doesn't see analyzer NOPE"):
        d.identify()


def test_timed_capture(drv):
    res = run(drv, LogicConfig(digital=[1, 2, 7], analog=[0], digital_rate=10e6,
                               analog_rate=1.5625e6, duration=0.02))
    assert set(res.digital) == {1, 2, 7} and set(res.analog) == {0}
    s1, s2 = digital_stats(res.digital[1]), digital_stats(res.digital[2])
    assert s1["freq"] == pytest.approx(1000, rel=1e-6) and s1["duty"] == pytest.approx(0.25, abs=0.02)
    assert s2["freq"] == pytest.approx(10_000, rel=1e-6)
    assert digital_stats(res.digital[7])["edges"] == pytest.approx(20, abs=1)   # shared pin
    a = analog_stats(res.analog[0])
    assert a["min"] == pytest.approx(0, abs=0.1) and a["max"] == pytest.approx(3.3, abs=0.1)
    assert res.span == pytest.approx((0.0, 0.02))
    assert drv.have_capture


@pytest.mark.parametrize("version", [0, 1])
def test_trigger_capture_is_relative_to_trigger(version):
    d = SaleaeLogic(SimLogic2(speed=20, version=version))
    d.identify()
    res = run(d, LogicConfig(digital=[1], mode="trigger", trig_channel=1, trig_type="falling",
                             pre_trigger=0.002, after_trigger=0.003))
    tr = res.digital[1]
    assert tr.t[0] == pytest.approx(-0.002) and tr.end == pytest.approx(0.003)
    assert tr.level_at(-1e-7) == 1 and tr.level_at(1e-7) == 0      # the falling edge at 0


def test_manual_capture_and_decode(drv):
    cfg = LogicConfig(digital=[0], mode="manual",
                      decoders=[{"name": "Async Serial", "label": "uart",
                                 "settings": {"Input Channel": 0, "Bit Rate (Bits/s)": 115200}}])
    drv.arm(cfg)
    time.sleep(0.01)              # 0.2 s of simulated time at speed 20
    assert not drv.capture_done()
    drv.stop_capture()
    assert drv.capture_done()
    res = drv.fetch()
    assert res.table_header[:4] == ["name", "type", "start_time", "duration"]
    data = bytes(int(r[4], 16) for r in res.table_rows)
    assert UART_TEXT in data and res.table_rows[0][0] == "uart"
    # decoders can run again on the held capture
    header, rows = drv.decode([{"name": "I2C", "settings": {"SCL": 0, "SDA": 1}}])
    assert header and rows == []


def test_abort_releases_wait(drv):
    drv.t.speed = 1.0
    drv.arm(LogicConfig(digital=[0], duration=30))
    drv.abort()
    assert drv.t.cap is None and drv._run is None and not drv.have_capture
    # and a new capture starts cleanly
    drv.t.speed = 20
    assert run(drv, LogicConfig(digital=[0], duration=0.01)).digital


def test_new_capture_closes_previous(drv):
    run(drv, LogicConfig(digital=[2], duration=0.01))
    run(drv, LogicConfig(digital=[2], duration=0.01))
    assert drv.t.calls.count("close") >= 1


def test_rejected_settings(drv):
    with pytest.raises(ValueError, match="at least one channel"):
        drv.arm(LogicConfig(digital=[], analog=[]))
    with pytest.raises(ValueError, match="trigger channel"):
        drv.arm(LogicConfig(digital=[0], mode="trigger", trig_channel=3))
    with pytest.raises(RuntimeError, match="sample rate"):
        drv.arm(LogicConfig(digital=[0], digital_rate=33e6))


def test_save_and_csv(drv, tmp_path):
    with pytest.raises(RuntimeError, match="Capture something first"):
        drv.save(str(tmp_path / "x.sal"))
    run(drv, LogicConfig(digital=[0, 2], duration=0.01))
    drv.save(str(tmp_path / "x.sal"))
    drv.export_csv(str(tmp_path))
    assert (tmp_path / "x.sal").exists()
    lines = (tmp_path / "digital.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "Time [s],Channel 0,Channel 2" and len(lines) > 100


def test_channel_edges_on_sample_grid():
    init, e = channel_edges(2, 0.0, 0.001, 3e6)
    assert init == 1 and e.size == 20
    np.testing.assert_allclose(e * 3e6, np.round(e * 3e6))


def test_worker_capture_end_to_end():
    """The real InstrumentWorker loop: connect, arm, wait, fetch."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QCoreApplication

    from otterbench.worker import InstrumentWorker
    app = QCoreApplication.instance() or QCoreApplication([])
    w = InstrumentWorker("SIM::SALEAE", "auto")
    w.poll_enabled = False
    got = {}
    w.connected.connect(lambda fam, caps, st: got.setdefault("caps", caps))
    w.capture_done.connect(lambda r: got.setdefault("res", r))
    w.error.connect(lambda e: got.setdefault("err", e))
    w.start()
    end = time.monotonic() + 5
    while "caps" not in got and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    w.request_capture(LogicConfig(digital=[1], duration=0.05))
    while "res" not in got and "err" not in got and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    w.stop()
    assert "err" not in got, got.get("err")
    assert got["res"].digital[1].t.size > 50
