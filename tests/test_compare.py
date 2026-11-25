"""Comparing two sessions."""
import math
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from otterbench import compare as cmp  # noqa: E402
from otterbench import report  # noqa: E402
from otterbench.analysis import trapezoid  # noqa: E402
from otterbench.session import SessionReader, SessionWriter  # noqa: E402


def make(path, gain=1.0, delay=1.0, extra=False, n=1001, gap=False):
    """2 V for `delay` s, then a 10 mA step; a sequencer table."""
    w = SessionWriter()
    w.start(str(path))
    w.stream("a", "PPK2 (PPK2::X)", "PPK2", "PPK2::X", ["voltage_V", "current_A"])
    t = np.linspace(0, 10, n)
    if gap:
        t = t[(t < 4) | (t > 8)]
    w.add_many("a", [(100 + x, 2.0, gain * (0.010 if x >= delay else 0.001)) for x in t])
    w.note("boot", 100 + delay)
    w.stream("s", "Sequencer", "sequence", "", ["vin", "eff", "result"])
    for k in range(5):
        w.add("s", 100.0 + k, [3.0 + k, 80 * gain + k, "PASS"])
    if extra:
        w.stream("b", "DMM", "XDM", "", ["value"])
        w.add("b", 100.0, [1.0])
    w.stop()
    return str(path)


@pytest.fixture
def pair(tmp_path):
    a, b = make(tmp_path / "a.otter"), make(tmp_path / "b.otter", 1.1, 3.0, extra=True)
    ra, rb = SessionReader(a), SessionReader(b)
    yield a, b, ra, rb
    ra.close()
    rb.close()


I = "ppk2.current_A"


def test_trapezoid():
    t = np.array([0, 1, 3.0])
    assert trapezoid([1, 1, 3], t) == 1 + 4 and trapezoid([5], [1]) == 0


def test_signals_and_start(pair):
    *_, ra, rb = pair
    c = cmp.compare(ra, rb)
    assert [r.key for r in c.rows] == ["ppk2.voltage_V", I, "sequencer.vin", "sequencer.eff"]
    assert c.only == ((), ("dmm.value",)) and c.verdict == ""
    r = c.rows[1]
    assert r.unit == "A" and r.base["duration"] == pytest.approx(10)
    assert r.base["mean"] == pytest.approx(0.0091, rel=0.01)
    assert r.base["integral"] == pytest.approx(0.091, rel=0.01)
    assert cmp.stat_unit("integral", "A") == "C" and cmp.stat_unit("duration", "A") == "s"
    seq = c.rows[3]
    assert math.isnan(seq.base["duration"]) and seq.new["mean"] == pytest.approx(90)
    t, d = cmp.difference(c.base["sequencer.eff"], c.new["sequencer.eff"])
    assert list(t) == [1, 2, 3, 4, 5] and np.allclose(d, 8)


@pytest.mark.parametrize("how", ["note:Boot", f"rise:{I}:0.005"])
def test_lined_up(pair, how):
    *_, ra, rb = pair
    c = cmp.compare(ra, rb, cmp.Align.parse(how), 0.1, 6.0)
    r = c.rows[1]
    assert r.base["mean"] == pytest.approx(0.010) and r.new["mean"] == pytest.approx(0.011)
    t, d = cmp.difference(c.base[I], c.new[I])
    assert t[0] >= 0.1 and np.allclose(d, 0.001)
    al = cmp.Align.parse(how)
    al.shift = 2.0
    assert cmp.compare(ra, rb, al).new[I].t[0] == pytest.approx(-3.0 + 2.0, abs=0.02)


@pytest.mark.parametrize("bad", ["", "start:x", "note:", "rise:x", "rise:x:y", "middle"])
def test_bad_alignment(bad):
    with pytest.raises(cmp.CompareError):
        cmp.Align.parse(bad)


def test_alignment_that_isnt_there(pair):
    *_, ra, rb = pair
    for how in ("note:reset", f"rise:{I}:5", f"fall:{I}:0.005", "rise:nothing:1",
                "rise:sequencer.eff:1"):
        with pytest.raises(cmp.CompareError):
            cmp.compare(ra, rb, cmp.Align.parse(how))


