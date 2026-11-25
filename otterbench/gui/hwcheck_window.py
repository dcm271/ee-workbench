"""Hardware check window (Tools ▾): try a driver against the instrument in front
of it, keep the traffic as a tape (hwcheck.py, tape.py)."""
from __future__ import annotations

import os
from dataclasses import asdict

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFormLayout, QGroupBox,
                             QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from .. import hwcheck, report, tape
from . import touch
from .forms import choice, hint, number
from .panel_base import open_panels
from .tool_window import InstBox, ToolWindow
from .widgets import C_BAD, C_GOOD, C_MUTED

HEAD = ["Step", "Result", "Detail", "ms"]
COLOR = {"PASS": C_GOOD, "FAIL": C_BAD, "SKIP": C_MUTED}

WARN = {
    hwcheck.SETTINGS: "The check changes settings of {name} with its outputs off, and puts them "
                      "back. (A model that can't report its settings is left at 0 V.)\n\n"
                      "Go on?",
    hwcheck.OUTPUT: "The check changes settings of {name}, then switches its output on at "
                    "{volts:g} V, {amps:g} A.\n\nIs EVERYTHING disconnected from its "
                    "outputs?",
}
METER = "\n\nA meter goes through all its functions, current and resistance too: disconnect " \
        "its test leads first."


