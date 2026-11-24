"""“Identify port…”: find out what is on the chosen serial port (identify.py)."""
from __future__ import annotations

import threading

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                             QPushButton, QVBoxLayout)

from ..about import APP_NAME
from ..identify import Cancelled, Found, identify_port, probeable
from ..instruments import all_models
from . import touch
from .theme import set_role


class _Signals(QObject):
    progress = pyqtSignal(str)
    done = pyqtSignal(object, str)           # Found | None, error text


class IdentifyDialog(QDialog):
    """exec() -> accepted when the user wants to use `found`."""

    def __init__(self, parent, path: str, open_link=None, quick: float = 1.0):
        super().__init__(parent)
        self.setWindowTitle("Identify port")
        self.found: Found | None = None
        self.stop = threading.Event()
        v = QVBoxLayout(self)
        title = QLabel(path)
        title.setStyleSheet("font-weight:bold;")
        v.addWidget(title)
        self.text = QLabel("Starting…")
        self.text.setWordWrap(True)
        self.text.setMinimumWidth(460)
        self.text.setMinimumHeight(60)
        self.text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        v.addWidget(self.text)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        v.addWidget(self.bar)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(self.reject)
        row.addWidget(self.cancel)
        self.use = QPushButton("Use it")
        set_role(self.use, "primary")
        self.use.setEnabled(False)
        self.use.clicked.connect(self.accept)
        row.addWidget(self.use)
        v.addLayout(row)
        if touch.TOUCH:
            for b in (self.cancel, self.use):
                b.setMinimumHeight(48)
        self.sig = _Signals()
        self.sig.progress.connect(self.text.setText)
        self.sig.done.connect(self._done)
        sig, stop = self.sig, self.stop

        def body():
            try:
                hit, err = identify_port(path, open_link, sig.progress.emit, stop, quick), ""
            except Cancelled:
                return
            except Exception as e:      # noqa: BLE001 - port busy, unplugged
                hit, err = None, str(e)
            try:
                sig.done.emit(hit, err)
            except RuntimeError:        # the dialog has gone
                pass
        self.thread = threading.Thread(target=body, daemon=True, name="identify")
        self.thread.start()

    def _done(self, hit, err):
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        self.cancel.setText("Close")
        if err:
            self.text.setText(err)
        elif hit is None:
            self.text.setText("Nothing recognised. Check the cable, that the instrument is on "
                              "and its baud rate, or choose the model by hand. (A meter that "
                              "only sends when its USB key is pressed must be sending.)")
        else:
            self.found = hit
            label = all_models().get(hit.model, "")
            self.text.setText(hit.text + (f"\n\n→ {label}" if label else "\n\nNo driver or "
                              "definition in the app fits it."
                              + (f" It answers at {hit.baud} baud." if hit.baud else "")))
            self.use.setEnabled(bool(hit.model and hit.resource))

    def done(self, code):
        self.stop.set()
        self.thread.join(3.0)           # the port must be free again before it is opened
        super().done(code)


def ask(parent, resource: str, open_resources=()) -> Found | None:
    """Confirm, probe, and return what the user chose to use."""
    path = probeable(resource)
    if not path:
        QMessageBox.information(parent, "Identify port",
                                "Choose a USB serial port first (ASRL/dev/ttyUSB0…). GPIB and "
                                "LAN instruments are found with model “Auto-detect”.")
        return None
    if any(probeable(r) == path for r in open_resources):
        QMessageBox.information(parent, "Identify port", f"{path} is open in a tab already.")
        return None
    if QMessageBox.question(
            parent, "Identify port",
            f"Listen on {path}, then send it the ID questions of the instruments {APP_NAME} "
            "knows, at several speeds?\n\nThey only read. But don't do this on a port whose "
            "device could act on unexpected text (a DUT's command console, a 3D printer, a "
            "modem).") != QMessageBox.StandardButton.Yes:
        return None
    dlg = IdentifyDialog(parent, path)
    return dlg.found if dlg.exec() else None
