"""Tab for a DC source (Agilent 663xx SCPI family or HP 6632A family)."""
from __future__ import annotations

import csv
import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
                             QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
                             QPushButton, QScrollArea, QSpinBox, QSplitter, QTabWidget,
                             QVBoxLayout, QWidget)

from ..analysis import trapezoid
from ..instruments.base import SupplyCaps, SupplyPoll, SupplyReading, Setpoints
from . import touch
from .capture_tab import CaptureTab
from .forms import select
from .system_tab import SystemTab
from .tests_tab import TestsTab
from .panel_base import PanelBase
from .profile_box import ProfileBox
from .widgets import (C_CURR, C_DVM, C_MUTED, C_ON, C_POW, C_VOLT, C_WARN, MONO,
                      Readout, SafeSpin, TraceBuffer, fmt_current, fmt_si)

# Digitizer presets for the 663xx: (label, points, tooltip)
SPEED_PRESETS = [
    ("Fast", 256, "256 samples = 4 ms window. ~21 readings/s (V+I) or ~32/s current only."),
    ("Normal", 1024, "1024 samples = 16 ms window. ~13 readings/s (V+I)."),
    ("Smooth", 2048, "2048 samples = 32 ms window (instrument default). ~9 readings/s (V+I). "
                     "Best averaging for pulsed loads."),
]



def _profile_page(box):
    """The Profile tab: the box plus what it is for."""
    from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget
    w = QWidget()
    v = QVBoxLayout(w)
    v.addWidget(box)
    hint = QLabel("Steps the setpoint in software, so the supply acts as a slow function "
                  "generator: battery discharge curves, brown-out tests, ripple on a rail. "
                  "GPIB supplies take ~20–50 commands/s, so keep rate= at 10–20 and periods "
                  "above ~1 s for a clean shape. Switch the output on yourself; the profile only "
                  "moves the setpoint. Esc stops it. The Trend tab shows the result.")
    hint.setWordWrap(True)
    hint.setStyleSheet("color:#8a93a0;")
    v.addWidget(hint)
    v.addStretch(1)
    return w

