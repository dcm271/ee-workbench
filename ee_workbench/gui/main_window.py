"""Main window: connection bar + one tab per connected instrument."""
from __future__ import annotations

import re
import sys
import time

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout,
                             QInputDialog, QLabel, QMainWindow, QMenu, QMessageBox, QPushButton,
                             QStatusBar, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from .. import crash
from ..clock import now
from ..config import Config, Entry, Session, load_config, norm_model, startup_entries
from ..instruments import MODELS, model_for
from ..session import SUFFIX
from ..session import recorder as session
from ..transport import describe, gpib_ready, list_resources, scan_gpib
from . import touch
from .connect_dialog import ConnectDialog
from .dmm_panel import DmmPanel
from .fgen_panel import FgenPanel
from .gpio_panel import GpioPanel
from .hub import hub
from .logic_panel import LogicPanel
from .ppk2_panel import Ppk2Panel
from .session_viewer import open_viewer
from .supply_panel import SupplyPanel
from .theme import PLOT_BG, set_role
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
        pg.setConfigOptions(antialias=True, background=PLOT_BG, foreground="#c9ccd1")

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
            "so select it by model.\nA Nordic PPK2 appears as PPK2::… when plugged in; a Saleae "
            "analyzer as SALEAE::… while Logic 2 runs with its automation server on; "
            "sigrok analyzers as SIGROK::….\nTry the SIM::… entries to explore without "
            "hardware.")
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
        nt = QShortcut(QKeySequence("Ctrl+M"), self)
        nt.activated.connect(self.add_note)
        hub.tripped.connect(self._on_trip)
        self._load_bench_state()
        self._sess_timer = QTimer(self)
        self._sess_timer.setInterval(1000)
        self._sess_timer.timeout.connect(self._session_tick)

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
        elif getattr(self, "_bench_warning", ""):
            self.status.showMessage(self._bench_warning, 20000)
        if self.config.api:
            QTimer.singleShot(0, lambda: self.api_action.setChecked(True))
        if hub.math.channels:
            from .math_window import MathWindow
            self.math_win = MathWindow(self)     # computes (and records) while hidden
        self._err_box = None
        crash.set_notifier(self._internal_error)

    # ---------------------------------------------------------------- errors
    def _internal_error(self, summary: str, details: str):
        """An exception nobody caught (see crash.py): the app carries on, the
        user is told once, and the traceback is in the crash log."""
        from PyQt6.QtCore import QThread
        if QThread.currentThread() is not self.thread():
            return                       # logged already; Qt widgets only on the GUI thread
        self.status.showMessage(f"⚠ Internal error: {summary}  (details in {crash.log_path()})",
                                20000)
        if self._err_box is not None and self._err_box.isVisible():
            return                       # one box at a time
        box = QMessageBox(QMessageBox.Icon.Warning, "EE Workbench: internal error",
                          f"Something went wrong, but EE Workbench is still running and "
                          f"your instruments are as they were.\n\n{summary}\n\n"
                          f"The details are in {crash.log_path()}; please include that "
                          f"file if you report it.", parent=self)
        box.setDetailedText(details[-6000:])
        box.setModal(False)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._err_box = box
        box.show()

    # ---------------------------------------------------------------- bench state
    def _bench_file(self):
        from ..config import config_dir
        return config_dir() / "bench.json"

    def _load_bench_state(self):
        """Watchdog rules and math channels from last time. A damaged
        bench.json loses only the damaged entries, never the start-up."""
        import json
        try:
            d = json.loads(self._bench_file().read_text())
            if not isinstance(d, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            self._bench_warning = f"{self._bench_file()} is damaged ({e}); not loaded"
            crash.write_log(self._bench_warning)
            return
        from ..mathchannels import Channel
        bad = hub.watchdog.load(d.get("watchdog", []))
        hub.watchdog.armed = bool(d.get("watchdog_armed", True))
        hub.math.channels = Channel.load_list(d.get("math", []))
        if bad:
            self._bench_warning = "Skipped damaged watchdog rules: " + "; ".join(bad)
            crash.write_log(self._bench_warning)

    def save_bench_state(self):
        import json
        import os
        d = {"watchdog": hub.watchdog.to_list(), "watchdog_armed": hub.watchdog.armed,
             "math": [c.to_dict() for c in hub.math.channels]}
        from ..config import json_default
        p = self._bench_file()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=1, default=json_default))
            os.replace(tmp, p)
        except (OSError, TypeError, ValueError) as e:
            crash.write_log(f"Couldn't save {p}: {e}")

    # ---------------------------------------------------------------- bars
    def _build_touch_bar(self):
        w = QFrame()
        w.setObjectName("bar")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(6, 4, 6, 4)
        add = QPushButton("＋ Add instrument")
        add.clicked.connect(self.add_dialog)
        set_role(add, "primary")
        lay.addWidget(add)
        self._build_session_buttons(lay, True)
        lay.addStretch(1)
        off = QPushButton("ALL OUTPUTS OFF")
        off.setMinimumWidth(220)
        set_role(off, "danger-solid")
        off.setStyleSheet("QPushButton { font-size:16px; }")
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

    def _build_session_buttons(self, lay, wide: bool):
        self.sess_btn = QPushButton("⏺ Record")
        self.sess_btn.setCheckable(True)
        self.sess_btn.setToolTip("Record every open instrument, logic capture and note into one "
                                 f"session file ({SUFFIX}), to look through later on one time axis")
        self.sess_btn.toggled.connect(self.toggle_session)
        lay.addWidget(self.sess_btn)
        self.note_btn = QPushButton("✎ Note")
        self.note_btn.setToolTip("Put a note into the session at this moment (Ctrl+M)")
        self.note_btn.clicked.connect(self.add_note)
        self.note_btn.setEnabled(False)
        lay.addWidget(self.note_btn)
        tools = QPushButton("Tools ▾")
        menu = QMenu(tools)
        menu.addAction("Sequencer…  (steps or a script, with pass / fail limits)",
                       self.show_sequencer)
        menu.addAction("Watchdog…  (switch outputs off when a limit is crossed)",
                       self.show_watchdog)
        menu.addAction("Math channels…  (efficiency, power, energy, averages)", self.show_math)
        menu.addSeparator()
        menu.addAction("Setups…  (save / recall the whole bench by name)", self.show_setups)
        menu.addAction("Open session…  (view a recording, cursors, report)",
                       lambda: open_viewer(self))
        menu.addAction("Session report…  (HTML / PDF of a recording)", self.session_report)
        menu.addSeparator()
        self.api_action = menu.addAction("Remote API  (drive the bench from Python / Jupyter)")
        self.api_action.setCheckable(True)
        self.api_action.toggled.connect(self.toggle_api)
        tools.setMenu(menu)
        self.tools_btn = tools
        lay.addWidget(tools)
        if wide:
            for b in (self.sess_btn, self.note_btn, tools):
                b.setMinimumHeight(44)

    def toggle_session(self, on: bool):
        if on and not session.active:
            path, _ = QFileDialog.getSaveFileName(
                self, "Record session", time.strftime("session_%Y%m%d_%H%M%S") + SUFFIX,
                f"EE Workbench session (*{SUFFIX})")
            if not path:
                self.sess_btn.setChecked(False)
                return
            if not path.endswith(SUFFIX):
                path += SUFFIX
            try:
                session.start(path)
            except Exception as e:    # noqa: BLE001
                QMessageBox.warning(self, "Record session", f"Can't write {path}:\n{e}")
                self.sess_btn.setChecked(False)
                return
            self._sess_timer.start()
            self.sess_btn.setText("■ Stop session")
            set_role(self.sess_btn, "recording")
            self.note_btn.setEnabled(True)
            self.status.showMessage(f"Recording session to {path}", 5000)
        elif not on and session.active:
            path = session.path
            err = session.stop()
            self._sess_timer.stop()
            self.sess_btn.setText("⏺ Record")
            set_role(self.sess_btn, "")
            self.note_btn.setEnabled(False)
            if err:
                QMessageBox.warning(self, "Session", f"The last second of the session may "
                                    f"be missing from {path}:\n{err}")
            box = QMessageBox(QMessageBox.Icon.Information, "Session saved",
                              f"Saved {path}", parent=self)
            show = box.addButton("Open in viewer", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Close)
            box.exec()
            if box.clickedButton() is show:
                open_viewer(self, path)

    def _session_tick(self):
        try:
            session.flush()
        except Exception as e:    # noqa: BLE001 - a full disk etc.
            self.sess_btn.setChecked(False)
            QMessageBox.warning(self, "Session", f"Recording stopped: {e}")
            return
        mins, secs = divmod(int(now() - session.started), 60)
        self.sess_btn.setText(f"■ Stop  {mins}:{secs:02d}")

    def _show(self, attr, factory):
        w = getattr(self, attr, None)
        if w is None:
            w = factory()
            setattr(self, attr, w)
        w.show()
        w.raise_()
        w.activateWindow()
        return w

    def show_sequencer(self):
        from .sequencer_window import SequencerWindow
        return self._show("seq_win", lambda: SequencerWindow(self))

    def sequencer(self):
        """The sequencer window, created (hidden) if needed."""
        from .sequencer_window import SequencerWindow
        if getattr(self, "seq_win", None) is None:
            self.seq_win = SequencerWindow(self)
        return self.seq_win

    def show_watchdog(self):
        from .watchdog_window import WatchdogWindow
        return self._show("wd_win", lambda: WatchdogWindow(self))

    def show_math(self):
        from .math_window import MathWindow
        return self._show("math_win", lambda: MathWindow(self))

    def show_setups(self):
        from .setups_dialog import SetupsDialog
        SetupsDialog(self).exec()

    def session_report(self):
        from .report_dialog import report_for_session
        report_for_session(self)

    # ---------------------------------------------------------------- watchdog
    def _on_trip(self, rule, name):
        what = f"Watchdog: {name} {rule.quantity} = {rule.trip_value:.6g} " \
               f"({rule.op} {rule.limit:g})"
        if rule.action == "all outputs off":
            self.all_outputs_off()
            what += " → all outputs OFF"
        elif rule.action == "turn off":
            hits = [p for p in self.panels() if rule.target and
                    rule.target.lower() in p.display_name().lower()]
            for p in hits:
                self._off(p)
            what += f" → {', '.join(p.display_name() for p in hits) or 'nothing matched'} OFF"
        elif rule.action == "stop sequence":
            seq = getattr(self, "seq_win", None)
            if seq is not None:
                seq.stop()
            what += " → sequence stopped"
        if session.active:
            session.note(what)
        self.status.showMessage(what, 30000)
        wd = getattr(self, "wd_win", None)
        if wd is not None:
            wd.on_trip(rule, name, what)
        if rule.action != "note only":
            QMessageBox.warning(self, "Watchdog tripped", what + "\n\nRe-arm it in Tools ▾ → "
                                "Watchdog once the cause is fixed.")

    # ---------------------------------------------------------------- remote API
    def toggle_api(self, on: bool):
        from ..api import ApiServer
        srv = getattr(self, "api", None)
        if on and (srv is None or not srv.running):
            from ..sequencer import Bench
            from .sequencer_window import Invoker, PanelInst, kind_of
            from .panel_base import open_panels
            import threading
            self._api_invoke = Invoker()
            self._api_stop = threading.Event()
            # make_bench runs on the server's threads, which must never create
            # Qt widgets: make the (hidden) sequencer window now, on this one.
            seq = self.sequencer()

            def make_bench():
                stop = self._api_stop
                insts = [PanelInst(p, self._api_invoke, stop) for p in open_panels()
                         if kind_of(p)]
                b = Bench(insts, now, log=lambda s: None, on_row=seq.sig.row.emit,
                          on_note=seq.sig.note.emit, stop=stop)
                return b
            self.api = ApiServer(make_bench, port=self.config.api_port,
                                 on_call=lambda s: None)
            try:
                self.api.start()
            except OSError as e:
                QMessageBox.warning(self, "Remote API", f"Can't listen on port "
                                    f"{self.config.api_port}: {e}")
                self.api_action.setChecked(False)
                return
            self.status.showMessage(f"Remote API on at http://127.0.0.1:{self.api.port} "
                                    "(from Python: from ee_workbench.client import connect)",
                                    10000)
        elif not on and srv is not None:
            srv.stop()
            self.status.showMessage("Remote API off", 4000)

    def add_note(self):
        if not session.active:
            self.status.showMessage("Notes go into a session: start ⏺ Record first",
                                    5000)
            return
        t = now()
        text, ok = QInputDialog.getText(self, "Note", "Note at this moment:")
        if ok and text.strip():
            session.note(text.strip(), t)
            self.status.showMessage(f"Note added: {text.strip()}", 4000)

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
        self.res_combo.setMinimumWidth(220)
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
        set_role(self.connect_btn, "primary")
        lay.addWidget(self.connect_btn)
        lay.addStretch(1)
        self._build_session_buttons(lay, False)
        lay.addSpacing(12)
        off = QPushButton("All outputs OFF (Esc)")
        set_role(off, "danger")
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
        implied = model_for(text.strip())
        if implied != "auto":
            self.model_combo.setCurrentIndex(self.model_combo.findData(implied))

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
            try:
                self.open_instrument(e.resource, e.model, e.settings)
            except Exception:       # noqa: BLE001 - e.g. damaged settings in last-session.json
                crash.report(*sys.exc_info(), where=f"reopening {e.resource}")
                try:
                    self.open_instrument(e.resource, e.model)     # without its settings
                except Exception:   # noqa: BLE001
                    crash.report(*sys.exc_info(), where=f"reopening {e.resource}")
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
        model = model_for(resource, model)
        cls = {"3456A": DmmPanel, "PPK2": Ppk2Panel, "SALEAE": LogicPanel,
               "SIGROK": LogicPanel, "FGEN": FgenPanel, "GPIO": GpioPanel}.get(model, SupplyPanel)
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
        panel.link_lost.connect(lambda m, p=panel: self._lost(p, m))
        panel.link_ok.connect(lambda p=panel: self._recovered(p))
        panel.settings_changed.connect(self.schedule_save)
        self._update_empty()
        self.schedule_save()

    def _failed(self, panel, msg):
        self.status.showMessage(f"{panel.resource}: {msg}", 15000)
        i = self.tabs.indexOf(panel)
        if i >= 0:
            self.tabs.setTabText(i, f"⚠ {describe(panel.resource)}")
            self._retry_button(panel, "⟳ Retry",
                               "Try connecting again (e.g. after switching the instrument on)")

    def _retry_button(self, panel, text: str, tip: str):
        i = self.tabs.indexOf(panel)
        if i < 0:
            return
        retry = QToolButton()
        retry.setText(text)
        retry.setToolTip(tip)
        retry.clicked.connect(lambda _, p=panel: self.reconnect(p))
        self.tabs.tabBar().setTabButton(i, self.tabs.tabBar().ButtonPosition.LeftSide, retry)

    def _lost(self, panel, msg):
        """Readings stopped after an error: an instrument switched off, a USB
        cable pulled. Offer a one-click reconnect (close + reopen, settings kept)."""
        i = self.tabs.indexOf(panel)
        if i < 0:
            return
        self.status.showMessage(f"{panel.display_name()}: {msg}", 15000)
        text = self.tabs.tabText(i)
        if not text.startswith("⚠"):
            self.tabs.setTabText(i, "⚠ " + text)
        self._retry_button(panel, "⟳ Reconnect",
                           "Close and reopen this instrument (after replugging or switching "
                           "it back on). Its tab settings are kept.")

    def _recovered(self, panel):
        i = self.tabs.indexOf(panel)
        if i < 0:
            return
        self.tabs.tabBar().setTabButton(i, self.tabs.tabBar().ButtonPosition.LeftSide, None)
        self.tabs.setTabText(i, self.tabs.tabText(i).removeprefix("⚠ "))

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
        try:
            p.shutdown()
        except Exception:       # noqa: BLE001 - the tab goes anyway
            crash.report(*sys.exc_info(), where=f"closing {p.resource}")
        self.tabs.removeTab(i)
        p.deleteLater()
        self._update_empty()
        self.schedule_save()

    def _update_empty(self):
        has = self.tabs.count() > 0
        self.tabs.setVisible(has)
        self.empty.setVisible(not has)

    def all_outputs_off(self):
        seq = getattr(self, "seq_win", None)
        if seq is not None and seq.running():
            seq.stop()
        stop = getattr(self, "_api_stop", None)
        if stop is not None:            # end any remote-API call in progress
            stop.set()
            import threading
            self._api_stop = threading.Event()
        n, bad = 0, []
        for p in self.panels():
            if p.KIND in ("supply", "ppk2", "fgen", "gpio") and p.connected:
                if self._off(p):
                    n += 1
                else:
                    bad.append(p.display_name())
        msg = f"Output OFF sent to {n} instrument(s)"
        if bad:
            msg += f"  ·  ⚠ FAILED for {', '.join(bad)}: check them by hand"
        self.status.showMessage(msg, 30000 if bad else 5000)

    def _off(self, panel) -> bool:
        """panel.output_off(), never raising: Esc and the watchdog go through
        every tab even if one of them has a problem."""
        try:
            panel.output_off()
            return True
        except Exception:       # noqa: BLE001
            crash.report(*sys.exc_info(), where=f"output off: {panel.resource}")
            return False

    def closeEvent(self, e):
        """Quit: save what's worth keeping and stop every thread. Each step
        runs even if an earlier one fails, so instruments are always released."""
        crash.set_notifier(None)        # no dialogs while closing; still logged

        def step(fn, what):
            try:
                fn()
            except Exception:           # noqa: BLE001
                crash.report(*sys.exc_info(), where=f"quit: {what}")

        seq = getattr(self, "seq_win", None)
        if seq is not None:
            step(seq.stop, "stop sequence")
            step(seq.close, "close sequencer")
        step(session.stop, "close session file")
        if getattr(self, "api", None) is not None:
            step(self.api.stop, "stop remote API")
        self._save_timer.stop()
        step(self.save_session, "save last-session.json")
        step(self.save_bench_state, "save bench.json")
        for p in self.panels():
            step(p.shutdown, f"close {p.resource}")
        super().closeEvent(e)


