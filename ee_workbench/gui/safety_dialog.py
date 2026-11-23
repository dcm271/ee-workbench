"""Tools ▾ → Safety…: outputs off on quit / after a crash, a heartbeat pin,
automatic reconnect and resuming a sequence (see ee_workbench/safety.py)."""
from __future__ import annotations

from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QFormLayout, QGroupBox, QLabel, QLayout, QVBoxLayout)

from ..about import APP_NAME
from ..safety import Safety
from . import touch
from .panel_base import open_panels
from .widgets import hint_label


def _hint(text: str) -> QLabel:
    return hint_label(text, 520 if not touch.TOUCH else 640, 11 if not touch.TOUCH else 13)


class SafetyDialog(QDialog):
    def __init__(self, parent, s: Safety):
        super().__init__(parent)
        self.setWindowTitle("Safety and unattended runs")
        v = QVBoxLayout(self)

        g = QGroupBox("Outputs")
        gl = QVBoxLayout(g)
        self.off_quit = QCheckBox(f"Switch all outputs off when {APP_NAME} quits")
        self.off_quit.setChecked(s.off_on_quit)
        gl.addWidget(self.off_quit)
        self.off_crash = QCheckBox("After a crash, switch off the outputs of every instrument "
                                   "that was open")
        self.off_crash.setChecked(s.off_after_crash)
        gl.addWidget(self.off_crash)
        gl.addWidget(_hint("A crash is noticed at the next start (run.sh --restart-on-crash "
                           "starts it again by itself). Instruments that were open are "
                           "reopened so their outputs can be switched off. The watchdog's "
                           "“In HW” limits protect the DUT even before that."))
        v.addWidget(g)

        g = QGroupBox("Heartbeat pin")
        form = QFormLayout(g)
        self.hb_tab = QComboBox()
        self.hb_tab.setEditable(True)
        self.hb_tab.addItem("")
        self.hb_tab.addItems([p.display_name() for p in open_panels() if p.KIND == "gpio"])
        self.hb_tab.setCurrentText(s.heartbeat_tab)
        form.addRow("GPIO tab", self.hb_tab)
        self.hb_pin = QComboBox()
        self.hb_pin.setEditable(True)
        self.hb_tab.currentTextChanged.connect(self._fill_pins)
        self._fill_pins()
        self.hb_pin.setCurrentText(s.heartbeat_pin)
        form.addRow("Output pin", self.hb_pin)
        self.hb_period = QDoubleSpinBox()
        self.hb_period.setRange(0.05, 60)
        self.hb_period.setDecimals(2)
        self.hb_period.setSuffix(" s")
        self.hb_period.setValue(s.heartbeat_period)
        touch.attach_keypad(self.hb_period, "Heartbeat half-period (s)")
        form.addRow("Toggle every", self.hb_period)
        form.addRow(_hint("The pin toggles while the app is running normally. Wire it to an "
                          "external watchdog (a retriggerable timer or a small "
                          "microcontroller) that cuts the DUT's supply when the toggling "
                          "stops. The pin must be set up as an output on its GPIO tab. "
                          "Leave the tab blank for no heartbeat."))
        v.addWidget(g)

        g = QGroupBox("Recovery")
        gl = QVBoxLayout(g)
        self.reconnect = QCheckBox("Reconnect automatically when an instrument stops "
                                   "answering")
        self.reconnect.setChecked(s.auto_reconnect)
        gl.addWidget(self.reconnect)
        gl.addWidget(_hint("Tries again after 3, 6, 12 … up to every 60 s. A USB device "
                           "(/dev/…) is tried only once it is plugged back in. Not while a "
                           "sequence is running: it would lose its instruments."))
        self.resume = QCheckBox("After a crash, resume a running sequence where it stopped")
        self.resume.setChecked(s.resume_sequence)
        gl.addWidget(self.resume)
        gl.addWidget(_hint("Steps mode only (not scripts, not production tests): it starts "
                           "again at the pass after the last one that finished, once its "
                           "instruments have reconnected. Results of the earlier passes are "
                           "in the session file, if one was recording."))
        v.addWidget(g)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        # word-wrapped hints: let the layout give them the height they need
        v.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.setMinimumWidth(560)

    def _fill_pins(self, *_):
        keep = self.hb_pin.currentText()
        self.hb_pin.clear()
        from ..signals import name_matches
        tab = self.hb_tab.currentText()
        for p in open_panels():
            if p.KIND == "gpio" and tab and name_matches(tab, p.display_name()):
                st = getattr(p, "state", None)
                pins = getattr(st, "pins", {}) if st is not None else {}
                self.hb_pin.addItems([x.label for x in pins.values()
                                      if getattr(x, "mode", "") == "out"])
        self.hb_pin.setCurrentText(keep)

    def result_settings(self) -> Safety:
        return Safety(off_on_quit=self.off_quit.isChecked(),
                      off_after_crash=self.off_crash.isChecked(),
                      heartbeat_tab=self.hb_tab.currentText().strip(),
                      heartbeat_pin=self.hb_pin.currentText().strip(),
                      heartbeat_period=self.hb_period.value(),
                      auto_reconnect=self.reconnect.isChecked(),
                      resume_sequence=self.resume.isChecked())