def test_gaps_are_left_out(tmp_path):
    a = SessionReader(make(tmp_path / "a.otter"))
    b = SessionReader(make(tmp_path / "b.otter", gap=True))
    c = cmp.compare(a, b)
    s = c.rows[1]
    assert s.new["integral"] == pytest.approx(0.051, rel=0.02)      # 6 s of it
    t, d = cmp.difference(c.base[I], c.new[I])
    assert np.isnan(d[(t > 4.1) & (t < 7.9)]).all() and np.isfinite(d[t < 3.9]).all()
    a.close()
    b.close()


def test_limits():
    L = cmp.Limit.parse
    x = L("a.b mean +5% -0.1")
    assert (x.up, x.up_pct, x.down, x.down_pct) == (5, True, 0.1, False)
    assert x.text() == "a.b mean +5% -0.1"
    assert x.check(1, 1.05)[0] and not x.check(1, 1.06)[0]
    assert x.check(1, 0.9)[0] and not x.check(1, 0.89)[0]
    assert L("a max +5%").check(-1, -0.96)[0] and not L("a max +5%").check(-1, -0.9)[0]
    assert L("a max ±1e-3").check(0, -0.001)[0]
    assert not L("a max +5%").check(0, 0.1)[0] and L("a max +5%").check(0, -9)[0]
    assert not L("a max +5%").check(math.nan, 1)[0]
    for bad in ("", "a", "a mean", "a median +1", "a mean 5", "a mean +x", "a mean +-"):
        with pytest.raises(cmp.CompareError):
            L(bad)


def test_command_line(pair, tmp_path, capsys):
    a, b, *_ = pair
    note = ["--align", "note:boot", "--from", "0.1", "--to", "6"]
    assert cmp.main([a, b, *note, "--check", f"{I} mean +11%"]) == 0
    assert "PASS" in capsys.readouterr().out
    html, table = str(tmp_path / "r.html"), str(tmp_path / "t.csv")
    assert cmp.main([a, b, *note, "--check", f"{I} mean +5%", "--report", html,
                     "--csv", table]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "voltage" not in out
    assert "FAIL" in open(html).read() and I in open(table).read()
    assert cmp.main([a, b, "--check", "dmm.value mean +5%"]) == 1
    assert "not in the base" in capsys.readouterr().out
    assert cmp.main([a, b, "--check", "x y z"]) == 2
    assert cmp.main([a, str(tmp_path / "none.otter")]) == 2
    (tmp_path / "x.otter").write_text("hello")
    assert cmp.main([a, str(tmp_path / "x.otter")]) == 2


def test_window(pair, tmp_path, monkeypatch):
    from otterbench.gui.compare_window import CompareWindow
    a, b, *_ = pair
    said = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *x, **k: said.append(x[2]))
    w = CompareWindow()
    w.run()
    assert "two sessions" in said.pop()
    w.open(0, a)
    w.open(1, str(tmp_path / "missing.otter"))
    assert "missing" in said.pop()
    w.open(1, b)
    assert w.list.count() == 4 and w.shown() == {I}
    assert w.table.rowCount() == len(cmp.STATS)
    w.limits.setPlainText(f"{I} max +5%")
    w.run()
    assert "FAIL" in w.result.text()
    w.limits.setPlainText("nonsense")
    w.run()
    assert "nonsense" in said.pop()
    w.limits.setPlainText("")
    w.how.setCurrentIndex(1)
    w.run()
    assert "note" in said.pop()
    w.note.setText("boot")
    w.cut.setChecked(True)
    w.t_to.setValue(5)
    w.run()
    assert not said
    out = str(tmp_path / "c.csv")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *x, **k: (out, ""))
    w.save_csv()
    assert os.path.exists(out)
    assert "Only in the new" in report.compare_report("t", w.c)
    w.swap()
    assert w.paths == [b, a] and w.c.only[0] == ("dmm.value",)
    w.release()
    w.close()
