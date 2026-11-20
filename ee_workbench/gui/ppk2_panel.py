"""Tab for the Nordic Power Profiler Kit II: a 100 kS/s current meter that can
also be the DUT's supply (source meter) or sit in series with it (ampere meter).

Data flow: the worker hands over every block of raw samples (~5000 per poll).
They go into
  * a ring buffer of the last 10 s at full rate (the Live tab, Save),
  * 1 ms min/mean/max bins for ~16 min (Trend spans up to 10 min),
  * 100 ms bins for the whole session (longer spans),
  * the CSV recorder, binned at the chosen interval.
Plots are drawn from a min/max envelope of at most a few thousand points, so
narrow spikes stay visible however far you zoom out.
"""
from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout,
                             QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QPushButton, QSpinBox, QSplitter, QTabWidget,
                             QVBoxLayout, QWidget)

from ..instruments.ppk2 import DT, SAMPLE_RATE, VDD_MAX_MV, VDD_MIN_MV, PpkReading, PpkState
from ..worker import now
from . import touch
from .panel_base import PanelBase
from .widgets import (C_CURR, C_MUTED, C_ON, C_WARN, MONO, Readout, SafeSpin, TraceBuffer,
                      fmt_si)

LIVE_SECONDS = 10
LIVE_WINDOWS = {"1 ms": 1e-3, "10 ms": 10e-3, "100 ms": 0.1, "1 s": 1.0, "5 s": 5.0,
                "10 s": 10.0}
CSV_INTERVALS = {"1 ms": 100, "10 ms": 1000, "100 ms": 10_000, "1 s": 100_000}
FINE_BIN, COARSE_BIN = 100, 10_000           # samples: 1 ms and 100 ms
FINE_SPAN_MAX = 600                          # s: longer spans use the 100 ms bins
LOGIC_COLORS = ["#ffb347", "#7ee787", "#c39bff", "#ef9a9a", "#4fc3f7", "#fff176",
                "#80cbc4", "#bcaaa4"]
PLOT_POINTS = 3000                           # envelope pairs drawn per curve


# ---------------------------------------------------------------- data helpers
class Binner:
    """Groups a sample stream into fixed-size bins: (t_mid, mean, min, max, logic OR)."""

    def __init__(self, size: int):
        self.size = size
        self.reset()

    def reset(self):
        self._i = np.empty(0)
        self._l = np.empty(0, np.uint8)
        self._t0 = None

    def push(self, t0: float, current: np.ndarray, logic: np.ndarray) -> np.ndarray:
        if self._t0 is None or not self._i.size:
            self._t0 = t0
        i = np.concatenate((self._i, current))
        lg = np.concatenate((self._l, logic))
        nb = i.size // self.size
        rows = np.empty((nb, 5))
        if nb:
            used = nb * self.size
            blk = i[:used].reshape(nb, self.size)
            rows[:, 0] = self._t0 + (np.arange(nb) * self.size + (self.size - 1) / 2) * DT
            rows[:, 1] = blk.mean(axis=1)
            rows[:, 2] = blk.min(axis=1)
            rows[:, 3] = blk.max(axis=1)
            rows[:, 4] = np.bitwise_or.reduce(lg[:used].reshape(nb, self.size), axis=1)
            self._t0 += used * DT
            i, lg = i[used:], lg[used:]
        self._i, self._l = i, lg
        return rows


