"""Calibration window (Tools ▾): the log of every instrument's calibration, and
“Check against a reference”: one meter's readings beside a better one's
(calibration.py)."""
from __future__ import annotations

import time
from dataclasses import asdict
from datetime import date

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
                             QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                             QVBoxLayout, QWidget)

from .. import accuracy
from .. import calibration as cal
from .. import report
from . import touch
from .battery_window import ReadingBox
from .forms import hint, integer, number
from .panel_base import open_panels
from .tool_window import PortBox, ToolWindow
from .widgets import C_BAD, C_GOOD, C_MUTED, C_VOLT, C_WARN

HEAD = ["Instrument", "Status", "Calibrated", "Due", "By", "Certificate", "Last check",
        "Identity"]
COLOR = {cal.OK: C_GOOD, cal.SOON: C_VOLT, cal.OVERDUE: C_BAD, cal.NONE: C_MUTED,
         cal.NOT_NEEDED: C_MUTED}
CHECK_HEAD = ["Level", "Reference", "Reading", "Error", "Error %", "Allowed ±", "Result"]


class EntryDialog(QDialog):
    """A new calibration of one instrument (or its details)."""

    def __init__(self, parent, e: cal.Entry, new: bool):
        super().__init__(parent)
        self.setWindowTitle("New calibration" if new else "Instrument details")
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.name = QLineEdit(e.name)
        f.addRow("Name", self.name)
        self.day = QLineEdit(date.today().isoformat() if new else e.calibrated)
        self.day.setPlaceholderText("yyyy-mm-dd")
        f.addRow("Calibrated on", self.day)
        self.months = integer("Interval in months", 0, 240, e.months, " months")
        self.months.setSpecialValueText("not required")
        f.addRow("Every", self.months)
        self.by = QLineEdit(e.by)
        f.addRow("By", self.by)
        self.cert = QLineEdit("" if new else e.certificate)
        f.addRow("Certificate", self.cert)
        self.notes = QPlainTextEdit("" if new else e.notes)
        self.notes.setMaximumHeight(90)
        f.addRow("Notes", self.notes)
        self.acc = QPlainTextEdit(accuracy.lines(e.accuracy))
        self.acc.setMaximumHeight(110)
        self.acc.setPlaceholderText("DC V = 0.1: 0.005% + 3.5u; 1: 0.004% + 7u; 10: 0.0035% + 50u"
                                    "\ncurrent = 0.1% + 2m")
        self.acc.setToolTip("From the datasheet or the certificate, one a line: the meter's "
                            "function or the reading's name = of the reading + a fixed part.\n"
                            "By range: end of range: specification; …")
        f.addRow("Accuracy", self.acc)
        v.addLayout(f)
        self.err = QLabel("")
        self.err.setStyleSheet(f"color:{C_WARN};")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.new = new
        self.specs = dict(e.accuracy)

    def _ok(self):
        day = self.day.text().strip()
        if (day or self.new) and cal.parse_date(day) is None:
            self.err.setText("The date must be yyyy-mm-dd")
            return
        if day and cal.parse_date(day) > date.today():
            self.err.setText("That date is in the future")
            return
        try:
            self.specs = accuracy.parse_lines(self.acc.toPlainText())
        except accuracy.AccuracyError as e:
            self.err.setText(str(e))
            return
        self.accept()


