"""The monitor: every open instrument's signals, what is on, and nicknames."""
import os
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from otterbench import monitor as mn
from otterbench.clock import now


def _supply(t, on=True, on2=None, v=3.3, i=0.01):
    r = NS(t=t, voltage=v, current=i, power=None, dvm=None, output=on,
           voltage2=None, current2=None, power2=None, output2=on2)
    if on2 is not None:
        r.voltage2, r.current2, r.power2, r.output2 = 5.0, 0.2, 1.0, on2
    return r


def test_a_supply_counts_as_on_only_while_its_output_is():
    m = mn.Monitor()
    t = now()
    m.feed("66309B", "supply", _supply(t, on=True, on2=False))
    live = {k for k, s in m.signals.items() if s.live()}
    assert live == {"_66309b.voltage", "_66309b.current", "_66309b.power"}
    assert m.signals["_66309b.current2"].on is False
    m.feed("66309B", "supply", _supply(t + 1, on=False, on2=True))
    live = {k for k, s in m.signals.items() if s.live()}
    assert live == {"_66309b.voltage2", "_66309b.current2", "_66309b.power2"}
    assert m.signals["_66309b.voltage"].unit == "V" and m.signals["_66309b.power2"].unit == "W"


def test_a_meter_is_on_while_readings_come_and_takes_its_unit():
    m = mn.Monitor()
    m.feed("XDM1041", "dmm", NS(t=now(), value=0.5, unit="A", overload=False))
    s = m.signals["xdm1041.value"]
    assert s.live() and s.on is None and s.unit == "A"
    assert not s.live(at=now() + mn.STALE_S + 1)


def test_a_block_of_fast_samples_is_one_point():
    m = mn.Monitor()
    cur = np.full(1000, 2e-3)
    cur[5] = np.nan
    m.feed("PPK2", "ppk2", NS(t=1.01, t0=1.0, current=cur, logic=np.zeros(1000, np.uint8)))
    t, v = m.signals["ppk2.current"].data()
    assert len(t) == 1 and v[0] == pytest.approx(2e-3)


def test_history_is_trimmed_and_capped(monkeypatch):
    monkeypatch.setattr(mn, "MAX_POINTS", 1000)
    m = mn.Monitor()
    for k in range(5000):
        m.feed("M", "dmm", NS(t=float(k) * 0.01, value=float(k), unit="V", overload=False))
    s = m.signals["m.value"]
    assert s.n <= 1000 and s.last() == 4999.0
    t, _ = s.data()
    assert np.all(np.diff(t) > 0)
    m2 = mn.Monitor()
    for k in range(10):
        m2.feed("M", "dmm", NS(t=k * 1000.0, value=1.0, unit="V", overload=False))
    t, _ = m2.signals["m.value"].data()
    assert t[0] >= t[-1] - mn.KEEP_S
    assert list(s.window(49.0, 49.02)) == [4900.0, 4901.0, 4902.0]


def test_closed_instruments_are_forgotten():
    m = mn.Monitor()
    m.feed("66309B", "supply", _supply(now()))
    m.feed("XDM1041", "dmm", NS(t=now(), value=1.0, unit="V", overload=False))
    m.keep_only(["XDM1041"])
    assert {s.instrument for s in m.signals.values()} == {"XDM1041"}


def test_nicknames():
    m = mn.Monitor()
    m.feed("66309B", "supply", _supply(now()))
    assert m.label("_66309b.current") == "66309B · current"
    m.set_nickname("_66309b.current", "  DUT   3V3 rail ")
    assert m.label("_66309b.current") == "DUT 3V3 rail"
    m.set_nickname("_66309b.current", "")
    assert m.label("_66309b.current") == "66309B · current"
    m.load_nicknames({"a.b": "x", "c.d": "  "})
    assert m.nicknames == {"a.b": "x"}
    m.load_nicknames(["not", "a", "dict"])
    assert m.nicknames == {}


# ---------------------------------------------------------------- the window
@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _wait(app, cond, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_monitor_window_shows_what_is_on_and_keeps_nicknames(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from otterbench.config import Config
    from otterbench.gui.hub import hub
    from otterbench.gui.main_window import MainWindow
    from otterbench.gui import monitor_window as mw
    hub.monitor.signals.clear()
    hub.monitor.nicknames.clear()
    win = MainWindow([("auto", "SIM::66309B"), ("auto", "SIM::XDM1041")], Config(), None)
    try:
        assert _wait(qapp, lambda: all(p.connected and p.readings > 2 for p in win.panels()))
        sup = next(p for p in win.panels() if p.KIND == "supply")
        sup.set_output(False)
        mon = win.show_monitor()
        assert _wait(qapp, lambda: mon._keys and not any(k.startswith("_66309b.")
                                                          for k in mon._keys))
        assert all(k.startswith("xdm1041.") for k in mon._keys)       # the meter only
        sup.set_output(True)
        assert _wait(qapp, lambda: "_66309b.voltage" in mon._keys, 15)
        assert "_66309b.voltage" in mon.view.curves
        # every unit on an axis of its own, in one chart
        assert {mon.view.traces[k].axis for k in mon._keys} >= {"u:V", "u:A"}
        mon.which.setCurrentText(mw.EVERYTHING)
        assert _wait(qapp, lambda: "_66309b.voltage2" in mon._keys)
        # a nickname, typed in the table
        row = mon._keys.index("_66309b.voltage")
        mon.table.item(row, mw.NICK).setText("DUT rail")
        assert hub.monitor.label("_66309b.voltage") == "DUT rail"
        assert _wait(qapp, lambda: mon.view.traces["_66309b.voltage"].label == "DUT rail")
        import json
        saved = json.loads((tmp_path / "otterbench" / "bench.json").read_text())
        assert saved["nicknames"] == {"_66309b.voltage": "DUT rail"}
        from otterbench.gui.setups_dialog import capture_bench
        assert capture_bench(win)["nicknames"] == {"_66309b.voltage": "DUT rail"}
        assert _wait(qapp, lambda: mon.table.item(row, mw.NOW).text() not in ("", "—"))
        assert mon.table.item(row, mw.AVG).text() != "—"
        mon.view.browsed.emit()
        assert not mon._live and "Live" in mon.fit_btn.text()
        mon.go_live()
        assert mon._live
        mon.close()
    finally:
        win.close()
        _wait(qapp, lambda: False, 0.2)
        hub.monitor.nicknames.clear()
