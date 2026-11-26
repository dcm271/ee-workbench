"""The supply Trend: which traces, one chart or several, zoom and browsing."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


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


@pytest.fixture
def panel(qapp):
    from otterbench.gui.supply_panel import SupplyPanel
    p = SupplyPanel("SIM::66309B", "auto", 9600)
    assert _wait(qapp, lambda: p.connected and p.readings > 3)
    yield p
    p.shutdown()


def test_traces_follow_what_is_measured(qapp, panel):
    v = panel.trend
    assert {"voltage", "current", "voltage2", "current2"} <= v.available
    assert "i_max" not in v.available and "dvm" not in v.available
    assert list(v.boxes) == ["V", "I"]                 # power is off until chosen
    panel.pulse_chk.setChecked(True)
    assert "i_max" in v.available and "i_max" in v.curves
    panel.meas2_chk.setChecked(False)
    assert "voltage2" not in v.curves
    panel.volt_chk.setChecked(False)
    assert list(v.boxes) == ["I"]


def test_one_chart_with_an_axis_each(qapp, panel):
    v = panel.trend
    v.show_trace("power", True)
    v.set_mode("one")
    assert list(v.boxes) == ["V", "I", "P"]
    assert len({id(b) for b in v.boxes.values()}) == 3
    assert panel._x_plot is v.boxes["V"] and len(panel._steady) == 3
    _wait(qapp, lambda: False, 0.5)
    panel.redraw()
    # one time axis: a moment is drawn at the same place on every axis (within
    # a pixel: the extra plot areas can be half a pixel bigger than the main one)
    from PyQt6.QtCore import QPointF
    xs = [b.mapViewToScene(QPointF(60.0, 0)).x() for b in v.boxes.values()]
    assert max(xs) - min(xs) < 1.0
    assert v.curves["power"].getData()[0] is not None
    # nothing shown at all
    for k in list(v.visible()):
        v.show_trace(k, False)
    assert v.boxes == {} and panel._x_plot is None
    panel.redraw()


def test_scrolling_back_stops_following_until_live(qapp, panel):
    vb = panel.trend.x_box()
    vb.scaleBy(x=0.25)
    vb.sigRangeChangedManually.emit([True, False])
    assert not panel._live and "Live" in panel.fit_btn.text()
    seen = vb.viewRange()[0]
    _wait(qapp, lambda: False, 1.0)
    assert vb.viewRange()[0] == pytest.approx(seen)
    assert all(s.auto for s in panel._steady)          # Y still fits what is shown
    panel.zoom_time(True)
    assert vb.viewRange()[0][1] - vb.viewRange()[0][0] == pytest.approx((seen[1] - seen[0]) / 2)
    panel.fit_btn.click()
    assert panel._live and panel.fit_btn.text() == "Fit"
    _wait(qapp, lambda: False, 0.5)
    assert vb.viewRange()[0] != pytest.approx(seen)


def test_zoom_buttons(qapp, panel):
    panel.zoom_time(True)
    assert panel.span_combo.currentText() == "30 s"
    panel.zoom_time(False), panel.zoom_time(False)
    assert panel.span_combo.currentText() == "10 min"
    _wait(qapp, lambda: False, 0.5)
    vb = panel.trend.boxes["I"]
    before = vb.viewRange()[1]
    panel.zoom_y(0.5)
    after = vb.viewRange()[1]
    assert after[1] - after[0] == pytest.approx((before[1] - before[0]) / 2)
    assert not any(s.auto for s in panel._steady)
    _wait(qapp, lambda: False, 0.5)
    assert vb.viewRange()[1] == pytest.approx(after)    # the autoscale leaves it alone
    panel.live_trend()
    assert all(s.auto for s in panel._steady)


def test_an_axis_drag_moves_only_that_axis(qapp, panel):
    from otterbench.gui.trend_view import TrendBox
    vb = panel.trend.boxes["V"]
    assert isinstance(vb, TrendBox)
    got = []
    vb.sigRangeChangedManually.connect(got.append)

    class Ev:
        def __init__(self, axis_delta):
            self.d = axis_delta
        def accept(self): pass
        def ignore(self): pass
        def pos(self):
            from PyQt6.QtCore import QPointF
            return QPointF(10, 10)
        def delta(self): return 120
    vb.wheelEvent(Ev(0))                   # over the plot: time only
    vb.wheelEvent(Ev(0), axis=1)           # over the axis: Y only
    assert [list(m) for m in got] == [[True, False], [False, True]]
    assert not panel._live
    assert vb.state["mouseEnabled"] == [True, True]


def test_choices_are_remembered(qapp, panel):
    v = panel.trend
    v.set_mode("one")
    v.show_trace("power", True)
    v.show_trace("current2", False)
    s = panel.session_settings()
    assert s == {"trend": {"layout": "one", "shown": {"power": True, "current2": False}}}
    v.restore({})
    assert v.mode == "stacked"             # nothing saved: the defaults
    panel.restore_settings({"trend": {"layout": "stacked", "shown": {"power": False}}})
    assert v.mode == "stacked" and not v.shown["power"]


def test_statistics_are_of_the_time_on_screen(qapp, panel):
    import numpy as np
    from otterbench.gui.trend_view import Axis, Trace, TrendView
    v = TrendView([Axis("I", "Current", "A", "#fff"), Axis("P", "Power", "W", "#fff")],
                  [Trace("current", "Current", "I", "#fff"), Trace("power", "Power", "P", "#fff")])
    t = np.arange(0.0, 100.0, 1.0)
    i = np.where(t < 50, 0.001, 0.003)                  # 1 mA, then 3 mA
    v.set_data(t, {"current": i, "power": i * 2})
    v.x_box().setXRange(60, 90, padding=0)
    n, rows = v.stats(t, {"current": i, "power": i * 2})
    cur = rows[0]
    assert n == 31 and cur["avg"] == pytest.approx(0.003) and cur["pp"] == pytest.approx(0)
    assert cur["total"] == pytest.approx(0.003 * 30 / 3600) and cur["total_unit"] == "Ah"
    assert rows[1]["total_unit"] == "Wh"
    v.x_box().setXRange(40, 60, padding=0)
    _, rows = v.stats(t, {"current": i, "power": i * 2})
    assert rows[0]["min"] == pytest.approx(0.001) and rows[0]["max"] == pytest.approx(0.003)


def test_panel_statistics_follow_the_view(qapp, panel):
    assert _wait(qapp, lambda: "readings" in panel.trend_stats.text())
    assert "average" in panel.view_stats.text() and ">I<" in panel.view_stats.text()
    vb = panel.trend.x_box()
    vb.setXRange(0, 0.001, padding=0)                   # before any reading
    vb.sigRangeChangedManually.emit([True, False])
    assert _wait(qapp, lambda: ", 0 readings" in panel.trend_stats.text())
    assert panel.view_stats.text() == ""
    panel.live_trend()
    assert _wait(qapp, lambda: "average" in panel.view_stats.text())
