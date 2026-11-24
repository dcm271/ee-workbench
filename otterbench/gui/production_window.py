"""Production test: scan or type a serial number, the sequencer runs, and a
big PASS / FAIL says what to do with the unit. Every unit is kept in a results
database (history, yield, CSV export), with an HTML report per unit if wanted.

The sequence is whatever the Sequencer window holds (steps or a script). The
serial number reaches it as `serial` (a Note can say {serial}; a script reads
the variable), along with `product` and `operator`."""
from __future__ import annotations

import re
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog, QFormLayout,
                             QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QMainWindow, QMessageBox, QPushButton, QSizePolicy,
                             QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from ..production import ResultsDB, verdict
from ..report import sequence_report
from ..session import recorder as session
from . import touch
from .theme import set_role
from .widgets import C_MUTED, MONO, io_errors

BANNER = {"READY": ("#2a2f36", "#c9ccd1"), "TESTING": ("#1e3a5f", "#e3f2fd"),
          "PASS": ("#1b5e20", "#ffffff"), "FAIL": ("#b71c1c", "#ffffff"),
          "ERROR": ("#8d6e00", "#ffffff")}


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "unit"


class ProductionWindow(QMainWindow):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Production test")
        self.resize(1100, 760)
        prefs = main.prefs.setdefault("production", {})
        self.db = ResultsDB(prefs.get("db") or None)
        self.serial = ""
        self.started = 0.0

        central = QWidget()
        v = QVBoxLayout(central)

        setup = QGroupBox("Station")
        form = QGridLayout(setup)
        self.product = QLineEdit(prefs.get("product", ""))
        self.product.setPlaceholderText("e.g. Sensor board rev C")
        self.operator = QLineEdit(prefs.get("operator", ""))
        self.operator.setPlaceholderText("your name or initials")
        self.pattern = QLineEdit(prefs.get("pattern", ""))
        self.pattern.setPlaceholderText(r"optional check, e.g. SB\d{6}")
        self.pattern.setToolTip("A regular expression the whole serial number must match "
                                "(catches a mis-scan). Leave blank to accept anything.")
        self.reports = QCheckBox("Save an HTML report for every unit")
        self.reports.setChecked(bool(prefs.get("reports", True)))
        self.report_dir = QLineEdit(prefs.get("report_dir", str(Path.home() /
                                                                "otterbench-reports")))
        browse = QPushButton("…")
        browse.setMaximumWidth(40)
        browse.clicked.connect(self._pick_dir)
        for w in (self.product, self.operator, self.pattern, self.report_dir):
            w.editingFinished.connect(self._save_prefs)
        self.reports.toggled.connect(self._save_prefs)
        form.addWidget(QLabel("Product"), 0, 0)
        form.addWidget(self.product, 0, 1)
        form.addWidget(QLabel("Operator"), 0, 2)
        form.addWidget(self.operator, 0, 3)
        form.addWidget(QLabel("Serial format"), 1, 0)
        form.addWidget(self.pattern, 1, 1)
        form.addWidget(self.reports, 1, 2)
        row = QHBoxLayout()
        row.addWidget(self.report_dir, 1)
        row.addWidget(browse)
        form.addLayout(row, 1, 3)
        form.setColumnStretch(1, 1)
        form.setColumnStretch(3, 1)
        v.addWidget(setup)

        entry = QHBoxLayout()
        lab = QLabel("Serial number")
        lab.setStyleSheet("font-size:16px;")
        entry.addWidget(lab)
        self.sn = QLineEdit()
        self.sn.setPlaceholderText("Scan the barcode or type, then Enter")
        f = QFont(MONO, 20)
        self.sn.setFont(f)
        self.sn.setMinimumHeight(52)
        self.sn.returnPressed.connect(self.start)
        entry.addWidget(self.sn, 1)
        self.go = QPushButton("▶ Test")
        self.go.setMinimumHeight(52)
        self.go.setMinimumWidth(120)
        set_role(self.go, "primary")
        self.go.clicked.connect(self.start)
        entry.addWidget(self.go)
        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.setMinimumHeight(52)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(lambda: self.main.sequencer().stop())
        entry.addWidget(self.stop_btn)
        v.addLayout(entry)

        self.banner = QLabel("READY")
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bf = QFont()
        bf.setPointSize(44 if not touch.TOUCH else 40)
        bf.setBold(True)
        self.banner.setFont(bf)
        self.banner.setMinimumHeight(130)
        self.banner.setWordWrap(True)
        v.addWidget(self.banner)
        self.detail = QLabel("Scan a serial number to start. The sequence in the Sequencer "
                             "window is run for each unit.")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(f"color:{C_MUTED};")
        # long report paths must never widen the window
        self.detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        v.addWidget(self.detail)

        split = QSplitter(Qt.Orientation.Horizontal)
        hist = QWidget()
        hv = QVBoxLayout(hist)
        hv.setContentsMargins(0, 0, 0, 0)
        hl = QLabel("History (this product, newest first)")
        hl.setStyleSheet(f"color:{C_MUTED};")
        hv.addWidget(hl)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Serial", "Result", "Finished", "s", "Operator",
                                              "Failures"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.cellDoubleClicked.connect(self._open_report)
        hv.addWidget(self.table, 1)
        split.addWidget(hist)
        stats = QGroupBox("Yield")
        stats.setToolTip("First-pass yield: units that passed on their first test. Final "
                         "yield: units whose latest test passed (after rework).")
        sf = QFormLayout(stats)
        self.stat = {}
        for key in ("Today", "Today's FPY", "All time", "Units tested", "First-pass yield",
                    "Final yield", "Pass / fail / error"):
            lab = QLabel("—")
            lab.setFont(QFont(MONO, 11))
            lab.setWordWrap(True)
            sf.addRow(key, lab)
            self.stat[key] = lab
        exp = QPushButton("Export CSV…")
        exp.clicked.connect(self.export)
        sf.addRow(exp)
        self.db_label = QLabel(self.db.path)
        self.db_label.setWordWrap(True)
        self.db_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.db_label.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        sf.addRow(self.db_label)
        split.addWidget(stats)
        split.setSizes([700, 380])
        v.addWidget(split, 1)
        self.setCentralWidget(central)
        self._show("READY")
        self.refresh()
        QTimer.singleShot(0, self.sn.setFocus)

    # ---------------------------------------------------------------- settings
    def _save_prefs(self, *_):
        self.main.prefs["production"] = {
            "product": self.product.text().strip(), "operator": self.operator.text().strip(),
            "pattern": self.pattern.text().strip(), "reports": self.reports.isChecked(),
            "report_dir": self.report_dir.text().strip(),
            "db": self.main.prefs.get("production", {}).get("db", "")}
        self.main.save_bench_state()
        self.refresh()

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Reports folder", self.report_dir.text())
        if d:
            self.report_dir.setText(d)
            self._save_prefs()

    # ---------------------------------------------------------------- running
    def _show(self, state: str, text: str = ""):
        bg, fg = BANNER[state]
        self.banner.setText(text or state)
        self.banner.setStyleSheet(f"background:{bg}; color:{fg}; border-radius:10px;")

    def start(self):
        sn = self.sn.text().strip()
        seq = self.main.sequencer()
        if not sn or seq.running():
            return
        pat = self.pattern.text().strip()
        if pat:
            try:
                ok = re.fullmatch(pat, sn) is not None
            except re.error as e:
                QMessageBox.warning(self, "Serial format", f"The serial format isn't a valid "
                                    f"regular expression: {e}")
                return
            if not ok:
                self._show("ERROR", "SERIAL?")
                self.detail.setText(f"“{sn}” doesn't match the serial format {pat}. "
                                    "Scan again.")
                self.sn.selectAll()
                return
        product = self.product.text().strip()
        before = self.db.previous(sn, product)
        if any(u.result == "PASS" for u in before):
            if QMessageBox.question(self, "Retest", f"{sn} already passed "
                                    f"({before[0].finished}). Test it again?") != \
                    QMessageBox.StandardButton.Yes:
                self.sn.selectAll()
                return
        self.serial = sn
        self.started = time.time()
        extra = {"serial": sn, "product": product, "operator": self.operator.text().strip()}
        if session.active:
            session.note(f"Production test: {sn}")
        if not seq.run(extra=extra, on_finish=self._finished):
            self._show("ERROR", "NO SEQUENCE")
            self.detail.setText("The Sequencer has no steps (or the sweep is invalid): set "
                                "up the test there first.")
            return
        self._show("TESTING", f"TESTING  {sn}")
        self.detail.setText(f"Running the sequence for {sn}… (Esc or ■ Stop to abort)")
        self.sn.setEnabled(False)
        self.go.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def _finished(self, outcome: str, bench):
        seq = self.main.sequencer()
        fails = [r.get("failures", "") for r in seq.rows if r.get("result") == "FAIL"]
        result = verdict(outcome, bench.passed, bench.failed)
        failures = "; ".join(f for f in fails if f)
        if result == "ERROR":
            failures = failures or outcome
        product = self.product.text().strip()
        uid = self.db.add(self.serial, product, self.operator.text().strip(), self.started,
                          result, failures, outcome, seq.rows)
        report = ""
        if self.reports.isChecked():
            report = self._write_report(seq, outcome, result)
            if report:
                self.db.set_report(uid, report)
        if session.active:
            session.note(f"Production test: {self.serial} {result}" +
                         (f" ({failures})" if failures else ""))
        self._show(result, f"{result}  {self.serial}")
        self.detail.setText((failures if result != "PASS" else
                             f"All checks passed in {time.time() - self.started:.1f} s") +
                            (f"\nReport: {report}" if report else ""))
        self.sn.setEnabled(True)
        self.go.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.sn.clear()
        self.sn.setFocus()
        self.refresh()

    def _write_report(self, seq, outcome: str, result: str) -> str:
        folder = Path(self.report_dir.text().strip() or Path.home() / "otterbench-reports")
        product = self.product.text().strip()
        if product:
            folder = folder / safe_name(product)
        name = f"{safe_name(self.serial)}_{time.strftime('%Y%m%d_%H%M%S')}_{result}.html"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            title = f"{product + ' · ' if product else ''}{self.serial} — {result}"
            html = sequence_report(title, seq.rows, seq.columns,
                                   getattr(seq, "run_plan_dict", None),
                                   seq.logbox.toPlainText(), getattr(seq, "run_insts", []),
                                   outcome, self.started)
            (folder / name).write_text(html)
            return str(folder / name)
        except OSError as e:
            self.main.status.showMessage(f"Couldn't save the unit report: {e}", 15000)
            return ""

    # ---------------------------------------------------------------- history
    def refresh(self):
        product = self.product.text().strip() or None
        units = self.db.history(product, 200)
        self.table.setRowCount(0)
        colors = {"PASS": "#7ee787", "FAIL": "#ff8a80", "ERROR": "#ffd54f"}
        for u in units:
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, val in enumerate((u.serial, u.result, u.finished.replace("T", " "),
                                     f"{u.seconds:.1f}", u.operator, u.failures)):
                it = QTableWidgetItem(str(val))
                if c == 1:
                    it.setForeground(QColor(colors.get(u.result, "#c9ccd1")))
                it.setData(Qt.ItemDataRole.UserRole, u.report)
                if u.report:
                    it.setToolTip("Double-click to open the report")
                self.table.setItem(r, c, it)
        today = time.strftime("%Y-%m-%dT00:00:00")
        t = self.db.yield_stats(product, today)
        a = self.db.yield_stats(product)

        def pct(x):
            return "—" if x is None else f"{100 * x:.1f} %"
        self.stat["Today"].setText(f"{t.passed}/{t.tested} pass")
        self.stat["Today's FPY"].setText(pct(t.first_pass_yield))
        self.stat["All time"].setText(f"{a.passed}/{a.tested} pass")
        self.stat["Units tested"].setText(str(a.units))
        self.stat["First-pass yield"].setText(pct(a.first_pass_yield))
        self.stat["Final yield"].setText(pct(a.final_yield))
        self.stat["Pass / fail / error"].setText(f"{a.passed} / {a.failed} / {a.errors}")

    def _open_report(self, r, _c):
        it = self.table.item(r, 0)
        path = it.data(Qt.ItemDataRole.UserRole) if it else ""
        if path and Path(path).exists():
            from PyQt6.QtCore import QUrl
            from PyQt6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    @io_errors("Export results")
    def export(self, *_):
        path, _ = QFileDialog.getSaveFileName(self, "Export results",
                                              time.strftime("results_%Y%m%d.csv"),
                                              "CSV (*.csv)")
        if path:
            n = self.db.export_csv(path, self.product.text().strip() or None)
            self.main.status.showMessage(f"Exported {n} results to {path}", 8000)

    def closeEvent(self, e):
        self._save_prefs()
        super().closeEvent(e)
