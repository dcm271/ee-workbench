"""Touch-friendly "Add instrument" dialog: big buttons, no typing required."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QButtonGroup, QDialog, QHBoxLayout, QLabel, QListWidget,
                             QListWidgetItem, QPushButton, QSpinBox, QVBoxLayout)

from ..instruments import MODELS, model_for
from ..transport import list_resources
from . import touch
from .widgets import C_MUTED

SHORT_MODELS = {"auto": "Auto\n(supplies)", "663xx": "66321D\n66311B/21B",
                "6632A": "6632A\n33A/34A", "3456A": "3456A\nDMM", "PPK2": "PPK2\nNordic"}


class ConnectDialog(QDialog):
    def __init__(self, parent, open_resources: set[str]):
        super().__init__(parent)
        self.setWindowTitle("Add instrument")
        self.setModal(True)
        self.open_resources = open_resources
        self.resource = ""
        self.model = "auto"

        v = QVBoxLayout(self)
        top = QHBoxLayout()
        t = QLabel("Add instrument")
        t.setStyleSheet("font-weight:bold; font-size:18px;")
        top.addWidget(t, 1)
        scan = QPushButton("⌕ Scan GPIB")
        scan.clicked.connect(lambda: self.populate(scan=True))
        top.addWidget(scan)
        v.addLayout(top)

        body = QHBoxLayout()
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.setStyleSheet("QListWidget::item { min-height: 46px; padding-left: 8px; }")
        self.list.itemClicked.connect(self._picked)
        touch.kinetic(self.list)
        left.addWidget(self.list, 1)
        other = QHBoxLayout()
        other.addWidget(QLabel("Other GPIB address"))
        self.addr = QSpinBox()
        self.addr.setRange(1, 30)
        self.addr.setValue(22)
        touch.attach_keypad(self.addr, "GPIB address", self._use_addr)
        other.addWidget(self.addr)
        use = QPushButton("Use")
        use.clicked.connect(self._use_addr)
        other.addWidget(use)
        left.addLayout(other)
        body.addLayout(left, 3)

        right = QVBoxLayout()
        lab = QLabel("Model")
        lab.setStyleSheet(f"color:{C_MUTED};")
        right.addWidget(lab)
        self.models = QButtonGroup(self)
        self.model_btns = {}
        for key in MODELS:
            b = QPushButton(SHORT_MODELS[key])
            b.setCheckable(True)
            b.setMinimumHeight(56)
            b.setToolTip(MODELS[key])
            b.clicked.connect(lambda _, k=key: setattr(self, "model", k))
            self.models.addButton(b)
            self.model_btns[key] = b
            right.addWidget(b)
        self.model_btns["auto"].setChecked(True)
        right.addStretch(1)
        body.addLayout(right, 2)
        v.addLayout(body, 1)

        self.chosen = QLabel("Choose an instrument")
        self.chosen.setStyleSheet(f"color:{C_MUTED};")
        bottom = QHBoxLayout()
        bottom.addWidget(self.chosen, 1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        bottom.addWidget(cancel)
        self.ok = QPushButton("Connect")
        self.ok.setStyleSheet("QPushButton { background:#2e7d32; color:white; font-weight:bold; }"
                              "QPushButton:disabled { background:#2b2f36; color:#6b7079; }")
        self.ok.setEnabled(False)
        self.ok.clicked.connect(self.accept)
        bottom.addWidget(self.ok)
        v.addLayout(bottom)
        self.resize(760, 430)
        self.populate()

    def populate(self, scan=True):
        self.list.clear()
        for r in list_resources(scan=scan):
            it = QListWidgetItem(r + ("   (already open)" if r in self.open_resources else ""))
            it.setData(Qt.ItemDataRole.UserRole, r)
            if r in self.open_resources:
                it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(it)

    def _picked(self, item):
        self._select(item.data(Qt.ItemDataRole.UserRole))

    def _use_addr(self):
        self._select(f"GPIB0::{self.addr.value()}::INSTR")

    def _select(self, resource: str):
        self.resource = resource
        implied = model_for(resource)
        if implied != "auto":
            self.model = implied
            self.model_btns[implied].setChecked(True)
        self.chosen.setText(resource)
        self.chosen.setStyleSheet("font-weight:bold;")
        self.ok.setEnabled(True)
