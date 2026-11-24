"""Small builders shared by the tool tabs (capture, tests, system)."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QLabel, QSpinBox

from . import touch
from .widgets import C_MUTED, SafeSpin


def number(title: str, lo: float, hi: float, value: float, decimals: int = 3,
           suffix: str = "", step: float | None = None) -> SafeSpin:
    """A plain numeric input (not mirroring an instrument value). On a touch
    screen tapping it opens the keypad."""
    s = SafeSpin(decimals, step if step is not None else 10 ** -decimals * 10, suffix, track=False)
    s.setRange(lo, hi)
    s.setValue(value)
    s.setMinimumWidth(90)
    touch.attach_keypad(s, title)
    return s


def integer(title: str, lo: int, hi: int, value: int, suffix: str = "") -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(value)
    s.setSuffix(suffix)
    s.setAlignment(Qt.AlignmentFlag.AlignRight)
    touch.attach_keypad(s, title)
    return s


def choice(items: list[tuple[str, str]], tip: str = "") -> QComboBox:
    """Combo box of (label, value) pairs; read with .currentData()."""
    c = QComboBox()
    for label, value in items:
        c.addItem(label, value)
    if tip:
        c.setToolTip(tip)
    return c


def select(combo: QComboBox, value) -> None:
    i = combo.findData(value)
    if i >= 0 and i != combo.currentIndex():
        combo.blockSignals(True)
        combo.setCurrentIndex(i)
        combo.blockSignals(False)


def hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet(f"color:{C_MUTED}; font-size:12px;")
    return lab