class Ring:
    """The last `n` samples (current as float32, logic) and the newest time."""

    def __init__(self, n: int):
        self.cur = np.zeros(n, np.float32)
        self.logic = np.zeros(n, np.uint8)
        self.n = n
        self.clear()

    def clear(self):
        self.w = 0            # next write position
        self.count = 0
        self.t_end = 0.0      # app time of the newest sample

    def push(self, r: PpkReading):
        c, lg = r.current, r.logic
        if c.size >= self.n:
            c, lg = c[-self.n:], lg[-self.n:]
        k = c.size
        first = min(k, self.n - self.w)
        self.cur[self.w:self.w + first] = c[:first]
        self.logic[self.w:self.w + first] = lg[:first]
        self.cur[:k - first] = c[first:]
        self.logic[:k - first] = lg[first:]
        self.w = (self.w + k) % self.n
        self.count = min(self.n, self.count + k)
        self.t_end = r.t0 + (r.n - 1) * DT

    def latest(self, k: int):
        """(t, current, logic) of the newest k samples, oldest first."""
        k = min(k, self.count)
        idx = (self.w - k + np.arange(k)) % self.n
        t = self.t_end - (k - 1 - np.arange(k)) * DT
        return t, self.cur[idx].astype(float), self.logic[idx]


def envelope(t, lo, hi, points=PLOT_POINTS):
    """Interleaved min/max per block, so a plot of it shows every spike."""
    n = t.size
    if n <= 2 * points:
        if lo is hi:
            return t, lo
        return np.repeat(t, 2), np.column_stack((lo, hi)).ravel()
    blk = int(np.ceil(n / points))
    m = n // blk
    tt = t[: m * blk].reshape(m, blk)[:, 0]
    mn = lo[: m * blk].reshape(m, blk).min(axis=1)
    mx = hi[: m * blk].reshape(m, blk).max(axis=1)
    return np.repeat(tt, 2), np.column_stack((mn, mx)).ravel()


def summarize(t_mid, mean, lo, hi, bin_s):
    """Average, min, max and charge of a run of bins."""
    if mean.size == 0:
        return None
    ok = np.isfinite(mean)
    if not ok.any():
        return None
    dur = ok.sum() * bin_s
    avg = float(mean[ok].mean())
    return {"avg": avg, "min": float(np.nanmin(lo)), "max": float(np.nanmax(hi)),
            "charge": avg * dur, "duration": dur}