class HwCheckWindow(ToolWindow):
    TITLE = "Hardware check"
    KEY = "hwcheck"

    def __init__(self, main):
        self.rows: list[dict] = []
        self.tape: tape.Tape | None = None
        self.idn = self.name = self.resource = self.def_path = ""
        self.done_level = hwcheck.READ
        super().__init__(main)
        self.run_btn.setText("▶ Run check")

    def buttons(self):
        return [("Save tape…", self.save_tape), ("Report…", self.save_html),
                ("Copy summary", self.copy), ("Mark as tested…", self.mark)]

    def build_left(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 6, 0)
        g = QGroupBox("Check")
        f = QFormLayout(g)
        self.inst = InstBox(lambda p: p.connected)
        f.addRow("Instrument", self.inst)
        self.level = choice([(hwcheck.LEVELS[k], k) for k in hwcheck.ORDER])
        f.addRow("How far", self.level)
        self.functions = QCheckBox("Go through a meter's functions, a load's modes")
        self.functions.setChecked(True)
        f.addRow(self.functions)
        self.volts = number("Test voltage", 0.1, 5, 1.0, 2, " V")
        f.addRow("Test voltage", self.volts)
        self.amps = number("Current limit", 0.01, 1, 0.1, 3, " A")
        f.addRow("Current limit", self.amps)
        self.settle = number("Settling time", 0, 30, 1.0, 1, " s")
        f.addRow("Settle", self.settle)
        v.addWidget(g)
        v.addWidget(hint(
            "Start with Read only. The next level needs the outputs off; the last switches a "
            "supply's output on, so nothing may be connected to it. If an output is on, the "
            "check stops before it sends anything.\n"
            "Save tape… keeps what the instrument said. Put the file in tests/tapes and the "
            "test suite replays it, so a later change to the driver can't break this model "
            "unnoticed. The serial number is taken out where that is possible."))
        v.addWidget(self.make_log(), 1)
        return w

    def build_right(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        v.addWidget(self.result)
        self.table = QTableWidget(0, len(HEAD))
        self.table.setHorizontalHeaderLabels(HEAD)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setWordWrap(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40 if touch.TOUCH else 28)
        v.addWidget(self.table, 1)
        return w

    def settings(self):
        return {"level": self.level.currentData(), "functions": self.functions.isChecked(),
                "volts": self.volts.value(), "amps": self.amps.value(),
                "settle": self.settle.value()}

    def restore(self, s):
        # never the level: each session starts with Read only
        self.functions.setChecked(bool(s.get("functions", True)))
        for k in ("volts", "amps", "settle"):
            if k in s:
                getattr(self, k).setValue(float(s[k]))

    def used(self):
        return [self.name]

    # ---- running
    def job(self):
        panel = next((p for p in open_panels() if p.connected
                      and p.display_name() == self.inst.name()), None)
        if panel is None:
            return self.warn("Open and connect an instrument first")
        opts = hwcheck.Options(**self.settings())
        if opts.problem():
            return self.warn(opts.problem())
        self.name = panel.display_name()
        ask = WARN.get(opts.level)
        if ask:
            text = ask.format(name=self.name, volts=opts.volts, amps=opts.amps)
            d = getattr(panel.caps, "definition", None)
            if opts.functions and (panel.KIND == "dmm" or d is not None and d.type == "meter"):
                text += METER
            if not self.confirm(text):
                return None
        worker, caps = panel.worker, panel.caps
        self.idn, self.resource = panel.identity(), panel.resource
        self.def_path = getattr(getattr(caps, "definition", None), "path", "")
        self.rows, self.tape, self.done_level = [], None, opts.level
        self.table.setRowCount(0)
        self.result.setText("")
        name, emit = self.name, self.runner.event.emit

        def go(bench):
            inst = bench.inst(name)
            stopping = []

            # After Stop / Esc the check only unwinds: outputs off, settings put
            # back. Those go to the instrument without waiting for it.
            def run(fn):
                if stopping:
                    worker.submit(fn, refresh=True)
                    return None
                try:
                    return inst.run(worker.taped(fn))
                except hwcheck.Stopped:
                    stopping.append(1)
                    raise
            def sleep(s):
                if not stopping:
                    try:
                        bench.wait(s)
                    except hwcheck.Stopped:
                        stopping.append(1)
                        raise

            def on_row(r):
                if not stopping:
                    emit(("row", r))
            # what is taped from here on belongs to this check
            inst.run(lambda d: worker.tap.tap_clear() if worker.tap else None)
            chk = hwcheck.HardwareCheck(run, caps, opts, sleep=sleep, on_row=on_row)
            chk.go()
            tp = tape.new_tape(worker.tap, worker.connect_events, worker.tape_model,
                               self.resource, caps.idn)
            tp.options, tp.rows = asdict(opts), list(chk.rows)
            emit(("tape", tp if tp.events else None))
        return go

    def on_event(self, e):
        what, v = e
        if what == "tape":
            self.tape = v
            return
        self.rows.append(v)
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, k in enumerate(("step", "result", "detail", "ms")):
            it = QTableWidgetItem(str(v[k]))
            it.setToolTip(str(v[k]))
            if c == 1:
                it.setForeground(QColor(COLOR[v["result"]]))
            self.table.setItem(r, c, it)
        self.table.scrollToBottom()
        self.say(f"{v['result']}  {v['step']}  {v['detail']}")

    def finished(self, outcome):
        verdict = hwcheck.verdict(self.rows)
        if outcome != "Finished":
            self.result.setText(f"{outcome}: not complete")
            return
        text = f"<b>{verdict}</b>: {hwcheck.counts(self.rows)}"
        if self.tape is None:
            text += ". This kind of link can't be taped."
        elif self.tape.truncated:
            text += ". The tape is full, so it is not complete."
        if not self.real():
            text += ". (A simulator: this says nothing about the real instrument.)"
        self.result.setText(text)

    def real(self) -> bool:
        return tape.link_kind(self.resource) != "simulator"

    # ---- results
    def _have(self) -> bool:
        if not self.rows or self.runner.running():
            self.warn("Run a check first")
            return False
        return True

    def save_tape(self):
        if not self._have():
            return
        if self.tape is None:
            return self.warn("There is no tape: the check didn't finish, or this kind of "
                             "instrument can't be taped")
        if not tape.hides_serial(self.tape) and not self.confirm(
                "This instrument's binary protocol may carry its serial number, which can't be "
                "taken out of the tape. Save it all the same?"):
            return
        model = self.tape.model.replace("SCPI:", "").replace("SCOPE:", "scope-")
        name = "".join(c if c.isalnum() or c in "-_." else "-" for c in model)
        path, _ = QFileDialog.getSaveFileName(self, "Save tape", f"{name}-{self.tape.date}.tape",
                                              "Tape (*.tape)")
        if path:
            self.write_tape(path)

    def write_tape(self, path: str):
        try:
            tape.save(self.tape, path)
            r = hwcheck.replay(path)
        except (OSError, ValueError) as e:
            return self.warn(f"Can't save the tape:\n{e}")
        self.say(f"Saved {path}")
        for p in r.problems:
            self.say("On playing back: " + p)
        if r.problems:
            self.warn("The tape is saved, but it doesn't play back the same (see the log). It "
                      "still shows what the instrument said.")

    def report_html(self) -> str:
        return report.hwcheck_report(
            f"Hardware check: {self.name}", self.rows, hwcheck.verdict(self.rows),
            hwcheck.counts(self.rows) + ("" if self.real() else " (simulator)"),
            {"Level": hwcheck.LEVELS[self.done_level], "Test voltage": self.volts.value(),
             "Current limit": self.amps.value()}, self.inst_list(), self.summary())

    def summary(self) -> str:
        s = hwcheck.summary_line(self.idn, self.rows, self.done_level)
        return s if self.real() else s + " | SIMULATOR"

    def save_html(self):
        if self._have():
            self.save_report(self.report_html(), "hwcheck")

    def copy(self):
        if self._have():
            QApplication.clipboard().setText(self.summary())
            self.status.setText("Copied (without the serial number)")

    def can_mark(self) -> bool:
        return bool(self.rows and self.real() and self.def_path
                    and self.done_level != hwcheck.READ
                    and self.result.text().startswith("<b>PASS")
                    and os.access(self.def_path, os.W_OK))

    def mark(self):
        if not self._have():
            return
        if not self.can_mark():
            return self.warn("Only a definition file you can write to is marked, after a check "
                             "on real hardware that passed at least the Settings level. (The "
                             "built-in drivers are listed in docs/01: Copy summary.)")
        text = hwcheck.tested_text(self.idn, self.done_level)
        if not self.confirm(f"Write this into {self.def_path}?\n\ntested = \"{text}\""):
            return
        try:
            hwcheck.mark_tested(self.def_path, text)
        except (OSError, ValueError) as e:
            return self.warn(f"Can't mark it:\n{e}")
        self.status.setText("Marked; the tab shows it from the next start")
