"""Tab for an oscilloscope: channels, timebase and trigger; Run / Stop /
Single; the waveforms it captures with automatic measurements; and every
capture saved into the session (like a logic-analyzer capture)."""
from __future__ import annotations

import time

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                             QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                             QVBoxLayout, QWidget)

from ..clock import now
from ..instruments.logic import LogicCapture
from ..instruments.scopes import SLOPES, SWEEPS, ScopeReading, ScopeState, trace_stats
from ..session import recorder as session
from . import touch
from .panel_base import PanelBase
from .theme import set_role
from .widgets import C_MUTED, MONO, fmt_si, io_errors

CH_COLORS = ["#fff176", "#4dd0e1", "#f06292", "#64b5f6"]      # the usual scope colours
VDIV = [1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2, 0.1, 0.2, 0.5, 1, 2, 5, 10]
TDIV = [5e-9, 1e-8, 2e-8, 5e-8, 1e-7, 2e-7, 5e-7, 1e-6, 2e-6, 5e-6, 1e-5, 2e-5, 5e-5, 1e-4,
        2e-4, 5e-4, 1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2, 0.1, 0.2, 0.5, 1, 2, 5, 10]
MEAS = [("vpp", "Vpp", "V"), ("vmax", "Max", "V"), ("vmin", "Min", "V"), ("vavg", "Mean", "V"),
        ("vrms", "RMS", "V"), ("freq", "Freq", "Hz"), ("period", "Period", "s"),
        ("duty", "Duty", "%"), ("rise", "Rise", "s"), ("fall", "Fall", "s")]


def _nearest(v, table):
    return min(range(len(table)), key=lambda i: abs(table[i] - v) / table[i])