# ---------------------------------------------------------------- panel
class Ppk2Panel(PanelBase):
    def __init__(self, resource: str, model: str, baud: int):
        super().__init__(resource, model, baud)
        self.state = PpkState()
        self.ring = Ring(LIVE_SECONDS * SAMPLE_RATE)
        self.fine = TraceBuffer(5, max_rows=1_000_000)       # t, mean, min, max, logic
        self.coarse = TraceBuffer(5)
        self.b_fine, self.b_coarse = Binner(FINE_BIN), Binner(COARSE_BIN)
        self.b_csv = Binner(CSV_INTERVALS["10 ms"])
        self.lost = 0
        self._samples: list[tuple[float, int]] = []          # (monotonic, n) for the rate
        self._armed = False
        self._wall_offset = time.time() - now()
        self.worker.interval_s = 0.05

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        console = self.build_console_tab(
            "Raw command bytes in hex, e.g.  0C 01  (DUT power on).  Reads nothing back.")
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_trend_tab(), "Trend")
        self.tabs.addTab(self._build_live_tab(), "Live")
        if not touch.TOUCH:
            lay.addWidget(self._build_idn())
        g = self._build_readouts()
        g.layout().insertWidget(0, self._build_name_edit())
        lay.addWidget(g)
        lay.addWidget(self._build_power_group())
        if touch.TOUCH:
            setup = QWidget()
            sv = QVBoxLayout(setup)
            sv.addWidget(self._build_idn())
            sv.addWidget(self._build_acq_group())
            sv.addStretch(1)
            self.tabs.addTab(touch.scroll(setup), "Setup")
        else:
            lay.addWidget(self._build_acq_group())
        lay.addStretch(1)
        sa = touch.scroll(inner)
        sa.setMinimumWidth(420 if touch.TOUCH else 400)
        split.addWidget(sa)
        self.tabs.addTab(console, "Console")
        split.addWidget(self.tabs)
        split.setStretchFactor(1, 1)
        split.setSizes([440, 840] if touch.TOUCH else [420, 1000])
        root.addWidget(split)
        self.set_enabled(False)
        self.start()

    # ================================================================ build
    def _build_idn(self):
        self.idn_label = QLabel(f"Connecting to {self.resource}…")
        self.idn_label.setWordWrap(True)
        self.idn_label.setStyleSheet(f"color:{C_MUTED};")
        return self.idn_label

    def _build_name_edit(self):
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("DUT name, e.g. “sensor node rev B”")
        self.name_edit.textChanged.connect(self._name_changed)
        return self.name_edit

    def _build_readouts(self):
        g = QGroupBox("Current")
        gl = QVBoxLayout(g)
        self.readout = Readout("Average of the last 0.25 s", C_CURR, 34 if touch.TOUCH else 28)
        gl.addWidget(self.readout)
        self.stats_title = QLabel("Trend span")
        self.stats_title.setStyleSheet(f"color:{C_MUTED};")
        gl.addWidget(self.stats_title)
        grid = QGridLayout()
        self.stat_labels = {}
        for n, key in enumerate(("Average", "Max", "Min", "Duration", "Charge", "Energy")):
            cap = QLabel(key)
            cap.setStyleSheet(f"color:{C_MUTED};")
            val = QLabel("—")
            val.setFont(QFont(MONO, 12 if touch.TOUCH else 10))
            val.setAlignment(Qt.AlignmentFlag.AlignRight)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if key in ("Charge", "Energy"):
                grid.addWidget(cap, 3 + (key == "Energy"), 0)
                grid.addWidget(val, 3 + (key == "Energy"), 1, 1, 3)
            else:
                grid.addWidget(cap, n // 2, (n % 2) * 2)
                grid.addWidget(val, n // 2, (n % 2) * 2 + 1)
            self.stat_labels[key] = val
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setHorizontalSpacing(12)
        gl.addLayout(grid)
        logic = QHBoxLayout()
        lab = QLabel("Logic")
        lab.setStyleSheet(f"color:{C_MUTED};")
        logic.addWidget(lab)
        self.logic_leds = []
        for k in range(8):
            led = QLabel(f"D{k}")
            led.setAlignment(Qt.AlignmentFlag.AlignCenter)
            led.setFixedWidth(34)
            led.setToolTip(f"Digital input D{k}: high at any time in the last 0.25 s")
            logic.addWidget(led)
            self.logic_leds.append(led)
        logic.addStretch(1)
        gl.addLayout(logic)
        self._set_leds(0)
        return g

    def _build_power_group(self):
        g = QGroupBox("Power")
        form = QFormLayout(g)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        modes = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(False)     # neither is lit while the mode is unknown
        self.mode_btns = {}
        for key, label, tip in (
                ("source", "Source meter",
                 "The PPK2 powers the DUT from VOUT at the voltage below and measures it."),
                ("ampere", "Ampere meter",
                 "The DUT has its own supply; the PPK2 measures current through VIN → VOUT.")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setStyleSheet("QPushButton:checked { background:#3d6fb4; color:white; "
                            "font-weight:bold; }")
            b.clicked.connect(lambda _, k=key: self._mode_clicked(k))
            self.mode_group.addButton(b)
            self.mode_btns[key] = b
            modes.addWidget(b)
        form.addRow("Mode", modes)
        row = QHBoxLayout()
        self.volt_spin = SafeSpin(decimals=0, step=50, suffix=" mV")
        self.volt_spin.setRange(VDD_MIN_MV, VDD_MAX_MV)
        self.volt_spin.editingFinished.connect(self._apply_voltage)
        touch.attach_keypad(self.volt_spin, "Voltage", self._apply_voltage)
        row.addWidget(self.volt_spin, 1)
        setb = QPushButton("Set")
        setb.clicked.connect(self._apply_voltage)
        row.addWidget(setb)
        self.volt_label = QLabel("Voltage")
        form.addRow(self.volt_label, row)
        self.out_btn = QPushButton()
        self.out_btn.setCheckable(True)
        self.out_btn.setMinimumHeight(56 if touch.TOUCH else 40)
        self.out_btn.clicked.connect(self.set_dut_power)
        form.addRow(self.out_btn)
        self._style_power_button()
        return g

    def _build_acq_group(self):
        g = QGroupBox("Acquisition")
        form = QFormLayout(g)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.run_btn = QPushButton("■ Stop")
        self.run_btn.setCheckable(True)
        self.run_btn.setChecked(True)
        self.run_btn.clicked.connect(self._run_clicked)
        self.run_btn.setToolTip("Start or stop the 100 kS/s sample stream")
        form.addRow(self.run_btn)
        self.spike_chk = QCheckBox("Spike filter")
        self.spike_chk.setChecked(True)
        self.spike_chk.setToolTip(
            "Smooth the 3 samples after each automatic range switch, like nRF Connect "
            "Power Profiler. Switching shunts briefly upsets the reading.")
        self.spike_chk.clicked.connect(
            lambda on: self.cmd(lambda d, x: d.set_spike_filter(x), on))
        form.addRow(self.spike_chk)
        self.csv_combo = QComboBox()
        self.csv_combo.addItems(list(CSV_INTERVALS))
        self.csv_combo.setCurrentText("10 ms")
        self.csv_combo.setToolTip("Each CSV row holds the average, min and max over this "
                                  "interval. Live ▸ Save writes every sample.")
        self.csv_combo.currentTextChanged.connect(self._csv_interval_changed)
        form.addRow("CSV row every", self.csv_combo)
        self.lost_label = QLabel("Samples lost: 0")
        self.lost_label.setStyleSheet(f"color:{C_MUTED};")
        self.lost_label.setToolTip("Gaps in the sample counter: data that never reached "
                                   "the computer (USB too busy, or the app fell behind).")
        form.addRow(self.lost_label)
        return g

    def _build_trend_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addLayout(self.build_trend_bar())
        opts = QHBoxLayout()
        self.log_chk = QCheckBox("Log scale")
        self.log_chk.setToolTip("Logarithmic current axis: sleep µA and radio mA on one plot")
        self.log_chk.toggled.connect(self._log_toggled)
        opts.addWidget(self.log_chk)
        self.region_chk = QCheckBox("Measure region")
        self.region_chk.setToolTip("Drag the shaded band to get the average, charge and energy "
                                   "of just that stretch (freezes the trend while on)")
        self.region_chk.toggled.connect(self._region_toggled)
        opts.addWidget(self.region_chk)
        opts.addStretch(1)
        v.addLayout(opts)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.setLabel("left", "Current", units="A", color=C_CURR)
        self.curve = self.plot.plot(pen=pg.mkPen(C_CURR, width=1))
        self.region = pg.LinearRegionItem(brush=pg.mkBrush(255, 255, 255, 28))
        self.region.setZValue(10)
        self.region.sigRegionChanged.connect(lambda *_: self._update_stats())
        touch.plots(self.plot)
        v.addWidget(self.plot, 1)
        return w

    def _build_live_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Window"))
        self.live_combo = QComboBox()
        self.live_combo.addItems(list(LIVE_WINDOWS))
        self.live_combo.setCurrentText("100 ms")
        self.live_combo.currentTextChanged.connect(self._live_changed)
        bar.addWidget(self.live_combo)
        self.logic_chk = QCheckBox("Logic D0–D7")
        self.logic_chk.toggled.connect(self._logic_toggled)
        bar.addWidget(self.logic_chk)
        self.live_log_chk = QCheckBox("Log scale")
        self.live_log_chk.toggled.connect(self._log_toggled)
        bar.addWidget(self.live_log_chk)
        save = QPushButton("Save…")
        save.setToolTip(f"Write every sample in the window (up to the last {LIVE_SECONDS} s) "
                        "to CSV: time, current, logic")
        save.clicked.connect(self.save_live)
        bar.addWidget(save)
        self.live_stats = QLabel("")
        self.live_stats.setFont(QFont(MONO, 9))
        self.live_stats.setWordWrap(True)
        bar.addWidget(self.live_stats, 1)
        v.addLayout(bar)
        hint = QLabel("Full 100 kS/s data. Freeze (Trend tab) holds this view too.")
        hint.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        v.addWidget(hint)
        self.glw = pg.GraphicsLayoutWidget()
        self.p_live = self.glw.addPlot(row=0, col=0)
        self.p_live.showGrid(x=True, y=True, alpha=0.15)
        self.p_live.setLabel("left", "Current", units="A", color=C_CURR)
        self.c_live = self.p_live.plot(pen=pg.mkPen(C_CURR, width=1))
        self.p_logic = self.glw.addPlot(row=1, col=0)
        self.p_logic.setXLink(self.p_live)
        self.p_logic.setLabel("bottom", "Time", units="s")
        self.p_logic.setYRange(-0.2, 8 * 1.5, padding=0)
        self.p_logic.getAxis("left").setTicks([[(k * 1.5 + 0.5, f"D{k}") for k in range(8)]])
        self.p_logic.setMouseEnabled(y=False)
        self.c_logic = [self.p_logic.plot(pen=pg.mkPen(LOGIC_COLORS[k], width=1))
                        for k in range(8)]
        self.glw.ci.layout.setRowStretchFactor(0, 3)
        self.glw.ci.layout.setRowStretchFactor(1, 2)
        self.p_logic.setVisible(False)
        self.p_live.setLabel("bottom", "Time", units="s")
        touch.plots(self.p_live, self.p_logic)
        v.addWidget(self.glw, 1)
        return w

    # ================================================================ actions
    def _mode_clicked(self, mode: str):
        self._style_mode()           # revert until the instrument confirms
        if mode == self.state.mode:
            return
        if mode == "source":
            ok = QMessageBox.question(
                self, "Source meter",
                f"In source mode the PPK2 drives VOUT at {self.volt_spin.value():.0f} mV "
                "when DUT power is on.\n\nMake sure the DUT has no other supply connected "
                "to it. Switch to source mode?")
            if ok != QMessageBox.StandardButton.Yes:
                return
        self.cmd(lambda d, m: d.set_mode(m), mode)

    def _apply_voltage(self):
        mv = int(round(self.volt_spin.value()))
        if mv != self.state.voltage_mv:
            self.cmd(lambda d, x: d.set_voltage(x), mv)

    def set_dut_power(self, on: bool):
        if on and touch.TOUCH and not self._armed:
            # A stray touch must never power a DUT: ON needs a second tap.
            self._armed = True
            self.out_btn.setChecked(False)
            self.out_btn.setText("Tap again to turn ON")
            self.out_btn.setStyleSheet("QPushButton { background:#8a6d00; color:white; "
                                       "font-weight:bold; font-size:17px; }")
            QTimer.singleShot(3000, self._disarm)
            return
        self._armed = False
        self.out_btn.setChecked(bool(self.state.dut_power))   # revert until confirmed
        self.cmd(lambda d, x: d.set_dut_power(x), on)

    def _disarm(self):
        if self._armed:
            self._armed = False
            self._style_power_button()

    def output_off(self):
        """Esc / ALL OUTPUTS OFF."""
        self._armed = False
        if self.connected:
            self.cmd(lambda d: d.set_dut_power(False))

    def _run_clicked(self, run: bool):
        self.run_btn.setChecked(self.state.measuring)          # revert until confirmed
        if run:
            self.worker.poll_enabled = True
            self.cmd(lambda d: d.start())
        else:
            self.cmd(lambda d: d.stop())

    def _on_poll_paused(self):
        self.on_error("Stopped after an error (unplugged?). Press ▶ Run to try again.")
        self.state.measuring = False
        self._style_run()

    def _send_raw(self, read):
        text = self.raw_line.text().strip()
        if not text or not self.connected:
            return
        try:
            data = bytes(int(x, 16) for x in text.replace(",", " ").split())
        except ValueError:
            self.console("⚠ Use hex bytes separated by spaces, e.g.  0C 01")
            return
        self.console("> " + data.hex(" ").upper())
        self.cmd(lambda d, b: d.t.write(b), data)
        self.raw_line.clear()

    def save_live(self):
        k = int(LIVE_WINDOWS[self.live_combo.currentText()] * SAMPLE_RATE)
        t, i, lg = self.ring.latest(k)
        if not t.size:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save live samples", time.strftime("PPK2_live_%Y%m%d_%H%M%S.csv"),
            "CSV (*.csv)")
        if not path:
            return
        with open(path, "w") as fh:
            fh.write("t_s,current_A,logic\n")
            np.savetxt(fh, np.column_stack((t, i, lg)), fmt=("%.5f", "%.6e", "%d"),
                       delimiter=",")
        self.message.emit(f"Saved {t.size} samples to {path}")

    # ================================================================ settings
    def _name_changed(self, text):
        self.title_changed.emit(f"PPK2 · {text}" if text else "PPK2")
        self.settings_changed.emit()

    def _csv_interval_changed(self, text):
        self.b_csv = Binner(CSV_INTERVALS[text])
        self.settings_changed.emit()

    def _live_changed(self, *_):
        self._dirty = True
        self.settings_changed.emit()

    def _log_toggled(self, *_):
        self.plot.setLogMode(y=self.log_chk.isChecked())
        self.p_live.setLogMode(y=self.live_log_chk.isChecked())
        self._dirty = True
        self.settings_changed.emit()

    def _logic_toggled(self, on):
        self.p_logic.setVisible(on)
        self.p_live.getAxis("bottom").setLabel("" if on else "Time", units=None if on else "s")
        self._dirty = True
        self.settings_changed.emit()

    def _region_toggled(self, on):
        if on:
            (x0, x1), _ = self.plot.viewRange()
            w = x1 - x0
            self.region.setRegion((x0 + 0.4 * w, x0 + 0.6 * w))
            self.plot.addItem(self.region)
            self.freeze_chk.setChecked(True)
        else:
            self.plot.removeItem(self.region)
        self._update_stats()

    def session_settings(self) -> dict:
        return {"name": self.name_edit.text(), "live_window": self.live_combo.currentText(),
                "log": self.log_chk.isChecked(), "live_log": self.live_log_chk.isChecked(),
                "logic": self.logic_chk.isChecked(), "csv_interval": self.csv_combo.currentText()}

    def restore_settings(self, s: dict):
        self.name_edit.setText(str(s.get("name", "")))
        if s.get("live_window") in LIVE_WINDOWS:
            self.live_combo.setCurrentText(s["live_window"])
        if s.get("csv_interval") in CSV_INTERVALS:
            self.csv_combo.setCurrentText(s["csv_interval"])
        self.log_chk.setChecked(bool(s.get("log", False)))
        self.live_log_chk.setChecked(bool(s.get("live_log", False)))
        self.logic_chk.setChecked(bool(s.get("logic", False)))

    # ================================================================ slots
    def on_connected(self, family, caps, state):
        self.idn_label.setText(f"{caps.idn}\n{caps.note}")
        self.idn_label.setStyleSheet("color:#c9ccd1;")
        self.title_changed.emit(f"PPK2 · {self.name_edit.text()}" if self.name_edit.text()
                                else "PPK2")

    def on_state(self, st: PpkState):
        self.state = st
        self._style_mode()
        self.volt_spin.set_from_instrument(st.voltage_mv)
        self.volt_label.setText("Voltage" if st.mode != "ampere" else "DUT voltage")
        self.volt_spin.setToolTip(
            "Source voltage on VOUT" if st.mode != "ampere" else
            "The DUT's own supply voltage. The PPK2 doesn't measure it; it is used for "
            "the calibration's voltage term and for energy.")
        self.spike_chk.setChecked(st.spike_filter)
        self._style_power_button()
        self._style_run()

    def on_reading(self, r: PpkReading):
        if not r.n:
            return
        self.ring.push(r)
        self.fine.extend(self.b_fine.push(r.t0, r.current, r.logic))
        self.coarse.extend(self.b_coarse.push(r.t0, r.current, r.logic))
        if r.lost:
            self.lost += r.lost
            self.lost_label.setText(f"Samples lost: {self.lost}")
            self.lost_label.setStyleSheet(f"color:{C_WARN};")
        m = time.monotonic()
        self._samples.append((m, r.n))
        while self._samples and m - self._samples[0][0] > 2:
            self._samples.pop(0)
        if self.recorder.active:
            rows = self.b_csv.push(r.t0, r.current, r.logic)
            if rows.size:
                wall = self._wall_offset + rows[:, 0]
                self.recorder.w.writerows(
                    [time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(w)) + f".{int(w % 1 * 1000):03d}",
                     f"{t:.5f}", f"{a:.6e}", f"{lo:.6e}", f"{hi:.6e}", int(lg)]
                    for w, (t, a, lo, hi, lg) in zip(wall, rows))
                self.recorder.fh.flush()

    def _toggle_record(self, on: bool):
        if on:
            self.b_csv.reset()
        super()._toggle_record(on)

    def csv_header(self):
        return ["timestamp", "t_s", "current_A", "current_min_A", "current_max_A", "logic"]

    def csv_name(self):
        return "PPK2_%Y%m%d_%H%M%S.csv"

    def clear_trend(self):
        self.fine.clear()
        self.coarse.clear()
        self.ring.clear()
        self.b_fine.reset()
        self.b_coarse.reset()
        super().clear_trend()

    # ================================================================ drawing
    def _maybe_redraw(self):
        super()._maybe_redraw()
        if self.connected and len(self._samples) > 1:
            dt = self._samples[-1][0] - self._samples[0][0]
            n = sum(k for _, k in self._samples[1:])
            self.rate_label.setText(f"{n / dt / 1000:.1f} kS/s" if dt > 0 else "")
        elif self.connected:
            self.rate_label.setText("stopped" if not self.state.measuring else "")

    def _trend_data(self):
        span = self.span()
        buf, bin_s = (self.fine, FINE_BIN * DT) if span and span <= FINE_SPAN_MAX else \
            (self.coarse, COARSE_BIN * DT)
        d = buf.view()
        if not len(d):
            return None, bin_s, span
        if span:
            d = d[int(np.searchsorted(d[:, 0], d[-1, 0] - span)):]
        return d, bin_s, span

    def redraw(self):
        d, bin_s, span = self._trend_data()
        if d is not None and len(d):
            t, _, lo, hi, _ = d.T
            x, y = envelope(t, lo, hi)
            if self.log_chk.isChecked():
                y = np.maximum(y, 1e-9)
            self.curve.setData(x, y)
            if span:
                self.plot.setXRange(max(0, t[-1] - span), t[-1], padding=0)
            else:
                self.plot.enableAutoRange(axis="x")
        self._redraw_live()
        self._update_readout()
        self._update_stats()

    def _redraw_live(self):
        win = LIVE_WINDOWS[self.live_combo.currentText()]
        t, i, lg = self.ring.latest(int(win * SAMPLE_RATE))
        if not t.size:
            return
        x, y = envelope(t, i, i)
        if self.live_log_chk.isChecked():
            y = np.maximum(y, 1e-9)
        self.c_live.setData(x, y)
        self.p_live.setXRange(t[-1] - win, t[-1], padding=0)
        if self.p_logic.isVisible():
            for k, c in enumerate(self.c_logic):
                bits = ((lg >> k) & 1).astype(float)
                bx, by = envelope(t, bits, bits)
                c.setData(bx, by + k * 1.5)
        st = summarize(t, i, i, i, DT)
        if st:
            self.live_stats.setText(
                f"avg {fmt_si(st['avg'], 'A')}   max {fmt_si(st['max'], 'A')}   "
                f"min {fmt_si(st['min'], 'A')}   charge {fmt_si(st['charge'], 'C')}")

    def _update_readout(self):
        t, i, lg = self.ring.latest(SAMPLE_RATE // 4)
        if i.size:
            self.readout.set(fmt_si(float(i.mean()), "A", 4))
            self._set_leds(int(np.bitwise_or.reduce(lg)) if lg.size else 0)

    def _update_stats(self):
        d, bin_s, _ = self._trend_data()
        if self.region_chk.isChecked():
            a, b = self.region.getRegion()
            # 1 ms bins if they still reach back to the region's start
            fine = self.fine.view()
            if b - a <= FINE_SPAN_MAX and len(fine) and fine[0, 0] <= a:
                d, bin_s = fine, FINE_BIN * DT
            else:
                d, bin_s = self.coarse.view(), COARSE_BIN * DT
            if len(d):
                d = d[(d[:, 0] >= a) & (d[:, 0] <= b)]
            self.stats_title.setText(f"Region  {a:.3f} s – {b:.3f} s")
        else:
            self.stats_title.setText("Trend span")
        st = summarize(*d.T[:4], bin_s) if d is not None and len(d) else None
        if not st:
            for v in self.stat_labels.values():
                v.setText("—")
            return
        volts = self.state.voltage_mv / 1000
        self.stat_labels["Average"].setText(fmt_si(st["avg"], "A"))
        self.stat_labels["Max"].setText(fmt_si(st["max"], "A"))
        self.stat_labels["Min"].setText(fmt_si(st["min"], "A"))
        self.stat_labels["Duration"].setText(fmt_si(st["duration"], "s"))
        self.stat_labels["Charge"].setText(
            f"{fmt_si(st['charge'], 'C')}  ·  {fmt_si(st['charge'] / 3600, 'Ah')}")
        self.stat_labels["Energy"].setText(
            f"{fmt_si(st['charge'] * volts, 'J')}  ·  {fmt_si(st['charge'] * volts / 3600, 'Wh')}"
            f"   at {volts:.2f} V")
        self.trend_stats.setText(
            f"avg {fmt_si(st['avg'], 'A')}   max {fmt_si(st['max'], 'A')}   "
            f"charge {fmt_si(st['charge'] / 3600, 'Ah')}")

    # ================================================================ styling
    def _set_leds(self, byte: int):
        for k, led in enumerate(self.logic_leds):
            on = bool(byte >> k & 1)
            led.setStyleSheet(
                f"background:{LOGIC_COLORS[k]}; color:black; border-radius:3px;" if on else
                f"background:#2b2f36; color:{C_MUTED}; border-radius:3px;")

    def _style_mode(self):
        for key, b in self.mode_btns.items():
            b.setChecked(self.state.mode == key)

    def _style_power_button(self):
        on = self.state.dut_power
        size = 18 if touch.TOUCH else 15
        self.out_btn.setChecked(bool(on))
        self.out_btn.setText({True: "DUT POWER ON", False: "DUT POWER OFF",
                              None: "DUT POWER ?  (state unknown)"}[on])
        self.out_btn.setStyleSheet(
            f"QPushButton {{ background:{C_ON}; color:white; font-weight:bold; font-size:{size}px; }}"
            if on else f"QPushButton {{ font-weight:bold; font-size:{size}px; }}")

    def _style_run(self):
        self.run_btn.setChecked(self.state.measuring)
        self.run_btn.setText("■ Stop" if self.state.measuring else "▶ Run")

    def set_enabled(self, on: bool):
        for cls in (QPushButton, QComboBox, QSpinBox, SafeSpin, QCheckBox):
            for w in self.findChildren(cls):
                w.setEnabled(on)
        for w in (self.span_combo, self.freeze_chk, self.trace_chk, self.log_chk,
                  self.region_chk, self.live_combo, self.logic_chk, self.live_log_chk):
            w.setEnabled(True)