class CalibrationWindow(ToolWindow):
    TITLE = "Calibration"
    KEY = "metercheck"

    def __init__(self, main):
        self.log = cal.Log()
        self.rows: list[dict] = []
        super().__init__(main)
        self.run_btn.setText("▶ Run check")
        self.fill()

    def buttons(self):
        return [("Status report…", self.status_report)]

    def build_left(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 6, 0)
        g = QGroupBox("Check against a reference")
        f = QFormLayout(g)
        self.dut = ReadingBox(lambda p, n: p.KIND in ("dmm", "scpi", "supply", "ppk2"))
        f.addRow("Instrument", self.dut)
        self.ref = ReadingBox(lambda p, n: p.KIND in ("dmm", "scpi", "supply", "ppk2"))
        f.addRow("Reference", self.ref)
        self.source = PortBox("source", optional="(one point, as it is now)")
        f.addRow("Source", self.source)
        self.levels = QLineEdit("0, 1, 2, 5, 10")
        self.levels.setToolTip("5, 9, 12    or    0 … 10 step 1    or    0..10 x 11")
        f.addRow("Levels (V)", self.levels)
        self.current_limit = number("Source current limit", 0.001, 10, 0.1, 3, " A")
        f.addRow("Source limit", self.current_limit)
        self.percent = number("Tolerance", 0, 100, 0.1, 4, " %")
        f.addRow("Allowed ±", self.percent)
        self.floor = number("plus", 0, 1e6, 0.001, 6)
        f.addRow("plus", self.floor)
        self.settle = number("Settling time", 0, 600, 2, 1, " s")
        f.addRow("Settle", self.settle)
        self.average = number("Averaging time", 0, 600, 2, 1, " s")
        f.addRow("Average", self.average)
        v.addWidget(g)
        v.addWidget(hint("Connect both to the same source: voltmeters in parallel, ammeters in "
                         "series. The reference should be at least four times better than the "
                         "tolerance you allow. A check is kept with the instrument's entry; it "
                         "doesn't change its calibration date."))
        v.addWidget(self.make_log(), 1)
        return w

    def build_right(self):
        tabs = QTabWidget()
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        for text, fn in (("Add open instruments", self.add_open), ("New calibration…", self.new),
                         ("Details…", self.details), ("Remove", self.remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color:{C_WARN};")
        v.addWidget(self.note)
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(HEAD))
        self.table.setHorizontalHeaderLabels(HEAD)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44 if touch.TOUCH else 30)
        self.table.itemSelectionChanged.connect(self._show_history)
        self.table.itemDoubleClicked.connect(lambda *_: self.details())
        split.addWidget(self.table)
        self.hist = QPlainTextEdit()
        self.hist.setReadOnly(True)
        split.addWidget(self.hist)
        split.setSizes([420, 160])
        v.addWidget(split, 1)
        tabs.addTab(w, "Log")
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.result = QLabel("")
        row.addWidget(self.result, 1)
        for text, fn in (("Keep in log", self.keep), ("Save CSV…", self.save_csv),
                         ("Report…", self.report)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)
        self.points = QTableWidget(0, len(CHECK_HEAD))
        self.points.setHorizontalHeaderLabels(CHECK_HEAD)
        self.points.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.points.verticalHeader().setVisible(False)
        v.addWidget(self.points, 1)
        tabs.addTab(w, "Check")
        self.tabs = tabs
        return tabs

    # ---- the log
    def fill(self):
        self.table.setRowCount(0)
        for e in self.log.entries:
            r = self.table.rowCount()
            self.table.insertRow(r)
            st = e.status()
            due = e.due()
            last = f"{e.checks[-1].get('date', '')} {e.checks[-1].get('result', '')}" \
                if e.checks else ""
            for c, text in enumerate((e.name or e.key, st, e.calibrated,
                                      due.isoformat() if due else "", e.by, e.certificate, last,
                                      e.idn or e.resource)):
                it = QTableWidgetItem(text)
                if c == 1:
                    it.setForeground(QColor(COLOR[st]))
                self.table.setItem(r, c, it)
        late = [e.name or e.key for e in self.log.entries if e.status() == cal.OVERDUE]
        self.note.setText(self.log.error or
                          ("Overdue: " + ", ".join(late) if late else ""))

    def _entry(self):
        r = self.table.currentRow()
        return self.log.entries[r] if 0 <= r < len(self.log.entries) else None

    def _need(self):
        e = self._entry()
        if e is None:
            self.warn("Choose an instrument in the log first")
        return e

    def _save(self):
        try:
            self.log.save()
        except OSError as e:
            self.warn(f"Can't write {self.log.path}:\n{e}")
        self.fill()

    def add_open(self):
        n = 0
        for p in open_panels():
            if not p.connected or cal.simulated(p.resource) or p.KIND == "uart":
                continue
            if self.log.find(p.identity(), p.resource) is None:
                n += 1
            self.log.get(p.identity(), p.resource, p.display_name())
        if n or self.log.entries:
            self._save()
        self.status.setText(f"{n} added" if n else "No new instruments open (simulators and "
                            "serial consoles aren't listed)")

    def new(self):
        e = self._need()
        if e is None:
            return
        d = EntryDialog(self, e, True)
        if d.exec():
            e.accuracy = d.specs
            e.name, e.months = d.name.text().strip() or e.name, d.months.value()
            e.record(d.day.text().strip(), d.by.text().strip(), d.cert.text().strip(),
                     d.notes.toPlainText().strip())
            self._save()

    def details(self):
        e = self._need()
        if e is None:
            return
        d = EntryDialog(self, e, False)
        if d.exec():
            e.accuracy = d.specs
            e.name, e.months = d.name.text().strip() or e.name, d.months.value()
            e.calibrated, e.by = d.day.text().strip(), d.by.text().strip()
            e.certificate, e.notes = d.cert.text().strip(), d.notes.toPlainText().strip()
            self._save()

    def remove(self):
        e = self._need()
        if e is not None and self.confirm(f"Remove {e.name or e.key} and its history from the "
                                          "log?"):
            self.log.entries.remove(e)
            self._save()

    def _show_history(self):
        e = self._entry()
        if e is None:
            return self.hist.clear()
        out = [e.idn or e.resource, e.describe()]
        if e.notes:
            out.append(e.notes)
        out += [f"before: {h.get('calibrated', '')}  {h.get('by', '')}  "
                f"{h.get('certificate', '')}  {h.get('notes', '')}" for h in
                reversed(e.history)]
        out += [f"check {c.get('date', '')}: {c.get('result', '')} against "
                f"{c.get('reference', '')}, worst error {c.get('worst', 0):.6g} "
                f"({len(c.get('rows', []))} points)" for c in reversed(e.checks)]
        self.hist.setPlainText("\n".join(out))

    def status_report(self):
        self.save_report(report.calibration_report(self.log.entries), "calibration")

    # ---- a check
    NUM = ("percent", "floor", "settle", "average", "current_limit")

    def current(self):
        return cal.CheckSettings(dut=self.dut.name(), ref=self.ref.name(),
                                 source=self.source.name(), levels=self.levels.text(),
                                 **{k: getattr(self, k).value() for k in self.NUM})

    def settings(self):
        return asdict(self.current())

    def restore(self, s):
        for k in ("dut", "ref", "source"):
            getattr(self, k).choose(s.get(k, ""))
        if s.get("levels"):
            self.levels.setText(str(s["levels"]))
        for k in self.NUM:
            if k in s:
                getattr(self, k).setValue(float(s[k]))

    def used(self):
        return [self.dut.name().partition(": ")[0], self.ref.name().partition(": ")[0],
                self.source.tab()]

    def job(self):
        s = self.current()
        if s.problem():
            self.warn(s.problem())
            return None
        if s.source and not self.confirm(f"{s.source} will be switched on at {s.levels} V."):
            return None
        self.rows, self.run_with = [], s
        self.run_insts = self.inst_list()
        self.points.setRowCount(0)
        self.result.setText("")
        self.tabs.setCurrentIndex(1)
        return lambda bench: cal.Check(s).run(bench, self.runner.event.emit)

    def on_event(self, row):
        self.rows.append(row)
        r = self.points.rowCount()
        self.points.insertRow(r)
        for c, k in enumerate(("level", "reference", "reading", "error", "percent", "limit",
                               "result")):
            x = row[k]
            it = QTableWidgetItem(x if isinstance(x, str) else f"{x:.7g}")
            if k == "result":
                it.setForeground(QColor(C_GOOD if x == "PASS" else C_BAD))
            self.points.setItem(r, c, it)

    def finished(self, outcome):
        self.outcome = outcome
        v = cal.verdict(self.rows)
        self.result.setText(f"{outcome} · {v}" if v else outcome)

    def keep(self):
        if not self.rows or getattr(self, "outcome", "") != "Finished" or self.runner.running():
            return self.warn("Run a check to its end first")
        tab = self.run_with.dut.partition(": ")[0]
        p = next((p for p in open_panels() if p.display_name() == tab), None)
        if p is None:
            return self.warn(f"{tab} isn't open any more")
        if cal.simulated(p.resource):
            return self.warn("Simulated instruments aren't kept in the log")
        e = self.log.get(p.identity(), p.resource, p.display_name())
        e.checks.append({"date": time.strftime("%Y-%m-%d %H:%M"), "result":
                         cal.verdict(self.rows), "reference": self.run_with.ref,
                         "worst": max((r["error"] for r in self.rows), key=abs),
                         "settings": asdict(self.run_with), "rows": list(self.rows)})
        self._save()
        self.status.setText(f"Kept with {e.name or e.key}")

    def save_csv(self):
        keys = ["level", "reference", "reading", "error", "percent", "limit", "result"]
        self.save_rows(keys, [[r[k] for k in keys] for r in self.rows], "check.csv")

    def report(self):
        if not self.rows:
            return self.warn("Nothing measured yet")
        self.save_report(report.check_report(
            f"Check of {self.run_with.dut}", self.rows, cal.verdict(self.rows),
            asdict(self.run_with), self.run_insts), "check")
