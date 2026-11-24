import time

import numpy as np
import pytest

from otterbench.instruments import make_driver, model_for
from otterbench.instruments.logic import LogicConfig, digital_stats
from otterbench.instruments.sigrok import (SigrokLink, SigrokLogic, device_spec,
                                             parse_annotations, parse_hz, parse_scan,
                                             parse_show, read_sr, sigrok_cli, trigger_index,
                                             traces_from_samples, write_sr)
from otterbench.sim.slogic import UART_TEXT
from otterbench.sim.ssigrok import SHOW, SimSigrokCli

# Real sigrok-cli 0.7.2 output (demo device)
DEMO_SHOW = """demo - Demo device with 13 channels: D0 D1 D2 D3 D4 D5 D6 D7 A0 A1 A2 A3 A4
Supported configuration options across all channel groups:
    continuous: on, off
    samplerate (1 Hz - 1 GHz in steps of 1 Hz)
    Supported triggers: 0 1 r f e
    captureratio: 20 (current)
"""


def test_parsers():
    assert parse_hz("24 MHz") == 24e6 and parse_hz("20 kHz") == 20e3 and parse_hz("1 Hz") == 1
    devs = parse_scan("The following devices were found:\n"
                      "demo - Demo device with 13 channels: D0 D1 D2 D3 D4 D5 D6 D7 A0 A1 A2 A3 A4\n"
                      "fx2lafw:conn=3.10 - Saleae Logic with 8 channels: D0 D1 D2 D3 D4 D5 D6 D7")
    assert [d["spec"] for d in devs] == ["demo", "fx2lafw:conn=3.10"]
    assert devs[1]["channels"][-1] == "D7"
    listed = parse_show(SHOW)
    assert listed["rates"][0] == 24e6 and listed["rates"][-1] == 20e3
    assert listed["triggers"] == ["rising", "falling", "either", "high", "low"]
    ranged = parse_show(DEMO_SHOW)
    assert 24e6 in ranged["rates"] and ranged["captureratio"]
    assert device_spec("SIGROK::fx2lafw:conn=1.4") == "fx2lafw:conn=1.4"
    assert model_for("SIGROK::fx2lafw") == "SIGROK" and model_for("SIM::SIGROK") == "SIGROK"


def test_sr_roundtrip_and_bit_numbering(tmp_path):
    data = np.array([0b001, 0b101, 0b100, 0b000], np.uint8)
    write_sr(str(tmp_path / "x.sr"), 1e6, {0: "D0", 2: "D2"}, data)
    rate, names, back = read_sr(str(tmp_path / "x.sr"))
    assert rate == 1e6 and names == {0: "D0", 2: "D2"}
    np.testing.assert_array_equal(back, data)
    wide = np.array([1, 1 << 12, 0], np.uint16)          # 16-channel analyzers: unitsize 2
    write_sr(str(tmp_path / "w.sr"), 1e6, {0: "D0", 12: "D12"}, wide)
    np.testing.assert_array_equal(read_sr(str(tmp_path / "w.sr"))[2], wide)


def test_traces_and_trigger_index():
    bits = np.array([0, 0, 1, 1, 0, 1, 1, 1], np.uint8)
    assert trigger_index(bits, "rising") == 2 and trigger_index(bits, "falling") == 4
    assert trigger_index(bits, "high") == 2 and trigger_index(np.ones(3, np.uint8), "rising") == 0
    tr = traces_from_samples(bits, 1e6, {0: 0}, trig=2)[0]
    np.testing.assert_allclose(tr.t, [-2e-6, 0, 2e-6, 3e-6])
    assert list(tr.level) == [0, 1, 0, 1] and tr.end == pytest.approx(6e-6)


def test_annotations():
    rows = parse_annotations("509-579 uart-1: 48\n596-666 uart-1: 69\njunk\n", "tx", "uart",
                             1e6, 9)
    assert rows[0] == ["tx", "uart", "0.000500000", "0.000070000", "48"] and len(rows) == 2


# ---------------------------------------------------------------- driver + simulator
@pytest.fixture
def drv():
    d = make_driver("SIGROK", SimSigrokCli(speed=50), "SIM::SIGROK")
    d.identify()
    yield d
    d.close()


def run(d, cfg, timeout=5.0):
    d.arm(cfg)
    end = time.monotonic() + timeout
    while not d.capture_done():
        assert time.monotonic() < end
        time.sleep(0.002)
    return d.fetch()


def test_identify(drv):
    c = drv.caps
    assert drv.spec == "fx2lafw:conn=1.4" and c.digital == 8 and c.analog == 0
    assert c.channel_names[3] == "D3" and c.capture_suffix == ".sr" and "UART" in c.decoders
    assert 24e6 in c.digital_rates and drv.cfg.digital_rate == 1e6


