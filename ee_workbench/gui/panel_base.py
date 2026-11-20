"""Behaviour shared by every instrument tab: worker wiring, console, trend
toolbar, CSV recording and the readings-per-second counter."""
from __future__ import annotations

import time

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit,
                             QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from ..worker import InstrumentWorker
from . import touch
from .widgets import C_WARN, MONO, CsvRecorder

SPANS = {"30 s": 30, "2 min": 120, "10 min": 600, "1 h": 3600, "All": None}


class PanelBase(QWidget):
    title_changed = pyqtSignal(str)
    settings_changed = pyqtSignal()    # something worth remembering for next start
    message = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, resource: str, model: str, baud: int):
        super().__init__()
        self.resource = resource
        self.model = model
        self.connected = False
        self.caps = None
        self.recorder = CsvRecorder()
        self._stamps: list[float] = []
        self._dirty = False

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
        self.plot_timer.start(100)

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
    def csv_name(self) -> str: return "reading_%Y%m%d_%H%M%S.csv"

    # ---- worker callbacks ----------------------------------------------------
    def _on_connected(self, family, caps, state):
        self.connected = True
        self.caps = caps
        self.set_enabled(True)
        self.on_connected(family, caps, state)
        self.on_state(state)

    def _on_connect_failed(self, msg):
        self.failed.emit(msg)
        self.console("⚠ Connect failed: " + msg)

    def _on_reading(self, r):
        now = time.monotonic()
        self._stamps.append(now)
        while self._stamps and now - self._stamps[0] > 5:
            self._stamps.pop(0)
        self.on_reading(r)
        self._dirty = True

    def _on_poll_paused(self):
        self.on_error("Polling paused after an error. Press Resume to retry.")
        self.pause_btn.setChecked(True)

    def readings_per_s(self) -> float:
        if len(self._stamps) < 2:
            return 0.0
        return (len(self._stamps) - 1) / max(1e-6, self._stamps[-1] - self._stamps[0])

    def _maybe_redraw(self):
        if self._dirty and not self.freeze_chk.isChecked():
            self._dirty = False
            self.redraw()
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
        self.span_combo.setCurrentIndex(1)
        self.span_combo.currentIndexChanged.connect(lambda *_: setattr(self, "_dirty", True))
        bar.addWidget(self.span_combo)
        self.freeze_chk = QCheckBox("Freeze")
        bar.addWidget(self.freeze_chk)
        clr = QPushButton("Clear")
        clr.clicked.connect(self.clear_trend)
        bar.addWidget(clr)
        self.trend_stats = QLabel("")
        self.trend_stats.setFont(QFont(MONO, 9))
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
        bar.addStretch(1)
        bar.addWidget(self.trend_stats)
        bar.addStretch(1)
        bar.addWidget(self.rate_label)
        bar.addWidget(self.rec_btn)
        return bar

    def span(self):
        return SPANS[self.span_combo.currentText()]

    def clear_trend(self):
        self._dirty = True

    def _toggle_record(self, on: bool):
        if on:
            if not self.recorder.start(self, self.csv_name(), self.csv_header()):
                self.rec_btn.setChecked(False)
                return
            self.rec_btn.setText("■ Stop recording")
            self.rec_btn.setStyleSheet(f"QPushButton {{ color:{C_WARN}; font-weight:bold; }}")
            self.message.emit(f"Recording to {self.recorder.path}")
        else:
            self.recorder.stop()
            self.rec_btn.setText("● Record CSV")
            self.rec_btn.setStyleSheet("")
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

    def shutdown(self):
        self.recorder.stop()
        self.worker.stop()
