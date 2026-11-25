"""For / Repeat / Retry / If, Command steps, and a meter's settings."""
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from otterbench import command
from otterbench.sequencer import (Bench, Inst, LimitFailed, Plan, SequenceError, Step, Stopped,
                                  depths, dmm_set, parse_blocks, run_plan)


class Meter(Inst):
    """A 'supply' whose measured voltage comes from a list (then its setting)."""

    def __init__(self, values=()):
        self.name, self.kind, self.values, self.v = "PSU", "supply", list(values), 0.0
        self.calls = []

    def run(self, fn, timeout=30.0):
        return fn(self)

    def set_voltage(self, v): self.v = v; self.calls.append(v)
    def set_current(self, v): self.calls.append(("i", v))
    def set_ocp(self, on): self.calls.append(("ocp", on))
    def clear_protection(self): self.calls.append("clear")

    def reading_after(self, t, timeout):
        v = self.values.pop(0) if self.values else self.v
        return SimpleNamespace(t=t, voltage=v, current=0.0, dvm=None, power=None)

    def outputs_off(self): pass


def go(steps, values=(), **plan):
    m = Meter(values)
    b = Bench([m], time.monotonic, log=lambda s: None)
    run_plan(Plan(steps=steps, **plan), b, extra={"serial": "S 1; rm -rf"})
    return b, m


def meas(col="Vout", lo="", hi=""):
    return Step("Measure", "PSU", "voltage", "", col, lo, hi)


def rows(b, *cols):
    return [tuple(r.get(c) for c in cols) for r in b.rows]


def test_nested_for_gives_a_row_each():
    b, _ = go([Step("For", "", "y", "1, 2"), Step("For", "", "z", "0..1 x 2"),
               Step("Set", "PSU", "voltage", "y * 10 + z"), meas(), Step("End"), Step("End")])
    assert rows(b, "pass", "y", "z", "Vout") == [(1, 1, 0, 10), (1, 1, 1, 11), (1, 2, 0, 20),
                                                 (1, 2, 1, 21)]


def test_for_inside_a_sweep_and_what_is_measured_around_it():
    b, _ = go([meas("idle"), Step("For", "", "y", "1 2"), Step("Set", "PSU", "voltage", "x + y"),
               meas(), Step("End"), meas("after")], values=(), loop="sweep", start=0, stop=10,
              step=10)
    assert rows(b, "x", "y", "idle", "Vout", "after") == [
        (0, 1, 0, 1, None), (0, 2, 0, 2, None), (0, None, 0, None, 2),
        (10, 1, 2, 11, None), (10, 2, 2, 12, None), (10, None, 2, None, 12)]


def test_for_variable_fills_notes():
    notes = []
    m = Meter()
    b = Bench([m], time.monotonic, log=lambda s: None, on_note=notes.append)
    run_plan(Plan([Step("For", "", "y", "1, 2.5"), Step("Note", value="at {y}"), Step("End")]), b)
    assert notes == ["at 1", "at 2.5"] and len(b.rows) == 1


def test_if_else_uses_measured_columns():
    steps = [meas("V out"), Step("If", value="v_out > 3 and x < 100"),
             Step("Set", "PSU", "voltage", "1"), Step("Else"),
             Step("Set", "PSU", "voltage", "2"), Step("End")]
    _, m = go(steps, values=[3.3], loop="sweep", start=0, stop=0, step=1)
    assert m.calls == [1]
    _, m = go(steps, values=[2.9], loop="sweep", start=0, stop=0, step=1)
    assert m.calls == [2]


def test_repeat_count_and_until():
    _, m = go([Step("Repeat", value="3"), Step("Set", "PSU", "voltage", "k"), Step("End")])
    assert m.calls == [0, 1, 2]
    b, m = go([Step("Repeat", value="until vout < 0.5 max=10"), meas(),
               Step("Set", "PSU", "voltage", "k"), Step("End")], values=[3, 2, 0.4])
    assert m.calls == [0, 1, 2] and rows(b, "Vout") == [(0.4,)] and "result" not in b.rows[0]
    b, _ = go([Step("Repeat", value="until vout < 0.5 max=2"), meas(), Step("End")],
              values=[3, 2, 0.1])
    assert b.rows[0]["result"] == "FAIL" and "2 rounds" in b.rows[0]["failures"]


