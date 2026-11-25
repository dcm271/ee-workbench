"""Behaviour shared by every instrument tab: worker wiring, console, trend
toolbar, CSV recording and the readings-per-second counter."""
from __future__ import annotations

import re
import time

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit,
                             QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout,
                             QWidget)

from ..worker import InstrumentWorker
from ..worker import now as app_time
from . import touch
from ..session import recorder as session
from .hub import hub
from .theme import set_role
from .widgets import MONO, CsvRecorder, SteadyY

SPANS = {"10 s": 10, "30 s": 30, "2 min": 120, "10 min": 600, "1 h": 3600, "All": None}
_OPEN: list = []          # every instrument tab, so tools can find each other


def open_panels() -> list:
    """The instrument tabs currently open (the sequencer, the session recorder
    and the logic analyzer's PPK2 alignment use this)."""
    return list(_OPEN)


class PanelBase(QWidget):
    KIND = ""                          # supply | ppk2 | dmm | logic | gpio | fgen | scpi | scope | uart
    title_changed = pyqtSignal(str)
    settings_changed = pyqtSignal()    # something worth remembering for next start
    message = pyqtSignal(str)
    failed = pyqtSignal(str)
    link_lost = pyqtSignal(str)        # polling stopped after an error (unplugged?)
    link_ok = pyqtSignal()             # readings arrive again after `link_lost`
    reopen_as = pyqtSignal(str)        # auto-detect found another kind of instrument: model key

    def __init__(self, resource: str, model: str, baud: int):
        super().__init__()
        self.resource = resource
        self.model = model
        self.connected = False
        self.caps = None
        self.recorder = CsvRecorder()
        self.last_reading = None
        self.readings = 0
        self._stamps: list[float] = []
        self._dirty = False
        self._redrawn = 0.0
        # Scrolling trend: data time of the newest reading, the app time it
        # arrived, the smoothed delay between the two, and the right edge shown.
        self._t_last: float | None = None
        self._t_seen = 0.0
        self._lag: float | None = None
        self._edge: float | None = None
        self._x_plot = None                   # the plot whose X range scrolls (others link to it)
        self._steady: list[SteadyY] = []
        self._pm_t = 0.0
        self._lost = False
        # Several of one model can be open: the first is plain "PPK2", then
        # "PPK2 #2", … The number is kept with the tab's settings, so rules,
        # steps and math that name "PPK2 #2" find the same unit next time.
        self.instance = self._free_instance()

        _OPEN.append(self)
        self.worker = InstrumentWorker(resource, model, baud)
        self.worker.connected.connect(self._on_connected)
        self.worker.connect_failed.connect(self._on_connect_failed)
        self.worker.reading.connect(self._on_reading)
        self.worker.state.connect(self.on_state)
        self.worker.log.connect(self.console)
        self.worker.error.connect(self.on_error)
        self.worker.reply.connect(lambda c, r: self.console(f"{c}  →  {r}"))
        self.worker.poll_paused.connect(self._on_poll_paused)

        self.plot_timer = QTimer(self)
        self.plot_timer.timeout.connect(self._maybe_redraw)
        # Small steps make the scroll look smooth; the Pi gets fewer of them.
        self.plot_timer.start(100 if touch.TOUCH else 40)

    def start(self):
        self.worker.start()

    # ---- to override ------------------------------------------------------
    def on_connected(self, family, caps, state): ...
    def on_state(self, state): ...
    def on_reading(self, r): ...
    def redraw(self): ...
    def set_enabled(self, on: bool): ...
    def csv_header(self) -> list[str]: return []

    def session_settings(self) -> dict:
        """Panel settings to restore next time (never supply setpoints: those
        are always read from the instrument)."""
        return {}

    def restore_settings(self, settings: dict): ...

    # ---- what the sequencer, watchdog and math channels can use ------------------
    # Fixed per kind for the built-in drivers; a generic SCPI tab or a scope
    # tab works them out from its definition / channel count.
    def quantities(self) -> list[str]:
        from ..signals import QUANTITIES
        return list(QUANTITIES.get(self.KIND, []))

    def set_names(self) -> list[str]:
        from ..sequencer import SET_WHAT
        return list(SET_WHAT.get(self.KIND, []))

    def setting_kinds(self) -> dict:
        """name -> "choice" for settings whose value is text, not a number."""
        return {"function": "choice"} if self.KIND == "fgen" else {}

    def measure_names(self) -> list[str]:
        from ..sequencer import MEASURE_WHAT
        return list(MEASURE_WHAT.get(self.KIND, []))

    def profile_names(self) -> list[str]:
        from ..sequencer import PROFILE_WHAT
        return list(PROFILE_WHAT.get(self.KIND, []))

    def identity(self) -> str:
        """What the instrument said it is (reports, the calibration log)."""
        return getattr(self.caps, "idn", "") or self.resource

    def calibration(self) -> str:
        """Its calibration status on this day ("" when no log is kept)."""
        from ..calibration import note_for
        try:
            return note_for(self.identity(), self.resource)
        except Exception as e:      # noqa: BLE001 - a report must not fail over this
            return f"calibration log unreadable: {e}"

    def accuracy_keys(self, what: str, r=None) -> list[str]:
        """The names a specification of this reading can go by, best first."""
        return [what]

    def builtin_accuracy(self) -> dict:
        return {}

    def pm_due(self) -> bool:
        """The ± on the tab: a few times a second is enough."""
        t = time.monotonic()
        if t - self._pm_t < 0.25:
            return False
        self._pm_t = t
        return True

    def uncertainty(self, what: str, value, r=None) -> float | None:
        """± of a reading; None when no specification was given (accuracy.py)."""
        from .. import accuracy
        try:
            keys = self.accuracy_keys(what, r)
            spec = accuracy.pick(accuracy.from_log(self.identity(), self.resource), keys) or \
                accuracy.pick(self.builtin_accuracy(), keys)
            return None if spec is None else spec.of(value)
        except Exception:       # noqa: BLE001 - a damaged log must not stop the readings
            return None

    # ---- several of one model -----------------------------------------------------
    def _free_instance(self, want: int | None = None) -> int:
        used = {p.instance for p in _OPEN if p is not self and p.model == self.model}
        if want and want not in used:
            return want
        n = 1
        while n in used:
            n += 1
        return n

    def claim_instance(self, want):
        """Take the instance number remembered for this tab, if it is free."""
        try:
            want = int(want)
        except (TypeError, ValueError):
            return
        if want >= 1:
            self.instance = self._free_instance(want)

    def saved_settings(self) -> dict:
        """session_settings() plus what every tab keeps (its instance number)."""
        d = dict(self.session_settings())
        if self.instance > 1:
            d["instance"] = self.instance
        return d

    def output_off(self):
        """Esc / ALL OUTPUTS OFF (tabs with outputs override this)."""
    def csv_name(self) -> str: return "reading_%Y%m%d_%H%M%S.csv"

    # ---- worker callbacks ----------------------------------------------------
    def _on_connected(self, family, caps, state):
        # Opened as "Auto" but it turned out to be a meter / load / scope: the
        # main window reopens it with the right kind of tab.
        key = getattr(caps, "model_key", "")
        if key and self.model == "auto" and self.KIND == "supply":
            self.reopen_as.emit(key)
            return
        self.connected = True
        self.caps = caps
        self.set_enabled(True)
        self.on_connected(family, caps, state)
        self.on_state(state)

    def _on_connect_failed(self, msg):
        self.failed.emit(msg)
        self.console("⚠ Connect failed: " + msg)

    def _on_reading(self, r):
        if self._lost:
            self._lost = False
            self.link_ok.emit()
        now = time.monotonic()
        self._stamps.append(now)
        while self._stamps and now - self._stamps[0] > 5:
            self._stamps.pop(0)
        self.on_reading(r)
        self.last_reading, self.readings = r, self.readings + 1   # the sequencer reads these
        hub.feed(self, r)                  # watchdog rules, math channels
        t = getattr(r, "t", None)
        if t is not None:
            self._t_last, self._t_seen = t, app_time()
            lag = self._t_seen - t
            self._lag = lag if self._lag is None else self._lag + 0.1 * (lag - self._lag)
        self._dirty = True

    def _on_poll_paused(self):
        self._lost = True
        btn = getattr(self, "pause_btn", None)
        if btn is not None:
            self.on_error("Polling paused after an error. Press Resume to retry, or "
                          "⟳ Reconnect on the tab.")
            btn.setChecked(True)
        else:
            # tabs without a Pause button (GPIO, generators): retry on their own
            self.on_error("Reading failed (unplugged?). Retrying every 5 s; ⟳ Reconnect "
                          "on the tab opens it again.")
            QTimer.singleShot(5000, self._auto_resume)
        self.link_lost.emit("Polling stopped after an error")

    def _auto_resume(self):
        if self._lost and self.connected and self in _OPEN:
            self.worker.poll_enabled = True

    def readings_per_s(self) -> float:
        if len(self._stamps) < 2:
            return 0.0
        return (len(self._stamps) - 1) / max(1e-6, self._stamps[-1] - self._stamps[0])

    def _maybe_redraw(self):
        if not self.freeze_chk.isChecked():
            m = time.monotonic()
            if self._dirty and m - self._redrawn >= 0.09:   # new data: ≤ ~10 redraws/s
                self._dirty = False
                self._redrawn = m
                self.redraw()
            self._scroll()
        self.rate_label.setText(f"{self.readings_per_s():.1f} readings/s" if self.connected else "")

    # ---- shared UI pieces ------------------------------------------------------
    def console(self, text: str):
        self.log_view.appendPlainText(time.strftime("%H:%M:%S  ") + text)

    def on_error(self, text: str):
        self.console("⚠ " + text)
        self.message.emit(text)

    def cmd(self, fn, *args):
        if self.connected:
            self.worker.submit(fn, *args, refresh=True)

    def build_pause_button(self) -> QPushButton:
        self.pause_btn = QPushButton("Pause polling")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._pause_toggled)
        return self.pause_btn

    def _pause_toggled(self, paused: bool):
        self.worker.poll_enabled = not paused
        self.pause_btn.setText("Resume polling" if paused else "Pause polling")

    def build_trend_bar(self):
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Span"))
        self.span_combo = QComboBox()
        self.span_combo.addItems(list(SPANS))
        self.span_combo.setCurrentText("2 min")
        self.span_combo.currentIndexChanged.connect(lambda *_: setattr(self, "_dirty", True))
        bar.addWidget(self.span_combo)
        self.freeze_chk = QCheckBox("Freeze")
        bar.addWidget(self.freeze_chk)
        clr = QPushButton("Clear")
        clr.clicked.connect(self.clear_trend)
        bar.addWidget(clr)
        self.trend_stats = QLabel("")
        self.trend_stats.setFont(QFont(MONO, 9))
        # Long stats must never widen the window: take the free space, wrap if needed.
        self.trend_stats.setWordWrap(True)
        self.trend_stats.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.rate_label = QLabel("")
        self.rate_label.setStyleSheet("color:#8a8f98;")
        self.rec_btn = QPushButton("● Record CSV")
        self.rec_btn.setCheckable(True)
        self.rec_btn.toggled.connect(self._toggle_record)
        if touch.TOUCH:
            # Narrow screen: controls on one row, the stats line underneath.
            bar.addStretch(1)
            bar.addWidget(self.rec_btn)
            stats = QHBoxLayout()
            stats.addWidget(self.trend_stats, 1)
            stats.addWidget(self.rate_label)
            col = QVBoxLayout()
            col.setSpacing(2)
            col.addLayout(bar)
            col.addLayout(stats)
            return col
        bar.addWidget(self.trend_stats, 1)
        bar.addWidget(self.rate_label)
        bar.addWidget(self.rec_btn)
        return bar

    def span(self):
        return SPANS[self.span_combo.currentText()]

    def scrolling_trend(self, x_plot, *plots):
        """Let the base class move the trend's X window and steady the Y
        autoscale.  `x_plot` sets X for any plots X-linked to it; every plot in
        `plots` gets a SteadyY."""
        self._x_plot = x_plot
        self._steady = [SteadyY(p) for p in plots]

    def _view_edge(self) -> float | None:
        """Right edge of the trend window.  It follows the clock, less the
        usual delay between a reading being taken and arriving here, so the chart
        glides instead of stepping once per reading.  It runs at most two
        reading intervals past the newest reading, so it stops soon after the
        readings do, and it never moves backwards."""
        if self._t_last is None:
            return None
        rate = self.readings_per_s()
        limit = self._t_last + (2 / rate if rate > 0 else 0.0)
        edge = min(app_time() - self._lag, limit)
        if self._edge is not None and self._edge > edge and self._edge - edge < 5:
            edge = self._edge                     # hold rather than step back
        self._edge = edge
        return edge

    def _scroll(self):
        if self._x_plot is None:
            return
        span = self.span()
        edge = self._view_edge()
        if span and edge is not None:
            self._x_plot.setXRange(max(0.0, edge - span), max(span, edge), padding=0)
        elif not span:
            self._x_plot.enableAutoRange(axis="x")
        for s in self._steady:
            s.update()

    def reset_steady(self):
        for s in self._steady:
            s.reset()

    def clear_trend(self):
        self._edge = None
        self._lag = None
        self.reset_steady()
        self._dirty = True

    def _toggle_record(self, on: bool):
        if on:
            if not self.recorder.start(self, self.csv_name(), self.csv_header()):
                self.rec_btn.setChecked(False)
                return
            self.rec_btn.setText("■ Stop recording")
            set_role(self.rec_btn, "recording")
            self.message.emit(f"Recording to {self.recorder.path}")
        else:
            self.recorder.stop()
            self.rec_btn.setText("● Record CSV")
            set_role(self.rec_btn, "")
            self.message.emit("Recording stopped")

    def build_console_tab(self, placeholder: str, read_button: bool = False) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont(MONO, 9))
        self.log_view.setMaximumBlockCount(5000)
        v.addWidget(self.log_view, 1)
        row = QHBoxLayout()
        self.raw_line = QLineEdit()
        self.raw_line.setPlaceholderText(placeholder)
        self.raw_line.setFont(QFont(MONO, 10))
        self.raw_line.returnPressed.connect(lambda: self._send_raw(None))
        row.addWidget(self.raw_line, 1)
        send = QPushButton("Send")
        send.clicked.connect(lambda: self._send_raw(False))
        row.addWidget(send)
        if read_button:
            sr = QPushButton("Send + read")
            sr.clicked.connect(lambda: self._send_raw(True))
            row.addWidget(sr)
        self.trace_chk = QCheckBox("Trace GUI commands")
        self.trace_chk.toggled.connect(self.worker.set_trace)
        row.addWidget(self.trace_chk)
        v.addLayout(row)
        return w

    def _send_raw(self, read):
        cmd = self.raw_line.text().strip()
        if not cmd or not self.connected:
            return
        if read is None:  # Enter key: read back if it looks like a query
            read = cmd.endswith("?")
        self.worker.request_raw(cmd, read)
        self.raw_line.clear()

    @property
    def recording(self) -> bool:
        """Is anything (CSV or a session file) taking readings?"""
        return self.recorder.active or session.active

    def record(self, row: list):
        """One reading, laid out like csv_header(): to the CSV file and the session."""
        if not self.recorder.row(row) and self.recorder.error:
            err, self.recorder.error = self.recorder.error, ""
            btn = getattr(self, "rec_btn", None)
            if btn is not None:
                btn.setChecked(False)               # resets the button
            self.on_error(f"CSV recording stopped: {err}")
        if session.active:
            hdr = self.csv_header()
            skip = ("timestamp", "t_s")
            session.stream(self, self.display_name(), self.model, self.resource,
                           [c for c in hdr if c not in skip], self.calibration)
            session.add(self, float(row[hdr.index("t_s")]),
                        [v for c, v in zip(hdr, row) if c not in skip])

    def base_name(self) -> str:
        """The tab's name before any instance number (subclasses override)."""
        text = self.user_name()
        model = getattr(self.caps, "model", "") or self.model
        return f"{model} · {text}" if text else f"{model} ({self.resource})"

    def user_name(self) -> str:
        name = getattr(self, "name_edit", None)
        return name.text().strip() if name is not None else ""

    def display_name(self) -> str:
        """How the tab is known in the sequencer, watchdog, math and session
        files. A second unit of a model gets " #2" unless the user named it
        (and the name isn't the same as another tab's)."""
        name = self.base_name()
        if self.instance <= 1:
            return name
        if self.user_name():
            from ..signals import slug
            mine = slug(name)
            if not any(p is not self and slug(p.base_name()) == mine for p in _OPEN):
                return name
        m = re.match(r"^(.*?)(\s*\([^()]*\))?$", name)
        return f"{m.group(1)} #{self.instance}{m.group(2) or ''}"

    def shutdown(self):
        """Closing the tab: stop everything that runs on its behalf. Each part
        is independent, so one failing can't leave the I/O thread running."""
        if self in _OPEN:
            _OPEN.remove(self)
        self.plot_timer.stop()
        for part in ("profile_box", "tests_tab"):
            obj = getattr(self, part, None)
            fn = getattr(obj, "stop" if part == "profile_box" else "abort", None)
            if fn is not None:
                try:
                    fn()
                except Exception:        # noqa: BLE001
                    pass
        try:
            self.recorder.stop()
        except Exception:                # noqa: BLE001
            pass
        self.worker.stop()
