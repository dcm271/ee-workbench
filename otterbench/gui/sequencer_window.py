"""Sequencer window: steps (or a Python script) run across the open instruments.

The engine is `otterbench.sequencer`; this window edits the plan, runs it
on a thread of its own and shows the results. Each instrument tab is wrapped
in a `PanelInst`, which sends driver calls through that tab's worker queue
and reads its latest readings, so the tab keeps working (and showing) as
usual while the sequence drives it. Esc (all outputs off) stops a sequence.
"""
from __future__ import annotations

import csv
import json
import threading
import time
import traceback
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox,
                             QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                             QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                             QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
                             QWidget)

from ..accuracy import GUARDS
from ..clock import now
from ..config import config_dir
from ..report import failing_columns, sequence_report
from ..sequencer import (ACTIONS, MEASURE_WHAT, PM, PROFILE_WHAT, SET_WHAT, Bench, Inst,
                         LimitFailed, depths, parse_blocks, Plan, SequenceError, Step, Stopped, run_plan, run_script)
from ..session import recorder as session
from . import touch
from .panel_base import open_panels
from .theme import set_role
from .widgets import C_MUTED, C_WARN, MONO

EXAMPLE_SCRIPT = '''# The same `bench` the steps use. Stop / Esc ends it at the next bench call.
# bench.instruments()                       names of the open tabs
# bench.set(name, "voltage"|"current"|"output"|"ovp"|"sense_protect", value)
#   function generator: "function" "frequency" "amplitude" "offset" "duty" "output"
# bench.measure(name, what, seconds=0)      supply: voltage current power dvm
#                                           PPK2: current peak charge;  DMM: value
# bench.capture(name)                       logic analyzer; returns the capture
# bench.gpio(name, pin, "on"|"off"|"pulse 0.1")   bench.read_pin(name, pin)
# bench.wait_for(name, pin, 1, timeout=10)  returns the seconds it took
# bench.send(name, "text")                  serial console (or a SCPI tab's command)
# bench.wait_text(name, r"adc=(\\d+)", timeout=2)   its value / text; None: no reply
# bench.profile(name, "voltage", "ramp 3.0..3.6 for=10")
# bench.expect("label", value, lo=None, hi=None)  → the row is PASS / FAIL
# bench.wait(s)  bench.note(text)  bench.record(col=value, ...)  print(...)
for v in np.arange(3.0, 3.61, 0.1):
    bench.set("PPK2", "voltage", v)
    bench.wait(0.5)
    i = bench.measure("PPK2", "current", 1.0)
    bench.expect("i_avg", i, hi=0.02)
    bench.record(v=round(v, 3), i_avg=i)
'''
FAIL_BG = QBrush(QColor("#4a1c1f"))
FAIL_CELL = QBrush(QColor("#7a2227"))
NEAR_CELL = QBrush(QColor("#6b5516"))
PASS_FG = QBrush(QColor("#7ee787"))
FAIL_FG = QBrush(QColor("#ff8a80"))


def write_checkpoint(path, plan: dict, next_pass: int, total: int):
    """Where a steps-mode run has got to, so it can resume after a crash."""
    import json
    import os
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"plan": plan, "next_pass": next_pass, "total": total,
                                   "saved": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def read_checkpoint(path) -> dict | None:
    import json
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("plan"), dict) \
                and int(d.get("next_pass", -1)) >= 0:
            return d
    except (OSError, ValueError, TypeError):
        pass
    return None


def kind_of(panel) -> str:
    return getattr(panel, "KIND", "")


class Invoker(QObject):
    """Runs a function on the GUI thread for the sequence thread, and waits."""
    _call = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self._call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, job):
        job()

    def __call__(self, fn, timeout=10.0):
        done, box = threading.Event(), {}

        def job():
            try:
                box["v"] = fn()
            except Exception as e:      # noqa: BLE001
                box["e"] = e
            done.set()
        self._call.emit(job)
        if not done.wait(timeout):
            raise SequenceError("The window didn't answer")
        if "e" in box:
            raise box["e"]
        return box.get("v")


