"""Compare two recorded sessions (Tools ▾): overlaid, with differences and
limits (compare.py)."""
from __future__ import annotations

import os

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog, QFormLayout, QGroupBox,
                             QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import compare as cmp
from .. import report
from ..about import APP_NAME
from ..session import OLD_SUFFIX, SUFFIX, SessionReader
from . import report_dialog, touch
from .forms import choice, hint, number, select
from .session_viewer import break_gaps
from .theme import set_role
from .widgets import C_BAD, C_GOOD, C_MUTED

C_BASE, C_NEW, C_DIFF = "#4fc3f7", "#ffb347", "#c39bff"
HOW = [("Start of each session", "start"), ("A note", "note"),
       ("A signal rising through", "rise"), ("A signal falling through", "fall")]


class CompareWindow(QMainWindow):
    TITLE = "Compare sessions"

    def __init__(self, main=None):
        super().__init__(main)
        self.main = main
        self.setWindowTitle(self.TITLE)
        self.resize(1280, 780)
        self.readers: list = [None, None]
        self.paths = ["", ""]
        self.c: cmp.Comparison | None = None
        left = QWidget()
        v = QVBoxLayout(left)
        v.setContentsMargins(0, 0, 6, 0)
        g = QGroupBox("Sessions")
        f = QFormLayout(g)
        self.file_btns = []
        for n, text in enumerate(("Base", "New")):
            b = QPushButton("Choose…")
            b.clicked.connect(lambda _=False, n=n: self.choose(n))
            self.file_btns.append(b)
            f.addRow(text, b)
        swap = QPushButton("Swap")
        swap.clicked.connect(self.swap)
        f.addRow("", swap)
        v.addWidget(g)
        g = QGroupBox("Line up by")
        f = QFormLayout(g)
        self.how = choice(HOW)
        self.how.activated.connect(lambda *_: self._how_changed())
        f.addRow("0 s is", self.how)
        self.note = QLineEdit()
        self.note.setPlaceholderText("text in the note")
        f.addRow("Note", self.note)
        self.sig = choice([])
        f.addRow("Signal", self.sig)
        self.level = number("Level", -1e6, 1e6, 0, 6)
        f.addRow("Level", self.level)
        self.shift = number("Move the new session", -1e6, 1e6, 0, 4, " s")
        f.addRow("Move new by", self.shift)
        row = QHBoxLayout()
        self.cut = QCheckBox("Only from")
        self.t_from = number("From", -1e6, 1e6, 0, 3, " s")
        self.t_to = number("To", -1e6, 1e6, 10, 3, " s")
        row.addWidget(self.cut)
        row.addWidget(self.t_from)
        row.addWidget(QLabel("to"))
        row.addWidget(self.t_to)
        f.addRow(row)
        v.addWidget(g)
        g = QGroupBox("Checks")
        gv = QVBoxLayout(g)
        self.limits = QPlainTextEdit()
        self.limits.setPlaceholderText("ppk2.current_A mean +5%\nppk2.current_A max +0.02 -0.02")
        self.limits.setMaximumHeight(110)
        gv.addWidget(self.limits)
        gv.addWidget(hint("One a line: signal, statistic (" + ", ".join(cmp.STATS) + "), how far "
                          "it may go up and down from the base: in % or in the signal's unit."))
        v.addWidget(g)
        b = QPushButton("Compare")
        set_role(b, "primary")
        b.clicked.connect(self.run)
        v.addWidget(b)
        self.list = QListWidget()
        self.list.setMinimumHeight(140)
        self.list.itemChanged.connect(lambda *_: self.draw())
        v.addWidget(self.list, 1)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.result = QLabel("")
        self.result.setWordWrap(True)
        top.addWidget(self.result, 1)
        for text, fn in (("Save CSV…", self.save_csv), ("Report…", self.report)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        rv.addLayout(top)
        self.glw = pg.GraphicsLayoutWidget()
        self.table = QTableWidget(0, len(cmp.TABLE))
        self.table.setHorizontalHeaderLabels(["Signal", "", "Unit", "Base", "New", "Difference",
                                              "%"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        sp = QSplitter(Qt.Orientation.Vertical)
        sp.addWidget(self.glw)
        sp.addWidget(self.table)
        sp.setStretchFactor(0, 3)
        sp.setStretchFactor(1, 1)
        rv.addWidget(sp, 1)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setContentsMargins(8, 8, 8, 8)
        split.setChildrenCollapsible(False)
        side = touch.scroll(left)
        side.setMinimumWidth(400 if touch.TOUCH else 360)
        split.addWidget(side)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)
        self._how_changed()

    def _how_changed(self):
        h = self.how.currentData()
        self.note.setEnabled(h == "note")
        self.sig.setEnabled(h in ("rise", "fall"))
        self.level.setEnabled(h in ("rise", "fall"))

    def warn(self, text: str):
        QMessageBox.warning(self, self.TITLE, text)

    # ---- files
    def choose(self, n: int):
        path, _ = QFileDialog.getOpenFileName(self, "Base session" if n == 0 else "New session",
                                              "", f"{APP_NAME} session (*{SUFFIX} *{OLD_SUFFIX})")
        if path:
            self.open(n, path)

    def open(self, n: int, path: str):
        try:
            r = SessionReader(path)
        except Exception as e:      # noqa: BLE001 - not a session, damaged, gone
            return self.warn(f"Can't open {path}:\n{e}")
        if self.readers[n] is not None:
            self.readers[n].close()
        self.readers[n], self.paths[n] = r, path
        self._files_changed()

    def swap(self):
        self.readers.reverse()
        self.paths.reverse()
        self._files_changed()

    def _files_changed(self):
        for b, p in zip(self.file_btns, self.paths):
            b.setText(os.path.basename(p) if p else "Choose…")
            b.setToolTip(p)
        self.c = None
        if all(self.readers):
            keep = self.sig.currentText()
            self.sig.clear()
            a, b = (cmp.signals(r) for r in self.readers)
            for k, s in a.items():
                if k in b and not s.rows:
                    self.sig.addItem(k, k)
            select(self.sig, keep)
            self.run()

    # ---- compare
    def align(self) -> cmp.Align:
        h = self.how.currentData()
        if h == "note" and not self.note.text().strip():
            raise cmp.CompareError("Type some of the note's text")
        return cmp.Align(h, self.note.text().strip() if h == "note" else
                         self.sig.currentText() if h != "start" else "",
                         self.level.value(), self.shift.value())

    def run(self):
        if not all(self.readers):
            return self.warn("Choose two sessions first")
        try:
            limits = [cmp.Limit.parse(x) for x in self.limits.toPlainText().splitlines()
                      if x.strip()]
            a, b = (self.t_from.value(), self.t_to.value()) if self.cut.isChecked() else (
                None, None)
            if a is not None and b <= a:
                raise cmp.CompareError("“To” must be after “from”")
            self.c = cmp.compare(*self.readers, self.align(), a, b, limits)
        except cmp.CompareError as e:
            return self.warn(str(e))
        shown = {self.list.item(i).text() for i in range(self.list.count())
                 if self.list.item(i).checkState() == Qt.CheckState.Checked}
        keys = [r.key for r in self.c.rows]
        if not shown & set(keys):
            shown = {x.signal for x in limits} & set(keys) or set(
                [k for k in keys if "current" in k][:2] or keys[:2])
        self.list.blockSignals(True)
        self.list.clear()
        for k in keys:
            it = QListWidgetItem(k)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if k in shown else Qt.CheckState.Unchecked)
            self.list.addItem(it)
        self.list.blockSignals(False)
        self.draw()

    def shown(self) -> set[str]:
        return {self.list.item(i).text() for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked}

    def draw(self):
        c = self.c
        if c is None:
            return
        keys = [r.key for r in c.rows if r.key in self.shown()]
        self.glw.clear()
        first = {}
        row = 0
        for k in keys[:6]:
            a, b = c.base[k], c.new[k]
            p = self.glw.addPlot(row=row, col=0)
            d = self.glw.addPlot(row=row + 1, col=0)
            row += 2
            for q in (p, d):
                q.showGrid(x=True, y=True, alpha=0.15)
                q.getAxis("left").setWidth(70)
                q.setClipToView(True)
                q.setDownsampling(auto=True, mode="peak")
                if a.rows in first:
                    q.setXLink(first[a.rows])
                first.setdefault(a.rows, q)
            p.setLabel("left", k, units=a.unit or None)
            d.setLabel("left", "new − base", units=a.unit or None)
            d.setMaximumHeight(110)
            p.addLegend(offset=(-10, 8), brush=pg.mkBrush(20, 22, 26, 200))
            for s, col, name in ((a, C_BASE, "base"), (b, C_NEW, "new")):
                x, y = (s.t, s.y) if s.rows else break_gaps(s.t, s.y)
                p.plot(x, y, pen=pg.mkPen(col, width=1.4), connect="finite", name=name)
            t, y = cmp.difference(a, b)
            d.plot(t, y, pen=pg.mkPen(C_DIFF, width=1.2), connect="finite")
            d.addItem(pg.InfiniteLine(0, 0, pen=pg.mkPen(C_MUTED, style=Qt.PenStyle.DashLine)))
            d.setLabel("bottom", "Row" if a.rows else "Time", units=None if a.rows else "s")
            touch.plots(p, d)
        rows = cmp.table(c, set(keys))
        self.table.setRowCount(len(rows))
        for r, vals in enumerate(rows):
            for n, x in enumerate(vals):
                it = QTableWidgetItem(x if isinstance(x, str) else
                                      ("" if x != x else f"{x:+.3f}" if n == 6 else f"{x:.6g}"))
                self.table.setItem(r, n, it)
        self.table.resizeColumnsToContents()
        parts = []
        if c.verdict:
            col = C_GOOD if c.verdict == "PASS" else C_BAD
            parts.append(f"<b style='color:{QColor(col).name()}'>{c.verdict}</b>")
            parts += [report.esc(f"{t}: {why}") for t, ok, why in c.checks if not ok][:4]
        for text, only in zip(("base", "new"), c.only):
            if only:
                parts.append(report.esc(f"only in the {text} session: {len(only)} signals"))
        if not c.rows:
            parts.append("The sessions have no signal in common")
        if len(keys) > 6:
            parts.append("the first 6 chosen signals are plotted")
        self.result.setText(" · ".join(parts))

    # ---- out
    def save_csv(self):
        if self.c is None:
            return self.warn("Choose two sessions first")
        path, _ = QFileDialog.getSaveFileName(self, "Save CSV", "comparison.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            cmp.save_csv(self.c, path, self.shown())
        except OSError as e:
            self.warn(f"Can't write {path}:\n{e}")

    def report(self):
        if self.c is None:
            return self.warn("Choose two sessions first")
        path = report_dialog.ask_path(self, report_dialog.default_name("comparison"))
        if path:
            report_dialog.write(self, report.compare_report("Comparison", self.c, self.shown()),
                                path)

    def release(self):
        for r in self.readers:
            if r is not None:
                r.close()
        self.readers = [None, None]
