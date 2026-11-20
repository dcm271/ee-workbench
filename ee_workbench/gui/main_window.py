"""Main window: connection bar + one tab per connected instrument."""
from __future__ import annotations

import re
import sys
import time

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (QApplication, QComboBox, QFrame, QHBoxLayout, QLabel,
                             QMainWindow, QMessageBox, QPushButton, QStatusBar, QTabWidget, QToolButton,
                             QVBoxLayout, QWidget)

from ..config import Config, Entry, Session, load_config, norm_model, startup_entries
from ..instruments import MODELS, SIM_MODELS
from ..transport import describe, gpib_ready, list_resources, scan_gpib
from . import touch
from .connect_dialog import ConnectDialog
from .dmm_panel import DmmPanel
from .supply_panel import SupplyPanel
from .widgets import C_MUTED, dark_palette


class MainWindow(QMainWindow):
    def __init__(self, initial: list[tuple[str, str]] | None = None,
                 config: Config | None = None, session: Session | None = None,
                 open_mode: str | None = None):
        super().__init__()
        self.config = config or Config()
        self.session = session
        self.open_mode = open_mode or self.config.open
        self._restoring = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1000)
        self._save_timer.timeout.connect(self.save_session)
        self.setWindowTitle("EE Workbench")
        self.resize(1440, 900)
        pg.setConfigOptions(antialias=True, background="#121417", foreground="#c9ccd1")

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 4)
        root.addWidget(self._build_touch_bar() if touch.TOUCH else self._build_bar())
        self.tabs = QTabWidget()
        self.tabs.setObjectName("instruments")
        self.tabs.setTabsClosable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        root.addWidget(self.tabs, 1)
        self.empty = QLabel(
            "Tap  ＋ Add instrument  to connect.\n\nThe HP 3456A has no ID query, so pick "
            "its model.\nSIM::… entries let you explore without hardware."
            if touch.TOUCH else
            "Pick a resource and model above, then Connect.\n\n"
            "Tip: ⌕ scans the GPIB bus. The HP 3456A has no ID query, "
            "so select it by model.\nTry SIM::66321D, SIM::6632A or "
            "SIM::3456A to explore without hardware.")
        self.empty.setWordWrap(True)
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
        fs = QShortcut(QKeySequence(Qt.Key.Key_F11), self)
        fs.activated.connect(self.toggle_fullscreen)

        if not touch.TOUCH:
            self.refresh_resources()
        if initial:
            for model, res in initial:
                self.open_instrument(res, model)
        elif self.session is not None:
            self._restoring = True
            QTimer.singleShot(0, self._restore)
        if self.config.warning:
            self.status.showMessage("Config: " + self.config.warning, 20000)

    # ---------------------------------------------------------------- bars
    def _build_touch_bar(self):
        w = QFrame()
        w.setObjectName("bar")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(6, 4, 6, 4)
        add = QPushButton("＋ Add instrument")
        add.clicked.connect(self.add_dialog)
        lay.addWidget(add)
        lay.addStretch(1)
        off = QPushButton("ALL OUTPUTS OFF")
        off.setMinimumWidth(220)
        off.setStyleSheet("QPushButton { background:#8e1c1c; color:white; font-weight:bold; "
                          "font-size:16px; }")
        off.clicked.connect(self.all_outputs_off)
        lay.addWidget(off)
        lay.addWidget(self._build_fs_button(56))
        self.quit_btn = QPushButton("✕")
        self.quit_btn.setFixedWidth(56)
        self.quit_btn.setToolTip("Quit EE Workbench")
        self.quit_btn.clicked.connect(self._confirm_quit)
        self.quit_btn.setVisible(False)   # shown in full-screen mode (no title bar)
        lay.addWidget(self.quit_btn)
        return w

    def add_dialog(self):
        dlg = ConnectDialog(self, {p.resource for p in self.panels()})
        if dlg.exec() and dlg.resource:
            self.open_instrument(dlg.resource, dlg.model)

    def _confirm_quit(self):
        if QMessageBox.question(self, "Quit", "Quit EE Workbench?\n\nSupply outputs stay "
                                "as they are.") == QMessageBox.StandardButton.Yes:
            self.close()

    def _build_fs_button(self, width: int | None = None):
        self.fs_btn = QPushButton("⛶")
        self.fs_btn.setToolTip("Full screen on/off (F11)")
        if width:
            self.fs_btn.setFixedWidth(width)
        self.fs_btn.clicked.connect(self.toggle_fullscreen)
        return self.fs_btn

    def go_fullscreen(self):
        """Some Wayland compositors (the Pi desktop included) ignore a
        full-screen request made before the window is first mapped, so ask
        again once it is on screen."""
        self._want_fs = True
        self.set_kiosk(True)
        self.showFullScreen()
        QTimer.singleShot(700, lambda: self._want_fs and self.showFullScreen())

    def set_kiosk(self, on: bool):
        """Full screen has no title bar, so show our own quit button."""
        if hasattr(self, "quit_btn"):
            self.quit_btn.setVisible(on)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self._want_fs = False
            self.showNormal()
            self.showMaximized()
            self.set_kiosk(False)
        else:
            self.go_fullscreen()

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
        lay.addWidget(self._build_fs_button())
        self.quit_btn = QPushButton("✕")
        self.quit_btn.setToolTip("Quit EE Workbench")
        self.quit_btn.clicked.connect(self._confirm_quit)
        self.quit_btn.setVisible(False)   # shown in full screen (no title bar)
        lay.addWidget(self.quit_btn)
        return w

    def refresh_resources(self, scan=True):
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

    # ---------------------------------------------------------------- start-up
    def _restore(self):
        """Open the start-up instruments, first waiting (without freezing the
        window) for the GPIB adapter, which may still be initialising at boot."""
        mode = self.open_mode
        if mode == "none":
            self._restoring = False
            return
        plan = startup_entries(self.config, self.session, [], mode)
        needs_gpib = mode == "scan" or any(e.resource.upper().startswith("GPIB") for e in plan)
        if not needs_gpib or self.config.wait_for_gpib <= 0 or gpib_ready():
            self._finish_restore()
            return
        deadline = time.monotonic() + self.config.wait_for_gpib

        def poll():
            left = deadline - time.monotonic()
            if gpib_ready() or left <= 0:
                self._wait_timer.stop()
                self._finish_restore()
            else:
                self.status.showMessage(f"Waiting for the GPIB adapter… ({left:.0f} s)")
        self._wait_timer = QTimer(self)
        self._wait_timer.timeout.connect(poll)
        self._wait_timer.start(1000)
        poll()

    def _finish_restore(self):
        mode = self.open_mode
        scanned = [f"GPIB0::{pad}::INSTR" for pad in scan_gpib()] if mode == "scan" else []
        plan = startup_entries(self.config, self.session, scanned, mode)
        for e in plan:
            self.open_instrument(e.resource, e.model, e.settings)
        self._restoring = False
        where = self.config.path or "config.toml"
        what = {"last": "Reopened last session", "list": "Opened configured instruments",
                "scan": "Opened instruments found on the bus"}[mode]
        self.status.showMessage(f"{what}: {len(plan)}   (start-up behaviour: {where})", 10000)
        if self.tabs.count():
            self.tabs.setCurrentIndex(0)

    def schedule_save(self):
        if self.session is not None and not self._restoring:
            self._save_timer.start()

    def save_session(self):
        if self.session is None or self._restoring:
            return
        self.session.save([Entry(p.resource, norm_model(p.model), p.session_settings())
                           for p in self.panels()])

    # ---------------------------------------------------------------- tabs
    def panels(self):
        return [self.tabs.widget(i) for i in range(self.tabs.count())]

    def open_instrument(self, resource: str, model: str, settings: dict | None = None,
                        index: int | None = None):
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
        baud = int(self.baud_combo.currentText()) if hasattr(self, "baud_combo") else 9600
        panel = cls(resource, model, baud)
        if settings:
            panel.restore_settings(settings)
        where = describe(resource)
        if index is None:
            idx = self.tabs.addTab(panel, f"… {where}")
        else:
            idx = self.tabs.insertTab(index, panel, f"… {where}")
        self.tabs.setCurrentIndex(idx)
        panel.title_changed.connect(
            lambda t, p=panel, w=where: self.tabs.setTabText(self.tabs.indexOf(p), f"{t}  ·  {w}"))
        panel.message.connect(lambda m: self.status.showMessage(m, 8000))
        panel.failed.connect(lambda m, p=panel: self._failed(p, m))
        panel.settings_changed.connect(self.schedule_save)
        self._update_empty()
        self.schedule_save()

    def _failed(self, panel, msg):
        self.status.showMessage(f"{panel.resource}: {msg}", 15000)
        i = self.tabs.indexOf(panel)
        if i >= 0:
            self.tabs.setTabText(i, f"⚠ {describe(panel.resource)}")
            retry = QToolButton()
            retry.setText("⟳ Retry")
            retry.setToolTip("Try connecting again (e.g. after switching the instrument on)")
            retry.clicked.connect(lambda _, p=panel: self.reconnect(p))
            self.tabs.tabBar().setTabButton(i, self.tabs.tabBar().ButtonPosition.LeftSide, retry)

    def reconnect(self, panel):
        i = self.tabs.indexOf(panel)
        if i < 0:
            return
        res, model, settings = panel.resource, panel.model, panel.session_settings()
        self._restoring = True          # don't save the brief gap as "closed"
        self.close_tab(i)
        self._restoring = False
        self.open_instrument(res, model, settings, index=i)

    def close_tab(self, i: int):
        p = self.tabs.widget(i)
        p.shutdown()
        self.tabs.removeTab(i)
        p.deleteLater()
        self._update_empty()
        self.schedule_save()

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
        self._save_timer.stop()
        self.save_session()
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
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--touch", dest="touch", action="store_true", default=None,
                      help="force the touch-screen layout")
    mode.add_argument("--no-touch", dest="touch", action="store_false",
                      help="force the desktop layout")
    ap.add_argument("--fullscreen", action="store_true",
                    help="start full screen (F11 toggles; a quit button appears)")
    ap.add_argument("--hide-cursor", action="store_true",
                    help="hide the mouse pointer (touch-only kiosks)")
    ap.add_argument("--open", choices=["last", "list", "scan", "none"],
                    help="what to open at start-up (overrides config.toml)")
    ap.add_argument("--config", metavar="FILE",
                    help="config file (default ~/.config/ee-workbench/config.toml)")
    args = ap.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("EE Workbench")
    dark_palette(app)
    if touch.detect(app, args.touch):
        app.setStyleSheet(app.styleSheet() + touch.TOUCH_STYLE)
    from pathlib import Path
    cfg = load_config(Path(args.config).expanduser() if args.config else None)
    if cfg.warning:
        print("ee-workbench:", cfg.warning, file=sys.stderr)
    win = MainWindow([parse_target(t) for t in args.targets], cfg, Session(), args.open)
    if args.hide_cursor:
        app.setOverrideCursor(Qt.CursorShape.BlankCursor)
    if args.fullscreen:
        win.go_fullscreen()
    elif touch.TOUCH:
        win.showMaximized()
    else:
        win.show()
    return app.exec()
