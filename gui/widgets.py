"""Shared widgets, formatting helpers and the dark theme."""
from __future__ import annotations

import csv
import math
import time

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import (QApplication, QDoubleSpinBox, QFileDialog, QLabel,
                             QVBoxLayout, QWidget)

C_VOLT = "#ffb347"
C_CURR = "#4fc3f7"
C_DVM = "#c39bff"
C_DMM = "#7ee787"
C_POW = "#9e9e9e"
C_ON = "#2e7d32"
C_WARN = "#ef5350"
C_MUTED = "#8a8f98"
MONO = "DejaVu Sans Mono"


def fmt_current(a: float | None) -> str:
    if a is None or math.isnan(a):
        return "—"
    if abs(a) < 0.1:
        return f"{a * 1000:+.3f} mA"
    return f"{a:+.4f} A"


def fmt_si(x: float | None, unit: str, digits: int = 4) -> str:
    if x is None or math.isnan(x):
        return "—"
    ax = abs(x)
    for scale, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"),
                          (1e-6, "µ"), (1e-9, "n")):
        if ax >= scale:
            break
    return f"{x / scale:.{digits}g} {prefix}{unit}"


def fmt_fixed(x: float, unit: str, sig: int) -> str:
    """Engineering notation with a fixed number of significant digits,
    keeping trailing zeros (meter-style)."""
    if x is None or math.isnan(x):
        return "—"
    ax = abs(x)
    exp = 0 if ax == 0 else int(math.floor(math.log10(ax) / 3) * 3)
    if unit == "V":
        exp = min(max(exp, -3), 0)
    exp = max(min(exp, 9), -9)
    prefix = {9: "G", 6: "M", 3: "k", 0: "", -3: "m", -6: "µ", -9: "n"}[exp]
    m = x / 10 ** exp
    int_digits = 1 if abs(m) < 1 else int(math.log10(abs(m))) + 1
    dec = max(0, sig - int_digits)
    return f"{m:+.{dec}f} {prefix}{unit}"


class SafeSpin(QDoubleSpinBox):
    """Spin box that ignores the mouse wheel unless focused, and highlights
    values that differ from what the instrument has (not yet applied)."""

    def __init__(self, decimals=3, step=0.01, suffix="", track=True):
        super().__init__()
        self.track = track  # False: a plain input, not mirroring an instrument value
        self.setDecimals(decimals)
        self.setSingleStep(step)
        self.setSuffix(suffix)
        self.setKeyboardTracking(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.setMinimumWidth(110)
        self.instrument_value: float | None = None
        self.valueChanged.connect(self._mark)

    def wheelEvent(self, e):
        if self.hasFocus():
            super().wheelEvent(e)
        else:
            e.ignore()

    def set_from_instrument(self, v: float | None):
        """None = instrument value unknown: leave the box, show it as unknown."""
        if v is not None:
            self.blockSignals(True)
            self.setValue(v)
            self.blockSignals(False)
        self.instrument_value = v
        self._mark()

    def _mark(self, *_):
        if not self.track:
            return
        if self.instrument_value is None:
            pending = True
            tip = "Instrument value unknown. Press Enter or Set to program it."
        else:
            pending = abs(self.value() - self.instrument_value) > 10 ** -self.decimals() / 2
            tip = "Not yet applied. Press Enter or Set." if pending else ""
        self.setStyleSheet("QDoubleSpinBox { background: #4a3b12; }" if pending else "")
        self.setToolTip(tip)


class Readout(QWidget):
    def __init__(self, label: str, color: str, size: int = 30):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.caption = QLabel(label.upper())
        self.caption.setStyleSheet(f"color:{C_MUTED}; font-size:10px; letter-spacing:1px;")
        self.value = QLabel("—")
        f = QFont(MONO, size)
        f.setBold(True)
        self.value.setFont(f)
        self.value.setStyleSheet(f"color:{color};")
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.caption)
        lay.addWidget(self.value)

    def set(self, text: str):
        self.value.setText(text)


class TraceBuffer:
    """Append-only numpy buffer of float rows; drops the oldest half when full."""

    def __init__(self, ncols: int, max_rows: int = 2_000_000):
        self.ncols, self.max_rows = ncols, max_rows
        self.clear()

    def clear(self):
        self.data = np.full((4096, self.ncols), np.nan)
        self.n = 0

    def append(self, row):
        if self.n == len(self.data):
            if len(self.data) >= self.max_rows:
                keep = self.n // 2
                self.data[:keep] = self.data[self.n - keep:self.n]
                self.n = keep
            else:
                self.data = np.concatenate([self.data, np.full_like(self.data, np.nan)])
        self.data[self.n] = [np.nan if v is None else v for v in row]
        self.n += 1

    def view(self) -> np.ndarray:
        return self.data[: self.n]


class CsvRecorder:
    def __init__(self):
        self.fh = None
        self.w = None
        self.path = ""

    @property
    def active(self) -> bool:
        return self.w is not None

    def start(self, parent, default_name: str, header: list[str]) -> bool:
        path, _ = QFileDialog.getSaveFileName(
            parent, "Record to CSV", time.strftime(default_name), "CSV (*.csv)")
        if not path:
            return False
        self.fh = open(path, "w", newline="")
        self.w = csv.writer(self.fh)
        self.w.writerow(header)
        self.path = path
        return True

    def row(self, values):
        if self.w:
            self.w.writerow(["" if v is None else v for v in values])
            self.fh.flush()

    def stop(self):
        if self.fh:
            self.fh.close()
        self.fh = self.w = None


def dark_palette(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    base, window, text = QColor("#1a1d21"), QColor("#202328"), QColor("#d7dae0")
    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, window)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, QColor("#2b2f36"))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.Highlight, QColor("#3d6fb4"))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor("#2b2f36"))
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText,
                 QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor("#6b7079"))
    app.setPalette(p)
    app.setStyleSheet("""
        QGroupBox { border:1px solid #33373e; border-radius:6px; margin-top:14px; padding:8px; }
        QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; color:#8a8f98; }
        QFrame#bar { background:#262a30; border-radius:6px; }
        QTabWidget#instruments::pane { border-top: 1px solid #33373e; }
    """)
