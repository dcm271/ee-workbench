"""The hardware check on every simulator, and its tape played back."""
import glob
import os
import time
from dataclasses import asdict

import pytest

from otterbench import hwcheck, tape
from otterbench.instruments import make_driver, model_for
from otterbench.instruments.scpi_generic import builtin_dir, definitions, parse_definition
from otterbench.transport import open_transport

TAPES = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "tapes", "*.tape")))
SIMS = ["SIM::66321D", "SIM::6632A", "SIM::3456A", "SIM::DPS150", "SIM::BK393", "SIM::BK393B",
        "SIM::XDM1041", "SIM::FGEN", "SIM::SDG", "SIM::SCOPE", "SIM::DSOX", "SIM::TBS",
        "SIM::SDS"] + [f"SIM::{k}" for k in sorted(definitions())]


def check(resource, level=hwcheck.OUTPUT, **kw):
    """A check on a simulator, taped. Returns (rows, tape, driver)."""
    model = model_for(resource)
    t, tap = tape.wrap(open_transport(resource, model=model))
    drv = make_driver(model, t, resource)
    if tap:
        tap.tap_on = True
    caps = drv.identify()
    for k in ("GAP", "SETTLE"):
        if hasattr(drv, k):
            setattr(drv, k, 0.0)
    opts = hwcheck.Options(level=level, settle=0.0, **kw)
    chk = hwcheck.HardwareCheck(lambda fn: fn(drv), caps, opts, sleep=lambda s: None)
    rows = chk.go()
    tp = tape.new_tape(tap, [], tape.model_of(drv, model), resource, caps.idn)
    tp.options, tp.rows = asdict(opts), rows
    return rows, tp, drv


@pytest.mark.parametrize("res", SIMS)
def test_check_and_replay(res, tmp_path):
    rows, tp, drv = check(res)
    # the simulated supplies feed a simulated DUT, which the output step rightly notices
    bad = [r for r in rows if r["result"] == "FAIL" and " on at " not in r["step"]]
    assert bad == []
    assert hwcheck.outputs_on(drv) in (False, None)
    if not tp.events:
        return                       # 1-Wire and SPI sensors have no link to tape
    p = str(tmp_path / "x.tape")
    tape.save(tp, p)
    r = hwcheck.replay(p)
    assert r.problems == [] and r.unknown == []


@pytest.mark.parametrize("path", TAPES or [None])
def test_tapes_of_real_instruments(path):
    """Tapes in tests/tapes: what a driver makes of them must not change."""
    if path is None:
        pytest.skip("no tapes yet (tests/tapes)")
    r = hwcheck.replay(path)
    assert r.problems == []


def test_read_level_sends_no_settings():
    rows, tp, _ = check("SIM::DP832", hwcheck.READ)
    sent = [e[1] for e in tp.events if e[0] == "w"]
    assert all(c.startswith("*CLS") or "?" in c for c in sent), sent
    assert len(rows) == 4


def test_a_live_output_is_left_alone():
    t = open_transport("SIM::DP832")
    drv = make_driver("SCPI:DP832", t, "SIM::DP832")
    caps = drv.identify()
    drv.set("out1", True)
    rows = hwcheck.HardwareCheck(lambda fn: fn(drv), caps, hwcheck.Options("output"),
                                 sleep=lambda s: None).go()
    assert rows[-1]["step"] == "Outputs are off" and rows[-1]["result"] == "FAIL"
    assert drv.read_setting(drv.d.setting("out1")) is True      # not switched either


def test_output_goes_off_and_settings_come_back_when_a_step_fails():
    t = open_transport("SIM::66321D")
    drv = make_driver("663xx", t, "SIM::66321D")
    caps = drv.identify()
    drv.set_voltage(3.3)
    real, n = drv.poll, [0]

    def poll(*a):
        n[0] += 1
        if n[0] > hwcheck.POLLS:
            raise RuntimeError("cable out")
        return real(*a)
    drv.poll = poll
    rows = hwcheck.HardwareCheck(lambda fn: fn(drv), caps, hwcheck.Options("output"),
                                 sleep=lambda s: None).go()
    assert hwcheck.verdict(rows) == "FAIL"
    s = drv.read_setpoints()
    assert s.output is False and s.voltage == pytest.approx(3.3)


