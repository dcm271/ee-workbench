"""The logo, the splash screen and the About box."""
from __future__ import annotations

import time

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QIcon, QImage, QLinearGradient, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QPushButton,
                             QSplashScreen, QTableWidget, QTableWidgetItem, QVBoxLayout)

from .. import about
from . import touch
from .theme import ACCENT, LINE_HI, MUTED, TEXT

SPLASH_S = 1.6              # shown at least this long (the window opens under it)
W, H = 560, 300


def logo(size: int) -> QPixmap:
    """The PNG, so Qt's SVG plugin isn't needed (Pi images often lack it)."""
    img = QImage(str(about.BRANDING / "logo.png"))
    if img.isNull():
        return QPixmap()
    return QPixmap.fromImage(img.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation))


def icon() -> QIcon:
    i = QIcon()
    for n in (16, 24, 32, 48, 64, 128, 256):
        p = logo(n)
        if not p.isNull():
            i.addPixmap(p)
    return i


def splash_pixmap(ratio: float = 1.0) -> QPixmap:
    px = QPixmap(int(W * ratio), int(H * ratio))
    px.setDevicePixelRatio(ratio)
    px.fill(QColor("#0f1115"))
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, 0, H)
    g.setColorAt(0, QColor("#1b2431"))
    g.setColorAt(1, QColor("#0d1117"))
    p.fillRect(QRectF(0, 0, W, H), g)
    p.setPen(QPen(QColor("#1f2a37"), 1))
    for x in range(40, W, 40):
        p.drawLine(x, 0, x, H)
    for y in range(40, H, 40):
        p.drawLine(0, y, W, y)
    mark = logo(int(180 * ratio))
    if not mark.isNull():
        mark.setDevicePixelRatio(ratio)
        p.drawPixmap(34, 48, mark)
    f = QFont(QApplication.font())
    f.setPixelSize(46)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor(TEXT))
    p.drawText(QRectF(240, 70, W - 250, 60), Qt.AlignmentFlag.AlignVCenter, about.APP_NAME)
    f.setPixelSize(16)
    f.setBold(False)
    p.setFont(f)
    p.setPen(QColor(MUTED))
    p.drawText(QRectF(242, 132, W - 270, 60), Qt.TextFlag.TextWordWrap, about.TAGLINE)
    p.setPen(QColor(ACCENT))
    p.drawText(QRectF(242, 190, W - 270, 30), Qt.AlignmentFlag.AlignVCenter,
               f"Version {about.VERSION}")
    f.setPixelSize(12)
    p.setFont(f)
    p.setPen(QColor(MUTED))
    p.drawText(QRectF(34, H - 34, W - 60, 24), Qt.AlignmentFlag.AlignVCenter, about.COPYRIGHT)
    p.setPen(QPen(QColor(LINE_HI), 2))
    p.drawRect(QRectF(1, 1, W - 2, H - 2))
    p.end()
    return px


class Splash(QSplashScreen):
    """Shown while the program starts. A click dismisses it; it never delays the
    start, and goes SPLASH_S after it came up."""

    def __init__(self, app):
        scr = app.primaryScreen()
        super().__init__(splash_pixmap(scr.devicePixelRatio() if scr else 1.0),
                         Qt.WindowType.WindowStaysOnTopHint)
        self.t0 = time.monotonic()

    def say(self, text: str):
        self.showMessage(text + "  ", Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                         QColor(MUTED))
        QApplication.processEvents()

    def done(self):
        left = SPLASH_S - (time.monotonic() - self.t0)
        QTimer.singleShot(max(0, int(left * 1000)), self.close)


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {about.APP_NAME}")
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        pic = QLabel()
        pic.setPixmap(logo(128))
        top.addWidget(pic)
        text = QLabel(
            f"<div style='font-size:26px; font-weight:bold'>{about.APP_NAME}</div>"
            f"<div>{about.TAGLINE}</div>"
            f"<p>Version {about.version_text()}<br>{about.COPYRIGHT}</p>"
            f"<p><a style='color:{ACCENT}' href='{about.URL}'>{about.URL}</a></p>")
        text.setOpenExternalLinks(True)
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setWordWrap(True)
        text.setMinimumWidth(380)
        top.addWidget(text, 1)
        v.addLayout(top)
        rows = about.components()
        t = QTableWidget(len(rows), 2)
        t.horizontalHeader().setVisible(False)
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setStretchLastSection(True)
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for r, (n, ver) in enumerate(rows):
            t.setItem(r, 0, QTableWidgetItem(n))
            t.setItem(r, 1, QTableWidgetItem(ver))
        t.setColumnWidth(0, 130)
        t.setMinimumHeight(220)
        v.addWidget(t)
        note = QLabel("Instrument names are their makers' trademarks; they are used here only "
                      "to say what the program works with. Measurements and switched outputs are "
                      "your responsibility: no software is a safety device.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        v.addWidget(note)
        row = QHBoxLayout()
        self.copy = QPushButton("Copy details")
        self.copy.setToolTip("Versions and file locations, for a bug report")
        self.copy.clicked.connect(self._copy)
        row.addWidget(self.copy)
        row.addStretch(1)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        row.addWidget(close)
        v.addLayout(row)
        if touch.TOUCH:
            for b in (self.copy, close):
                b.setMinimumHeight(48)

    def _copy(self):
        QApplication.clipboard().setText(about.details())
        self.copy.setText("Copied")
