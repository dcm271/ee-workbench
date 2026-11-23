"""DUT serial console: driver, patterns, sequencer steps, tab, session."""
import math
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from ee_workbench.clock import now  # noqa: E402
from ee_workbench.gui.main_window import parse_target  # noqa: E402
from ee_workbench.instruments import make_driver, model_for  # noqa: E402
from ee_workbench.instruments import uart as u  # noqa: E402
from ee_workbench.sequencer import Bench, Inst, Plan, SequenceError, Step, run_plan  # noqa: E402
from ee_workbench.session import SessionReader, recorder  # noqa: E402
from ee_workbench.sim import open_sim  # noqa: E402
from ee_workbench.signals import signals_of  # noqa: E402


class Link:
    """Feeds exact bytes."""
    resource = "test"

    def __init__(self):
        self.inbox, self.sent = b"", []

    def read_available(self):
        out, self.inbox = self.inbox, b""
        return out

    def write(self, data):
        self.sent.append(data)

    def set_baud(self, b):
        self.baud = b

    def set_line(self, n, on):
        self.line = (n, on)


def drv():
    d = u.UartDriver(Link())
    d.identify()
    return d


def feed(d, data):
    d.t.inbox += data
    d._take()


def test_resources_and_model():
    assert parse_target("UART::/dev/ttyUSB0@9600") == ("UART", "UART::/dev/ttyUSB0@9600")
    assert parse_target("UART@/dev/ttyUSB0@921600") == ("UART", "UART::/dev/ttyUSB0@921600")
    assert u.parse_resource("UART::/dev/ttyACM0") == ("/dev/ttyACM0", 115200)
    assert model_for("UART::/dev/ttyUSB0@9600") == "UART" == model_for("SIM::UART")


def test_line_splitting():
    d = drv()
    feed(d, b"one\r\ntwo\nthr")
    feed(d, b"ee\r\n\r\nfour\n")
    assert [x.text for x in d.poll(0).lines] == ["one", "two", "three", "", "four"]
    feed(d, b"bare cr\r")
    assert d.poll(0).lines == []              # may be half of CR LF
    time.sleep(0.04)
    assert [x.text for x in d.poll(0).lines] == ["bare cr"]
    feed(d, b"bad \xff utf8\n" + b"x" * (u.MAX_LINE + 10))
    feed(d, b"\n")
    lines = [x.text for x in d.poll(0).lines]
    assert lines[0].startswith("bad ") and len(lines[1]) == u.MAX_LINE and len(lines[2]) == 10


def test_crlf_split_across_reads_is_one_line_end():
    d = drv()
    feed(d, b"a\r")
    feed(d, b"\nb\r\n")
    assert [x.text for x in d.poll(0).lines] == ["a", "b"]


def test_patterns_make_signals():
    d = drv()
    d.set_patterns([u.Pattern("vbat", r"vbat=([\d.]+)", "V"), u.Pattern("panic", "PANIC"),
                    u.Pattern("reg", r"reg=(0x[0-9a-f]+)")])
    feed(d, b"vbat=3.71 reg=0x1f\nvbat=3.70\nPANIC at 0\nvbat=oops\n")
    sig = signals_of("uart", d.poll(0))
    assert list(sig["vbat"][1]) == [3.71, 3.70]
    assert list(sig["panic"][1]) == [1.0] and list(sig["reg"][1]) == [31.0]
    for bad in (u.Pattern("1x", "a"), u.Pattern("text", "a"), u.Pattern("a", "("),
                u.Pattern("a", "")):
        with pytest.raises(ValueError):
            d.set_patterns([bad])
    with pytest.raises(ValueError, match="two"):
        d.set_patterns([u.Pattern("a", "x"), u.Pattern("a", "y")])


def test_match_sees_only_what_came_after_the_send():
    d = drv()
    feed(d, b"adc=1\n")
    d.send("adc")
    assert d.t.sent == [b"adc\n"]
    assert d.match(r"adc=(\d+)") is None      # the old answer doesn't count
    feed(d, b"adc=2\nadc=3\n")                # the answer came before we looked
    assert d.match(r"adc=(\d+)")["value"] == 2
    assert d.match(r"adc=(\d+)")["value"] == 3
    assert d.match(r"adc=(\d+)") is None


def test_prompts():
    d = drv()
    feed(d, b"boot\n> ")
    assert d.match(r"^> $") is None           # still arriving
    time.sleep(u.PARTIAL_S + 0.02)
    assert d.match(r"^> $")["text"] == "> "
    assert d.match(r"> ") is None             # the same prompt isn't found twice
    d.send("ver")
    feed(d, b"1.2.3\n> ")                     # no echo: the answer is on the prompt's line
    assert d.match(r"^(\d+\.\d+\.\d+)")["groups"] == ["1.2.3"]
    time.sleep(u.PARTIAL_S + 0.02)
    assert d.match(r"> ")["text"] == "> "


