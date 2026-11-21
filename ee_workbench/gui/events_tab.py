"""PPK2 'Events' tab: find every burst above a threshold (radio TX, sensor
wake-ups) and list it with duration, peak, average and charge; a histogram of
any column; and, when a logic capture is aligned to this PPK2, the decoded
bus traffic during each event ("this 4.2 µC burst is the I²C read of 0x48")."""
from __future__ import annotations

import csv
import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout,
                             QHeaderView, QLabel, QPushButton, QSplitter, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from .. import events as ev
from . import touch
from .panel_base import open_panels
from .theme import set_role
from .widgets import C_CURR, C_MUTED, C_WARN, SafeSpin, fmt_si, io_errors

COLS = ["#", "Start (s)", "Duration", "Peak", "Average", "Charge", "Bus during the event"]
HIST = {"Duration": ("duration", "s"), "Charge": ("charge", "C"), "Peak": ("peak", "A"),
        "Average": ("mean", "A"), "Interval": ("interval", "s")}


class NumItem(QTableWidgetItem):
    """Sorts by the number behind the text (1 ms after 900 µs)."""

    def __lt__(self, other):
        a = self.data(Qt.ItemDataRole.UserRole + 1)
        b = other.data(Qt.ItemDataRole.UserRole + 1)
        if a is None or b is None:
            return super().__lt__(other)
        return a < b


