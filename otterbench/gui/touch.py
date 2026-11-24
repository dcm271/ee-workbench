"""Touch-screen support: detection, finger-sized styling, an on-screen numeric
keypad (which also accepts the physical keyboard) and kinetic scrolling.

Designed for a 10-inch 1280x800 panel that also has a mouse and keyboard.

Touch mode is decided once at start-up (see `detect`) and read by the panels
while they build their widgets.
"""
from __future__ import annotations

import os

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtGui import QFont, QInputDevice
from PyQt6.QtWidgets import (QAbstractSpinBox, QDialog, QGridLayout, QHBoxLayout, QLabel,
                             QPushButton, QScrollArea, QScroller, QSizePolicy, QVBoxLayout,
                             QWidget)

from .widgets import C_MUTED, C_WARN, MONO

from . import theme as _theme  # noqa: E402

TOUCH = False
HIT = 52  # minimum touch target, px (~9 mm at 150 dpi)


def detect(app, force: bool | None = None) -> bool:
    """force > OTTERBENCH_TOUCH env > a touchscreen is present > the screen is small."""
    global TOUCH
    if force is None:
        env = os.environ.get("OTTERBENCH_TOUCH", "").strip().lower()
        if env in ("1", "yes", "on", "true"):
            force = True
        elif env in ("0", "no", "off", "false"):
            force = False
    if force is None:
        has_ts = any(d.type() == QInputDevice.DeviceType.TouchScreen
                     for d in QInputDevice.devices())
        scr = app.primaryScreen()
        small = bool(scr) and (scr.size().width() <= 1280 and scr.size().height() <= 800)
        force = has_ts or small
    TOUCH = bool(force)
    if TOUCH:
        # Raise the default font via the application font, not a stylesheet
        # rule: a stylesheet font-size would override the big readout fonts.
        f = app.font()
        f.setPixelSize(16)
        app.setFont(f)
    return TOUCH


TOUCH_STYLE = f"""
    QPushButton, QToolButton {{ min-height: {HIT - 8}px; padding: 4px 14px; }}
    QComboBox {{ min-height: {HIT - 8}px; padding: 2px 10px; combobox-popup: 0; }}
    QComboBox QAbstractItemView::item {{ min-height: {HIT}px; }}
    QComboBox::drop-down {{ width: 36px; }}
    QSpinBox, QDoubleSpinBox, QLineEdit {{ min-height: {HIT - 8}px; padding: 0 8px; }}
    QCheckBox {{ min-height: {HIT - 8}px; spacing: 10px; }}
    QCheckBox::indicator {{ width: 28px; height: 28px; }}
    QTabBar::tab {{ min-height: {HIT - 6}px; padding: 0 18px; }}
    QTabBar::close-button {{ width: 26px; height: 26px; }}
    QScrollBar:vertical {{ width: 22px; }}
    QScrollBar:horizontal {{ height: 22px; }}
    QPushButton#key {{ font: bold 22px '{MONO}'; min-height: 52px; min-width: 64px; }}
    QPushButton#keyok {{ font: bold 18px; min-height: 52px; background: #2e7d32; color: white; }}
""" + _theme.touch_extra(HIT)


def kinetic(area: QScrollArea) -> QScrollArea:
    """Let a finger drag scroll the area (with momentum)."""
    QScroller.grabGesture(area.viewport(), QScroller.ScrollerGestureType.LeftMouseButtonGesture)
    return area


def scroll(widget: QWidget) -> QScrollArea:
    from PyQt6.QtWidgets import QFrame
    sa = QScrollArea()
    sa.setWidget(widget)
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return kinetic(sa) if TOUCH else sa


