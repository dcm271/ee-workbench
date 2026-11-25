"""Window for looking through a recorded session file (.otter).

Everything is on one time axis (seconds from the start of the session, with
the wall clock alongside): the chosen columns of each instrument as stacked
plots, the logic-analyzer captures as lanes at the moment they were taken
(to µs where aligned to a PPK2), every capture's decoded data in one table,
and the notes as markers. Click a note, a capture or a decoded row to go to it.

Cursors: two vertical lines across every plot, with each signal's value at
both, the difference, and the mean (and charge, for currents) between them.
Math: a trace computed from the recorded signals (efficiency, power, …), the
same expressions as the live Math channels. Export: the visible stretch as
CSV or PNG, or a report (HTML / PDF).
"""
from __future__ import annotations

import csv
import math

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox,
                             QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit,
                             QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
                             QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QWidget)

import numpy as np

from ..about import APP_NAME
from ..instruments.logic import digital_envelope
from ..mathchannels import MathError, evaluate, integrate
from ..session import OLD_SUFFIX, SUFFIX, SessionReader, Stream
from ..signals import slug
from . import touch
from .widgets import C_CURR, C_MUTED, fmt_si, io_errors

COLORS = ["#4fc3f7", "#ffb347", "#7ee787", "#c39bff", "#ef9a9a", "#fff176", "#80cbc4",
          "#f48fb1", "#aed581", "#90caf9"]
LANE = 1.5
DEFAULT_COLS = {"voltage_V", "current_A", "value", "power_W"}
MAX_ROWS = 20_000



def break_gaps(t: np.ndarray, y: np.ndarray):
    """Insert a NaN wherever the readings stop for a while (an instrument
    unplugged, or between the windows of an "only around events" recording),
    so the plot shows a gap instead of a straight line across it."""
    if t.size < 3:
        return t, y
    dt = np.diff(t)
    typical = float(np.median(dt))
    gaps = np.nonzero(dt > max(10 * typical, 1.0))[0]
    if not gaps.size:
        return t, y
    return (np.insert(t, gaps + 1, (t[gaps] + t[gaps + 1]) / 2),
            np.insert(y.astype(float), gaps + 1, np.nan))