class EventsTab(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.p = panel
        self.events: list[ev.Event] = []
        self._t = self._i = None
        v = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Data"))
        self.source = QComboBox()
        self.source.addItem("Live buffer (10 s, 10 µs samples)", "ring")
        self.source.addItem("Trend span shown (1 ms averages)", "trend")
        self.source.setToolTip("The live buffer catches bursts down to 10 µs; the trend reaches "
                               "back up to 16 min but averages each millisecond")
        bar.addWidget(self.source)
        # two rows, so the tab fits beside the readouts on a 1280-px touch screen
        find = QPushButton("Find events")
        set_role(find, "primary")
        find.clicked.connect(self.find)
        bar.addWidget(find)
        save = QPushButton("Save CSV…")
        save.clicked.connect(self.save_csv)
        bar.addWidget(save)
        bar.addStretch(1)
        v.addLayout(bar)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Above"))
        self.thr = SafeSpin(3, 0.1, " mA", track=False)
        self.thr.setRange(0.001, 1000.0)
        self.thr.setValue(1.0)
        self.thr.setToolTip("An event is where the current is above this")
        touch.attach_keypad(self.thr, "Event threshold (mA)")
        bar.addWidget(self.thr)
        bar.addWidget(QLabel("merge gaps <"))
        self.gap = SafeSpin(2, 0.1, " ms", track=False)
        self.gap.setRange(0.0, 10_000.0)
        self.gap.setValue(1.0)
        self.gap.setToolTip("Bursts closer together than this count as one event")
        touch.attach_keypad(self.gap, "Merge gaps shorter than (ms)")
        bar.addWidget(self.gap)
        bar.addWidget(QLabel("ignore <"))
        self.min_len = SafeSpin(2, 0.01, " ms", track=False)
        self.min_len.setRange(0.0, 10_000.0)
        self.min_len.setValue(0.05)
        self.min_len.setToolTip("Drop events shorter than this (spikes, noise)")
        touch.attach_keypad(self.min_len, "Ignore events shorter than (ms)")
        bar.addWidget(self.min_len)
        bar.addStretch(1)
        v.addLayout(bar)
        self.summary = QLabel("Press Find events. The PPK2 must have been measuring (▶ Run).")
        self.summary.setStyleSheet(f"color:{C_MUTED};")
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeader().setSectionResizeMode(len(COLS) - 1,
                                                           QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self._show_selected)
        split.addWidget(self.table)
        plots = QWidget()
        pl = QHBoxLayout(plots)
        pl.setContentsMargins(0, 0, 0, 0)
        hv = QVBoxLayout()
        row = QHBoxLayout()
        row.addWidget(QLabel("Histogram of"))
        self.hist_what = QComboBox()
        self.hist_what.addItems(list(HIST))
        self.hist_what.setCurrentText("Charge")
        self.hist_what.currentTextChanged.connect(self._hist)
        row.addWidget(self.hist_what)
        row.addStretch(1)
        hv.addLayout(row)
        self.hplot = pg.PlotWidget()
        self.hplot.showGrid(x=True, y=True, alpha=0.15)
        self.hplot.setLabel("left", "Events")
        self.bars = None
        hv.addWidget(self.hplot, 1)
        pl.addLayout(hv, 1)
        self.wplot = pg.PlotWidget()
        self.wplot.showGrid(x=True, y=True, alpha=0.15)
        self.wplot.setLabel("left", "Current", units="A", color=C_CURR)
        self.wplot.setLabel("bottom", "Time from event start", units="s")
        self.wcurve = self.wplot.plot(pen=pg.mkPen(C_CURR, width=1))
        self.wthr = pg.InfiniteLine(angle=0, pen=pg.mkPen(C_WARN, style=Qt.PenStyle.DashLine))
        self.wplot.addItem(self.wthr)
        pl.addWidget(self.wplot, 1)
        touch.plots(self.hplot, self.wplot)
        split.addWidget(plots)
        split.setSizes([300, 260])
        v.addWidget(split, 1)

    # ---------------------------------------------------------------- finding
    def _data(self):
        if self.source.currentData() == "ring":
            k = self.p.ring.count
            if k < 2:
                return None, None
            t, i, _ = self.p.ring.latest(k)
            return t, i
        d, _, _ = self.p._trend_data()
        if d is None or len(d) < 2:
            return None, None
        return d[:, 0], d[:, 1]

    def find(self):
        t, i = self._data()
        if t is None:
            self.summary.setText("No data yet: press ▶ Run on the PPK2 first.")
            self.summary.setStyleSheet(f"color:{C_WARN};")
            return
        self._t, self._i = t, i
        thr = self.thr.value() / 1000
        self.events = ev.find_events(t, i, thr, min_gap=self.gap.value() / 1000,
                                     min_len=self.min_len.value() / 1000, hysteresis=0.1)
        bus_note = self._attach_bus()
        s = ev.summary(self.events)
        span = t[-1] - t[0]
        if s["count"]:
            text = (f"{s['count']} events in {fmt_si(span, 's')}: average "
                    f"{fmt_si(s['duration_mean'], 's')} long, {fmt_si(s['charge_mean'], 'C')} each "
                    f"({fmt_si(s['charge_total'], 'C')} in all), highest peak "
                    f"{fmt_si(s['peak_max'], 'A')}")
            if "interval_mean" in s:
                text += f", one every {fmt_si(s['interval_mean'], 's')} on average"
            base = i[i <= thr]
            if base.size:
                text += f". Between events: {fmt_si(float(base.mean()), 'A')} average"
        else:
            text = f"No events above {fmt_si(thr, 'A')} in {fmt_si(span, 's')}"
        self.summary.setText(text + "." + (f"  {bus_note}" if bus_note else ""))
        self.summary.setStyleSheet(f"color:{C_MUTED};")
        self.wthr.setValue(thr)
        self._fill()
        self._hist()

    def _attach_bus(self) -> str:
        """Decoded rows from a logic capture aligned to this PPK2."""
        for lp in open_panels():
            cap = getattr(lp, "capture", None)
            if getattr(lp, "KIND", "") != "logic" or cap is None:
                continue
            if cap.offset is None:
                return "(A logic capture is open but not aligned to the PPK2, so bus " \
                       "traffic isn't matched: use Align with PPK2 on the analyzer tab.)"
            rows = []
            for r in cap.table_rows:
                try:
                    rows.append((float(r[2]), " ".join(str(x) for x in (r[0], r[4] if len(r) > 4
                                                                         else "")).strip()))
                except (ValueError, IndexError, TypeError):
                    continue
            ev.attach_bus(self.events, rows, cap.offset, pad=50e-6)
            return f"Bus traffic from {lp.display_name()}."
        return ""

    def _fill(self):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.events))
        t0 = self._t[0] if self._t is not None else 0.0
        for r, e in enumerate(self.events):
            vals = [(r + 1, str(r + 1)), (e.t0 - t0, f"{e.t0 - t0:.6f}"),
                    (e.duration, fmt_si(e.duration, "s")), (e.peak, fmt_si(e.peak, "A")),
                    (e.mean, fmt_si(e.mean, "A")), (e.charge, fmt_si(e.charge, "C")),
                    (0, "; ".join(e.bus[:8]) + (" …" if len(e.bus) > 8 else ""))]
            for c, (num, text) in enumerate(vals):
                it = NumItem(text)
                it.setData(Qt.ItemDataRole.UserRole, r)
                if c < len(COLS) - 1:
                    it.setData(Qt.ItemDataRole.UserRole + 1, num)
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, it)
        self.table.resizeColumnsToContents()
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)

    def _hist(self, *_):
        if self.bars is not None:
            self.hplot.removeItem(self.bars)
            self.bars = None
        if not self.events:
            return
        key, unit = HIST[self.hist_what.currentText()]
        if key == "interval":
            vals = np.diff([e.t0 for e in self.events])
        else:
            vals = [getattr(e, key) for e in self.events]
        counts, edges = ev.histogram(vals, 30)
        if not counts.size:
            return
        self.bars = pg.BarGraphItem(x0=edges[:-1], x1=edges[1:], height=counts,
                                    brush=pg.mkBrush(79, 195, 247, 160), pen=pg.mkPen(None))
        self.hplot.addItem(self.bars)
        self.hplot.setLabel("bottom", self.hist_what.currentText(), units=unit)
        self.hplot.autoRange()

    def _show_selected(self):
        items = self.table.selectedItems()
        if not items or self._t is None:
            return
        e = self.events[items[0].data(Qt.ItemDataRole.UserRole)]
        pad = max(e.duration * 0.5, 200e-6)
        a = int(np.searchsorted(self._t, e.t0 - pad))
        b = int(np.searchsorted(self._t, e.t1 + pad))
        self.wcurve.setData(self._t[a:b] - e.t0, self._i[a:b])
        self.wplot.autoRange()

    @io_errors("Save events")
    def save_csv(self):
        if not self.events:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save events", time.strftime("PPK2_events_%Y%m%d_%H%M%S.csv"), "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["start_s", "duration_s", "peak_A", "mean_A", "charge_C", "bus"])
            for e in self.events:
                w.writerow([f"{e.t0:.6f}", f"{e.duration:.6e}", f"{e.peak:.6e}", f"{e.mean:.6e}",
                            f"{e.charge:.6e}", " | ".join(e.bus)])