# ---------------------------------------------------------------- keypad
class NumPad(QDialog):
    """Numeric keypad. Result in `.value` when accepted."""

    def __init__(self, parent, title: str, value: float, lo: float, hi: float,
                 decimals: int, suffix: str = "", integer: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.lo, self.hi, self.decimals, self.integer = lo, hi, decimals, integer
        self.suffix = suffix.strip()
        self.value = value
        self._fresh = True  # first key replaces the shown value
        self.text = f"{int(value)}" if integer else f"{value:.{decimals}f}"

        v = QVBoxLayout(self)
        v.setSpacing(6)
        head = QHBoxLayout()
        t = QLabel(title)
        t.setStyleSheet("font-weight:bold; font-size:17px;")
        head.addWidget(t, 1)
        rng = QLabel(f"{lo:g} … {hi:g} {self.suffix}")
        rng.setStyleSheet(f"color:{C_MUTED};")
        head.addWidget(rng)
        v.addLayout(head)
        self.display = QLabel()
        self.display.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.display.setFont(QFont(MONO, 24, QFont.Weight.Bold))
        self.display.setMinimumHeight(54)
        self.display.setStyleSheet("background:#111418; border:1px solid #3a3f47; "
                                   "border-radius:6px; padding:0 10px;")
        v.addWidget(self.display)

        g = QGridLayout()
        g.setSpacing(6)
        keys = [("7", 0, 0), ("8", 0, 1), ("9", 0, 2), ("⌫", 0, 3),
                ("4", 1, 0), ("5", 1, 1), ("6", 1, 2), ("C", 1, 3),
                ("1", 2, 0), ("2", 2, 1), ("3", 2, 2), ("±", 2, 3),
                ("0", 3, 0), (".", 3, 1)]
        for k, r, c in keys:
            b = QPushButton(k)
            b.setObjectName("key")
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            b.clicked.connect(lambda _, k=k: self._key(k))
            if (k == "." and integer) or (k == "±" and lo >= 0):
                b.setEnabled(False)
            g.addWidget(b, r, c)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("key")
        cancel.setStyleSheet("font-size:16px;")
        cancel.clicked.connect(self.reject)
        g.addWidget(cancel, 3, 2)
        self.ok = QPushButton("Set")
        self.ok.setObjectName("keyok")
        self.ok.clicked.connect(self._accept)
        g.addWidget(self.ok, 3, 3)
        v.addLayout(g)
        self.resize(380, 400)
        self._refresh()

    def _key(self, k: str):
        if self._fresh and k not in ("⌫", "±"):
            self.text = ""
        self._fresh = False
        if k == "⌫":
            self.text = self.text[:-1]
        elif k == "C":
            self.text = ""
        elif k == "±":
            self.text = self.text[1:] if self.text.startswith("-") else "-" + self.text
        elif k == ".":
            if "." not in self.text:
                self.text = (self.text or "0") + "."
        else:
            frac = self.text.split(".")[1] if "." in self.text else ""
            if "." in self.text and len(frac) >= self.decimals:
                return
            self.text = ("" if self.text == "0" else self.text) + k
        self._refresh()

    def event(self, e):
        # Esc is the app-wide "all outputs off" shortcut; inside the keypad it
        # should only cancel, so claim it before the shortcut sees it.
        if e.type() == QEvent.Type.ShortcutOverride and e.key() == Qt.Key.Key_Escape:
            e.accept()
            return True
        return super().event(e)

    def keyPressEvent(self, e):
        """Physical keyboard: digits . - Backspace Delete Enter Esc."""
        k, t = e.key(), e.text()
        if t and t in "0123456789":
            self._key(t)
        elif t in (".", ","):
            self._key(".")
        elif t == "-" and self.lo < 0:
            self._key("±")
        elif k == Qt.Key.Key_Backspace:
            self._key("⌫")
        elif k == Qt.Key.Key_Delete:
            self._key("C")
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.ok.isEnabled():
                self._accept()
        elif k == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(e)

    def _parsed(self):
        try:
            return float(self.text)
        except ValueError:
            return None

    def _refresh(self):
        x = self._parsed()
        ok = x is not None and self.lo - 1e-12 <= x <= self.hi + 1e-12
        self.display.setText(f"{self.text or '0'} {self.suffix}")
        self.display.setStyleSheet(
            "background:#111418; border:1px solid %s; border-radius:6px; padding:0 10px; color:%s;"
            % (("#3a3f47", "#e6e8eb") if ok else (C_WARN, C_WARN)))
        self.ok.setEnabled(ok)

    def _accept(self):
        x = self._parsed()
        if x is None:
            return
        self.value = int(x) if self.integer else x
        self.accept()


class _KeypadFilter(QObject):
    def __init__(self, spin, title, on_accept):
        super().__init__(spin)
        self.spin, self.title, self.on_accept = spin, title, on_accept

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.MouseButtonRelease and self.spin.isEnabled():
            self.open()
            return True
        if ev.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            return True
        return False

    def open(self):
        s = self.spin
        integer = not hasattr(s, "decimals")
        pad = NumPad(s.window(), self.title, s.value(), s.minimum(), s.maximum(),
                     0 if integer else s.decimals(), s.suffix(), integer)
        if pad.exec() == QDialog.DialogCode.Accepted:
            s.setValue(pad.value)
            if self.on_accept:
                self.on_accept()


def attach_keypad(spin, title: str, on_accept=None):
    """In touch mode, tapping `spin` opens the keypad instead of a text cursor.
    `on_accept` (e.g. "send to instrument") runs after Set is pressed."""
    if not TOUCH:
        return
    spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
    spin.lineEdit().setReadOnly(True)
    spin.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    f = _KeypadFilter(spin, title, on_accept)
    spin.lineEdit().installEventFilter(f)
    spin.installEventFilter(f)


def plots(*items):
    """Fingers drag plots by accident: in touch mode the plots don't pan or
    zoom (the Span selector controls the view) and keep autoscaling Y."""
    if not TOUCH:
        return
    for p in items:
        p = p.getPlotItem() if hasattr(p, "getPlotItem") else p
        p.setMouseEnabled(x=False, y=False)
        p.hideButtons()
        p.setMenuEnabled(False)