def test_stop_switches_off():
    from otterbench.sequencer import Stopped
    t = open_transport("SIM::66321D")
    drv = make_driver("663xx", t, "SIM::66321D")
    caps = drv.identify()

    def sleep(_s):
        raise Stopped()
    with pytest.raises(Stopped):
        hwcheck.HardwareCheck(lambda fn: fn(drv), caps, hwcheck.Options("output"),
                              sleep=sleep).go()
    assert drv.read_setpoints().output is False


def test_wrong_readback_and_errors_fail():
    t = open_transport("SIM::DP832")
    drv = make_driver("SCPI:DP832", t, "SIM::DP832")
    caps = drv.identify()
    real = drv.read_setting
    drv.read_setting = lambda s: 0.123 if s.name == "vset1" else real(s)
    rows = hwcheck.HardwareCheck(lambda fn: fn(drv), caps, hwcheck.Options("settings"),
                                 sleep=lambda s: None).go()
    bad = [r["step"] for r in rows if r["result"] == "FAIL"]
    assert "vset1: set and put back" in bad
    caps.idn = "ACME,9000,1,1"
    rows = hwcheck.HardwareCheck(lambda fn: fn(drv), caps, sleep=lambda s: None).go()
    assert rows[0]["result"] == "FAIL" and "DP832" in rows[0]["detail"]


def test_options():
    assert hwcheck.Options(volts=12).problem() and hwcheck.Options(level="x").problem()
    assert hwcheck.Options(amps=0).problem() and not hwcheck.Options().problem()


# ---- tapes
def test_serial_number_is_hidden(tmp_path):
    ev = [["r", "*IDN?", "RIGOL,DP832,DP8A123456,00.01"], ["w", "*CLS"],
          ["x", "FOO?", "timeout"]]
    tp = tape.Tape(model="SCPI:DP832", idn="RIGOL,DP832,DP8A123456,00.01", events=ev,
                   rows=[{"step": "Identity", "result": "PASS",
                          "detail": "RIGOL,DP832,DP8A123456,00.01"}])
    p = tmp_path / "a.tape"
    tape.save(tp, str(p))
    assert "DP8A123456" not in p.read_text(encoding="utf-8")
    back = tape.load(str(p))
    assert back.events[0][2] == "RIGOL,DP832,XXXXXXXXXX,00.01" and back.events[2] == ev[2]
    tape.save(tp, str(p), hide_serial=False)
    assert "DP8A123456" in p.read_text(encoding="utf-8")


def test_serial_number_in_a_byte_stream():
    ev = [["w", b"*IDN?\n".hex()], ["r", b"OWON,XDM1041,24".hex()], ["r", b"151234,V4\r\n".hex()]]
    tp = tape.without_serial(tape.Tape(idn="OWON,XDM1041,24151234,V4", stream=True, events=ev))
    assert bytes.fromhex(tp.events[1][1]) == b"OWON,XDM1041,XXXXXXXX,V4\r\n"
    ev[1][1] = "00ff"                  # binary: a checksum would break, so left as it is
    tp = tape.Tape(idn="OWON,XDM1041,24151234,V4", stream=True, events=ev)
    assert not tape.hides_serial(tp)
    assert tape.without_serial(tp).events == ev and "X" * 8 in tape.without_serial(tp).idn


def test_replay_answers_by_command():
    from otterbench.transport import TransportError, TransportTimeout
    r = tape.Replay([["r", "A?", "1"], ["r", "B?", "x"], ["r", "A?", "2"], ["w", "T3"],
                     ["r", "T3", "+1.5"], ["x", "C?", "timeout"], ["x", "D?", "USB error"],
                     ["s", "T3", 4], ["b", "W?", "0102"], ["f", "F?", [1.0, 2.5]]])
    assert r.query("B?") == "x" and r.query("A?") == "1" and r.query("A?") == "2"
    assert r.query("A?") == "2" and r.repeats == 1
    r.write("T3")
    assert r.read() == "+1.5" and r.read_stb() == 4
    with pytest.raises(TransportTimeout):
        r.query("C?")
    with pytest.raises(TransportError):
        r.query("D?")
    assert r.query_raw("W?") == b"\x01\x02" and r.query_binary("F?") == [1.0, 2.5]
    assert r.unknown == []
    with pytest.raises(TransportTimeout):
        r.query("NEW?")
    r.write("NEW 1")
    assert r.unknown == ["NEW?", "NEW 1"]