class SupplyPanel(PanelBase):
    KIND = "supply"

    def __init__(self, resource: str, model: str, baud: int):
        super().__init__(resource, model, baud)
        self.caps = SupplyCaps()
        self.sp = Setpoints()
        self.poll = SupplyPoll()
        self.worker.poll_cfg = self.poll
        self.last_voltage = None
        # columns: t, V, I, Imax, DVM
        self.buf = TraceBuffer(5)

        self._armed = False  # touch: first tap on OUTPUT arms, second turns ON
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        console = self.build_console_tab(
            "Hex frame: START CMD [DATA], e.g.  a1 de 00  (model name)"
            if self.model == "DPS150" else
            "Command, e.g.  MEAS:CURR:ACDC?   (6632A: VOUT?  STS?)",
            read_button=self.model == "DPS150")
        self.capture_tab = CaptureTab(self)
        self.tests_tab = TestsTab(self)
        self.system_tab = SystemTab(self)
        self.worker.capture_status.connect(self.capture_tab.on_status)
        self.worker.capture_done.connect(self.capture_tab.on_capture)
        self.worker.job_result.connect(self.system_tab.on_result)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(self._build_left())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_trend_tab(), "Trend")
        self.tabs.addTab(self.tests_tab, "Tests")
        self.profile_box = ProfileBox(self, {"voltage": lambda d, v: d.set_voltage(v),
                                             "current": lambda d, v: d.set_current(v)},
                                      {"voltage": "V", "current": "A"})
        self.tabs.addTab(touch.scroll(_profile_page(self.profile_box)), "Profile")
        if touch.TOUCH:
            # Touch (1280x800): the left column holds only readouts + output
            # controls so they never need scrolling; setup gets its own tab.
            setup = QWidget()
            sv = QVBoxLayout(setup)
            sv.addLayout(self._build_header())
            sv.addWidget(self._build_measure_group())
            sv.addStretch(1)
            self.tabs.addTab(touch.scroll(setup), "Setup")
        self.system_scroll = touch.scroll(self.system_tab)
        self.tabs.addTab(self.system_scroll, "System")
        self.tabs.setTabVisible(self.tabs.indexOf(self.system_scroll), False)
        self.tabs.addTab(console, "Console")
        split.addWidget(self.tabs)
        split.setStretchFactor(1, 1)
        split.setSizes([440, 840] if touch.TOUCH else [420, 1000])
        root.addWidget(split)
        self.set_enabled(False)
        self.start()

    # ================================================================ build
    def _build_header(self):
        head = QHBoxLayout()
        self.idn_label = QLabel(f"Connecting to {self.resource}…")
        self.idn_label.setWordWrap(True)
        self.idn_label.setStyleSheet(f"color:{C_MUTED};")
        head.addWidget(self.idn_label, 1)
        self.reset_btn = QPushButton("*RST")
        self.reset_btn.clicked.connect(self.reset_instrument)
        head.addWidget(self.reset_btn)
        return head

    def _build_left(self):
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        if not touch.TOUCH:
            lay.addLayout(self._build_header())
        lay.addWidget(self._build_readouts())
        lay.addWidget(self._build_output_group())
        if not touch.TOUCH:
            lay.addWidget(self._build_measure_group())
        lay.addStretch(1)
        sa = touch.scroll(inner)
        sa.setMinimumWidth(420 if touch.TOUCH else 400)
        return sa

    def _build_readouts(self):
        g = QGroupBox("Measurements")
        lay = QVBoxLayout(g)
        top = QHBoxLayout()
        self.mode_badge = QLabel("—")
        self.mode_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mode_badge.setFixedSize(64, 26)
        self._style_badge("—")
        self.flags_label = QLabel("")
        self.flags_label.setStyleSheet(f"color:{C_WARN}; font-weight:bold;")
        self.flags_label.setWordWrap(True)
        top.addWidget(self.mode_badge)
        top.addWidget(self.flags_label, 1)
        lay.addLayout(top)
        self.r_volt = Readout("Voltage", C_VOLT, 30 if touch.TOUCH else 26)
        self.r_curr = Readout("Current", C_CURR, 30 if touch.TOUCH else 26)
        self.r_pow = Readout("Power", C_POW, 16)
        self.r_dvm = Readout("DVM", C_DVM, 18)
        for r in (self.r_volt, self.r_curr, self.r_pow, self.r_dvm):
            lay.addWidget(r)
        self.r_dvm.setVisible(False)

        self.pulse_box = QWidget()
        grid = QGridLayout(self.pulse_box)
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setColumnMinimumWidth(2, 44)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        self.pulse_labels = {}
        names = {"i_max": "Max", "i_min": "Min", "i_high": "High", "i_low": "Low", "i_rms": "RMS"}
        for n, (key, name) in enumerate(names.items()):
            cap = QLabel(name)
            cap.setStyleSheet(f"color:{C_MUTED};")
            cap.setAlignment(Qt.AlignmentFlag.AlignRight)
            val = QLabel("—")
            val.setFont(QFont(MONO, 11))
            val.setStyleSheet(f"color:{C_CURR};")
            val.setAlignment(Qt.AlignmentFlag.AlignRight)
            grid.addWidget(cap, n // 2, (n % 2) * 2)
            grid.addWidget(val, n // 2, (n % 2) * 2 + 1)
            self.pulse_labels[key] = val
        self.pulse_box.setVisible(False)
        self.pulse_box.setToolTip("Statistics over one acquisition (samples × interval).\n"
                                  "High/Low are the two levels of a pulsed waveform.")
        lay.addWidget(self.pulse_box)
        return g

    def _set_row(self, form, label, spin, apply_fn):
        row = QHBoxLayout()
        row.addWidget(spin, 1)
        b = QPushButton("Set")
        b.setFixedWidth(48)
        b.clicked.connect(apply_fn)
        spin.lineEdit().returnPressed.connect(apply_fn)
        row.addWidget(b)
        if touch.TOUCH:
            # The keypad's own Set button sends the value; no separate button needed.
            touch.attach_keypad(spin, label, apply_fn)
            b.setVisible(False)
        lab = QLabel(label)
        form.addRow(lab, row)
        return lab, b

    def _build_output_group(self):
        g = QGroupBox("Output")
        v = QVBoxLayout(g)
        self.out_btn = QPushButton("OUTPUT —")
        self.out_btn.setCheckable(True)
        self.out_btn.setMinimumHeight(60 if touch.TOUCH else 44)
        self.out_btn.setToolTip("Toggle the output. Esc turns every supply's output OFF.")
        self.out_btn.clicked.connect(lambda on: self.set_output(on))
        v.addWidget(self.out_btn)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.sp_volt = SafeSpin(4, 0.01, " V")
        self.sp_curr = SafeSpin(4, 0.001, " A")
        self.sp_ovp = SafeSpin(3, 0.1, " V")
        self.sp_res = SafeSpin(4, 0.001, " Ω")
        self.sp_res.setToolTip("Programmable source resistance: emulates a battery's "
                               "internal resistance.")
        self._set_row(form, "Voltage", self.sp_volt,
                      lambda: self.cmd(lambda d, x: d.set_voltage(x), self.sp_volt.value()))
        self._set_row(form, "Current limit", self.sp_curr,
                      lambda: self.cmd(lambda d, x: d.set_current(x), self.sp_curr.value()))
        self.ovp_label, _ = self._set_row(
            form, "OVP", self.sp_ovp,
            lambda: self.cmd(lambda d, x: d.set_ovp(x), self.sp_ovp.value()))
        self.res_label, self.res_set = self._set_row(
            form, "Output resistance", self.sp_res,
            lambda: self.cmd(lambda d, x: d.set_resistance(x), self.sp_res.value()))
        v.addLayout(form)

        row = QHBoxLayout()
        self.ocp_chk = QCheckBox("OCP")
        self.ocp_chk.setToolTip("Over-current protection: turn the output off if the supply "
                                "goes into current limit.")
        self.ocp_chk.clicked.connect(lambda on: self.cmd(lambda d, x: d.set_ocp(x), on))
        row.addWidget(self.ocp_chk)
        self.ovt_chk = QCheckBox("OV trip")
        self.ovt_chk.setToolTip("Turn the output off if it goes about 2 V above the voltage "
                                "setting.")
        self.ovt_chk.clicked.connect(lambda on: self.cmd(lambda d, x: d.set_ovp_tracking(x), on))
        self.ovt_chk.setVisible(False)
        row.addWidget(self.ovt_chk)
        self.sense_chk = QCheckBox("Open-sense detect")
        self.sense_chk.setToolTip(
            "Turn the output off if a remote-sense lead (+S / −S) comes disconnected.\n"
            "Use it when the sense leads run to the DUT. With the local-sense jumpers\n"
            "fitted it never trips. Off after *RST and at power-on, unless you save it\n"
            "into the power-on state (System tab).")
        self.sense_chk.clicked.connect(
            lambda on: self.cmd(lambda d, x: d.set_sense_protect(x), on))
        self.sense_chk.setVisible(False)
        row.addStretch(1)
        self.clr_btn = QPushButton("Clear trip")
        self.clr_btn.setToolTip("Reset a latched OV / OCP / open-sense trip (remove the cause "
                                "first)")
        self.clr_btn.clicked.connect(lambda: self.cmd(lambda d: d.clear_protection()))
        row.addWidget(self.clr_btn)
        v.addLayout(row)
        v.addWidget(self.sense_chk)     # own row: keeps the column narrow on a touch screen
        self.note_label = QLabel("")
        self.note_label.setWordWrap(True)
        self.note_label.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        self.note_label.setVisible(False)
        v.addWidget(self.note_label)
        return g

    def _build_measure_group(self):
        g = QGroupBox("Measurement setup")
        form = QFormLayout(g)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.meas_widgets = []

        def add(label, w):
            lab = QLabel(label) if label else None
            if lab:
                form.addRow(lab, w)
            else:
                form.addRow(w)
            self.meas_widgets += [x for x in (lab, w) if x is not None]

        self.range_combo = QComboBox()
        self.range_combo.setToolTip("Current measurement range. Use the lowest range that "
                                    "covers the peak current for the best resolution.")
        self.range_combo.activated.connect(self._apply_range)
        add("Current range", self.range_combo)
        self.win_combo = QComboBox()
        self.win_combo.addItem("Hanning", "HANN")
        self.win_combo.addItem("Rectangular", "RECT")
        self.win_combo.setToolTip("Measurement window. Hanning reduces ripple errors when the "
                                  "acquisition isn't a whole number of cycles; Rectangular "
                                  "gives the true average of exactly what was sampled.")
        self.win_combo.activated.connect(
            lambda: self.cmd(lambda d, x: d.set_window(x), self.win_combo.currentData()))
        self.win_label = QLabel("Window")
        form.addRow(self.win_label, self.win_combo)
        self.det_combo = QComboBox()
        self.det_combo.addItems(["ACDC", "DC"])
        self.det_combo.setToolTip("ACDC: measure the full waveform (use for pulsed loads).\n"
                                  "DC: DC component only.")
        self.det_combo.activated.connect(
            lambda: self.cmd(lambda d, x: d.set_detector(x), self.det_combo.currentText()))
        add("Detector", self.det_combo)

        presets = QWidget()
        pr = QHBoxLayout(presets)
        pr.setContentsMargins(0, 0, 0, 0)
        for label, pts, tip in SPEED_PRESETS:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(lambda _, p=pts: self._apply_sweep(p, 15.6))
            pr.addWidget(b)
        add("Speed", presets)

        samples = QWidget()
        row = QHBoxLayout(samples)
        row.setContentsMargins(0, 0, 0, 0)
        self.sw_points = QSpinBox()
        self.sw_points.setRange(1, 4096)
        self.sw_points.setValue(2048)
        self.sw_tint = SafeSpin(1, 1.0, " µs")
        self.sw_tint.setRange(15.6, 1e6)
        self.sw_tint.setValue(15.6)
        row.addWidget(self.sw_points)
        row.addWidget(QLabel("×"))
        row.addWidget(self.sw_tint, 1)
        b = QPushButton("Set")
        b.setFixedWidth(64 if touch.TOUCH else 48)
        b.clicked.connect(lambda: self._apply_sweep(self.sw_points.value(), self.sw_tint.value()))
        touch.attach_keypad(self.sw_points, "Samples per reading")
        touch.attach_keypad(self.sw_tint, "Sample interval")
        row.addWidget(b)
        add("Samples", samples)
        self.acq_label = QLabel("")
        self.acq_label.setStyleSheet(f"color:{C_MUTED};")
        add("", self.acq_label)
        self.sw_points.valueChanged.connect(self._update_acq_label)
        self.sw_tint.valueChanged.connect(self._update_acq_label)

        self.volt_chk = QCheckBox("Measure voltage (uncheck for faster current-only)")
        self.volt_chk.setChecked(True)
        self.volt_chk.toggled.connect(self._apply_poll)
        form.addRow(self.volt_chk)
        self.pulse_chk = QCheckBox("Pulse statistics (max/min/high/low/RMS)")
        self.pulse_chk.toggled.connect(self._apply_poll)
        add(None, self.pulse_chk)
        self.dvm_chk = QCheckBox("Read DVM input")
        self.dvm_chk.toggled.connect(self._apply_poll)
        add(None, self.dvm_chk)

        self.poll_ms = QSpinBox()
        self.poll_ms.setRange(0, 60000)
        self.poll_ms.setSingleStep(50)
        self.poll_ms.setValue(0)
        self.poll_ms.setSuffix(" ms")
        self.poll_ms.setSpecialValueText("none (as fast as possible)")
        self.poll_ms.setToolTip("Extra delay between readings")
        self.poll_ms.valueChanged.connect(self._apply_poll)
        touch.attach_keypad(self.poll_ms, "Poll delay")
        form.addRow("Poll delay", self.poll_ms)
        form.addRow(self.build_pause_button())
        self._update_acq_label()
        return g

    def _build_trend_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addLayout(self.build_trend_bar())
        self.glw = pg.GraphicsLayoutWidget()
        self.p_v = self.glw.addPlot(row=0, col=0)
        self.p_i = self.glw.addPlot(row=1, col=0)
        self.p_d = self.glw.addPlot(row=2, col=0)
        for p, name, unit, c in ((self.p_v, "Voltage", "V", C_VOLT),
                                 (self.p_i, "Current", "A", C_CURR),
                                 (self.p_d, "DVM", "V", C_DVM)):
            p.showGrid(x=True, y=True, alpha=0.15)
            p.setLabel("left", name, units=unit, color=c)
            p.getAxis("left").setWidth(70)
            p.setClipToView(True)
            p.setDownsampling(auto=True, mode="peak")
        self.p_i.setXLink(self.p_v)
        self.p_d.setXLink(self.p_v)
        self.p_d.setLabel("bottom", "Time", units="s")
        self.c_v = self.p_v.plot(pen=pg.mkPen(C_VOLT, width=1.5))
        self.c_i = self.p_i.plot(pen=pg.mkPen(C_CURR, width=1.5))
        self.c_imax = self.p_i.plot(pen=pg.mkPen(C_CURR, width=1, style=Qt.PenStyle.DotLine))
        self.c_d = self.p_d.plot(pen=pg.mkPen(C_DVM, width=1.5))
        self.p_d.setVisible(False)
        touch.plots(self.p_v, self.p_i, self.p_d)
        self.scrolling_trend(self.p_v, self.p_v, self.p_i, self.p_d)
        v.addWidget(self.glw, 1)
        return w

    # ================================================================ actions
    def set_output(self, on: bool):
        if on and touch.TOUCH and not self._armed:
            # A stray touch must never energize a load: ON needs a second tap.
            self._armed = True
            self.out_btn.setChecked(False)
            self.out_btn.setText("Tap again to turn ON")
            self.out_btn.setStyleSheet("QPushButton { background:#8a6d00; color:white; "
                                       "font-weight:bold; font-size:17px; }")
            QTimer.singleShot(3000, self._disarm)
            return
        self._armed = False
        self.out_btn.setChecked(bool(self.sp.output))  # revert until confirmed
        self.cmd(lambda d, x: d.set_output(x), on)

    def _disarm(self):
        if self._armed:
            self._armed = False
            self._style_output_button()

    def output_off(self):
        self._armed = False
        self.profile_box.stop()
        self.tests_tab.abort()
        if self.capture_tab.busy or self.capture_tab.running:
            self.capture_tab.stop()
        if self.connected:
            self.set_output(False)

    def reset_instrument(self):
        what = ("CLR returns the 6632A to power-on defaults: 0 V, minimum current, "
                "OUTPUT ON, OVP max." if self.caps.reset_label == "CLR" else
                "*RST turns the output off and returns all settings to defaults.")
        if QMessageBox.question(self, "Reset", what + "\n\nContinue?") == \
                QMessageBox.StandardButton.Yes:
            self.cmd(lambda d: d.reset())

    def _apply_range(self):
        self.cmd(lambda d, x: d.set_current_range(x), self.range_combo.currentData())

    def _apply_sweep(self, points, tint_us):
        self.cmd(lambda d, p, t: d.set_sweep(p, t), points, tint_us * 1e-6)

    def _update_acq_label(self):
        t = self.sw_points.value() * self.sw_tint.value() * 1e-6
        self.acq_label.setText(f"Each reading averages {fmt_si(t, 's')}")

    def _apply_poll(self, *_):
        self.worker.interval_s = self.poll_ms.value() / 1000
        self.poll.measure_voltage = self.volt_chk.isChecked()
        self.poll.pulse_stats = self.pulse_chk.isChecked() and self.caps.has_pulse_stats
        self.poll.dvm = self.dvm_chk.isChecked() and self.caps.has_dvm
        self.pulse_box.setVisible(self.poll.pulse_stats)
        self.r_dvm.setVisible(self.poll.dvm)
        self.p_d.setVisible(self.poll.dvm)
        self.p_v.setVisible(self.poll.measure_voltage)
        self.r_volt.setVisible(self.poll.measure_voltage)
        self.r_pow.setVisible(self.poll.measure_voltage)

    # ================================================================ slots
    def on_connected(self, family, caps: SupplyCaps, sp):
        self.idn_label.setText(caps.idn)
        self.idn_label.setStyleSheet("color:#c9ccd1;")
        self.title_changed.emit(caps.model)
        self.reset_btn.setText(caps.reset_label)
        self.sp_volt.setRange(0, caps.v_max)
        self.sp_curr.setRange(caps.i_min, caps.i_max)
        self.sp_ovp.setRange(0, caps.ovp_max)
        self.sp_res.setRange(caps.r_min, caps.r_max)
        if not caps.setpoint_readback:
            # Unknown setpoints: start from safe values so a stray "Set" can't
            # program OVP to 0 V (which fires the crowbar).
            self.sp_ovp.setValue(caps.ovp_max)
            self.sp_curr.setValue(caps.i_min)
        for w in (self.res_label, self.sp_res):
            w.setVisible(caps.has_resistance)
        # touch: the keypad sends the value, so the row has no Set button
        self.res_set.setVisible(caps.has_resistance and not touch.TOUCH)
        for w in self.meas_widgets:
            w.setVisible(caps.has_meas_setup)
        self.pulse_chk.setVisible(caps.has_pulse_stats)
        self.dvm_chk.setVisible(caps.has_dvm)
        self.range_combo.clear()
        for label, fs in caps.current_ranges:
            self.range_combo.addItem(f"{label}", label)
        if caps.has_auto_range:
            self.range_combo.addItem("Auto", "AUTO")
        self.win_combo.setVisible(caps.has_window)
        self.win_label.setVisible(caps.has_window)
        self.ovp_label.setText(caps.ovp_label)
        self.ovp_label.setToolTip(caps.ovp_tip)
        self.sp_ovp.setToolTip(caps.ovp_tip)
        self.ovt_chk.setVisible(caps.has_ovp_tracking)
        self.sense_chk.setVisible(caps.has_sense_protect)
        if caps.has_waveform and self.tabs.indexOf(self.capture_tab) < 0:
            self.tabs.insertTab(1, self.capture_tab, "Capture")
        self.tabs.setTabVisible(self.tabs.indexOf(self.system_scroll), caps.has_system)
        self.system_tab.on_connected(caps)
        self.tests_tab.on_connected(caps)
        self.capture_tab._what_changed()
        self.note_label.setText(caps.note)
        self.note_label.setVisible(bool(caps.note))
        self.buf.clear()
        self._apply_poll()

    def on_state(self, sp: Setpoints):
        self.sp = sp
        self.sp_volt.set_from_instrument(sp.voltage)
        self.sp_curr.set_from_instrument(sp.current)
        self.sp_ovp.set_from_instrument(sp.ovp)
        if self.caps.has_resistance:
            self.sp_res.set_from_instrument(sp.resistance)
        self.ocp_chk.setChecked(bool(sp.ocp))
        if sp.ovp_tracking is not None:
            self.ovt_chk.setChecked(sp.ovp_tracking)
        if sp.sense_protect is not None:
            self.sense_chk.setChecked(sp.sense_protect)
        self.system_tab.on_state(sp)
        self.capture_tab.on_state(sp)
        self._style_output_button()
        if self.caps.has_meas_setup:
            self.sw_points.blockSignals(True)
            self.sw_points.setValue(sp.sweep_points)
            self.sw_points.blockSignals(False)
            self.sw_tint.set_from_instrument(sp.sweep_tint * 1e6)
            self._update_acq_label()
            select(self.range_combo, sp.current_range)
            if sp.window:
                select(self.win_combo, sp.window[:4].upper())
            self.det_combo.setCurrentText(sp.detector if sp.detector in ("ACDC", "DC") else "ACDC")

    def on_reading(self, r: SupplyReading):
        if r.voltage is not None:
            self.last_voltage = r.voltage
        self.system_tab.on_reading(r)
        self.tests_tab.on_reading(r)
        self.r_volt.set("—" if r.voltage is None else f"{r.voltage:+.4f} V")
        self.r_curr.set(fmt_current(r.current))
        self.r_pow.set("—" if r.power is None else f"{r.power:+.4f} W")
        if r.dvm is not None:
            self.r_dvm.set(f"{r.dvm:+.4f} V")
        if r.i_max is not None:
            for k, lab in self.pulse_labels.items():
                lab.setText(fmt_current(getattr(r, k)))
        self.mode_badge.setText(r.mode)
        self._style_badge(r.mode)
        trip = {"OV", "OCP", "SENSE-OPEN"} & set(r.flags)
        hint = ""
        if "SENSE-OPEN" in r.flags:
            hint = "  (a sense lead is open: reconnect +S / −S, then Clear trip)"
        elif trip:
            hint = "  (press Clear trip)"
        self.flags_label.setText("  ".join(r.flags) + hint)
        self.buf.append([r.t, r.voltage, r.current, r.i_max, r.dvm])
        if self.recording:
            self.record([time.strftime("%Y-%m-%dT%H:%M:%S"), f"{r.t:.3f}", r.voltage,
                               r.current, r.power, r.mode, "|".join(r.flags), r.dvm,
                               r.i_max, r.i_min, r.i_high, r.i_low, r.i_rms])

    def csv_header(self):
        return ["timestamp", "t_s", "voltage_V", "current_A", "power_W", "mode", "flags",
                "dvm_V", "i_max_A", "i_min_A", "i_high_A", "i_low_A", "i_rms_A"]

    def csv_name(self):
        return f"{self.caps.model or 'supply'}_%Y%m%d_%H%M%S.csv"

    def on_error(self, text):
        super().on_error(text)
        self.capture_tab.on_error()

    def _style_output_button(self):
        on = self.sp.output
        size = 18 if touch.TOUCH else 15
        self.out_btn.setChecked(bool(on))
        self.out_btn.setText({True: "OUTPUT ON", False: "OUTPUT OFF",
                              None: "OUTPUT ?  (state unknown)"}[on])
        self.out_btn.setStyleSheet(
            f"QPushButton {{ background:{C_ON}; color:white; font-weight:bold; font-size:{size}px; }}"
            if on else f"QPushButton {{ font-weight:bold; font-size:{size}px; }}")

    # ================================================================ trend
    def clear_trend(self):
        self.buf.clear()
        super().clear_trend()

    def redraw(self):
        d = self.buf.view()
        if not len(d):
            return
        t = d[:, 0]
        span = self.span()
        start = int(np.searchsorted(t, t[-1] - span)) if span else 0
        # Plot one point past the window so the lines run to the left edge;
        # the statistics below stay on the window itself.
        t, v, i, imax, dvm = d[max(0, start - 1):].T
        w = d[start:]
        self.c_v.setData(t, v, connect="finite")
        self.c_i.setData(t, i)
        if np.isfinite(imax).any():
            self.c_imax.setData(t, imax, connect="finite")
        else:
            self.c_imax.setData([], [])
        if self.p_d.isVisible():
            self.c_d.setData(t, dvm, connect="finite")
        t, v, i = w[:, 0], w[:, 1], w[:, 2]
        charge = trapezoid(i, t) / 3600 if t.size > 1 else float("nan")
        vtxt = f"   V avg {np.nanmean(v):.4f} V" if np.isfinite(v).any() else ""
        self.trend_stats.setText(
            f"I avg {fmt_current(i.mean())}  min {fmt_current(i.min())}  "
            f"max {fmt_current(i.max())}{vtxt}   charge {fmt_si(charge, 'Ah')}")

    # ================================================================ misc
    def _style_badge(self, mode: str):
        bg = {"CV": "#1b5e20", "CC": "#b71c1c", "CC-": "#880e4f", "OFF": "#37474f"}.get(mode, "#263238")
        self.mode_badge.setStyleSheet(
            f"background:{bg}; color:white; font-weight:bold; border-radius:4px;")

    def set_enabled(self, on: bool):
        for cls in (QPushButton, QComboBox, QSpinBox, SafeSpin, QCheckBox):
            for w in self.findChildren(cls):
                w.setEnabled(on)
        # Trend/console controls stay usable even while disconnected.
        for w in (self.span_combo, self.freeze_chk, self.trace_chk):
            w.setEnabled(True)
        self.capture_tab._buttons()
        self.profile_box.set_enabled(on)
        if on:
            self.system_tab.set_enabled(on)
        else:
            self.tests_tab.abort()