def parse_target(s: str) -> tuple[str, str]:
    """'GPIB0::22::INSTR' or '3456A@GPIB0::22::INSTR' -> (model, resource)."""
    if s.upper().startswith("SALEAE::"):
        return "SALEAE", s                 # its own '@host:port' is not MODEL@
    if s.upper().startswith("SIGROK::"):
        return "SIGROK", s
    if s.upper().startswith("GPIO::"):
        return "GPIO", s
    if "@" in s:
        model, res = s.split("@", 1)
        model = {"66321D": "663xx", "66321B": "663xx", "66311B": "663xx",
                 "6633A": "6632A", "6634A": "6632A", "ppk2": "PPK2", "fgen": "FGEN",
                 "gpio": "GPIO"}.get(model, model)
        if model == "PPK2" and not res.upper().startswith(("PPK2::", "SIM")):
            res = "PPK2::" + res           # PPK2@/dev/ttyACM0
        if model.upper() == "SALEAE":
            model = "SALEAE"
            if not res.upper().startswith(("SALEAE::", "SIM")):
                res = "SALEAE::" + res     # SALEAE@F4241 (SALEAE@ = the first one)
        if model.upper() == "SIGROK":
            model = "SIGROK"
            if not res.upper().startswith(("SIGROK::", "SIM")):
                res = "SIGROK::" + (res or "fx2lafw")   # SIGROK@fx2lafw
        return model, res
    return "auto", s


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="EE Workbench instrument control")
    ap.add_argument("targets", nargs="*",
                    help="resources to open, optionally MODEL@RESOURCE, e.g. "
                         "GPIB0::5::INSTR 3456A@GPIB0::22::INSTR PPK2@/dev/ttyACM0 SALEAE@ "
                         "FGEN@TCPIP::192.168.1.50::INSTR GPIO:: SIM::6632A")
    ap.add_argument("--api", action="store_true",
                    help="start the remote API (Python / Jupyter control) at start-up")
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
    crash.install()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("EE Workbench")
    dark_palette(app)
    if touch.detect(app, args.touch):
        app.setStyleSheet(app.styleSheet() + touch.TOUCH_STYLE)
    from pathlib import Path
    cfg = load_config(Path(args.config).expanduser() if args.config else None)
    if cfg.warning:
        print("ee-workbench:", cfg.warning, file=sys.stderr)
    if args.api:
        cfg.api = True
    win = MainWindow([parse_target(t) for t in args.targets], cfg, Session(), args.open)
    crash.install_signal_handlers(app, win.close)
    if args.hide_cursor:
        app.setOverrideCursor(Qt.CursorShape.BlankCursor)
    if args.fullscreen:
        win.go_fullscreen()
    elif touch.TOUCH:
        win.showMaximized()
    else:
        win.show()
    code = app.exec()
    from ..worker import wait_all
    if not wait_all(5.0):
        # An instrument thread is still stuck in a driver call. Destroying it
        # would abort with a core dump; everything worth saving is saved.
        crash.write_log("Exiting with an instrument I/O thread still busy")
        sys.stdout.flush()
        sys.stderr.flush()
        import os
        os._exit(code)
    return code