def test_retry_counts_only_the_last_try():
    steps = [Step("Retry", value="3", label="tries"), meas(lo="3.2"), Step("End")]
    b, _ = go(steps, values=[1, 3.3])
    assert rows(b, "Vout", "tries", "result", "failures") == [(3.3, 2, "PASS", "")]
    assert (b.passed, b.failed) == (1, 0)
    b, _ = go(steps, values=[1, 1.5, 2])
    assert b.rows[0]["result"] == "FAIL" and b.rows[0]["failures"].count("Vout") == 1
    # a failure before the Retry is kept
    b, _ = go([meas("a", lo="9")] + steps, values=[1, 1, 3.3])
    assert b.rows[0]["result"] == "FAIL" and b.rows[0]["failures"].startswith("a=")


def test_stop_on_fail_inside_a_for():
    m = Meter()
    b = Bench([m], time.monotonic, log=lambda s: None)
    with pytest.raises(LimitFailed):
        run_plan(Plan([Step("For", "", "y", "1 2 3"), Step("Set", "PSU", "voltage", "y"),
                       meas(hi="1.5"), Step("End")], stop_on_fail=True), b)
    assert [r["result"] for r in b.rows] == ["PASS", "FAIL"]


def test_stop_ends_an_endless_loop():
    m = Meter()
    b = Bench([m], time.monotonic, log=lambda s: None)
    threading.Timer(0.1, b.stop_event.set).start()
    with pytest.raises(Stopped):
        run_plan(Plan([Step("Repeat", value="until 0 max=100000 wait=0.001"), Step("End")]), b)


@pytest.mark.parametrize("steps", [
    [Step("End")], [Step("Else")], [Step("If", value="1")], [Step("If", value=""), Step("End")],
    [Step("Repeat", value="2"), Step("Else"), Step("End")],
    [Step("If", value="1"), Step("Else"), Step("Else"), Step("End")],
    [Step("For", "", "", "1 2"), Step("End")], [Step("For", "", "x", "1"), Step("End")],
    [Step("For", "", "2y", "1"), Step("End")], [Step("For", "", "pi", "1"), Step("End")],
    [Step("For", "", "y", "lots"), Step("End")], [Step("Repeat", value="0"), Step("End")],
    [Step("For", "", "y", "1"), Step("For", "", "y", "1"), Step("End"), Step("End")],
    [Step("Repeat", value="until v max=-1"), Step("End")], [Step("Retry", value="no"), Step("End")],
    [Step("Repeat", value="5 wait=soon"), Step("End")]])
def test_bad_blocks_are_refused_before_anything_runs(steps):
    with pytest.raises(SequenceError):
        parse_blocks(steps)
    m = Meter()
    with pytest.raises(SequenceError):
        run_plan(Plan([Step("Set", "PSU", "voltage", "1")] + steps),
                 Bench([m], time.monotonic, log=lambda s: None))
    assert m.calls == []


def test_indent():
    assert depths(["For", "Set", "If", "Set", "Else", "Set", "End", "End", "Note", "End"]) == \
        [0, 1, 1, 2, 1, 2, 1, 0, 0, 0]


def test_more_supply_settings():
    _, m = go([Step("Set", "PSU", "ocp", "on"), Step("Set", "PSU", "clear_protection", "1")])
    assert m.calls == [("ocp", True), "clear"]


# ---- Command
PY = sys.executable


def cmd_step(code, what="", tail="", label="out", lo="", hi=""):
    return Step("Command", "", what, f"{PY} -c '{code}'{tail}", label, lo, hi)


def test_command_exit_status_and_pattern():
    b, _ = go([cmd_step('print("flash ok, 42 kB")', r"(\d+) kB", lo="40", hi="50")])
    assert b.rows[0]["out"] == 42 and b.rows[0]["result"] == "PASS"
    b, _ = go([cmd_step('print("42 kB")', r"(\d+) kB", hi="10")])
    assert b.rows[0]["result"] == "FAIL"
    b, _ = go([cmd_step("raise SystemExit(3)")])
    assert b.rows[0]["result"] == "FAIL" and "exit status 3" in b.rows[0]["failures"]
    b, _ = go([cmd_step('print("no")', "verified")])
    assert "no 'verified'" in b.rows[0]["failures"]
    b, _ = go([Step("Command", value="/no/such/flasher x")])
    assert "can't start" in b.rows[0]["failures"]
    b, _ = go([Step("Retry", value="2"), Step("Command", value="/no/such"), Step("End")])
    assert b.failed == 1


