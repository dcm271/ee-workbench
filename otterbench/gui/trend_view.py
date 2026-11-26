"""A trend chart whose traces can be shown or hidden, stacked (a chart per
quantity) or drawn on one chart with an axis each, and zoomed by hand.

In the plot area the mouse, or a finger, works on time: the wheel zooms, a
drag scrolls back (the panel then stops following the newest reading until
Live). Over an axis it works on that axis alone, so each quantity can be
scaled and moved on its own.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup
from PyQt6.QtWidgets import QMenu, QToolButton, QVBoxLayout, QWidget

STACKED, ONE = "stacked", "one"


@dataclass
class Axis:
    key: str
    label: str
    unit: str
    color: str


@dataclass
class Trace:
    key: str
    label: str
    axis: str                 # an Axis key
    color: str
    style: Qt.PenStyle = Qt.PenStyle.SolidLine
    width: float = 1.5
    shown: bool = True        # by default
    short: str = ""           # in the statistics table


class TrendBox(pg.ViewBox):
    """Plot area: time only. Over an axis (pyqtgraph passes `axis`): that axis.

    pyqtgraph reports a drag as having moved every axis the mouse may move,
    which would switch off the Y autoscale on a scroll through time, so each
    gesture runs with only its own axis enabled."""

    def _only(self, fn, ev, axis):
        axis = 0 if axis is None else axis
        keep = list(self.state["mouseEnabled"])
        self.state["mouseEnabled"] = [keep[0] and axis == 0, keep[1] and axis == 1]
        try:
            fn(self, ev, axis=axis)
        finally:
            self.state["mouseEnabled"] = keep

    def wheelEvent(self, ev, axis=None):
        self._only(pg.ViewBox.wheelEvent, ev, axis)

    def mouseDragEvent(self, ev, axis=None):
        self._only(pg.ViewBox.mouseDragEvent, ev, axis)


class TrendView(QWidget):
    """`axes` in display order; `traces` drawn on them."""

    rebuilt = pyqtSignal()          # new view boxes: the panel re-registers them
    browsed = pyqtSignal()          # the user moved through time
    changed = pyqtSignal()          # a choice to remember

    def __init__(self, axes: list[Axis], traces: list[Trace]):
        super().__init__()
        self.axes = {a.key: a for a in axes}
        self.traces = {t.key: t for t in traces}
        self.mode = STACKED
        self.shown = {t.key: t.shown for t in traces}
        self.available = set(self.traces)
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.boxes: dict[str, pg.ViewBox] = {}      # axis key -> its view box
        self._extra: list[pg.ViewBox] = []          # one chart: the other axes' boxes
        self._last: tuple | None = None
        self.legend = True              # in the one-chart layout
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.glw = pg.GraphicsLayoutWidget()
        lay.addWidget(self.glw)
        self.menu_button = QToolButton()
        self.menu_button.setText("View ▾")
        self.menu_button.setToolTip("Which traces to show, and how")
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_button.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        self.menu = QMenu(self.menu_button)
        self.menu_button.setMenu(self.menu)
        self.menu.aboutToShow.connect(self._fill_menu)
        self.rebuild()

    # ---- choices -----------------------------------------------------------------
    def _fill_menu(self):
        m = self.menu
        m.clear()
        group = QActionGroup(m)
        for mode, text in ((STACKED, "A chart for each quantity"),
                           (ONE, "One chart, an axis for each")):
            a = QAction(text, m, checkable=True, checked=self.mode == mode)
            a.triggered.connect(lambda _, x=mode: self.set_mode(x))
            group.addAction(a)
            m.addAction(a)
        m.addSeparator()
        for key, t in self.traces.items():
            if key in self.available:
                a = QAction(t.label, m, checkable=True, checked=self.shown[key])
                a.toggled.connect(lambda on, k=key: self.show_trace(k, on))
                m.addAction(a)

    def set_mode(self, mode: str):
        if mode != self.mode:
            self.mode = mode
            self.rebuild()
            self.changed.emit()

    def show_trace(self, key: str, on: bool):
        if self.shown.get(key) != on:
            self.shown[key] = on
            self.rebuild()
            self.changed.emit()

    def set_traces(self, axes: list[Axis], traces: list[Trace]):
        """New axes and traces (the monitor's come and go). Shown / hidden
        choices are kept for the traces that stay; rebuilt only on a change."""
        sig = ([(a.key, a.label, a.unit, a.color) for a in axes],
               [(t.key, t.label, t.axis, t.color, t.style, t.width) for t in traces])
        if sig == getattr(self, "_sig", None):
            return
        self._sig = sig
        self.axes = {a.key: a for a in axes}
        self.traces = {t.key: t for t in traces}
        self.shown = {t.key: self.shown.get(t.key, t.shown) for t in traces}
        self.available = set(self.traces)
        self.rebuild()

    def set_available(self, keys):
        keys = set(keys) & set(self.traces)
        if keys != self.available:
            self.available = keys
            self.rebuild()

    def state(self) -> dict:
        return {"layout": self.mode,
                "shown": {k: v for k, v in self.shown.items() if v != self.traces[k].shown}}

    def restore(self, s: dict | None):
        s = s or {}
        self.mode = s.get("layout") if s.get("layout") in (STACKED, ONE) else STACKED
        for k, v in (s.get("shown") or {}).items():
            if k in self.shown:
                self.shown[k] = bool(v)
        self.rebuild()

    # ---- building --------------------------------------------------------------
    def visible(self) -> list[str]:
        return [k for k in self.traces if self.shown[k] and k in self.available]

    def visible_axes(self) -> list[str]:
        used = {self.traces[k].axis for k in self.visible()}
        return [a for a in self.axes if a in used]

    def _pen(self, t: Trace):
        return pg.mkPen(t.color, width=t.width, style=t.style)

    def rebuild(self):
        # the extra axes' boxes sit on the scene, not in the layout: clear()
        # leaves them (with their curves, on top of the new chart)
        for vb in self._extra:
            vb.clear()
            if vb.scene() is not None:
                vb.scene().removeItem(vb)
        self._extra = []
        self.glw.clear()
        self.curves, self.boxes = {}, {}
        axes = self.visible_axes()
        if not axes:
            self.glw.addLabel("Nothing to show: choose traces under View ▾")
        elif self.mode == ONE:
            self._build_one(axes)
        else:
            self._build_stacked(axes)
        for vb in self.boxes.values():
            vb.setMenuEnabled(False)
            vb.sigRangeChangedManually.connect(self._manual)
        if self._last is not None:
            self.set_data(*self._last)
        self.rebuilt.emit()

    def _plot(self, row: int) -> pg.PlotItem:
        p = self.glw.addPlot(row=row, col=0, viewBox=TrendBox())
        p.hideButtons()
        p.showGrid(x=True, y=True, alpha=0.15)
        p.setClipToView(True)
        p.setDownsampling(auto=True, mode="peak")
        p.getAxis("bottom").enableAutoSIPrefix(False)     # seconds, never "ks"
        return p

    def _build_stacked(self, axes: list[str]):
        first = None
        for row, key in enumerate(axes):
            a = self.axes[key]
            p = self._plot(row)
            p.setLabel("left", a.label, units=a.unit, color=a.color)
            p.getAxis("left").setWidth(70)
            if first is None:
                first = p
            else:
                p.setXLink(first)
            self.boxes[key] = p.getViewBox()
            for k in self.visible():
                if self.traces[k].axis == key:
                    self.curves[k] = p.plot(pen=self._pen(self.traces[k]))
        p.setLabel("bottom", "Time", units="s")

    def _build_one(self, axes: list[str]):
        p = self._plot(0)
        p.setLabel("bottom", "Time", units="s")
        main = p.getViewBox()
        legend = p.addLegend(offset=(10, 5), labelTextSize="8pt", colCount=3) \
            if self.legend else None
        extra = []
        for n, key in enumerate(axes):
            a = self.axes[key]
            if n == 0:
                vb, ax = main, p.getAxis("left")
            else:
                vb = TrendBox()
                if n == 1:
                    p.showAxis("right")
                    ax = p.getAxis("right")
                else:
                    ax = pg.AxisItem("right")
                    p.layout.addItem(ax, 2, 1 + n)
                p.scene().addItem(vb)
                ax.linkToView(vb)
                vb.setXLink(main)
                extra.append(vb)
                self._extra.append(vb)
            if n:
                ax.setGrid(False)          # one grid, the first axis's: more is clutter
            ax.setLabel(a.label, units=a.unit, color=a.color)
            ax.setTextPen(pg.mkPen(a.color))
            self.boxes[key] = vb
            for k in self.visible():
                t = self.traces[k]
                if t.axis == key:
                    c = pg.PlotDataItem(pen=self._pen(t))
                    c.setClipToView(True)
                    c.setDownsampling(auto=True, method="peak")
                    vb.addItem(c)
                    if legend is not None:
                        legend.addItem(c, t.label)
                    self.curves[k] = c

        def follow():
            for vb in extra:
                vb.setGeometry(main.sceneBoundingRect())
                vb.linkedViewChanged(main, vb.XAxis)

        main.sigResized.connect(follow)
        follow()

    def _manual(self, mask):
        if mask[0]:
            self.browsed.emit()

    # ---- data ------------------------------------------------------------------
    def set_data(self, t, cols: dict):
        """`cols`: trace key -> values, as long as `t`, or a (times, values)
        pair of its own. Gaps (NaN) break the line."""
        self._last = (t, cols)
        for k, c in self.curves.items():
            y = cols.get(k)
            tt = t
            if isinstance(y, tuple):
                tt, y = y
            if y is None or not len(y) or not np.isfinite(y).any():
                c.setData([], [])
            else:
                c.setData(tt, y, connect="finite")

    def window(self) -> tuple[float, float] | None:
        """The time shown."""
        vb = self.x_box()
        return tuple(vb.viewRange()[0]) if vb is not None else None

    def stats(self, t, cols: dict) -> tuple[int, list[dict]]:
        """For each shown trace, over the time on screen: average, min, max,
        peak to peak, and the integral (charge for a current, energy for a
        power). Returns (readings in view, rows)."""
        w = self.window()
        if w is None or not len(t):
            return 0, []
        a, b = np.searchsorted(t, w[0], "left"), np.searchsorted(t, w[1], "right")
        tt = t[a:b]
        rows = []
        for k in self.visible():
            y = cols.get(k)
            if y is None:
                continue
            y = y[a:b]
            ok = np.isfinite(y)
            if not ok.any():
                continue
            yy, ts = y[ok], tt[ok]
            unit = self.axes[self.traces[k].axis].unit
            row = {"key": k, "unit": unit, "avg": float(yy.mean()), "min": float(yy.min()),
                   "max": float(yy.max()), "n": int(ok.sum())}
            row["pp"] = row["max"] - row["min"]
            if unit in ("A", "W") and ts.size > 1:
                area = float(np.sum((yy[1:] + yy[:-1]) / 2 * np.diff(ts))) / 3600
                row["total"], row["total_unit"] = area, "Ah" if unit == "A" else "Wh"
            rows.append(row)
        return int(b - a), rows

    def x_box(self) -> pg.ViewBox | None:
        return next(iter(self.boxes.values()), None)

    def zoom_y(self, factor: float):
        """Every shown axis about its middle (>1 zooms out)."""
        for vb in self.boxes.values():
            vb.scaleBy(y=factor)
