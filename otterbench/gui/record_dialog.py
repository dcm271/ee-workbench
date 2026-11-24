"""Asked when a session recording starts: everything, or only around events
(a flight recorder that keeps the seconds before and after each event)."""
from __future__ import annotations

from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                             QLayout, QRadioButton, QVBoxLayout)

from . import touch
from .widgets import hint_label


class RecordDialog(QDialog):
    def __init__(self, parent, gated: bool = False, pre: float = 10.0, post: float = 10.0):
        super().__init__(parent)
        self.setWindowTitle("Record session")
        v = QVBoxLayout(self)
        self.all = QRadioButton("Everything, from now until Stop")
        self.events = QRadioButton("Only around events  (flight recorder)")
        (self.events if gated else self.all).setChecked(True)
        v.addWidget(self.all)
        v.addWidget(self.events)
        hint = hint_label("Readings are kept in memory and written only around an event: a "
                          "watchdog rule tripping, a logic-analyzer or scope capture, or ✎ "
                          "Note / Ctrl+M. Notes and captures are always written. Good for a "
                          "long unattended run where only the moments that matter should be "
                          "kept.", 480 if not touch.TOUCH else 620,
                          11 if not touch.TOUCH else 13)
        v.addWidget(hint)
        form = QFormLayout()
        self.pre = QDoubleSpinBox()
        self.post = QDoubleSpinBox()
        for w, val, title in ((self.pre, pre, "Seconds before"), (self.post, post,
                                                                  "Seconds after")):
            w.setRange(0, 3600)
            w.setDecimals(1)
            w.setSuffix(" s")
            w.setValue(val)
            touch.attach_keypad(w, title)
        form.addRow("Keep before each event", self.pre)
        form.addRow("Keep after each event", self.post)
        v.addLayout(form)
        self.events.toggled.connect(self._enable)
        self._enable(self.events.isChecked())
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Choose file…")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        # word-wrapped hints: let the layout give them the height they need
        v.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.setMinimumWidth(520)

    def _enable(self, on: bool):
        self.pre.setEnabled(on)
        self.post.setEnabled(on)

    @property
    def gate(self) -> tuple[float, float] | None:
        return (self.pre.value(), self.post.value()) if self.events.isChecked() else None