class PanelInst(Inst):
    def __init__(self, panel, invoke: Invoker, stop: threading.Event):
        self.p, self.invoke, self.stop = panel, invoke, stop
        self.name = panel.display_name()
        self.kind = kind_of(panel)
        if self.kind in ("scpi", "scope", "uart", "dmm", "supply"):
            self.set_names = panel.set_names()
            self.setting_kinds = panel.setting_kinds()
            self.measure_names = panel.measure_names()
            self.profile_names = panel.profile_names()

    def uncertainty(self, what, value, r=None):
        return self.p.uncertainty(what, value, r)

    def unit_of(self, what: str) -> str:
        fn = getattr(self.p, "unit_of", None)
        return fn(what) if fn else ""

    def _need(self):
        if not self.p.connected:
            raise SequenceError(f"{self.name} isn't connected")

    def run(self, fn, timeout=30.0):
        self._need()
        done, box = threading.Event(), {}

        def job(drv):
            try:
                box["v"] = fn(drv)
            except Exception as e:      # noqa: BLE001
                box["e"] = e
            finally:
                done.set()
        self.p.worker.submit(job, refresh=True)
        end = time.monotonic() + timeout
        while not done.wait(0.05):
            if self.stop.is_set():
                raise Stopped()
            if time.monotonic() > end:
                raise SequenceError(f"{self.name} didn't answer in {timeout:g} s")
        if "e" in box:
            raise SequenceError(f"{self.name}: {box['e']}")
        return box.get("v")

    def reading_after(self, t, timeout):
        self._need()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r = self.p.last_reading
            if r is not None and getattr(r, "t", -1) > t:
                return r
            if self.stop.wait(0.02):
                return None
        return None

    def current_between(self, t0, t1):
        self._need()
        t, i, _ = self.invoke(lambda: self.p.ring.between(t0, t1))
        if t.size < 2:
            raise SequenceError(f"{self.name} has no samples for that time: press ▶ Run "
                                "on its tab so it is measuring")
        return float(i.mean()), float(i.max())

    def capture(self, timeout):
        self._need()
        old = self.invoke(lambda: self.p.capture)
        self.invoke(self.p.capture_now)
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self.stop.wait(0.05):
                self.invoke(self.p.abort)
                return None
            cap, busy = self.invoke(lambda: (self.p.capture, self.p._busy))
            if cap is not old and not busy:
                return cap
            if not busy and time.monotonic() - start > 1.0:
                return None              # it failed (the tab shows why)
        self.invoke(self.p.abort)
        return None

    def outputs_off(self):
        if self.kind in ("supply", "ppk2", "gpio", "fgen", "scpi") and self.p.connected:
            self.invoke(self.p.output_off)


class Signals(QObject):
    row = pyqtSignal(dict)
    log = pyqtSignal(str)
    done = pyqtSignal(str)
    note = pyqtSignal(str)


class SequencerWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sequencer")
        self.resize(1280, 820)
        self.path = config_dir() / "sequence.json"
        self.invoke = Invoker()
        self.sig = Signals()
        self.sig.row.connect(self._on_row)
        self.sig.log.connect(self._log)
        self.sig.done.connect(self._on_done)
        self.sig.note.connect(self._on_note)
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.rows: list[dict] = []
        self.columns: list[str] = []

        central = QWidget()
        root = QVBoxLayout(central)
        root.addLayout(self._build_run_bar())
        split = QSplitter(Qt.Orientation.Vertical)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_steps_tab(), "Steps")
        self.tabs.addTab(self._build_script_tab(), "Script")
        split.addWidget(self.tabs)
        split.addWidget(self._build_results())
        split.setSizes([380, 440])
        root.addWidget(split, 1)
        self.setCentralWidget(central)
        self._load(self.path, quiet=True)
        self._loop_changed()

    # ---------------------------------------------------------------- building
    def _build_run_bar(self):
        bar = QHBoxLayout()
        self.loop = QComboBox()
        for key, lab in (("once", "Run once"), ("repeat", "Repeat"), ("sweep", "Sweep x")):
            self.loop.addItem(lab, key)
        self.loop.currentIndexChanged.connect(self._loop_changed)
        bar.addWidget(self.loop)
        self.count = QSpinBox()
        self.count.setRange(1, 1_000_000)
        self.count.setSuffix(" times")
        bar.addWidget(self.count)
        self.sweep_box = QWidget()
        sb = QHBoxLayout(self.sweep_box)
        sb.setContentsMargins(0, 0, 0, 0)
        self.x_from, self.x_to, self.x_step = (QDoubleSpinBox() for _ in range(3))
        for lab, w in (("from", self.x_from), ("to", self.x_to), ("step", self.x_step)):
            w.setRange(-1e6, 1e6)
            w.setDecimals(4)
            sb.addWidget(QLabel(lab))
            sb.addWidget(w)
            touch.attach_keypad(w, f"x {lab}")
        self.x_to.setValue(1.0)
        self.x_step.setValue(0.1)
        bar.addWidget(self.sweep_box)
        self.off_chk = QCheckBox("Outputs off at the end")
        self.off_chk.setChecked(True)
        self.off_chk.setToolTip("Turn every supply and PPK2 output off when the sequence ends, "
                                "is stopped or fails")
        bar.addWidget(self.off_chk)
        self.fail_chk = QCheckBox("Stop on first fail")
        self.fail_chk.setToolTip("End the run (outputs off) as soon as a pass fails a limit")
        bar.addWidget(self.fail_chk)
        self.guard = QComboBox()
        for k, text in GUARDS.items():
            self.guard.addItem(text, k)
        self.guard.setToolTip("Readings closer to a limit than their own ± (from the accuracy "
                              "in the calibration log or the definition file). Without one, "
                              "limits are taken as they are.")
        bar.addWidget(self.guard)
        bar.addStretch(1)
        self.status = QLabel("Ready")
        self.status.setStyleSheet(f"color:{C_MUTED};")
        bar.addWidget(self.status)
        self.run_btn = QPushButton("▶ Run")
        set_role(self.run_btn, "primary")
        self.run_btn.clicked.connect(self.run)
        bar.addWidget(self.run_btn)
        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        bar.addWidget(self.stop_btn)
        for b in (self.run_btn, self.stop_btn):
            b.setMinimumWidth(100)
            if touch.TOUCH:
                b.setMinimumHeight(48)
        return bar

    def _build_steps_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Action", "Instrument", "Setting / quantity / pin",
                                              "Value  (seconds / text / profile)", "Min", "Max",
                                              "Results column"])
        hh = self.table.horizontalHeader()
        for c, mode in enumerate((QHeaderView.ResizeMode.ResizeToContents,
                                  QHeaderView.ResizeMode.Stretch,
                                  QHeaderView.ResizeMode.ResizeToContents,
                                  QHeaderView.ResizeMode.Stretch,
                                  QHeaderView.ResizeMode.Interactive,
                                  QHeaderView.ResizeMode.Interactive,
                                  QHeaderView.ResizeMode.Stretch)):
            hh.setSectionResizeMode(c, mode)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 90)
        self.table.horizontalHeaderItem(4).setToolTip(
            "Measure / Capture / Wait for: the lowest value that passes (blank: no limit). "
            "SI-style numbers like 10e-6, or expressions in x")
        self.table.horizontalHeaderItem(5).setToolTip("The highest value that passes")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setDefaultSectionSize(40 if touch.TOUCH else 30)
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, fn in (("＋ Step", self.add_step), ("Remove", self.remove_step),
                         ("▲", lambda: self.move_step(-1)), ("▼", lambda: self.move_step(1)),
                         ("Load…", self.load_dialog), ("Save…", self.save_dialog)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        hint = QLabel("Values can use x (the sweep value), i (pass, from 0), n and math: "
                      "e.g.  x,  x / 2,  3.3 + 0.1 * i.   Measure's value = seconds to average "
                      "(blank: one reading); Min / Max make the pass PASS or FAIL.   "
                      "GPIO Set: on, off or pulse 0.1.   Wait for: a GPIO input's level, e.g. "
                      "1 timeout=10 (its column = seconds it took).   Serial console: Send a "
                      "line, then Wait for a regex, e.g.  adc=(\\d+) timeout=2  (its column = "
                      "the number in the parentheses; no reply fails the pass).   Profile: e.g. "
                      "ramp 3.0..3.6 for=10.   Command: a program, e.g.  esptool write_flash 0 "
                      "app.bin timeout=120  (its exit status must be 0; setting = a regex for "
                      "its output).   For: a variable and its values, e.g. y and 1, 2, 5 or "
                      "0..10 x 11.   Repeat: 10, or until vout < 0.1 max=100 wait=1.   Retry: "
                      "3 wait=1 (again while a limit fails).   If: vout > 3.2, with an Else "
                      "if you like.   Each of these needs an End; results columns can be used "
                      "by name (Vout → vout).   Note text can include {x} and {i}.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        v.addLayout(row)
        v.addWidget(hint)
        return w

    def _build_script_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.use_script = QCheckBox("Run the script instead of the steps")
        v.addWidget(self.use_script)
        self.script = QPlainTextEdit(EXAMPLE_SCRIPT)
        self.script.setFont(QFont(MONO, 10))
        v.addWidget(self.script, 1)
        return w

    def _build_results(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Results"))
        row.addStretch(1)
        row.addWidget(QLabel("Plot"))
        self.y_col = QComboBox()
        self.y_col.setMinimumWidth(220)
        self.y_col.currentIndexChanged.connect(self._replot)
        row.addWidget(self.y_col)
        row.addWidget(QLabel("against"))
        self.x_col = QComboBox()
        self.x_col.setMinimumWidth(120)
        self.x_col.currentIndexChanged.connect(self._replot)
        row.addWidget(self.x_col)
        csv_btn = QPushButton("Save CSV…")
        csv_btn.clicked.connect(self.save_csv)
        row.addWidget(csv_btn)
        rep = QPushButton("Report…")
        rep.setToolTip("An HTML or PDF report of this run: pass / fail summary, plots, the "
                       "results table, the sequence and the log")
        rep.clicked.connect(self.save_report)
        row.addWidget(rep)
        v.addLayout(row)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.results = QTableWidget(0, 0)
        self.results.verticalHeader().setVisible(False)
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        split.addWidget(self.results)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.curve = self.plot.plot(pen=pg.mkPen("#4fc3f7", width=2), symbol="o", symbolSize=6,
                                    symbolBrush="#4fc3f7")
        touch.plots(self.plot.getPlotItem())
        split.addWidget(self.plot)
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setFont(QFont(MONO, 9))
        self.logbox.setMaximumBlockCount(5000)
        self.logbox.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        split.addWidget(self.logbox)
        split.setSizes([480, 480, 320])
        v.addWidget(split, 1)
        return w

    # ---------------------------------------------------------------- steps
    def _names(self) -> list[str]:
        return [p.display_name() for p in open_panels() if kind_of(p)]

    def _pins_for(self, name: str) -> list[str]:
        for p in open_panels():
            if p.KIND == "gpio" and (p.display_name() == name or name and
                                     name.lower() in p.display_name().lower()):
                return [p.state.pins[g].label for g in p._used()]
        return []

    def _panel_for(self, name: str):
        from ..signals import name_matches
        if not name:
            return None
        panels = open_panels()
        return next((p for p in panels if p.display_name() == name), None) or \
            next((p for p in panels if name_matches(name, p.display_name())), None)

    def _kind_for(self, name: str) -> str:
        p = self._panel_for(name)
        return kind_of(p) if p is not None else ""

    def add_step(self, step: Step | None = None, at: int | None = None):
        step = step or Step("Set")
        r = self.table.rowCount() if at is None else at
        self.table.insertRow(r)
        act = QComboBox()
        act.addItems(ACTIONS)
        act.setCurrentText(step.action)
        target = QComboBox()
        target.setEditable(True)
        target.addItems(self._names())
        target.setCurrentText(step.target)
        what = QComboBox()
        what.setEditable(True)
        self.table.setCellWidget(r, 0, act)
        self.table.setCellWidget(r, 1, target)
        self.table.setCellWidget(r, 2, what)
        self.table.setItem(r, 3, QTableWidgetItem(step.value))
        self.table.setItem(r, 4, QTableWidgetItem(step.lo))
        self.table.setItem(r, 5, QTableWidgetItem(step.hi))
        self.table.setItem(r, 6, QTableWidgetItem(step.label))

        def refresh(*_):
            a = act.currentText()
            kind = self._kind_for(target.currentText())
            panel = self._panel_for(target.currentText())
            if kind == "gpio" and a in ("Set", "Measure", "Wait for"):
                opts = self._pins_for(target.currentText())
            elif a == "Wait for":
                opts = []
            elif panel is not None and a in ("Set", "Measure", "Profile"):
                opts = {"Set": panel.set_names, "Measure": panel.measure_names,
                        "Profile": panel.profile_names}[a]()
            elif a == "Profile":
                opts = PROFILE_WHAT.get(kind, [])
            else:
                opts = (SET_WHAT if a == "Set" else MEASURE_WHAT if a == "Measure" else {}
                        ).get(kind, [])
            keep = what.currentText()
            what.blockSignals(True)
            what.clear()
            what.addItems(opts)
            what.setCurrentText(keep if keep in opts or not opts else opts[0])
            what.blockSignals(False)
            uses_inst = a in ("Set", "Measure", "Capture", "Profile", "Wait for", "Send", "Screenshot")
            target.setEnabled(uses_inst)
            what.setEnabled(a in ("Set", "Measure", "Profile", "For", "Command") or
                            a == "Wait for" and kind != "uart")
            tip = {"For": "The variable's name, e.g. y",
                   "Command": "A regex its output must match (blank: only its exit status)"
                   }.get(a, "")
            what.setToolTip(tip)
            what.lineEdit().setPlaceholderText(tip.split(" (")[0])
            self._indent()
        act.currentTextChanged.connect(refresh)
        target.currentTextChanged.connect(refresh)
        refresh()
        what.setCurrentText(step.what or what.currentText())

    def _step_at(self, r) -> Step:
        cell = self.table.cellWidget
        item = self.table.item
        def text(c):
            return item(r, c).text().strip() if item(r, c) else ""
        return Step(cell(r, 0).currentText(), cell(r, 1).currentText().strip(),
                    cell(r, 2).currentText().strip(), text(3), text(6), text(4), text(5))

    def _indent(self):
        """The row numbers show what is inside a For / Repeat / Retry / If."""
        acts = [self.table.cellWidget(r, 0) for r in range(self.table.rowCount())]
        names = [a.currentText() if a is not None else "" for a in acts]
        for r, d in enumerate(depths(names)):
            self.table.setVerticalHeaderItem(r, QTableWidgetItem(f"{r + 1} " + "│ " * d))

    def remove_step(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)
            self._indent()

    def move_step(self, d):
        r = self.table.currentRow()
        if r < 0 or not 0 <= r + d < self.table.rowCount():
            return
        st = self._step_at(r)
        self.table.removeRow(r)
        self.add_step(st, r + d)
        self.table.selectRow(r + d)

    def plan(self) -> Plan:
        return Plan(steps=[self._step_at(r) for r in range(self.table.rowCount())],
                    loop=self.loop.currentData(), count=self.count.value(),
                    start=self.x_from.value(), stop=self.x_to.value(), step=self.x_step.value(),
                    off_at_end=self.off_chk.isChecked(), stop_on_fail=self.fail_chk.isChecked(),
                    guard=self.guard.currentData())

    def _set_plan(self, p: Plan):
        self.table.setRowCount(0)
        for st in p.steps:
            self.add_step(st)
        self.loop.setCurrentIndex(max(0, self.loop.findData(p.loop)))
        self.count.setValue(int(p.count))
        self.x_from.setValue(p.start)
        self.x_to.setValue(p.stop)
        self.x_step.setValue(p.step)
        self.off_chk.setChecked(p.off_at_end)
        self.fail_chk.setChecked(p.stop_on_fail)
        self.guard.setCurrentIndex(max(0, self.guard.findData(p.guard)))

    def _loop_changed(self, *_):
        k = self.loop.currentData()
        self.count.setVisible(k == "repeat")
        self.sweep_box.setVisible(k == "sweep")

    # ---------------------------------------------------------------- files
    def state(self) -> dict:
        d = self.plan().to_dict()
        d["script"] = self.script.toPlainText()
        d["use_script"] = self.use_script.isChecked()
        return d

    _state = state

    def set_state(self, d: dict):
        self._set_plan(Plan.from_dict(d))
        if d.get("script"):
            self.script.setPlainText(d["script"])
        self.use_script.setChecked(bool(d.get("use_script")))

    def _save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._state(), fh, indent=1)
        tmp.replace(path)            # atomic: a crash mid-write can't leave half a file

    def _load(self, path, quiet=False):
        """quiet: the remembered sequence at start-up; a missing or damaged
        one just gives an empty table (a damaged file is kept as .bad)."""
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            if not isinstance(d, dict):
                raise ValueError("not a sequence (expected a JSON object)")
            self.set_state(d)
            return
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as e:
            err = e
        if quiet:
            if not isinstance(err, FileNotFoundError):
                try:
                    Path(path).replace(Path(path).with_suffix(".bad"))
                except OSError:
                    pass
            self.table.setRowCount(0)
            self.add_step(Step("Set", "", "", ""))
        else:
            QMessageBox.warning(self, "Load sequence", f"Can't read {path}:\n{err}")

    def save_dialog(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save sequence", "sequence.json",
                                              "Sequence (*.json)")
        if path:
            try:
                self._save(path)
            except OSError as e:
                QMessageBox.warning(self, "Save sequence", f"Can't write {path}:\n{e}")

    def load_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load sequence", "", "Sequence (*.json)")
        if path:
            self._load(path)

    def save_csv(self):
        if not self.rows:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save results", time.strftime("sequence_%Y%m%d_%H%M%S.csv"), "CSV (*.csv)")
        if path:
            try:
                with open(path, "w", newline="", encoding="utf-8") as fh:
                    w = csv.DictWriter(fh, self.columns, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(self.rows)
            except OSError as e:
                QMessageBox.warning(self, "Save results", f"Can't write {path}:\n{e}")

    # ---------------------------------------------------------------- running
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def run(self, extra: dict | None = None, on_finish=None, start: int = 0):
        """Run the sequence. extra: names for the steps / script (the
        production-test window passes serial=…); on_finish(outcome, bench)
        on the GUI thread; start: the pass to begin at (resuming)."""
        if self.running():
            return False
        self._on_finish = on_finish
        try:
            self._save(self.path)
        except OSError:
            pass
        self.stop_event = threading.Event()
        insts = [PanelInst(p, self.invoke, self.stop_event) for p in open_panels() if kind_of(p)]
        bench = Bench(insts, now, log=self.sig.log.emit, on_row=self.sig.row.emit,
                      on_note=self.sig.note.emit, stop=self.stop_event)
        self.bench = bench

        def on_image(data, name):
            def put():
                if session.active:
                    session.image(data, name)
            self.invoke(put)
        bench.on_image = on_image
        self.run_started = time.time()
        self.run_insts = [(i.name, i.p.identity(), i.p.calibration())
                          for i in insts]
        self.run_plan_dict = self.state()
        plan = self.plan()
        bench.guard = plan.guard
        script = self.script.toPlainText() if self.use_script.isChecked() else None
        if script is None:
            try:
                plan.passes()
                parse_blocks(plan.steps)
            except SequenceError as e:
                QMessageBox.warning(self, "Sequencer", str(e))
                return False
            if not plan.steps:
                return False
        self.rows, self.columns = [], []
        self.results.setRowCount(0)
        self.results.setColumnCount(0)
        self.logbox.clear()
        self._log("Instruments: " + (", ".join(i.name for i in insts) or "none open"))

        extra = dict(extra or {})
        checkpoint = self.checkpoint_path()
        resume_ok = script is None and not extra

        def on_pass(idx):
            if resume_ok:
                write_checkpoint(checkpoint, self.run_plan_dict, idx + 1, len(plan.passes()))

        if start:
            self._log(f"Resuming at pass {start + 1}")

        def body():
            outcome = "Finished"
            try:
                if script is not None:
                    run_script(script, bench, extra)
                else:
                    run_plan(plan, bench, start=start, extra=extra, on_pass=on_pass)
            except Stopped:
                outcome = "Stopped"
            except LimitFailed as e:
                outcome = f"Stopped at the first fail ({e})"
            except SequenceError as e:
                outcome = f"Failed: {e}"
            except Exception as e:      # noqa: BLE001 - a script's own error
                tb = traceback.format_exc().rstrip().splitlines()
                self.sig.log.emit("\n".join(tb[-6:]))
                outcome = f"Failed: {type(e).__name__}: {e}"
            finally:
                if plan.off_at_end:
                    self.sig.log.emit("Outputs off")
                    bench.stop_event = threading.Event()   # let the off commands through
                    bench.outputs_off()
                try:
                    checkpoint.unlink()         # ended (any way but a crash): no resume
                except OSError:
                    pass
            self.sig.done.emit(outcome)
        self.thread = threading.Thread(target=body, daemon=True, name="sequence")
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("Running…")
        self.status.setStyleSheet("color:#7ee787;")
        if resume_ok:
            write_checkpoint(checkpoint, self.run_plan_dict, start, len(plan.passes()))
        self.thread.start()
        return True

    @staticmethod
    def checkpoint_path():
        return config_dir() / "sequence-run.json"

    def stop(self):
        if self.running():
            self.stop_event.set()
            self.status.setText("Stopping…")

    def _on_done(self, outcome: str):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        b = self.bench
        tally = ""
        if b.passed or b.failed:
            tally = f"  ·  {b.passed} PASS, {b.failed} FAIL"
        self.outcome = outcome
        self.status.setText(outcome.split(" (")[0] + f"  ({len(self.rows)} rows){tally}")
        bad = outcome != "Finished" or b.failed
        self.status.setStyleSheet(f"color:{'#ff8a80' if b.failed else C_WARN if bad else '#7ee787' if b.passed else C_MUTED}; font-weight:bold;")
        self._log(outcome + tally)
        cb, self._on_finish = getattr(self, "_on_finish", None), None
        if cb is not None:
            cb(outcome, b)

    def _log(self, text: str):
        self.logbox.appendPlainText(f"{now():9.3f}  {text}")

    def _on_note(self, text: str):
        if session.active:
            session.note(text)

    def _on_row(self, row: dict):
        self.rows.append(row)
        new = [k for k in row if k not in self.columns]
        if new:
            self.columns += new
            if "result" in new:
                # the verdict next to the pass number, not off to the right
                self.columns.remove("result")
                after = max(self.columns.index(c) for c in ("pass", "x") if c in self.columns) \
                    if any(c in self.columns for c in ("pass", "x")) else -1
                self.columns.insert(after + 1, "result")
            self.results.setColumnCount(len(self.columns))
            self.results.setHorizontalHeaderLabels(self.columns)
            for combo in (self.y_col, self.x_col):
                keep = combo.currentText()
                combo.blockSignals(True)
                combo.clear()
                combo.addItems(self.columns)
                combo.blockSignals(False)
                if keep:
                    combo.setCurrentText(keep)
            if not self.x_col.currentText() or self.x_col.currentText() == self.columns[0]:
                self.x_col.setCurrentText("x" if "x" in self.columns else "pass")
            numeric = [c for c in self.columns if not c.endswith(PM) and c not in (
                "t", "pass", "x", "result", "failures", "marginal")]
            if numeric and self.y_col.currentText() in ("t", "pass", "x", ""):
                self.y_col.setCurrentText(numeric[0])
        if new:
            # columns changed (or moved): lay every row out again
            self.results.setRowCount(0)
            for old in self.rows:
                self._put_row(old)
        else:
            self._put_row(row)
        self.results.scrollToBottom()
        if session.active:
            cols = [c for c in row if c not in ("t",)]
            session.stream(("sequencer", tuple(cols)), "Sequencer", "sequence", "",
                           cols)
            session.add(("sequencer", tuple(cols)), row["t"], [row[c] for c in cols])
        self._replot()

    def _put_row(self, row: dict):
        r = self.results.rowCount()
        self.results.insertRow(r)
        failed = row.get("result") == "FAIL"
        bad = set(failing_columns(row))
        near = set(str(row.get("marginal", "")).split("; ")) - {""}
        for c, k in enumerate(self.columns):
            v = row.get(k, "")
            it = QTableWidgetItem(f"{v:.6g}" if isinstance(v, float) else str(v))
            if failed:
                it.setBackground(FAIL_CELL if k in bad else FAIL_BG)
            elif k in near:
                it.setBackground(NEAR_CELL)
            if k == "result":
                it.setForeground(FAIL_FG if failed else PASS_FG)
            self.results.setItem(r, c, it)

    def _replot(self, *_):
        xk, yk = self.x_col.currentText(), self.y_col.currentText()
        pts = [(r.get(xk), r.get(yk)) for r in self.rows]
        pts = [(x, y) for x, y in pts if isinstance(x, (int, float)) and isinstance(y, (int, float))]
        if pts:
            a = np.array(pts, float)
            self.curve.setData(a[:, 0], a[:, 1])
        else:
            self.curve.setData([], [])
        self.plot.setLabel("bottom", xk)
        self.plot.setLabel("left", yk)

    def save_report(self):
        from .report_dialog import ask_path, default_name, write
        if not self.rows:
            QMessageBox.information(self, "Report", "Run the sequence first.")
            return
        from PyQt6.QtWidgets import QInputDialog
        title, ok = QInputDialog.getText(self, "Report", "Title:", text="Test report")
        if not ok:
            return
        path = ask_path(self, default_name("report"))
        if not path:
            return
        y = [self.y_col.currentText()] if self.y_col.currentText() else None
        html = sequence_report(title, self.rows, self.columns,
                               getattr(self, "run_plan_dict", self.state()),
                               self.logbox.toPlainText(), getattr(self, "run_insts", []),
                               getattr(self, "outcome", ""), getattr(self, "run_started", None),
                               self.x_col.currentText(), None if y is None else
                               list(dict.fromkeys(y + [c for c in self.columns if c not in
                                                       ("t", "pass", "x", "result", "failures",
                                                        "marginal") and not c.endswith(PM)
                                                       and any(isinstance(r.get(c), (int, float))
                                                               for r in self.rows)]))[:4],
                               images=getattr(getattr(self, "bench", None), "images", None))
        write(self, html, path)

    def closeEvent(self, e):
        try:
            self._save(self.path)
        except OSError:
            pass
        super().closeEvent(e)