def test_settings():
    d = drv()
    d.set("baud", 9600)
    assert d.t.baud == 9600 and d.cfg.baud == 9600
    d.set("dtr", 0)
    assert d.t.line == ("dtr", False)
    d.set_eol("CR LF")
    d.send("x")
    assert d.t.sent[-1] == b"x\r\n"
    for a in (("baud", 1), ("volume", 1)):
        with pytest.raises(ValueError):
            d.set(*a)
    with pytest.raises(ValueError):
        d.set_eol("EBCDIC")


# ---- sequencer --------------------------------------------------------------
class UartInst(Inst):
    kind, name = "uart", "DUT"
    set_names = list(u.SET_NAMES)
    setting_kinds: dict = {}

    def __init__(self, d):
        self.d = d
        self.measure_names = [p.name for p in d.state.patterns]

    def run(self, fn, timeout=30.0):
        return fn(self.d)


@pytest.fixture
def bench():
    t = open_sim("SIM::UART")
    d = make_driver("UART", t, "SIM::UART")
    d.identify()
    d.set_patterns([u.Pattern("Temp", r"temp=([\d.]+)", "°C")])
    logs = []
    b = Bench([UartInst(d)], now, log=logs.append)
    b.logs = logs
    return b


def test_send_and_wait_steps(bench):
    run_plan(Plan(steps=[
        Step("Send", "DUT", "", "adc"),
        Step("Wait for", "DUT", "", r"adc=(\d+) timeout=2", "adc", "2000", "2100"),
        Step("Send", "DUT", "", "ver"),
        Step("Wait for", "DUT", "", r"version (\S+) timeout=2", "fw"),
        Step("Send", "DUT", "", "slow"),
        Step("Wait for", "DUT", "", "done timeout=2", "slow")]), bench)
    row = bench.rows[0]
    assert 2000 < row["adc"] < 2100 and row["fw"] == "1.2.3" and row["slow"] == "done"
    assert row["result"] == "PASS"


def test_no_reply_fails_the_pass_but_goes_on(bench):
    run_plan(Plan(steps=[
        Step("Send", "DUT", "", "bogus {x}"),
        Step("Wait for", "DUT", "", r"val=(\d+) timeout=0.1", "val"),
        Step("Send", "DUT", "", "led on"),
        Step("Wait for", "DUT", "", "^OK timeout=2", "led")],
        loop="sweep", start=1, stop=2, step=1), bench)
    assert len(bench.rows) == 2 and bench.failed == 2
    assert math.isnan(bench.rows[0]["val"]) and bench.rows[0]["led"] == "OK"
    assert "no 'val=" in bench.rows[0]["failures"]
    assert b"bogus 1\n" in bench._insts[0].d.t.sent


def test_script_calls(bench):
    bench.send("DUT", "adc")
    assert bench.wait_text("DUT", r"adc=(\d+)", 2) > 2000
    assert bench.wait_text("DUT", "never", 0.05) is None
    with pytest.raises(SequenceError, match="never\\("):
        bench.wait_text("DUT", "never(", 0.05)
    v = bench.measure("DUT", "temp")
    assert 20 < v < 28
    bench.set("DUT", "dtr", "off")
    assert bench._insts[0].d.t.lines["dtr"] is False
    with pytest.raises(SequenceError):
        bench.measure("DUT", "nothing")


# ---- tab --------------------------------------------------------------------
def spin(cond, s=5.0):
    end = time.monotonic() + s
    while not cond() and time.monotonic() < end:
        _APP.processEvents()
        time.sleep(0.01)
    assert cond()


def test_tab_console_values_and_session(tmp_path):
    from ee_workbench.gui.uart_panel import UartPanel
    p = UartPanel("SIM::UART", "UART", 9600)
    try:
        p.restore_settings({"name": "DUT", "eol": "CR LF",
                            "patterns": [["temp", r"temp=([\d.]+)", "°C"], ["x", "(", ""]]})
        spin(lambda: p.connected)
        assert p.quantities() == ["temp"] and p.unit_of("temp") == "°C"
        path = str(tmp_path / "a.eewb")
        recorder.start(path)
        spin(lambda: "temp" in p.latest)
        p.raw_line.setText("ver")
        p._send_raw(None)
        spin(lambda: "version 1.2.3" in p.log_view.toPlainText())
        assert "» ver" in p.log_view.toPlainText()
        assert p._state.eol == "CR LF"
        p.redraw()
        recorder.stop()
        s = SessionReader(path)
        st = s.streams[0]
        assert ("tx", "ver") in [(d, x) for _, d, x in st.lines]
        assert 20 < st.data["temp_°C"][~(st.data["temp_°C"] != st.data["temp_°C"])][0] < 28
        s.close()
        from ee_workbench.gui.session_viewer import SessionViewer
        w = SessionViewer(path)
        assert w.serial.count() == len(st.lines) and w.side_tabs.isTabVisible(3)
        w.close()
        saved = p.session_settings()
        assert saved["patterns"] == [["temp", r"temp=([\d.]+)", "°C"]] and saved["name"] == "DUT"
    finally:
        recorder.stop()
        p.shutdown()