@pytest.mark.parametrize("text", ["", "hello", "[]", '{"a": 1}', '{"otterbench_tape": 99}',
                                  '{"otterbench_tape": 1}\n{"x": 1}', '{"otterbench_tape": 1}\n["r'])
def test_damaged_tapes_are_refused(text, tmp_path):
    p = tmp_path / "bad.tape"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(tape.TapeError):
        tape.load(str(p))


def test_tap_is_off_and_bounded(monkeypatch):
    t, tap = tape.wrap(open_transport("SIM::66321D"))
    t.query("*IDN?")
    assert tap.tap_events == []
    monkeypatch.setattr(tape, "MAX_EVENTS", 3)
    tap.tap_on = True
    for _ in range(9):
        t.query("*IDN?")
    assert len(tap.tap_events) == 3 and tap.tap_full
    t.timeout_s = 9.0                  # set on the link, not on the Tap
    assert tap.tap_link.timeout_s == 9.0
    with t.temp_timeout(1.0):
        assert tap.tap_link.timeout_s == 1.0


def test_mark_tested(tmp_path):
    src = (builtin_dir() / "DP832.toml").read_text(encoding="utf-8")
    p = tmp_path / "DP832.toml"
    p.write_text(src, encoding="utf-8")
    text = hwcheck.tested_text("RIGOL,DP832,DP8A1,00.01.16", "output", "2026-11-24")
    assert text == "DP832 fw 00.01.16, 2026-11-24, output"
    import tomllib
    for txt in (text, "again"):
        hwcheck.mark_tested(str(p), txt)
        d = parse_definition(tomllib.loads(p.read_text(encoding="utf-8")), str(p))
        assert d.tested == txt and p.read_text(encoding="utf-8").count("\ntested =") == 1
    assert len(d.settings) == len(definitions()["DP832"].settings)
    with pytest.raises(ValueError):
        hwcheck.mark_tested(str(p), 'a"b')


def test_summary_line_has_no_serial():
    s = hwcheck.summary_line("RIGOL,DP832,DP8A123456,1.0", [{"result": "PASS"}], "read", "d")
    assert "DP8A" not in s and "PASS: 1 passed" in s


# ---- in the app
def test_window_and_replay_tab(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from otterbench.gui.hwcheck_window import HwCheckWindow
    from otterbench.gui.main_window import MainWindow

    def until(cond, s=20.0):
        end = time.monotonic() + s
        while time.monotonic() < end and not cond():
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        return cond()
    m = MainWindow(initial=[("auto", "SIM::DP832")])
    try:
        assert until(lambda: m.panels() and m.panels()[0].connected)
        w = m.show_hwcheck()
        assert isinstance(w, HwCheckWindow)
        w.confirm = lambda text: True
        w.inst.refresh()
        w.level.setCurrentIndex(w.level.findData("output"))
        w.settle.setValue(0.0)
        w.run()
        assert until(lambda: w.rows and not w.runner.running() and w.run_btn.isEnabled())
        assert hwcheck.verdict(w.rows) == "PASS"
        assert m.panels()[0].worker.connect_events      # the connect traffic is kept
        p = str(tmp_path / "dp832.tape")
        w.write_tape(p)
        assert tape.load(p).simulated
        assert hwcheck.replay(p).problems == []
        assert not w.can_mark()                          # a simulator proves nothing
        # Stop while the output is on: it goes off, and the settings come back
        drv = m.panels()[0].worker.drv
        v0 = drv.values["vset1"]
        w.settle.setValue(20.0)
        w.run()
        assert until(lambda: drv.values.get("out1") is True)
        w.runner.stop()
        assert until(lambda: not w.runner.running() and w.run_btn.isEnabled())
        assert until(lambda: drv.values["out1"] is False and drv.values["vset1"] == v0)
        assert "Stopped" in w.result.text() and w.tape is None and not w.can_mark()
        html = w.report_html()
        assert "Hardware check" in html and "PASS" in html
        # and the tape opens as a tab
        m.open_instrument(f"REPLAY::{p}", "auto")
        assert until(lambda: len(m.panels()) == 2 and m.panels()[1].connected
                     and m.panels()[1].last_reading is not None)
        n, t0 = m.panels()[1].readings, time.monotonic()
        until(lambda: False, 1.0)                        # paced: doesn't flood the window
        assert (m.panels()[1].readings - n) / (time.monotonic() - t0) < 120
    finally:
        m._confirm_quit = lambda: True
        m.close()
        until(lambda: False, 0.3)
