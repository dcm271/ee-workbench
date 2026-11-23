"""What the measurement windows (Bode, regulator, battery, meter check) share:
Run / Stop, a runner, instrument choosers, the log line, saved settings."""
from __future__ import annotations

import csv

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel, QMainWindow,
                             QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout,
                             QWidget)

from ..roles import ports
from . import report_dialog, touch
from .panel_base import open_panels
from .runner import Runner
from .theme import set_role
from .widgets import C_MUTED


class InstBox(QComboBox):
    """The open tabs that `accept` likes; refreshed whenever it is opened."""

    def __init__(self, accept, optional: str = ""):
        super().__init__()
        self.accept, self.optional = accept, optional
        self.setMinimumWidth(170)
        self.refresh()

    def names(self) -> list[str]:
        return [p.display_name() for p in open_panels() if self.accept(p)]

    def refresh(self):
        cur = self.currentText()
        names = ([self.optional] if self.optional else []) + self.names()
        if names != [self.itemText(i) for i in range(self.count())]:
            self.blockSignals(True)
            self.clear()
            self.addItems(names)
            if cur in names:
                self.setCurrentText(cur)
            self.blockSignals(False)

    def showPopup(self):
        self.refresh()
        super().showPopup()

    def name(self) -> str:
        t = self.currentText()
        return "" if t == self.optional else t

    def choose(self, name: str):
        self.refresh()
        i = self.findText(name)
        if name and i >= 0:
            self.setCurrentIndex(i)


class PortBox(InstBox):
    """The open sources / loads (roles.py)."""

    def __init__(self, *roles: str, optional: str = ""):
        self.roles = roles
        super().__init__(lambda p: True, optional)

    def found(self) -> list:
        insts = [PortInst(p) for p in open_panels() if p.connected]
        return [f for f in ports(insts) if f.role in self.roles]

    def names(self):
        return [f.text for f in self.found()]

    def tab(self) -> str:
        """The name of the tab the chosen port belongs to."""
        return next((f.name for f in self.found() if f.text == self.name()), "")


class PortInst:
    """Just enough of a tab for roles.ports()."""

    def __init__(self, p):
        self.p, self.name, self.kind = p, p.display_name(), p.KIND
        self.set_names, self.measure_names = p.set_names(), p.measure_names()


class ToolWindow(QMainWindow):
    TITLE = "Tool"
    KEY = "tool"                 # where its settings live in bench.json

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle(self.TITLE)
        self.resize(1240, 760)
        self.runner = Runner(self.TITLE)
        self.runner.log.connect(self.say)
        self.runner.event.connect(self.on_event)
        self.runner.done.connect(self._finished)
        central = QWidget()
        v = QVBoxLayout(central)
        top = QHBoxLayout()
        self.run_btn = QPushButton("▶ Run")
        set_role(self.run_btn, "primary")
        self.run_btn.clicked.connect(self.run)
        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.runner.stop)
        top.addWidget(self.run_btn)
        top.addWidget(self.stop_btn)
        for text, fn in self.buttons():
            b = QPushButton(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color:{C_MUTED};")
        top.addWidget(self.status, 1)
        v.addLayout(top)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        left = touch.scroll(self.build_left())
        left.setMinimumWidth(400 if touch.TOUCH else 340)
        split.addWidget(left)
        split.addWidget(self.build_right())
        split.setStretchFactor(1, 1)
        split.setSizes([420, 840])
        v.addWidget(split, 1)
        self.setCentralWidget(central)
        try:
            self.restore(dict(main.prefs.get(self.KEY) or {}))
        except (TypeError, ValueError, KeyError):
            pass                 # settings of another version

    # ---- for subclasses
    def buttons(self) -> list:
        return []

    def build_left(self) -> QWidget:
        raise NotImplementedError

    def build_right(self) -> QWidget:
        raise NotImplementedError

    def settings(self) -> dict:
        return {}

    def restore(self, s: dict):
        pass

    def job(self):
        """Returns fn(bench), or None after telling the user what is wrong."""
        raise NotImplementedError

    def on_event(self, e):
        pass

    def finished(self, outcome: str):
        pass

    # ---- shared
    def make_log(self) -> QPlainTextEdit:
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        return self.log_view

    def say(self, text: str):
        self.log_view.appendPlainText(text)

    def warn(self, text: str):
        QMessageBox.warning(self, self.TITLE, text)

    def confirm(self, text: str) -> bool:
        return QMessageBox.question(self, self.TITLE, text) == QMessageBox.StandardButton.Yes

    def remember(self):
        self.main.prefs[self.KEY] = self.settings()
        self.main.save_bench_state()

    def run(self):
        seq = getattr(self.main, "seq_win", None)
        if seq is not None and seq.running():
            return self.warn("The sequencer is running: stop it first")
        fn = self.job()
        if fn is None:
            return
        self.remember()
        self.log_view.clear()
        err = self.runner.start(fn)
        if err:
            return self.warn(err)
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("Running…")

    def _finished(self, outcome: str):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText(outcome)
        self.say(outcome)
        self.finished(outcome)

    def save_rows(self, header, rows, name: str):
        if not rows:
            return self.warn("Nothing measured yet")
        path, _ = QFileDialog.getSaveFileName(self, "Save CSV", name, "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(header)
                w.writerows(rows)
        except OSError as e:
            self.warn(f"Can't write {path}:\n{e}")

    def save_report(self, html: str, prefix: str):
        path = report_dialog.ask_path(self, report_dialog.default_name(prefix))
        if path:
            report_dialog.write(self, html, path)

    def inst_list(self) -> list[tuple]:
        """(name, identity, calibration) of the instruments used, for reports."""
        out = []
        for p in open_panels():
            if p.display_name() in self.used():
                out.append((p.display_name(), p.identity(), p.calibration()))
        return out

    def used(self) -> list[str]:
        return []

    def showEvent(self, e):
        for b in self.findChildren(InstBox):
            b.refresh()
        super().showEvent(e)

    def closeEvent(self, e):
        """Closing the window leaves a run going (like the sequencer's)."""
        self.remember()
        super().closeEvent(e)