class ScopePanel(PanelBase):
    KIND = "scope"

    def __init__(self, resource: str, model: str, baud: int):
        super().__init__(resource, model, baud)
        self.state = ScopeState()
        self.capture: LogicCapture | None = None
        self._busy = ""
        self._busy_since = 0.0
        self._pending: dict | None = None
        self._ch_widgets: list = []
        self.worker.interval_s = 0.5
        self.worker.capture_status.connect(self._on_capture_status)
        self.worker.capture_done.connect(self._on_capture)
        self.worker.job_result.connect(self._on_job)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        console = self.build_console_tab("SCPI command, e.g.  :TIM:SCAL?   (Enter on a "
                                         "query reads the answer)", read_button=True)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        self.idn_label = QLabel(f"Connecting to {self.resource}…")
        self.idn_label.setWordWrap(True)
        self.idn_label.setStyleSheet(f"color:{C_MUTED};")
        lay.addWidget(self.idn_label)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Tab name, e.g. “DUT scope”")
        self.name_edit.textChanged.connect(self._name_changed)
        lay.addWidget(self.name_edit)
        lay.addWidget(self._build_acq())
        self.ch_box = QGroupBox("Channels")
        self.ch_grid = QGridLayout(self.ch_box)
        lay.addWidget(self.ch_box)
        lay.addWidget(self._build_time_trig())
        lay.addStretch(1)
        sa = touch.scroll(inner)
        sa.setMinimumWidth(420 if touch.TOUCH else 380)
        split.addWidget(sa)
        tabs = QTabWidget()
        tabs.addTab(self._build_wave_tab(), "Waveforms")
        tabs.addTab(console, "Console")
        split.addWidget(tabs)
        split.setStretchFactor(1, 1)
        split.setSizes([460, 820] if touch.TOUCH else [420, 1000])
        root.addWidget(split)
        self.busy_timer = QTimer(self)
        self.busy_timer.timeout.connect(self._show_busy)
        self.set_enabled(False)
        self.start()

    # ---- building ------------------------------------------------------------------
    def _build_acq(self):
        g = QGroupBox("Acquisition")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        self.run_btn = QPushButton("▶ Run")
        self.run_btn.clicked.connect(lambda: self.cmd(lambda d: d.run()))
        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.clicked.connect(lambda: self.cmd(lambda d: d.stop()))
        self.single_btn = QPushButton("Single → capture")
        self.single_btn.setToolTip("Arm one acquisition; when the scope triggers, the "
                                   "waveforms are read and kept (and saved in the session)")
        self.single_btn.clicked.connect(self.capture_now)
        set_role(self.single_btn, "primary")
        self.grab_btn = QPushButton("Grab screen")
        self.grab_btn.setToolTip("Read the waveforms on screen now, without re-arming")
        self.grab_btn.clicked.connect(self.grab_now)
        for b in (self.run_btn, self.stop_btn, self.single_btn, self.grab_btn):
            b.setMinimumHeight(40 if touch.TOUCH else 28)
        row.addWidget(self.run_btn)
        row.addWidget(self.stop_btn)
        v.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(self.single_btn)
        row2.addWidget(self.grab_btn)
        v.addLayout(row2)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"color:{C_MUTED};")
        self.status_label.setWordWrap(True)
        v.addWidget(self.status_label)
        return g

    def _fill_channels(self, n: int):
        while self.ch_grid.count():
            w = self.ch_grid.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self._ch_widgets = []
        for col, text in enumerate(("", "V/div", "Offset")):
            if text:
                lab = QLabel(text)
                lab.setStyleSheet(f"color:{C_MUTED};")
                self.ch_grid.addWidget(lab, 0, col)
        for i in range(n):
            ch = i + 1
            on = QCheckBox(f"CH{ch}")
            on.setStyleSheet(f"color:{CH_COLORS[i % 4]}; font-weight:bold;")
            on.clicked.connect(lambda v, c=ch: self.cmd(lambda d, c, v: d.set_channel(c, on=v),
                                                        c, v))
            scale = QComboBox()
            for x in VDIV:
                scale.addItem(fmt_si(x, "V", 3), x)
            scale.activated.connect(lambda _i, c=ch, w=scale: self.cmd(
                lambda d, c, v: d.set_channel(c, scale=v), c, w.currentData()))
            off = QDoubleSpinBox()
            off.setRange(-400, 400)
            off.setDecimals(3)
            off.setSuffix(" V")
            off.setKeyboardTracking(False)
            off.editingFinished.connect(lambda c=ch, w=off: self.cmd(
                lambda d, c, v: d.set_channel(c, offset=v), c, w.value()))
            touch.attach_keypad(off, f"CH{ch} offset", lambda c=ch, w=off: self.cmd(
                lambda d, c, v: d.set_channel(c, offset=v), c, w.value()))
            for col, w in enumerate((on, scale, off)):
                self.ch_grid.addWidget(w, ch, col)
            self._ch_widgets.append((on, scale, off))
        self.ch_grid.setColumnStretch(1, 1)
        self.ch_grid.setColumnStretch(2, 1)

    def _build_time_trig(self):
        g = QGroupBox("Timebase and trigger")
        form = QFormLayout(g)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.tdiv = QComboBox()
        for x in TDIV:
            self.tdiv.addItem(fmt_si(x, "s", 3) + "/div", x)
        self.tdiv.activated.connect(lambda _i: self.cmd(
            lambda d, v: d.set_timebase(scale=v), self.tdiv.currentData()))
        form.addRow("Time/div", self.tdiv)
        self.tpos = QDoubleSpinBox()
        self.tpos.setRange(-1000, 1000)
        self.tpos.setDecimals(6)
        self.tpos.setSuffix(" s")
        self.tpos.setKeyboardTracking(False)
        self.tpos.editingFinished.connect(lambda: self.cmd(
            lambda d, v: d.set_timebase(pos=v), self.tpos.value()))
        touch.attach_keypad(self.tpos, "Trigger position (s)")
        form.addRow("Position", self.tpos)
        self.tsrc = QComboBox()
        self.tsrc.activated.connect(lambda _i: self.cmd(
            lambda d, v: d.set_trigger(source=v), self.tsrc.currentData()))
        form.addRow("Trigger on", self.tsrc)
        self.tlev = QDoubleSpinBox()
        self.tlev.setRange(-400, 400)
        self.tlev.setDecimals(3)
        self.tlev.setSuffix(" V")
        self.tlev.setKeyboardTracking(False)
        self.tlev.editingFinished.connect(lambda: self.cmd(
            lambda d, v: d.set_trigger(level=v), self.tlev.value()))
        touch.attach_keypad(self.tlev, "Trigger level (V)")
        form.addRow("Level", self.tlev)
        self.tslope = QComboBox()
        self.tslope.addItems(SLOPES)
        self.tslope.activated.connect(lambda _i: self.cmd(
            lambda d, v: d.set_trigger(slope=v), self.tslope.currentText()))
        form.addRow("Slope", self.tslope)
        self.tsweep = QComboBox()
        self.tsweep.addItems([s for s in SWEEPS if s != "single"])
        self.tsweep.activated.connect(lambda _i: self.cmd(
            lambda d, v: d.set_trigger(sweep=v), self.tsweep.currentText()))
        form.addRow("Sweep", self.tsweep)
        return g

    def _build_wave_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        bar = QHBoxLayout()
        self.cap_label = QLabel("No capture yet: press Single or Grab screen")
        self.cap_label.setStyleSheet(f"color:{C_MUTED};")
        self.cap_label.setWordWrap(True)
        bar.addWidget(self.cap_label, 1)
        self.save_btn = QPushButton("Save CSV…")
        self.save_btn.clicked.connect(self.save_csv)
        bar.addWidget(self.save_btn)
        v.addLayout(bar)
        split = QSplitter(Qt.Orientation.Vertical)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("bottom", "Time from trigger", units="s")
        self.plot.setLabel("left", "Voltage", units="V")
        self.plot.addLegend(offset=(-10, 10))
        touch.plots(self.plot)
        split.addWidget(self.plot)
        self.meas = QTableWidget(0, len(MEAS) + 1)
        self.meas.setHorizontalHeaderLabels(["Ch"] + [m[1] for m in MEAS])
        self.meas.verticalHeader().setVisible(False)
        self.meas.setFont(QFont(MONO, 9))
        self.meas.horizontalHeader().setStretchLastSection(True)
        self.meas.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        split.addWidget(self.meas)
        split.setSizes([520, 140])
        v.addWidget(split, 1)
        return w

    def _maybe_redraw(self):
        """No scrolling trend on a scope tab: captures are drawn when they arrive."""

    # ---- names the sequencer / watchdog / math see ----------------------------------
    def quantities(self) -> list[str]:
        return []

    def set_names(self) -> list[str]:
        out = ["acquire", "timebase", "position", "trigger_source", "trigger_level",
               "trigger_slope", "sweep"]
        for ch in range(1, self.state.nch + 1):
            out += [f"ch{ch}_on", f"ch{ch}_scale", f"ch{ch}_offset"]
        return out

    def setting_kinds(self) -> dict:
        return {"acquire": "choice", "trigger_slope": "choice", "sweep": "choice"}

    def measure_names(self) -> list[str]:
        return [f"{m[0]}{ch}" for ch in range(1, self.state.nch + 1) for m in MEAS]

    # ---- actions ---------------------------------------------------------------------
    def capture_now(self):
        if not self.connected or self._busy:
            return
        self._set_busy("waiting")
        self.worker.request_capture(None)

    def grab_now(self):
        if not self.connected or self._busy:
            return
        self._set_busy("transferring")
        self.worker.submit_result("grab", lambda d: d.grab())

    def abort(self):
        if self._busy:
            self.worker.abort_capture()
            self._set_busy("")

    def output_off(self):
        """Esc: a scope has no outputs, but stop waiting for a trigger."""
        self.abort()

    def _on_capture_status(self, status: str):
        if status == "idle":
            self._set_busy("")
        else:
            self._set_busy(status)

    def _on_job(self, tag, value):
        if tag == "grab":
            self._set_busy("")
            self._on_capture(value)

    def on_error(self, text: str):
        super().on_error(text)
        if self._busy and not self.worker.capturing:
            self._set_busy("")

    def _set_busy(self, state: str):
        if state and not self._busy:
            self._busy_since = time.monotonic()
            self.busy_timer.start(200)
        self._busy = state
        if not state:
            self.busy_timer.stop()
        self._show_busy()
        for b in (self.single_btn, self.grab_btn):
            b.setEnabled(self.connected and not state)

    def _show_busy(self):
        if not self._busy:
            self.status_label.setText(f"Scope: {self.state.status or '—'}")
            return
        dt = time.monotonic() - self._busy_since
        self.status_label.setText("Reading the waveforms…" if self._busy == "transferring"
                                  else f"Waiting for the trigger…  {dt:.1f} s  (Esc stops)")

    def _on_capture(self, res: LogicCapture):
        res.t_app = now()
        self.capture = res
        if session.active:
            session.capture(res, self.display_name())
        self._show_capture()

    def _show_capture(self):
        res = self.capture
        self.plot.clear()
        legend = self.plot.plotItem.legend
        if legend is not None:
            legend.clear()
        self.meas.setRowCount(0)
        for ch, tr in sorted(res.analog.items()):
            pen = pg.mkPen(CH_COLORS[(ch - 1) % 4], width=1.3)
            self.plot.plot(tr.t, tr.v, pen=pen, name=f"CH{ch}")
            st = trace_stats(tr)
            r = self.meas.rowCount()
            self.meas.insertRow(r)
            it = QTableWidgetItem(f"CH{ch}")
            self.meas.setItem(r, 0, it)
            for k, (key, _, unit) in enumerate(MEAS, start=1):
                v = st[key]
                text = "—" if v != v else (f"{v:.1f} %" if unit == "%" else fmt_si(v, unit, 4))
                self.meas.setItem(r, k, QTableWidgetItem(text))
        self.meas.resizeColumnsToContents()
        n = sum(tr.v.size for tr in res.analog.values())
        self.cap_label.setText(time.strftime("%H:%M:%S", time.localtime(res.wall)) +
                               f"  ·  {len(res.analog)} channel(s), {n:,} points" +
                               ("  ·  saved in the session" if session.active else ""))
        self.cap_label.setStyleSheet("")

    @io_errors("Save waveform CSV")
    def save_csv(self, *_):
        if self.capture is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save waveforms",
                                              time.strftime("scope_%Y%m%d_%H%M%S.csv"),
                                              "CSV (*.csv)")
        if not path:
            return
        import numpy as np
        traces = sorted(self.capture.analog.items())
        n = max(tr.v.size for _, tr in traces)
        cols = []
        hdr = []
        for ch, tr in traces:
            t = np.full(n, np.nan)
            v = np.full(n, np.nan)
            t[:tr.v.size], v[:tr.v.size] = tr.t, tr.v
            cols += [t, v]
            hdr += [f"t{ch}_s", f"ch{ch}_V"]
        np.savetxt(path, np.column_stack(cols), delimiter=",", header=",".join(hdr),
                   comments="", fmt="%.9g")
        self.message.emit(f"Saved {path}")

    # ---- worker callbacks ---------------------------------------------------------------
    def on_connected(self, family, caps, state: ScopeState):
        self.idn_label.setText(caps.idn + (f"\n{caps.note}" if caps.note else ""))
        self.idn_label.setStyleSheet("color:#c9ccd1;")
        self._fill_channels(caps.nch)
        self.tsrc.clear()
        for ch in range(1, caps.nch + 1):
            self.tsrc.addItem(f"CH{ch}", ch)
        self.title_changed.emit(self._title())
        self.set_enabled(True)
        self._apply_pending()

    def on_state(self, st: ScopeState):
        self.state = st
        for i, (on, scale, off) in enumerate(self._ch_widgets):
            if i >= len(st.channels):
                break
            c = st.channels[i]
            for w in (on, scale, off):
                w.blockSignals(True)
            on.setChecked(c.on)
            scale.setCurrentIndex(_nearest(c.scale, VDIV) if c.scale > 0 else 9)
            if not off.hasFocus():
                off.setValue(c.offset)
            for w in (on, scale, off):
                w.blockSignals(False)
        for w in (self.tdiv, self.tpos, self.tsrc, self.tlev, self.tslope, self.tsweep):
            w.blockSignals(True)
        if st.tscale > 0:
            self.tdiv.setCurrentIndex(_nearest(st.tscale, TDIV))
        if not self.tpos.hasFocus():
            self.tpos.setValue(st.tpos)
        i = self.tsrc.findData(st.trig_source)
        if i >= 0:
            self.tsrc.setCurrentIndex(i)
        if not self.tlev.hasFocus():
            self.tlev.setValue(st.trig_level)
        self.tslope.setCurrentText(st.trig_slope)
        if st.sweep != "single":
            self.tsweep.setCurrentText(st.sweep)
        for w in (self.tdiv, self.tpos, self.tsrc, self.tlev, self.tslope, self.tsweep):
            w.blockSignals(False)
        self._show_busy()
        self.settings_changed.emit()

    def on_reading(self, r: ScopeReading):
        if r.status != self.state.status:
            self.state.status = r.status
            self._show_busy()

    # ---- naming / settings ----------------------------------------------------------------
    def _name_changed(self, _text):
        self.title_changed.emit(self._title())
        self.settings_changed.emit()

    def _title(self) -> str:
        model = getattr(self.caps, "model", "") or "Scope"
        t = self.name_edit.text().strip()
        return f"{model} · {t}" if t else model

    def base_name(self) -> str:
        model = getattr(self.caps, "model", "") or "Scope"
        t = self.user_name()
        return f"{model} · {t}" if t else f"{model} ({self.resource})"

    def session_settings(self) -> dict:
        if self._pending is not None and not self.connected:
            return dict(self._pending)
        return {"name": self.name_edit.text()}

    def restore_settings(self, settings: dict):
        self._pending = dict(settings)
        self.name_edit.setText(str(settings.get("name", "")))

    def _apply_pending(self):
        self._pending = None     # the scope keeps its own setup; only the name is ours

    def set_enabled(self, on: bool):
        for cls in (QPushButton, QComboBox, QDoubleSpinBox, QCheckBox):
            for w in self.findChildren(cls):
                w.setEnabled(on)
        self.trace_chk.setEnabled(True)
