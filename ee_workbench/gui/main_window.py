"""Main window: connection bar + one tab per connected instrument."""
from __future__ import annotations

import re
import sys

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (QApplication, QComboBox, QFrame, QHBoxLayout, QLabel,
                             QMainWindow, QMessageBox, QPushButton, QStatusBar, QTabWidget, QToolButton,
                             QVBoxLayout, QWidget)

from ..instruments import MODELS, SIM_MODELS
from ..transport import describe, list_resources
from .dmm_panel import DmmPanel
from .supply_panel import SupplyPanel
from .widgets import C_MUTED, dark_palette


class MainWindow(QMainWindow):
    def __init__(self, initial: list[tuple[str, str]] | None = None):
        super().__init__()
        self.setWindowTitle("EE Workbench")
        self.resize(1440, 900)
        pg.setConfigOptions(antialias=True, background="#121417", foreground="#c9ccd1")

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 4)
        root.addWidget(self._build_bar())
        self.tabs = QTabWidget()
        self.tabs.setObjectName("instruments")
        self.tabs.setTabsClosable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        root.addWidget(self.tabs, 1)
        self.empty = QLabel("Pick a resource and model above, then Connect.\n\n"
                            "Tip: ⌕ scans the GPIB bus. The HP 3456A has no ID query, "
                            "so select it by model.\nTry SIM::66321D, SIM::6632A or "
                            "SIM::3456A to explore without hardware.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(f"color:{C_MUTED}; font-size:14px;")
        root.addWidget(self.empty, 1)
        self.setCentralWidget(central)
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self._update_empty()

        sc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc.activated.connect(self.all_outputs_off)

        self.refresh_resources()
        for model, res in initial or []:
            self.open_instrument(res, model)

    # ---------------------------------------------------------------- bar
    def _build_bar(self):
        w = QFrame()
        w.setObjectName("bar")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.addWidget(QLabel("Resource"))
        self.res_combo = QComboBox()
        self.res_combo.setEditable(True)
        self.res_combo.setMinimumWidth(240)
        self.res_combo.currentTextChanged.connect(self._resource_changed)
        lay.addWidget(self.res_combo)
        rb = QToolButton()
        rb.setText("⟳")
        rb.setToolTip("Rescan VISA resources")
        rb.clicked.connect(lambda: self.refresh_resources())
        lay.addWidget(rb)
        sb = QToolButton()
        sb.setText("⌕ Scan GPIB")
        sb.setToolTip("Find every address with a listener on GPIB0 (sends no commands)")
        sb.clicked.connect(lambda: self.refresh_resources(scan=True))
        lay.addWidget(sb)
        lay.addSpacing(12)
        lay.addWidget(QLabel("Model"))
        self.model_combo = QComboBox()
        for key, label in MODELS.items():
            self.model_combo.addItem(label, key)
        lay.addWidget(self.model_combo)
        self.baud_label = QLabel("Baud")
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["300", "600", "1200", "2400", "4800", "9600"])
        self.baud_combo.setCurrentText("9600")
        lay.addWidget(self.baud_label)
        lay.addWidget(self.baud_combo)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setMinimumWidth(100)
        self.connect_btn.clicked.connect(
            lambda: self.open_instrument(self.res_combo.currentText().strip(),
                                         self.model_combo.currentData()))
        lay.addWidget(self.connect_btn)
        lay.addStretch(1)
        off = QPushButton("All outputs OFF (Esc)")
        off.setStyleSheet("QPushButton { color:#ef9a9a; font-weight:bold; }")
        off.clicked.connect(self.all_outputs_off)
        lay.addWidget(off)
        return w

    def refresh_resources(self, scan=False):
        cur = self.res_combo.currentText()
        self.res_combo.blockSignals(True)
        self.res_combo.clear()
        found = list_resources(scan=scan)
        self.res_combo.addItems(found)
        self.res_combo.blockSignals(False)
        if cur:
            self.res_combo.setCurrentText(cur)
        self._resource_changed(self.res_combo.currentText())
        if scan:
            gp = [r for r in found if r.startswith("GPIB")]
            self.status.showMessage("GPIB listeners: " + (", ".join(gp) or "none found"), 8000)

    def _resource_changed(self, text):
        serial = text.upper().startswith("ASRL")
        self.baud_label.setVisible(serial)
        self.baud_combo.setVisible(serial)
        if text.upper() in SIM_MODELS:
            self.model_combo.setCurrentIndex(self.model_combo.findData(SIM_MODELS[text.upper()]))

    # ---------------------------------------------------------------- tabs
    def panels(self):
        return [self.tabs.widget(i) for i in range(self.tabs.count())]

    def open_instrument(self, resource: str, model: str):
        if not resource:
            return
        if re.match(r"GPIB\d*::0(::|$)", resource, re.I):
            QMessageBox.warning(
                self, "GPIB address 0",
                "GPIB address 0 belongs to the controller (the GPIB-USB adapter itself), "
                "so no instrument can use it.\n\nPress ⌕ Scan GPIB to see which addresses "
                "your instruments are really on (the HP 3456A defaults to 22).")
            return
        for p in self.panels():
            if p.resource == resource:
                self.tabs.setCurrentWidget(p)
                self.status.showMessage(f"{resource} is already open", 4000)
                return
        if resource.upper() in SIM_MODELS and model == "auto":
            model = SIM_MODELS[resource.upper()]
        cls = DmmPanel if model == "3456A" else SupplyPanel
        panel = cls(resource, model, int(self.baud_combo.currentText()))
        where = describe(resource)
        idx = self.tabs.addTab(panel, f"… {where}")
        self.tabs.setCurrentIndex(idx)
        panel.title_changed.connect(
            lambda t, p=panel, w=where: self.tabs.setTabText(self.tabs.indexOf(p), f"{t}  ·  {w}"))
        panel.message.connect(lambda m: self.status.showMessage(m, 8000))
        panel.failed.connect(lambda m, p=panel: self._failed(p, m))
        self._update_empty()

    def _failed(self, panel, msg):
        self.status.showMessage(f"{panel.resource}: {msg}", 15000)
        i = self.tabs.indexOf(panel)
        if i >= 0:
            self.tabs.setTabText(i, f"⚠ {describe(panel.resource)}")

    def close_tab(self, i: int):
        p = self.tabs.widget(i)
        p.shutdown()
        self.tabs.removeTab(i)
        p.deleteLater()
        self._update_empty()

    def _update_empty(self):
        has = self.tabs.count() > 0
        self.tabs.setVisible(has)
        self.empty.setVisible(not has)

    def all_outputs_off(self):
        n = 0
        for p in self.panels():
            if isinstance(p, SupplyPanel) and p.connected:
                p.output_off()
                n += 1
        self.status.showMessage(f"Output OFF sent to {n} supply(s)", 5000)

    def closeEvent(self, e):
        for p in self.panels():
            p.shutdown()
        super().closeEvent(e)


def parse_target(s: str) -> tuple[str, str]:
    """'GPIB0::22::INSTR' or '3456A@GPIB0::22::INSTR' -> (model, resource)."""
    if "@" in s:
        model, res = s.split("@", 1)
        model = {"66321D": "663xx", "66321B": "663xx", "66311B": "663xx",
                 "6633A": "6632A", "6634A": "6632A"}.get(model, model)
        return model, res
    return "auto", s


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="EE Workbench instrument control")
    ap.add_argument("targets", nargs="*",
                    help="resources to open, optionally MODEL@RESOURCE, e.g. "
                         "GPIB0::5::INSTR 3456A@GPIB0::22::INSTR SIM::6632A")
    args = ap.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("EE Workbench")
    dark_palette(app)
    win = MainWindow([parse_target(t) for t in args.targets])
    win.show()
    return app.exec()
