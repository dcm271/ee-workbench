"""Wiring notes (Tools ▾): what is connected to what, with a photo."""
from __future__ import annotations

import base64

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PyQt6.QtGui import QImage, QImageReader, QPixmap
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                             QLabel, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout)

from .. import wiring
from .forms import hint

LONG_SIDE = 1600                 # pixels a photo is scaled down to


def to_jpeg(img: QImage) -> bytes:
    """Scaled down, without the camera's metadata (place, time)."""
    if max(img.width(), img.height()) > LONG_SIDE:
        img = img.scaled(LONG_SIDE, LONG_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    plain = QImage(img.size(), QImage.Format.Format_RGB32)
    plain.fill(Qt.GlobalColor.white)
    from PyQt6.QtGui import QPainter
    p = QPainter(plain)
    p.drawImage(0, 0, img)
    p.end()
    plain.save(buf, "JPG", 82)
    return bytes(data)


class WiringDialog(QDialog):
    def __init__(self, parent, title: str = "Wiring notes"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 640)
        w = wiring.current()
        self.photo = w.photo
        v = QVBoxLayout(self)
        v.addWidget(hint("What is connected to what. It is saved with every setup and in the "
                         "session being recorded, and printed in reports."))
        self.text = QPlainTextEdit(w.text)
        self.text.setPlaceholderText("66321D OUT+ → PPK2 VIN\nPPK2 VOUT → DUT J3 pin 1\n"
                                     "3456A → TP4 / GND (Kelvin)\nscope CH1 → SW node, 10×")
        v.addWidget(self.text, 1)
        self.pic = QLabel("")
        self.pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pic.setMinimumHeight(40)
        v.addWidget(self.pic)
        row = QHBoxLayout()
        for label, fn in (("Photo…", self.choose), ("Paste photo", self.paste),
                          ("Remove photo", self.remove)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._show()

    def _show(self):
        pm = QPixmap()
        if self.photo and pm.loadFromData(wiring.decode(self.photo)):
            self.pic.setPixmap(pm.scaled(700, 320, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
        else:
            self.pic.setPixmap(QPixmap())
            self.pic.setText("No photo")

    def use(self, img: QImage):
        if img.isNull():
            QMessageBox.warning(self, "Photo", "That isn't a picture this can read.")
            return
        self.photo = base64.b64encode(to_jpeg(img)).decode()
        self._show()

    def choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "Photo of the wiring", "",
                                              "Pictures (*.jpg *.jpeg *.png *.webp *.bmp)")
        if path:
            r = QImageReader(path)
            r.setAutoTransform(True)         # a phone's rotation
            self.use(r.read())

    def paste(self):
        self.use(QApplication.clipboard().image())

    def remove(self):
        self.photo = ""
        self._show()

    def save(self):
        try:
            wiring.set_current(wiring.Wiring(self.text.toPlainText()[:wiring.MAX_TEXT],
                                             self.photo))
        except OSError as e:
            QMessageBox.warning(self, "Wiring notes", f"Can't save:\n{e}")
            return
        self.accept()