def test_missing_device():
    d = SigrokLogic(SimSigrokCli(), "SIGROK::dreamsourcelab-dslogic")
    with pytest.raises(RuntimeError, match="finds no dreamsourcelab-dslogic"):
        d.identify()


def test_timed_capture_and_args(drv):
    res = run(drv, LogicConfig(digital=[1, 2], digital_rate=4e6, duration=0.01))
    args = drv.t.calls[-1]
    assert "--samples" in args and args[args.index("--samples") + 1] == "40000"
    assert args[args.index("--channels") + 1] == "D1,D2"
    assert digital_stats(res.digital[1])["freq"] == pytest.approx(1000, rel=1e-3)
    assert digital_stats(res.digital[2])["freq"] == pytest.approx(10_000, rel=1e-3)
    assert res.span == pytest.approx((0, 0.01))


def test_trigger_capture(drv):
    res = run(drv, LogicConfig(digital=[1], digital_rate=1e6, mode="trigger", trig_channel=1,
                               trig_type="falling", pre_trigger=0.002, after_trigger=0.008))
    args = drv.t.calls[-1]
    assert args[args.index("--triggers") + 1] == "D1=f"
    assert "captureratio=20" in args[args.index("--config") + 1]
    tr = res.digital[1]
    # sigrok fires on the first falling edge after starting: here 0.95 ms in,
    # so there is less pre-trigger data than the 2 ms asked for
    assert tr.t[0] == pytest.approx(-0.00095, abs=2e-6)
    assert tr.level_at(-1.5e-6) == 1 and tr.level_at(0.5e-6) == 0
    assert tr.end == pytest.approx(0.01 - 0.00095, abs=2e-6)


def test_manual_capture_and_uart_decode(drv):
    drv.arm(LogicConfig(digital=[0], digital_rate=2e6, mode="manual",
                        decoders=[{"name": "UART", "label": "dbg",
                                   "settings": {"rx": 0, "baudrate": 115200}}]))
    time.sleep(0.002)                       # 0.1 s of simulated time at speed 50
    drv.stop_capture()
    res = drv.fetch()
    data = bytes(int(r[4], 16) for r in res.table_rows)
    assert UART_TEXT in data and res.table_rows[0][0] == "dbg"
    assert any("rx=D0" in a for a in drv.t.calls[-1])


def test_decoder_errors(drv):
    run(drv, LogicConfig(digital=[0], duration=0.005))
    with pytest.raises(RuntimeError, match="No channel"):
        drv.decode([{"name": "UART", "settings": {"rx": 7, "baudrate": 9600}}])
    with pytest.raises(RuntimeError, match="Failed to load"):
        drv.decode([{"name": "nosuch", "settings": {}}])


def test_limits_and_validation(drv):
    with pytest.raises(ValueError, match="limit"):
        drv.arm(LogicConfig(digital=[0], digital_rate=24e6, duration=60))
    with pytest.raises(ValueError, match="trigger channel"):
        drv.arm(LogicConfig(digital=[0], mode="trigger", trig_channel=2))


def test_abort_and_save(drv, tmp_path):
    drv.t.speed = 1
    drv.arm(LogicConfig(digital=[0], duration=30))
    drv.abort()
    assert drv.capture_done() and not drv.have_capture
    drv.t.speed = 50
    run(drv, LogicConfig(digital=[0, 3], duration=0.002))
    drv.save(str(tmp_path / "c.sr"))
    assert read_sr(str(tmp_path / "c.sr"))[1] == {0: "D0", 3: "D3"}
    drv.export_csv(str(tmp_path))
    assert (tmp_path / "digital.csv").read_text().startswith("time,D0,D3")


# ---------------------------------------------------------------- the real program
real = pytest.mark.skipif(not sigrok_cli(), reason="sigrok-cli not installed")


@real
def test_real_sigrok_demo(tmp_path):
    d = SigrokLogic(SigrokLink(), "SIGROK::demo")
    try:
        caps = d.identify()
        assert caps.digital == 8 and "falling" in caps.trigger_types
        res = run(d, LogicConfig(digital=[0, 3], digital_rate=1e6, mode="trigger",
                                 trig_channel=3, trig_type="falling", pre_trigger=2e-4,
                                 after_trigger=1e-3))
        tr = res.digital[3]
        assert tr.level_at(-0.5e-6) == 1 and tr.level_at(0.5e-6) == 0
        d.arm(LogicConfig(digital=[0], digital_rate=100e3, mode="manual"))
        time.sleep(0.3)
        d.stop_capture()
        assert d.fetch().span[1] > 0.1
        header, rows = d.decode([{"name": "UART", "settings": {"rx": 0, "baudrate": 9600}}])
        assert header[2] == "start_time"
        d.export_csv(str(tmp_path))
        assert "D0" in (tmp_path / "digital.csv").read_text()[:400]
    finally:
        d.close()