def test_command_fills_in_names_as_single_words():
    assert command.split("flash --sn {serial} {other}", {"serial": "S 1; rm -rf"}) == \
        ["flash", "--sn", "S 1; rm -rf", "{other}"]
    b, _ = go([Step("Command", "", r"^\[(.*)\]",
                    f"{PY} -c 'import sys; print(sys.argv[1:])' {{serial}}", "args")])
    assert b.rows[0]["args"] == "'S 1; rm -rf'"
    for bad in ("", "a 'b"):
        with pytest.raises(ValueError):
            command.split(bad, {})


def test_command_timeout_and_stop_kill_it():
    t0 = time.monotonic()
    b, _ = go([cmd_step("import time; time.sleep(30)", tail=" timeout=0.3")])
    assert "not finished after 0.3 s" in b.rows[0]["failures"]
    m = Meter()
    b = Bench([m], time.monotonic, log=lambda s: None)
    threading.Timer(0.3, b.stop_event.set).start()
    with pytest.raises(Stopped):
        run_plan(Plan([cmd_step("import time; time.sleep(30)")]), b)
    assert time.monotonic() - t0 < 10


def test_command_takes_its_children_along(tmp_path):
    pid = tmp_path / "pid"
    res = command.run(["sh", "-c", f"sleep 60 & echo $! > {pid}; wait"], 0.5, threading.Event(),
                      lambda s: None)
    assert "not finished" in res.problem
    time.sleep(0.2)
    stat = f"/proc/{pid.read_text(encoding='utf-8').strip()}/stat"
    assert not os.path.exists(stat) or open(stat, encoding="utf-8").read().split()[2] == "Z"


def test_command_isnt_in_the_remote_api():
    from otterbench.api import METHODS
    assert "command" not in METHODS


# ---- meters
def test_meter_settings():
    from otterbench.instruments import make_driver
    from otterbench.transport import open_transport
    d = make_driver("3456A", open_transport("SIM::3456A"), "SIM::3456A")
    d.identify()
    dmm_set(d, "range", "10 v")
    assert d.cfg.range == "10 V"
    dmm_set(d, "function", "2w ω")
    assert d.cfg.function == "2W Ω" and d.cfg.range == "AUTO"
    dmm_set(d, "nplc", 0.1)
    dmm_set(d, "filter", 1.0)
    assert d.cfg.nplc == 0.1 and d.cfg.filter is True
    for what, bad in (("function", "furlongs"), ("range", "10 V"), ("nplc", 3.0)):
        with pytest.raises(ValueError):
            dmm_set(d, what, bad)
    x = make_driver("XDM", open_transport("SIM::XDM1041"), "SIM::XDM1041")
    x.identify()
    dmm_set(x, "function", "ACV")
    dmm_set(x, "rate", "fast")
    assert x.cfg.function == "ACV" and x.cfg.rate == "Fast"


def test_steps_in_the_window():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from otterbench.config import Config
    from otterbench.gui.main_window import MainWindow

    def until(cond, s=15.0):
        end = time.monotonic() + s
        while time.monotonic() < end and not cond():
            app.processEvents()
            time.sleep(0.005)
        return cond()
    m = MainWindow([("auto", "SIM::3456A"), ("auto", "SIM::6632A")], Config(), None)
    try:
        assert until(lambda: len(m.panels()) == 2 and all(p.connected for p in m.panels()))
        w = m.sequencer()
        dmm, psu = (p.display_name() for p in m.panels())
        steps = [Step("Set", dmm, "function", "DCV"), Step("Set", psu, "ocp", "on"),
                 Step("For", "", "y", "1, 2"), Step("Set", dmm, "nplc", "y * 0 + 1"),
                 Step("Measure", dmm, "value", "", "v"), Step("End")]
        w._set_plan(Plan(steps))
        assert [w._step_at(r) for r in range(6)] == steps
        assert "│" in w.table.verticalHeaderItem(4).text()
        assert "│" not in w.table.verticalHeaderItem(5).text()
        w.run()
        assert until(lambda: not w.running() and w.run_btn.isEnabled())
        assert w.outcome == "Finished" and [r["y"] for r in w.rows] == [1, 2]
        assert m.panels()[1].worker.drv.sp.ocp is True
    finally:
        m._confirm_quit = lambda: True
        m.close()
