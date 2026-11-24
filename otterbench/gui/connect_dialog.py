"""Touch-friendly "Add instrument" dialog: big buttons, no typing required."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QButtonGroup, QDialog, QGridLayout, QHBoxLayout, QInputDialog,
                             QLabel, QListWidget, QListWidgetItem, QPushButton, QSpinBox,
                             QVBoxLayout)

from ..identify import probeable
from ..instruments import MODELS, all_models, definition_for, model_for
from ..transport import list_resources
from . import touch
from .theme import set_role
from .widgets import C_MUTED

SHORT_MODELS = {"auto": "Auto-\ndetect", "663xx": "66321D\n66311B/21B",
                "6632A": "6632A\n33A/34A", "DPS150": "DPS-150\nFNIRSI", "3456A": "3456A\nDMM",
                "BK393": "B&&K 393\nhandheld", "BK393B": "B&&K 393B\nhandheld",
                "XDM": "OWON\nXDM1041", "PPK2": "PPK2\nNordic",
                "SALEAE": "Saleae\nLogic", "SIGROK": "sigrok\nlogic",
                "FGEN": "Function\ngenerator", "GPIO": "Pi GPIO\nheader",
                "UART": "Serial\nconsole"}


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
        typed = QPushButton("Type a resource…")
        typed.setToolTip("Any VISA resource, e.g. a LAN function generator: "
                         "TCPIP::192.168.1.50::INSTR")
        typed.clicked.connect(self._type_resource)
        other.addWidget(typed)
        self.ident_btn = QPushButton("Identify port…")
        self.ident_btn.setToolTip("Find out what is on the chosen USB serial port")
        self.ident_btn.setEnabled(False)
        self.ident_btn.clicked.connect(self._identify)
        other.addWidget(self.ident_btn)
        left.addLayout(other)
        body.addLayout(left, 3)

        right = QVBoxLayout()
        lab = QLabel("Model")
        lab.setStyleSheet(f"color:{C_MUTED};")
        right.addWidget(lab)
        self.models = QButtonGroup(self)
        self.model_btns = {}
        grid = QGridLayout()
        for n, key in enumerate(MODELS):
            b = QPushButton(SHORT_MODELS.get(key, key))
            b.setCheckable(True)
            b.setMinimumHeight(56)
            b.setToolTip(MODELS[key])
            b.clicked.connect(lambda _, k=key: setattr(self, "model", k))
            self.models.addButton(b)
            self.model_btns[key] = b
            grid.addWidget(b, n // 2, n % 2)
        n = len(MODELS)
        self.more_btn = QPushButton("Meter / load /\nscope / SCPI…")
        self.more_btn.setCheckable(True)
        self.more_btn.setMinimumHeight(56)
        self.more_btn.setToolTip("Bench meters, electronic loads, multi-output supplies and "
                                 "oscilloscopes (generic SCPI definitions)")
        self.more_btn.clicked.connect(self._pick_more)
        self.models.addButton(self.more_btn)
        grid.addWidget(self.more_btn, n // 2, n % 2)
        right.addLayout(grid)
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
        set_role(self.ok, "primary")
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

    def _type_resource(self):
        text, ok = QInputDialog.getText(
            self, "Resource", "VISA resource, e.g. TCPIP::192.168.1.50::INSTR\n"
            "(a LAN instrument), USB0::…::INSTR, ASRL/dev/ttyUSB0::INSTR:", text=self.resource)
        if ok and text.strip():
            self._select(text.strip())

    def _identify(self):
        from .identify_dialog import ask
        hit = ask(self, self.resource, self.open_resources)
        if hit is not None:
            self._select(hit.resource)
            self._set_model(hit.model)

    def _set_model(self, key: str):
        self.model = key
        if key in self.model_btns:
            self.model_btns[key].setChecked(True)
        else:
            self.more_btn.setChecked(True)
            self.more_btn.setText(self._short(key))

    def _pick_more(self):
        extra = {k: v for k, v in all_models().items() if k not in MODELS}
        labels = list(extra.values())
        cur = extra.get(self.model, labels[0] if labels else "")
        text, ok = QInputDialog.getItem(self, "Model", "Instrument model:", labels,
                                        labels.index(cur) if cur in labels else 0, False)
        if ok and text:
            self.model = next(k for k, v in extra.items() if v == text)
            self.more_btn.setText(self._short(self.model))
        elif self.model in self.model_btns:
            self.model_btns[self.model].setChecked(True)

    @staticmethod
    def _short(key: str) -> str:
        d = definition_for(key) if key.startswith("SCPI:") else None
        kind = "scope" if key.startswith("SCOPE:") else d.type if d and d.protocol else "SCPI"
        return f"{key.split(':', 1)[-1].title() if kind == 'scope' else key[5:]}\n{kind}"

    def _select(self, resource: str):
        self.resource = resource
        implied = model_for(resource)
        if implied != "auto":
            self._set_model(implied)
        self.ident_btn.setEnabled(bool(probeable(resource)))
        self.chosen.setText(resource)
        self.chosen.setStyleSheet("font-weight:bold;")
        self.ok.setEnabled(True)
