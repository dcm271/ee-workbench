import threading
import time
from types import SimpleNamespace

import pytest

from otterbench.sequencer import (Bench, Inst, Plan, SequenceError, Step, Stopped, run_plan,
                                    run_script, value_of)


class FakeSupply:
    def __init__(self):
        self.v, self.i_lim, self.out = 0.0, 1.0, False
        self.calls = []

    def set_voltage(self, v): self.v = v; self.calls.append(("v", v))
    def set_current(self, a): self.i_lim = a
    def set_output(self, on): self.out = on; self.calls.append(("out", on))
    def set_ovp(self, v): pass


class FakeInst(Inst):
    def __init__(self, name, kind, drv=None, clock=None):
        self.name, self.kind, self.drv, self.clock = name, kind, drv, clock
        self.off = False

    def run(self, fn, timeout=30.0):
        return fn(self.drv)

    def reading_after(self, t, timeout):
        if self.kind == "dmm":
            return SimpleNamespace(t=t + 0.01, value=1.234)
        d = self.drv
        return SimpleNamespace(t=t + 0.01, voltage=d.v, current=d.v / 100 if d.out else 0.0,
                               dvm=None, power=None)

    def current_between(self, t0, t1):
        return 2e-3, 9e-3

    def capture(self, timeout):
        return SimpleNamespace(table_rows=[1, 2, 3], offset=None)

    def outputs_off(self):
        self.off = True


@pytest.fixture
def bench():
    sup = FakeSupply()
    insts = [FakeInst("66321D · DUT rail", "supply", sup), FakeInst("3456A · probe", "dmm"),
             FakeInst("PPK2 (SIM::PPK2)", "ppk2"), FakeInst("Logic · uart", "logic")]
    rows, notes = [], []
    b = Bench(insts, time.monotonic, log=lambda s: None, on_row=rows.append,
              on_note=notes.append)
    b.sup, b.rows_seen, b.notes = sup, rows, notes
    return b


def test_value_of():
    assert value_of("x * 2", {"x": 1.5}) == 3.0
    assert value_of("on", {}) == 1.0 and value_of("off", {}) == 0.0
    assert value_of("sqrt(16) + i", {"i": 1}) == 5.0
    with pytest.raises(SequenceError):
        value_of("__import__('os')", {})


def test_find_instruments(bench):
    assert bench.inst("66321D").kind == "supply"
    assert bench.inst("probe").kind == "dmm"
    with pytest.raises(SequenceError, match="No open instrument"):
        bench.inst("scope")


def test_sweep_plan(bench):
    plan = Plan(steps=[Step("Set", "66321D", "output", "on"),
                       Step("Set", "66321D", "voltage", "x"),
                       Step("Wait", value="0"),
                       Step("Measure", "66321D", "current"),
                       Step("Measure", "PPK2", "current", "0.01", label="I_ppk"),
                       Step("Measure", "3456A", "value"),
                       Step("Capture", "Logic"),
                       Step("Note", value="at {x} V")],
                loop="sweep", start=3.0, stop=3.6, step=0.3)
    run_plan(plan, bench)
    rows = bench.rows_seen
    assert [r["x"] for r in rows] == [3.0, 3.3, 3.6]
    assert rows[1]["66321D current (A)"] == pytest.approx(0.033)
    assert rows[0]["I_ppk"] == 2e-3 and rows[0]["3456A value"] == 1.234
    assert rows[0]["Logic decoded"] == 3
    assert bench.notes == ["at 3 V", "at 3.3 V", "at 3.6 V"]
    assert ("v", 3.3) in bench.sup.calls


def test_bad_steps(bench):
    with pytest.raises(SequenceError, match="can't set"):
        run_plan(Plan(steps=[Step("Set", "3456A", "voltage", "1")]), bench)
    with pytest.raises(SequenceError, match="can't measure"):
        bench.measure("PPK2", "voltage")
    with pytest.raises(SequenceError, match="step can't be 0"):
        Plan(loop="sweep", step=0).passes()
    with pytest.raises(SequenceError, match="never reaches"):
        Plan(loop="sweep", start=0, stop=1, step=-1).passes()
    assert len(Plan(loop="repeat", count=4).passes()) == 4


def test_stop_interrupts_a_wait(bench):
    threading.Timer(0.05, bench.stop_event.set).start()
    t = time.monotonic()
    with pytest.raises(Stopped):
        run_plan(Plan(steps=[Step("Wait", value="30")]), bench)
    assert time.monotonic() - t < 2


def test_script(bench):
    run_script("for v in (1.0, 2.0):\n"
               "    bench.set('66321D', 'voltage', v)\n"
               "    bench.record(v=v, i=bench.measure('PPK2', 'current', 0.01))\n", bench)
    assert [r["v"] for r in bench.rows_seen] == [1.0, 2.0]
    bench.outputs_off()
    assert all(i.off for i in bench._insts)


def test_plan_roundtrip():
    p = Plan(steps=[Step("Wait", value="1")], loop="repeat", count=3)
    q = Plan.from_dict(p.to_dict())
    assert q == p and isinstance(q.steps[0], Step)