class SessionViewer(QMainWindow):
    def __init__(self, path: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Session viewer")
        self.resize(1400, 860)
        self.s: SessionReader | None = None
        self.t_ref = 0.0
        self.plots: list[pg.PlotItem] = []
        self.logic_plot: pg.PlotItem | None = None
        self.lanes: list[tuple] = []          # (capture, channel, lane, curve)
        self.path = path or ""
        self.math_streams: list[Stream] = []  # computed in this window (negative ids)
        self.plotted: list[tuple] = []        # (label, unit, t relative, y)
        self.cursors: list[tuple] = []        # (line a, line b) per plot
        self._cur_pos = None                  # (a, b) relative seconds
        self._syncing = False

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setContentsMargins(8, 8, 8, 8)
        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 6, 0)
        row = QHBoxLayout()
        op = QPushButton("Open…")
        op.clicked.connect(self.choose)
        row.addWidget(op)
        fit = QPushButton("Fit all")
        fit.clicked.connect(self.fit)
        row.addWidget(fit)
        sv.addLayout(row)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setStyleSheet(f"color:{C_MUTED};")
        sv.addWidget(self.info)
        self.side_tabs = QTabWidget()
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemChanged.connect(lambda *_: self._build_plots())
        self.side_tabs.addTab(self.tree, "Signals")
        self.notes = QListWidget()
        self.notes.itemClicked.connect(lambda it: self.go(it.data(Qt.ItemDataRole.UserRole)))
        self.side_tabs.addTab(self.notes, "Notes")
        self.caps = QListWidget()
        self.caps.itemClicked.connect(self._capture_clicked)
        self.side_tabs.addTab(self.caps, "Captures")
        self.serial = QListWidget()
        self.serial.itemClicked.connect(lambda it: self.go(it.data(Qt.ItemDataRole.UserRole)))
        self.side_tabs.addTab(self.serial, "Serial")
        self.side_tabs.setTabVisible(3, False)
        sv.addWidget(self.side_tabs, 1)
        split.addWidget(side)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.glw = pg.GraphicsLayoutWidget()
        rv.addWidget(self.glw, 3)
        tools = QHBoxLayout()
        self.cur_chk = QCheckBox("Cursors")
        self.cur_chk.setToolTip("Two vertical cursors: drag them; values, differences and "
                                "means between them appear below")
        self.cur_chk.toggled.connect(self._cursors_toggled)
        tools.addWidget(self.cur_chk)
        for text, tip, fn in (
                ("Math…", "Add a trace computed from the recorded signals", self.add_math),
                ("Export CSV…", "Every checked signal in the stretch shown, as CSV",
                 self.export_csv),
                ("Save PNG…", "A picture of the plots as shown", self.save_png),
                ("Report…", "HTML / PDF report of the stretch shown", self.report)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            tools.addWidget(b)
        tools.addStretch(1)
        self.cursor = QLabel("")
        self.cursor.setStyleSheet(f"color:{C_MUTED};")
        tools.addWidget(self.cursor)
        rv.addLayout(tools)
        self.cur_table = QTableWidget(0, 7)
        self.cur_table.setHorizontalHeaderLabels(["Signal", "at A", "at B", "B − A", "Mean A…B",
                                                  "Min…Max A…B", "Charge / energy A…B"])
        self.cur_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.cur_table.verticalHeader().setVisible(False)
        self.cur_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cur_table.setMaximumHeight(170)
        self.cur_table.setVisible(False)
        rv.addWidget(self.cur_table)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Time", "Capture", "Decoder", "Duration", "Data"])
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.cellClicked.connect(self._row_clicked)
        rv.addWidget(self.table, 1)
        split.addWidget(right)
        split.setSizes([320, 1080])
        self.setCentralWidget(split)
        self.proxy = None
        if path:
            self.load(path)

    # ---------------------------------------------------------------- loading
    def choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open session", "",
                                              f"{APP_NAME} session (*{SUFFIX} *{OLD_SUFFIX})")
        if path:
            self.load(path)

    def load(self, path: str):
        try:
            s = SessionReader(path)
        except Exception as e:    # noqa: BLE001
            QMessageBox.warning(self, "Open session", f"Can't read {path}:\n{e}")
            return
        if self.s is not None:
            self.s.close()
        self.s = s
        self.path = path
        self.math_streams = []
        a, b = s.span()
        self.t_ref = a
        self.setWindowTitle(f"Session viewer · {path}")
        n_rows = sum(st.t.size for st in s.streams)
        self.info.setText(f"{s.wall(a)} to {s.wall(b)} ({fmt_si(b - a, 's')})\n"
                          f"{len(s.streams)} streams, {n_rows} readings, {len(s.captures)} "
                          f"captures, {len(s.notes)} notes.\nTime is seconds from "
                          f"{s.wall(a)}.")
        self.tree.blockSignals(True)
        self.tree.clear()
        for st in s.streams:
            top = QTreeWidgetItem([f"{st.name}  ({st.t.size})"])
            top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self.tree.addTopLevelItem(top)
            first = True
            for c in st.columns:
                if c not in st.data:
                    continue
                it = QTreeWidgetItem([c])
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                on = c in DEFAULT_COLS or (first and not (DEFAULT_COLS & set(st.data))
                                            and c not in ("pass", "x"))
                it.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                it.setData(0, Qt.ItemDataRole.UserRole, (st.id, c))
                top.addChild(it)
                first = first and c in ("pass", "x")
            top.setExpanded(True)
        self.tree.blockSignals(False)
        self.notes.clear()
        for t, text in s.notes:
            it = QListWidgetItem(f"{self._clock(t)}  {text}")
            it.setData(Qt.ItemDataRole.UserRole, t)
            self.notes.addItem(it)
        self.serial.clear()
        texts = sorted((t, d, x) for st in s.streams for t, d, x in st.lines)
        self.side_tabs.setTabVisible(3, bool(texts))
        for t, d, x in texts[:MAX_ROWS]:
            it = QListWidgetItem(f"{self._clock(t)}  {'» ' if d == 'tx' else ''}{x}")
            it.setData(Qt.ItemDataRole.UserRole, t)
            self.serial.addItem(it)
        self.caps.clear()
        for c in s.captures:
            a0, b0 = c.span
            it = QListWidgetItem(f"{self._clock(a0)}  {c.name}  {fmt_si(b0 - a0, 's')}"
                                 + ("  ✓ aligned" if c.aligned else ""))
            it.setData(Qt.ItemDataRole.UserRole, (a0, b0))
            it.setToolTip(c.note or "Not aligned: placed to within a few ms by the host clock")
            self.caps.addItem(it)
        for t, name, data in s.images:
            it = QListWidgetItem(f"{self._clock(t)}  picture: {name}")
            it.setData(Qt.ItemDataRole.UserRole, (t, t))
            it.setData(Qt.ItemDataRole.UserRole + 1, data)
            self.caps.addItem(it)
        self._fill_table()
        self._build_plots()
        self.fit()

    def _capture_clicked(self, it):
        data = it.data(Qt.ItemDataRole.UserRole + 1)
        a, b = it.data(Qt.ItemDataRole.UserRole)
        if data is None:
            self.go(a, b)
            return
        self.go(a)
        pm = QPixmap()
        pm.loadFromData(bytes(data))
        dlg = QDialog(self)
        dlg.setWindowTitle(it.text().strip())
        lab = QLabel("This picture can't be shown") if pm.isNull() else QLabel()
        if not pm.isNull():
            lab.setPixmap(pm.scaled(1100, 700, Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
                          if pm.width() > 1100 or pm.height() > 700 else pm)
        v = QVBoxLayout(dlg)
        v.addWidget(lab)
        dlg.exec()

    def _clock(self, t: float) -> str:
        return f"{t - self.t_ref:9.3f} s"

    def _fill_table(self):
        rows = []
        for c in self.s.captures:
            res = c.load()
            for r in res.table_rows:
                try:
                    t = c.t0 + float(r[2])
                    dur = float(r[3]) if r[3] not in ("", None) else 0.0
                except (ValueError, IndexError):
                    continue
                dec = str(r[0]) if str(r[0]).lower() == str(r[1]).lower() else f"{r[0]} {r[1]}"
                rows.append((t, c.name, dec.strip(), dur, str(r[4]) if len(r) > 4 else ""))
        rows.sort()
        self.table.setRowCount(min(len(rows), MAX_ROWS))
        for i, (t, cap, dec, dur, data) in enumerate(rows[:MAX_ROWS]):
            for k, v in enumerate((f"{t - self.t_ref:.6f}", cap, dec, fmt_si(dur, "s"), data)):
                item = QTableWidgetItem(v)
                if k == 0:
                    item.setData(Qt.ItemDataRole.UserRole, (t, t + dur))
                self.table.setItem(i, k, item)
        self.table.resizeColumnsToContents()

    # ---------------------------------------------------------------- plots
    def _checked(self):
        out = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for k in range(top.childCount()):
                it = top.child(k)
                if it.checkState(0) == Qt.CheckState.Checked:
                    out.append(it.data(0, Qt.ItemDataRole.UserRole))
        return out

    def _build_plots(self):
        if self.s is None:
            return
        keep = self.plots[0].viewRange()[0] if self.plots else None
        self.glw.clear()
        self.plots, self.lanes, self.logic_plot = [], [], None
        self.plotted, self.cursors = [], []
        streams = {st.id: st for st in self.s.streams + self.math_streams}
        row = 0
        caps = self.s.captures
        if caps:
            p = self.glw.addPlot(row=row, col=0)
            p.setMouseEnabled(y=False)
            p.hideButtons()
            chans = sorted({ch for c in caps for ch in c.load().digital})
            ticks = []
            for lane, ch in enumerate(chans):
                ticks.append((-lane * LANE + 0.5, f"D{ch}"))
                for c in caps:
                    if ch in c.load().digital:
                        cv = p.plot(pen=pg.mkPen(COLORS[(ch + 1) % len(COLORS)], width=1))
                        self.lanes.append((c, ch, lane, cv))
            p.getAxis("left").setTicks([ticks])
            p.setYRange(-(max(len(chans), 1) - 1) * LANE - 0.3, 1.3, padding=0)
            self.logic_plot = p
            self.plots.append(p)
            row += 1
        for k, (sid, col) in enumerate(self._checked()):
            st = streams[sid]
            p = self.glw.addPlot(row=row, col=0)
            p.hideButtons()
            unit = col.rsplit("_", 1)[1] if "_" in col else ""
            si = unit in ("V", "A", "W", "C")
            name = col.rsplit("_", 1)[0] if si else col
            color = C_CURR if unit == "A" else COLORS[k % len(COLORS)]
            # the name as a small title (long axis labels would run into each other)
            p.setTitle(f"<span style='color:{color}'>{st.name}</span>"
                       f"<span style='color:{C_MUTED}'>  ·  {name}</span>", size="9pt",
                       justify="left")
            if si:
                p.setLabel("left", units=unit)
            self.plotted.append((f"{st.name} · {name}", unit if si else "", st.t - self.t_ref,
                                 st.data[col]))
            gx, gy = break_gaps(st.t - self.t_ref, st.data[col])
            c = p.plot(gx, gy, pen=pg.mkPen(color, width=1), connect="finite")
            c.setClipToView(True)
            c.setDownsampling(auto=True, method="peak")
            if st.model == "PPK2" and col == "current_A":
                # aligned captures carry the PPK2's full 10 µs samples for their span
                for cap in caps:
                    fine = cap.load().extra.get("ppk2")
                    if fine is not None and cap.aligned:
                        f = p.plot(fine["t"] + cap.t0 - self.t_ref, fine["i"],
                                   pen=pg.mkPen("#ffffff", width=1))
                        f.setClipToView(True)
                        f.setDownsampling(auto=True, method="peak")
            self.plots.append(p)
            row += 1
        for p in self.plots:
            p.showGrid(x=True, y=True, alpha=0.15)
            p.getAxis("left").setWidth(70)
            if p is not self.plots[0]:
                p.setXLink(self.plots[0])
            for n, (t, text) in enumerate(self.s.notes):
                ln = pg.InfiniteLine(t - self.t_ref, angle=90,
                                     pen=pg.mkPen("#fff176", width=1, style=Qt.PenStyle.DashLine),
                                     label=text if p is self.plots[0] else None,
                                     labelOpts={"position": 0.9 - 0.25 * (n % 3),
                                                "color": "#fff176",
                                                "fill": (11, 13, 17, 220)})
                p.addItem(ln)
            if p is not self.logic_plot:
                for a, b, _why in getattr(self.s, "events", []):
                    # flight-recorder windows: what was kept around each event
                    reg = pg.LinearRegionItem((a - self.t_ref, b - self.t_ref), movable=False,
                                              brush=pg.mkBrush(76, 154, 255, 22))
                    for ln in reg.lines:
                        ln.setPen(pg.mkPen(None))
                    reg.setZValue(-20)
                    p.addItem(reg)
                for c in caps:
                    a, b = c.span
                    reg = pg.LinearRegionItem((a - self.t_ref, b - self.t_ref), movable=False,
                                              brush=pg.mkBrush(255, 255, 255, 18))
                    for ln in reg.lines:
                        ln.setPen(pg.mkPen(None))
                    p.addItem(reg)
        if self.plots:
            last = self.plots[-1]
            last.setLabel("bottom", "Time", units="s")
            for p in self.plots[:-1]:
                p.getAxis("bottom").setStyle(showValues=False)
            self.plots[0].sigXRangeChanged.connect(self._redraw_lanes)
            self.proxy = pg.SignalProxy(self.glw.scene().sigMouseMoved, rateLimit=30,
                                        slot=self._mouse)
            touch.plots(*self.plots)
            if keep:
                self.plots[0].setXRange(*keep, padding=0)
            self._redraw_lanes()
            if self.cur_chk.isChecked():
                self._add_cursors()

    def _redraw_lanes(self, *_):
        if not self.plots or not self.lanes:
            return
        x0, x1 = self.plots[0].viewRange()[0]
        bins = max(200, int(self.plots[0].width() or 1000))
        for c, ch, lane, cv in self.lanes:
            res = c.load()
            off = c.t0 - self.t_ref
            a, b = res.span
            if b + off < x0 or a + off > x1:
                cv.setData([], [])
                continue
            x, y = digital_envelope(res.digital[ch], x0 - off, x1 - off, bins)
            cv.setData(x + off, y - lane * LANE)

    def fit(self):
        if self.s is None or not self.plots:
            return
        a, b = self.s.span()
        self.plots[0].setXRange(a - self.t_ref, b - self.t_ref, padding=0.02)
        for p in self.plots:
            if p is not self.logic_plot:
                p.enableAutoRange(axis="y")

    def go(self, a: float, b: float | None = None):
        """Centre the view on app time a (or show a..b)."""
        if not self.plots:
            return
        if b is None or b <= a:
            x0, x1 = self.plots[0].viewRange()[0]
            half = min((x1 - x0) / 2, 5.0)
            self.plots[0].setXRange(a - self.t_ref - half, a - self.t_ref + half, padding=0)
        else:
            self.plots[0].setXRange(a - self.t_ref, b - self.t_ref, padding=0.1)

    def _row_clicked(self, r, _c):
        item = self.table.item(r, 0)
        if item is not None:
            a, b = item.data(Qt.ItemDataRole.UserRole)
            pad = max((b - a) * 4, 20e-6)
            self.go(a - pad, b + pad)

    def _mouse(self, ev):
        if not self.plots or self.s is None or self.cur_chk.isChecked():
            return
        pos = ev[0]
        vb = self.plots[0].vb
        x = vb.mapSceneToView(pos).x()
        self.cursor.setText(f"t = {x:.6f} s   ({self.s.wall(x + self.t_ref)})")


    # ---------------------------------------------------------------- cursors
    def _cursors_toggled(self, on: bool):
        self.cur_table.setVisible(on)
        if on:
            self._add_cursors()
        else:
            for a, b in self.cursors:
                a.scene() and a.getViewBox().removeItem(a)
                b.scene() and b.getViewBox().removeItem(b)
            self.cursors = []

    def _add_cursors(self):
        if not self.plots:
            return
        if self._cur_pos is None:
            x0, x1 = self.plots[0].viewRange()[0]
            self._cur_pos = (x0 + (x1 - x0) / 3, x0 + 2 * (x1 - x0) / 3)
        self.cursors = []
        for p in self.plots:
            pair = []
            for k, (pos, col) in enumerate(zip(self._cur_pos, ("#ffb347", "#4fc3f7"))):
                ln = pg.InfiniteLine(pos, angle=90, movable=True,
                                     pen=pg.mkPen(col, width=1.5),
                                     label="AB"[k] if p is self.plots[0] else None,
                                     labelOpts={"position": 0.96, "color": col})
                ln.sigPositionChanged.connect(lambda ln, k=k: self._cursor_moved(k, ln))
                p.addItem(ln)
                pair.append(ln)
            self.cursors.append(tuple(pair))
        self._update_cursor_table()

    def _cursor_moved(self, k, line):
        if self._syncing:
            return
        self._syncing = True
        x = line.value()
        pos = list(self._cur_pos)
        pos[k] = x
        self._cur_pos = tuple(pos)
        for pair in self.cursors:
            if pair[k] is not line:
                pair[k].setValue(x)
        self._syncing = False
        self._update_cursor_table()

    def _update_cursor_table(self):
        if not self._cur_pos:
            return
        a, b = sorted(self._cur_pos)
        dt = b - a
        self.cursor.setText(f"A = {self._cur_pos[0]:.6f} s   B = {self._cur_pos[1]:.6f} s   "
                            f"Δt = {fmt_si(dt, 's')}" + (f"   1/Δt = {fmt_si(1 / dt, 'Hz')}"
                                                         if dt > 0 else ""))
        self.cur_table.setRowCount(len(self.plotted))
        for r, (label, unit, t, y) in enumerate(self.plotted):
            def at(x):
                if not t.size:
                    return math.nan
                return float(np.interp(x, t, y))
            va, vb = at(self._cur_pos[0]), at(self._cur_pos[1])
            keep = (t >= a) & (t <= b)
            seg = y[keep]
            seg = seg[np.isfinite(seg)]

            def f(v):
                return "—" if v is None or not math.isfinite(v) else (
                    fmt_si(v, unit) if unit else f"{v:.6g}")
            mean = float(seg.mean()) if seg.size else math.nan
            rng = f"{f(float(seg.min()))} … {f(float(seg.max()))}" if seg.size else "—"
            integ = "—"
            if unit in ("A", "W") and keep.sum() > 1:
                tot = float(integrate(t[keep], y[keep])[-1])
                integ = fmt_si(tot, "C" if unit == "A" else "J")
            for c, text in enumerate((label, f(va), f(vb), f(vb - va), f(mean), rng, integ)):
                self.cur_table.setItem(r, c, QTableWidgetItem(text))

    # ---------------------------------------------------------------- math
    def _series(self) -> dict:
        out = {}
        for st in self.s.streams + self.math_streams:
            iid = slug(st.name)
            for c, v in st.data.items():
                q = c.rsplit("_", 1)[0] if c.rsplit("_", 1)[-1] in ("V", "A", "W", "C", "J") \
                    else c
                out[(iid, q)] = (st.t, v)
        return out

    def add_math(self):
        if self.s is None:
            return
        series = self._series()
        dlg = QDialog(self)
        dlg.setWindowTitle("Math trace")
        form = QFormLayout(dlg)
        name = QLineEdit(f"math{len(self.math_streams) + 1}")
        expr = QLineEdit()
        expr.setMinimumWidth(460)
        expr.setPlaceholderText("e.g. " + (" * ".join(f"{i}.{q}" for i, q in list(series)[:2])
                                           or "a.voltage * a.current"))
        unit = QLineEdit()
        integ = QCheckBox("Integrate over time (W → J, A → C)")
        form.addRow("Name", name)
        form.addRow("Expression", expr)
        form.addRow("Unit (V, A, W …)", unit)
        form.addRow("", integ)
        sigs = QLabel("Signals: " + ", ".join(sorted(f"{i}.{q}" for i, q in series)))
        sigs.setWordWrap(True)
        sigs.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        sigs.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        form.addRow(sigs)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        while dlg.exec():
            try:
                t, y = evaluate(expr.text(), series)
            except MathError as e:
                QMessageBox.warning(self, "Math trace", str(e))
                continue
            if integ.isChecked():
                y = integrate(t, y)
            u = unit.text().strip()
            col = f"{name.text().strip() or 'math'}" + (f"_{u}" if u else "")
            st = Stream(-(len(self.math_streams) + 1), f"Math · {name.text().strip()}", "math",
                        expr.text(), [col], t, {col: y})
            self.math_streams.append(st)
            self.tree.blockSignals(True)
            top = QTreeWidgetItem([f"{st.name}  = {expr.text()}"])
            it = QTreeWidgetItem([col])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Checked)
            it.setData(0, Qt.ItemDataRole.UserRole, (st.id, col))
            top.addChild(it)
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)
            self.tree.blockSignals(False)
            self._build_plots()
            break

    # ---------------------------------------------------------------- export
    def _view_range(self) -> tuple[float, float]:
        if not self.plots:
            a, b = self.s.span()
            return a, b
        x0, x1 = self.plots[0].viewRange()[0]
        return x0 + self.t_ref, x1 + self.t_ref

    @io_errors("Export")
    def export_csv(self):
        if self.s is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export the stretch shown", "session_range.csv",
                                              "CSV (*.csv)")
        if not path:
            return
        a, b = self._view_range()
        streams = {st.id: st for st in self.s.streams + self.math_streams}
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["t_s", "time", "stream", "column", "value"])
            for sid, col in self._checked():
                st = streams[sid]
                keep = (st.t >= a) & (st.t <= b)
                for t, v in zip(st.t[keep], st.data[col][keep]):
                    w.writerow([f"{t - self.t_ref:.6f}", self.s.wall(t), st.name, col,
                                f"{v:.9g}"])
        self.cursor.setText(f"Saved {path}")

    @io_errors("Save picture")
    def save_png(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save picture", "session.png", "PNG (*.png)")
        if path:
            path = path if path.lower().endswith(".png") else path + ".png"
            if not self.glw.grab().save(path):
                raise OSError(f"Can't write {path}")

    def report(self):
        if self.s is None:
            return
        from .report_dialog import report_for_session
        cols = [c for c in self._checked() if c[0] > 0]
        report_for_session(self, self.path, self._view_range(), cols or None)


def open_viewer(parent=None, path: str | None = None) -> SessionViewer | None:
    if path is None:
        path, _ = QFileDialog.getOpenFileName(parent, "Open session", "",
                                              f"{APP_NAME} session (*{SUFFIX} *{OLD_SUFFIX})")
        if not path:
            return None
    w = SessionViewer(path, parent)
    w.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    w.show()
    return w

