"""Math channels window: traces computed from the open instruments.

    efficiency   _66321d_out.power / _6632a_in.power * 100       %
    energy       _66321d_dut.power   (Integrate ticked)          J
    I smoothed   avg(ppk2.current, 5)                            A

Values update five times a second; each channel is plotted and, while a
session is recording, saved into it (stream "Math"), so the session viewer
shows it next to the instruments. Double-click a signal to put it in the
selected expression."""
from __future__ import annotations

import collections
import math

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QListWidget, QMainWindow, QPushButton, QSplitter,
                             QTableWidget, QVBoxLayout, QWidget)

from ..clock import now
from ..mathchannels import Channel
from ..session import recorder as session
from ..signals import slug
from . import touch
from .hub import hub
from .panel_base import open_panels
from .widgets import C_MUTED, C_WARN, MONO, fmt_si

COLS = ["On", "Name", "Expression", "Unit", "Integrate", "Value"]
COLORS = ["#4fc3f7", "#ffb347", "#7ee787", "#c39bff", "#ef9a9a", "#fff176"]
KEEP = 600.0           # s of values plotted


class MathWindow(QMainWindow):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Math channels")
        self.resize(1200, 700)
        self.hist: dict[str, collections.deque] = {}
        self.curves: dict[str, pg.PlotDataItem] = {}
        central = QWidget()
        v = QVBoxLayout(central)
        top = QHBoxLayout()
        for text, fn in (("＋ Channel", self.add_channel), ("Remove", self.remove_channel),
                         ("Reset totals", self.reset_totals)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        top.addStretch(1)
        v.addLayout(top)
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44 if touch.TOUCH else 32)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        lv.addWidget(self.table, 2)
        self.err = QLabel("")
        self.err.setWordWrap(True)
        self.err.setStyleSheet(f"color:{C_WARN};")
        lv.addWidget(self.err)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.addLegend(offset=(-10, 10), brush=pg.mkBrush(20, 22, 26, 200))
        touch.plots(self.plot)
        lv.addWidget(self.plot, 3)
        split.addWidget(left)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(QLabel("Signals  (double-click to insert)"))
        self.sigs = QListWidget()
        self.sigs.setFont(QFont(MONO, 9))
        self.sigs.itemDoubleClicked.connect(self._insert)
        rv.addWidget(self.sigs, 1)
        help_ = QLabel("instrument.quantity, + − × ÷ and **, avg(x, seconds) rolling mean, "
                       "min, max, abs, sqrt, log10, clip(x, lo, hi), where(cond, a, b). "
                       "Integrate turns power (W) into energy (J), current (A) into charge (C).\n\n"
                       "Efficiency in %:\n  _out.power / _in.power * 100")
        help_.setWordWrap(True)
        help_.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        rv.addWidget(help_)
        split.addWidget(right)
        split.setSizes([860, 320])
        v.addWidget(split, 1)
        self.setCentralWidget(central)
        for ch in hub.math.channels:
            self._add_row(ch)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(200)
        self._last_focus: QLineEdit | None = None

    # ---------------------------------------------------------------- table
    def _add_row(self, ch: Channel):
        r = self.table.rowCount()
        self.table.insertRow(r)
        on = QCheckBox()
        on.setChecked(ch.enabled)
        name = QLineEdit(ch.name)
        expr = QLineEdit(ch.expr)
        expr.setFont(QFont(MONO, 10))
        expr.setPlaceholderText("e.g. _66321d_out.power / _6632a_in.power * 100")
        unit = QLineEdit(ch.unit)
        unit.setMaximumWidth(60)
        integ = QCheckBox()
        integ.setChecked(ch.integrate)
        val = QLabel("—")
        val.setFont(QFont(MONO, 11))
        val.setMinimumWidth(130)
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for c, w in enumerate((on, name, expr, unit, integ, val)):
            self.table.setCellWidget(r, c, w)
        for w in (name, expr, unit):
            w.editingFinished.connect(self._commit)
        expr.installEventFilter(self)
        on.toggled.connect(self._commit)
        integ.toggled.connect(self._commit)

    def eventFilter(self, obj, ev):
        if ev.type() == ev.Type.FocusIn and isinstance(obj, QLineEdit):
            self._last_focus = obj
        return super().eventFilter(obj, ev)

    def _commit(self, *_):
        chans = []
        w = self.table.cellWidget
        for r in range(self.table.rowCount()):
            chans.append(Channel(w(r, 1).text().strip() or f"math{r + 1}", w(r, 2).text().strip(),
                                 w(r, 3).text().strip(), w(r, 4).isChecked(),
                                 w(r, 0).isChecked()))
        names = [c.name for c in chans]
        if len(names) != len(set(names)):
            self.err.setText("Two channels have the same name")
            return
        hub.math.channels = chans
        for n in list(self.curves):
            if n not in names:
                self.plot.removeItem(self.curves.pop(n))
                self.hist.pop(n, None)
        self.main.save_bench_state()

    def add_channel(self):
        n = len(hub.math.channels) + 1
        names = {c.name for c in hub.math.channels}
        while f"math{n}" in names:
            n += 1
        ch = Channel(f"math{n}", "")
        hub.math.channels.append(ch)
        self._add_row(ch)
        self._commit()

    def remove_channel(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)
            self._commit()

    def reset_totals(self):
        hub.math.reset_totals()
        for d in self.hist.values():
            d.clear()

    def _insert(self, item):
        target = self._last_focus
        if target is None:
            r = max(0, self.table.currentRow())
            if not self.table.rowCount():
                self.add_channel()
            target = self.table.cellWidget(r, 2)
        target.insert(item.text().split()[0])
        target.setFocus()
        self._commit()

    # ---------------------------------------------------------------- live
    def _fill_signals(self):
        known = set()
        for p in open_panels():
            for q in p.quantities():
                known.add(f"{slug(p.display_name())}.{q}")
        known |= set(hub.math.available())
        have = [self.sigs.item(i).text().split()[0] for i in range(self.sigs.count())]
        if sorted(known) != have:
            self.sigs.clear()
            for k in sorted(known):
                self.sigs.addItem(k)

    def tick(self):
        if not self.isVisible() and not session.active:
            return
        self._fill_signals()
        t = now()
        vals = hub.math.tick(t)
        errs = []
        for r, ch in enumerate(hub.math.channels):
            if r >= self.table.rowCount():
                break
            lab = self.table.cellWidget(r, 5)
            if ch.name in hub.math.errors:
                lab.setText("error")
                errs.append(f"{ch.name}: {hub.math.errors[ch.name]}")
                continue
            v = vals.get(ch.name)
            if v is None:
                lab.setText("—")
                continue
            lab.setText(fmt_si(v, ch.unit) if ch.unit in ("V", "A", "W", "J", "C", "s", "Ω")
                        else f"{v:.6g} {ch.unit}".strip())
            d = self.hist.setdefault(ch.name, collections.deque())
            if math.isfinite(v):
                d.append((t, v))
            while d and d[0][0] < t - KEEP:
                d.popleft()
            c = self.curves.get(ch.name)
            if c is None:
                c = self.plot.plot(pen=pg.mkPen(COLORS[r % len(COLORS)], width=1.5),
                                   name=f"{ch.name} ({ch.unit})" if ch.unit else ch.name)
                self.curves[ch.name] = c
            if d:
                xs, ys = zip(*d)
                c.setData(xs, ys)
        self.err.setText("\n".join(errs))
        if session.active and vals:
            cols = [c.name for c in hub.math.channels if c.name in vals]
            key = ("math", tuple(cols))
            session.stream(key, "Math", "math", "", cols)
            session.add(key, t, [vals[c] for c in cols])
