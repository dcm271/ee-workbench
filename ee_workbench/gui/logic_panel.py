"""Tab for a Saleae logic analyzer (Logic 8 / Pro 8 / Pro 16) driven through
Logic 2's automation server.

Unlike the other instruments nothing is polled: each Capture runs one Logic 2
capture (timed, triggered or manual), brings the samples back and shows them
here: digital lanes, analog traces, measurements over the view or a region,
and the decoded protocol data from Logic 2's analyzers. The capture stays open
in Logic 2, so decoders can be changed and re-run, or the capture saved as a
.sal file, without capturing again.
"""
from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict, fields

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QBrush, QFont
from PyQt6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog,
                             QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
                             QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
                             QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                             QTabWidget, QVBoxLayout, QWidget)

from ..instruments.saleae import (DECODERS, MODES, LogicCapture, LogicCaps,
                                  LogicConfig, analog_stats, digital_envelope, digital_stats,
                                  fmt_rate)
from ..worker import now
from . import touch
from .panel_base import PanelBase
from .widgets import C_MUTED, C_WARN, MONO, SafeSpin, fmt_si

COLORS = ["#ffb347", "#7ee787", "#c39bff", "#ef9a9a", "#4fc3f7", "#fff176", "#80cbc4",
          "#bcaaa4", "#f48fb1", "#aed581", "#90caf9", "#ffcc80", "#b39ddb", "#80deea",
          "#e6ee9c", "#ffab91"]
LANE = 1.5                      # vertical pitch of the digital lanes
MAX_TABLE_ROWS = 50_000
TRIGGER_LABELS = {"rising": "Rising edge", "falling": "Falling edge",
                  "pulse_high": "High pulse", "pulse_low": "Low pulse"}
MODE_LABELS = {"timed": "Timed", "trigger": "Trigger", "manual": "Manual"}


def _si(x, unit, digits=4):
    return "—" if x is None else fmt_si(x, unit, digits)


def _span(lo, hi, unit="s"):
    """'50.0 µs', or '8.70 µs … 26.1 µs' when the widths really differ."""
    if lo is None:
        return "—"
    if np.isclose(lo, hi, rtol=1e-6, atol=0):
        return _si(lo, unit, 3)
    return f"{_si(lo, unit, 3)} … {_si(hi, unit, 3)}"


# ---------------------------------------------------------------- decoder dialog
class DecoderDialog(QDialog):
    """Pick a Logic 2 analyzer and its channels / settings."""

    def __init__(self, parent, n_digital: int, existing: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Protocol decoder")
        self.n = n_digital
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItems(list(DECODERS) + ["Other…"])
        self.kind.currentTextChanged.connect(self._rebuild)
        form.addRow("Analyzer", self.kind)
        self.label = QLineEdit()
        self.label.setPlaceholderText("Name shown in the table (optional)")
        form.addRow("Label", self.label)
        v.addLayout(form)
        self.box = QWidget()
        self.box_form = QFormLayout(self.box)
        self.box_form.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.box)
        hint = QLabel("Logic 2 fills in any setting not listed here with its default. "
                      "For “Other…”, the analyzer name and setting names must match Logic 2's "
                      "Add Analyzer panel exactly.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        v.addWidget(hint)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.widgets: dict = {}
        self.result_dict: dict | None = None
        if existing:
            name = existing["name"]
            self.kind.setCurrentText(name if name in DECODERS else "Other…")
            self._rebuild(self.kind.currentText(), existing)
            self.label.setText(existing.get("label", ""))
        else:
            self._rebuild(self.kind.currentText())
        self.resize(420, 0)

    def _rebuild(self, kind: str, existing: dict | None = None):
        while self.box_form.rowCount():
            self.box_form.removeRow(0)
        self.widgets = {}
        current = (existing or {}).get("settings", {})
        if kind == "Other…":
            self.other_name = QLineEdit((existing or {}).get("name", ""))
            self.other_name.setPlaceholderText("e.g. CAN, 1-Wire, Manchester")
            self.box_form.addRow("Logic 2 name", self.other_name)
            self.other_settings = QPlainTextEdit(json.dumps(current, indent=1) if current else
                                                 '{"Input Channel": 0}')
            self.other_settings.setFont(QFont(MONO, 10))
            self.other_settings.setMinimumHeight(110)
            self.box_form.addRow("Settings (JSON)", self.other_settings)
            return
        for key, default in DECODERS[kind].items():
            value = current.get(key, default)
            if "Rate" in key:
                w = QSpinBox()
                w.setRange(1, 100_000_000)
                w.setValue(int(value))
                w.setSuffix(" bit/s")
            else:
                w = QComboBox()
                for ch in range(self.n):
                    w.addItem(f"D{ch}", ch)
                w.setCurrentIndex(min(int(value), self.n - 1))
            self.widgets[key] = w
            self.box_form.addRow(key, w)

    def _ok(self):
        kind = self.kind.currentText()
        if kind == "Other…":
            name = self.other_name.text().strip()
            try:
                settings = json.loads(self.other_settings.toPlainText() or "{}")
                if not isinstance(settings, dict):
                    raise ValueError("settings must be a JSON object")
            except ValueError as e:
                QMessageBox.warning(self, "Settings", f"Not valid JSON settings: {e}")
                return
            if not name:
                QMessageBox.warning(self, "Analyzer", "Give the Logic 2 analyzer name.")
                return
        else:
            name = kind
            settings = {k: (w.value() if isinstance(w, QSpinBox) else w.currentData())
                        for k, w in self.widgets.items()}
        self.result_dict = {"name": name, "label": self.label.text().strip(),
                            "settings": settings}
        self.accept()


def describe_decoder(d: dict) -> str:
    s = d.get("settings", {})
    parts = []
    for k, v in s.items():
        if isinstance(v, int) and "Rate" not in k and not isinstance(v, bool):
            parts.append(f"{k} D{v}")
        else:
            parts.append(f"{k} {v}")
    label = f"{d['label']}: " if d.get("label") else ""
    return f"{label}{d['name']}  ·  " + ", ".join(parts)


# ---------------------------------------------------------------- panel
class LogicPanel(PanelBase):
    def __init__(self, resource: str, model: str, baud: int):
        super().__init__(resource, model, baud)
        self.caps = LogicCaps()
        self.cfg = LogicConfig()
        self.capture: LogicCapture | None = None
        self._pending: dict | None = None      # restored settings, applied once connected
        self._busy = ""                        # "", "waiting", "transferring"
        self._busy_since = 0.0
        self._busy_mode = "timed"
        self._view_dirty = False
        self.worker.poll_enabled = False       # nothing to poll: captures only
        self.worker.capture_status.connect(self._on_capture_status)
        self.worker.capture_done.connect(self._on_capture)
        self.worker.job_result.connect(self._on_job)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        console = self.build_console_tab(
            "Logic 2 has no command console; this log shows what was asked of it.")
        self.raw_line.setEnabled(False)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_wave_tab(), "Waveform")
        self.tabs.addTab(self._build_table_tab(), "Decoded")
        if not touch.TOUCH:
            lay.addWidget(self._build_idn())
        lay.addWidget(self._build_name_edit())
        lay.addWidget(self._build_capture_group())
        lay.addWidget(self._build_channel_group())
        if touch.TOUCH:
            setup = QWidget()
            sv = QVBoxLayout(setup)
            sv.addWidget(self._build_idn())
            sv.addWidget(self._build_sampling_group())
            sv.addWidget(self._build_decoder_group())
            sv.addStretch(1)
            self.tabs.addTab(touch.scroll(setup), "Setup")
        else:
            lay.addWidget(self._build_sampling_group())
            lay.addWidget(self._build_decoder_group())
        lay.addStretch(1)
        sa = touch.scroll(inner)
        sa.setMinimumWidth(420 if touch.TOUCH else 400)
        split.addWidget(sa)
        self.tabs.addTab(console, "Console")
        split.addWidget(self.tabs)
        split.setStretchFactor(1, 1)
        split.setSizes([440, 840] if touch.TOUCH else [420, 1000])
        root.addWidget(split)

        self.view_timer = QTimer(self)
        self.view_timer.setSingleShot(True)
        self.view_timer.timeout.connect(self._redraw_view)
        self.busy_timer = QTimer(self)
        self.busy_timer.timeout.connect(self._show_busy)
        self._set_mode("timed")
        self.set_enabled(False)
        self.start()

    # ================================================================ build
    def _build_idn(self):
        self.idn_label = QLabel(f"Connecting to Logic 2 for {self.resource}…")
        self.idn_label.setWordWrap(True)
        self.idn_label.setStyleSheet(f"color:{C_MUTED};")
        return self.idn_label

    def _build_name_edit(self):
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("What it's probing, e.g. “sensor I²C bus”")
        self.name_edit.textChanged.connect(self._name_changed)
        return self.name_edit

    def _spin(self, title, lo, hi, decimals, step, suffix, value):
        s = SafeSpin(decimals=decimals, step=step, suffix=suffix, track=False)
        s.setRange(lo, hi)
        s.setValue(value)
        s.valueChanged.connect(lambda *_: self.settings_changed.emit())
        touch.attach_keypad(s, title)
        return s

    def _build_capture_group(self):
        g = QGroupBox("Capture")
        v = QVBoxLayout(g)
        modes = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_btns = {}
        for key, tip in (("timed", "Record for a set time"),
                         ("trigger", "Wait for an edge or pulse on a digital channel, then "
                                     "record what came just before and after it"),
                         ("manual", "Record until you press Stop")):
            b = QPushButton(MODE_LABELS[key])
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setStyleSheet("QPushButton:checked { background:#3d6fb4; color:white; "
                            "font-weight:bold; }")
            b.clicked.connect(lambda _, k=key: self._set_mode(k))
            self.mode_group.addButton(b)
            self.mode_btns[key] = b
            modes.addWidget(b)
        v.addLayout(modes)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.duration = self._spin("Duration", 0.0001, 3600, 4, 0.01, " s", 0.1)
        self.duration.setToolTip("How long a timed capture records")
        form.addRow("Duration", self.duration)
        self.trig_ch = QComboBox()
        self.trig_ch.currentIndexChanged.connect(lambda *_: self.settings_changed.emit())
        form.addRow("Trigger on", self.trig_ch)
        self.trig_type = QComboBox()
        for k, lab in TRIGGER_LABELS.items():
            self.trig_type.addItem(lab, k)
        self.trig_type.currentIndexChanged.connect(self._trig_type_changed)
        form.addRow("Condition", self.trig_type)
        self.pulse_min = self._spin("Shortest pulse", 0, 1e6, 3, 1, " µs", 0)
        self.pulse_max = self._spin("Longest pulse", 0, 1e6, 3, 1, " µs", 0)
        self.pulse_min.setToolTip("Only pulses at least this long trigger (0: any)")
        self.pulse_max.setToolTip("Only pulses at most this long trigger (0: no limit)")
        prow = QHBoxLayout()
        prow.addWidget(self.pulse_min)
        prow.addWidget(QLabel("to"))
        prow.addWidget(self.pulse_max)
        self.pulse_label = QLabel("Pulse width")
        form.addRow(self.pulse_label, prow)
        self.pre = self._spin("Before trigger", 0, 600, 4, 0.001, " s", 0.01)
        self.pre.setToolTip("How much to keep from before the trigger")
        form.addRow("Before trigger", self.pre)
        self.after = self._spin("After trigger", 0.0001, 3600, 4, 0.01, " s", 0.05)
        self.after.setToolTip("How long to record after the trigger")
        form.addRow("After trigger", self.after)
        self._mode_rows = {"timed": [self.duration],
                           "trigger": [self.trig_ch, self.trig_type, self.pre, self.after]}
        self._form = form
        v.addLayout(form)
        row = QHBoxLayout()
        self.run_btn = QPushButton("▶ Capture")
        self.run_btn.setMinimumHeight(56 if touch.TOUCH else 40)
        self.run_btn.clicked.connect(self._run_clicked)
        row.addWidget(self.run_btn, 1)
        self.abort_btn = QPushButton("✕")
        self.abort_btn.setToolTip("Abort: throw this capture away")
        self.abort_btn.setMinimumHeight(56 if touch.TOUCH else 40)
        self.abort_btn.clicked.connect(self.abort)
        self.abort_btn.setVisible(False)
        row.addWidget(self.abort_btn)
        v.addLayout(row)
        self.repeat_chk = QCheckBox("Repeat")
        self.repeat_chk.setToolTip("Start the next capture as soon as one is shown, like a "
                                   "scope in Normal mode")
        self.repeat_chk.toggled.connect(lambda *_: self.settings_changed.emit())
        v.addWidget(self.repeat_chk)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"color:{C_MUTED};")
        self.status_label.setWordWrap(True)
        v.addWidget(self.status_label)
        return g

    def _build_channel_group(self):
        g = QGroupBox("Channels")
        v = QVBoxLayout(g)
        self.dig_grid = QGridLayout()
        self.dig_grid.setHorizontalSpacing(4)
        v.addWidget(QLabel("Digital"))
        v.addLayout(self.dig_grid)
        self.an_title = QLabel("Analog")
        v.addWidget(self.an_title)
        self.an_grid = QGridLayout()
        self.an_grid.setHorizontalSpacing(4)
        v.addLayout(self.an_grid)
        self.dig_chks: list[QCheckBox] = []
        self.an_chks: list[QCheckBox] = []
        self._make_channel_boxes(8, 8)
        return g

    def _make_channel_boxes(self, nd: int, na: int):
        for chks, grid in ((self.dig_chks, self.dig_grid), (self.an_chks, self.an_grid)):
            for c in chks:
                grid.removeWidget(c)
                c.deleteLater()
            chks.clear()
        per_row = 4 if touch.TOUCH else 8
        for prefix, n, chks, grid in (("D", nd, self.dig_chks, self.dig_grid),
                                      ("A", na, self.an_chks, self.an_grid)):
            for k in range(n):
                c = QCheckBox(f"{prefix}{k}")
                c.setStyleSheet(f"QCheckBox {{ color:{COLORS[k % len(COLORS)]}; }}")
                c.toggled.connect(self._channels_changed)
                grid.addWidget(c, k // per_row, k % per_row)
                chks.append(c)
        self.an_title.setVisible(na > 0)
        self.trig_ch.blockSignals(True)
        self.trig_ch.clear()
        for k in range(nd):
            self.trig_ch.addItem(f"D{k}", k)
        self.trig_ch.blockSignals(False)

    def _build_sampling_group(self):
        g = QGroupBox("Sampling")
        form = QFormLayout(g)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.d_rate = QComboBox()
        self.d_rate.setToolTip("Logic 2 refuses rates the analyzer can't reach with this many "
                               "channels on; the console then says why.")
        self.d_rate.currentIndexChanged.connect(lambda *_: self.settings_changed.emit())
        form.addRow("Digital rate", self.d_rate)
        self.a_rate = QComboBox()
        self.a_rate.currentIndexChanged.connect(lambda *_: self.settings_changed.emit())
        self.a_rate_label = QLabel("Analog rate")
        form.addRow(self.a_rate_label, self.a_rate)
        self.thresh = QComboBox()
        self.thresh.setToolTip("Logic level threshold of the digital inputs")
        self.thresh.currentIndexChanged.connect(lambda *_: self.settings_changed.emit())
        self.thresh_label = QLabel("Threshold")
        form.addRow(self.thresh_label, self.thresh)
        return g

    def _build_decoder_group(self):
        g = QGroupBox("Protocol decoders (Logic 2 analyzers)")
        v = QVBoxLayout(g)
        self.dec_list = QListWidget()
        self.dec_list.setMaximumHeight(140 if touch.TOUCH else 100)
        self.dec_list.itemDoubleClicked.connect(lambda *_: self._edit_decoder())
        v.addWidget(self.dec_list)
        row = QHBoxLayout()
        for text, fn, tip in (("Add…", self._add_decoder, "Add a protocol decoder"),
                              ("Edit…", self._edit_decoder, "Change the selected decoder"),
                              ("Remove", self._remove_decoder, "Remove the selected decoder")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        self.decode_btn = QPushButton("Decode")
        self.decode_btn.setToolTip("Run the decoders on the last capture again")
        self.decode_btn.clicked.connect(self.decode_now)
        row.addWidget(self.decode_btn)
        v.addLayout(row)
        return g

    def _build_wave_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        bar = QHBoxLayout()
        for text, tip, fn in (("Fit", "Show the whole capture", self.fit),
                              ("＋", "Zoom in", lambda: self.zoom(0.5)),
                              ("－", "Zoom out", lambda: self.zoom(2.0)),
                              ("◀", "Scroll left", lambda: self.pan(-0.5)),
                              ("▶", "Scroll right", lambda: self.pan(0.5))):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            if text != "Fit":
                b.setFixedWidth(52 if touch.TOUCH else 34)
            bar.addWidget(b)
        self.region_chk = QCheckBox("Measure region")
        self.region_chk.setToolTip("Drag the band's edges: measurements cover just that "
                                   "stretch, and its width shows as Δt")
        self.region_chk.toggled.connect(self._region_toggled)
        bar.addWidget(self.region_chk)
        bar.addStretch(1)
        self.save_btn = QPushButton("Save .sal…")
        self.save_btn.setToolTip("Save the capture in Logic 2's own format, to open in Logic 2")
        self.save_btn.clicked.connect(self.save_sal)
        bar.addWidget(self.save_btn)
        self.csv_btn = QPushButton("Export CSV…")
        self.csv_btn.setToolTip("Logic 2 writes digital.csv / analog.csv into a folder")
        self.csv_btn.clicked.connect(self.export_csv)
        bar.addWidget(self.csv_btn)
        v.addLayout(bar)
        self.info = QLabel("No capture yet.")
        self.info.setFont(QFont(MONO, 9))
        self.info.setStyleSheet(f"color:{C_MUTED};")
        self.info.setWordWrap(True)
        v.addWidget(self.info)

        self.glw = pg.GraphicsLayoutWidget()
        self.p_dig = self.glw.addPlot(row=0, col=0)
        self.p_dig.showGrid(x=True, y=False, alpha=0.15)
        self.p_dig.setMouseEnabled(y=False)
        self.p_dig.getAxis("left").setWidth(44)
        self.p_an = self.glw.addPlot(row=1, col=0)
        self.p_an.showGrid(x=True, y=True, alpha=0.15)
        self.p_an.setLabel("left", "Volts", units="V")
        self.p_an.setLabel("bottom", "Time", units="s")
        self.p_an.getAxis("left").setWidth(44)
        self.p_an.setXLink(self.p_dig)
        self.an_legend = self.p_an.addLegend(offset=(-10, 10), brush=pg.mkBrush(20, 22, 26, 200))
        self.glw.ci.layout.setRowStretchFactor(0, 3)
        self.glw.ci.layout.setRowStretchFactor(1, 2)
        self.p_dig.sigXRangeChanged.connect(self._view_changed)
        self.region = pg.LinearRegionItem(brush=pg.mkBrush(255, 255, 255, 28))
        self.region.setZValue(10)
        self.region.sigRegionChanged.connect(self._view_changed)
        self.trig_lines = []
        self.dig_curves: dict[int, pg.PlotDataItem] = {}
        self.an_curves: dict[int, pg.PlotDataItem] = {}
        touch.plots(self.p_dig, self.p_an)
        v.addWidget(self.glw, 1)
        self.meas_title = QLabel("Measurements: view")
        self.meas_title.setStyleSheet(f"color:{C_MUTED};")
        v.addWidget(self.meas_title)
        self.dig_table = self._table(["Ch", "Frequency", "Period", "Duty", "High",
                                      "Low", "Edges"])
        v.addWidget(self.dig_table)
        self.an_table = self._table(["Ch", "Mean", "Min", "Max", "Pk-pk", "RMS"])
        v.addWidget(self.an_table)
        self._layout_plots(digital=True, analog=False)
        return w

    def _table(self, headers):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setFont(QFont(MONO, 11 if touch.TOUCH else 9))
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        return t

    def _build_table_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        bar = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter rows, e.g. 0x41 or error")
        self.filter_edit.textChanged.connect(self._fill_table)
        bar.addWidget(self.filter_edit, 1)
        save = QPushButton("Save…")
        save.setToolTip("Write the decoded table to CSV")
        save.clicked.connect(self.save_table)
        bar.addWidget(save)
        v.addLayout(bar)
        self.table_note = QLabel("Add a decoder (left) and capture, or press Decode.")
        self.table_note.setStyleSheet(f"color:{C_MUTED};")
        v.addWidget(self.table_note)
        self.table = QTableWidget(0, 0)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setFont(QFont(MONO, 11 if touch.TOUCH else 9))
        self.table.cellClicked.connect(self._row_clicked)
        touch.kinetic(self.table)
        v.addWidget(self.table, 1)
        hint = QLabel("Click a row to show it on the Waveform tab.")
        hint.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        v.addWidget(hint)
        return w

    # ================================================================ settings
    def _set_mode(self, mode: str):
        self.cfg.mode = mode
        for k, b in self.mode_btns.items():
            b.setChecked(k == mode)
        for k, ws in self._mode_rows.items():
            for wdg in ws:
                self._form.setRowVisible(wdg, k == mode)
        pulse = mode == "trigger" and str(self.trig_type.currentData()).startswith("pulse")
        self._form.setRowVisible(self.pulse_label, pulse)
        self._style_run()
        self.settings_changed.emit()

    def _trig_type_changed(self, *_):
        self._set_mode(self.cfg.mode)

    def _channels_changed(self, *_):
        self.settings_changed.emit()

    def _name_changed(self, text):
        self.title_changed.emit(self._title())
        self.settings_changed.emit()

    def _title(self):
        name = self.name_edit.text()
        return f"Logic · {name}" if name else "Logic"

    def collect(self) -> LogicConfig:
        """The capture settings shown in the panel."""
        trig = str(self.trig_type.currentData() or "rising")
        pmin, pmax = self.pulse_min.value() * 1e-6, self.pulse_max.value() * 1e-6
        return LogicConfig(
            digital=[k for k, c in enumerate(self.dig_chks) if c.isChecked()],
            analog=[k for k, c in enumerate(self.an_chks) if c.isChecked()],
            digital_rate=float(self.d_rate.currentData() or self.cfg.digital_rate),
            analog_rate=float(self.a_rate.currentData() or self.cfg.analog_rate),
            threshold=self.thresh.currentData() if self.thresh.count() else None,
            mode=self.cfg.mode,
            duration=self.duration.value(),
            trig_channel=int(self.trig_ch.currentData() or 0),
            trig_type=trig,
            pulse_min=pmin or None, pulse_max=pmax or None,
            after_trigger=self.after.value(), pre_trigger=self.pre.value(),
            decoders=[dict(d) for d in self.cfg.decoders])

    def show_config(self, c: LogicConfig):
        for k, chk in enumerate(self.dig_chks):
            chk.setChecked(k in c.digital)
        for k, chk in enumerate(self.an_chks):
            chk.setChecked(k in c.analog)
        for combo, val in ((self.d_rate, c.digital_rate), (self.a_rate, c.analog_rate),
                           (self.thresh, c.threshold)):
            i = combo.findData(val)
            if i >= 0:
                combo.setCurrentIndex(i)
        self.duration.setValue(c.duration)
        i = self.trig_ch.findData(c.trig_channel)
        self.trig_ch.setCurrentIndex(max(0, i))
        self.trig_type.setCurrentIndex(max(0, self.trig_type.findData(c.trig_type)))
        self.pulse_min.setValue((c.pulse_min or 0) * 1e6)
        self.pulse_max.setValue((c.pulse_max or 0) * 1e6)
        self.after.setValue(c.after_trigger)
        self.pre.setValue(c.pre_trigger)
        self.cfg.decoders = [dict(d) for d in c.decoders]
        self._fill_decoders()
        self._set_mode(c.mode if c.mode in MODES else "timed")

    def session_settings(self) -> dict:
        return {"name": self.name_edit.text(), "repeat": self.repeat_chk.isChecked(),
                "capture": asdict(self.collect()) if self.connected else
                (self._pending or {}).get("capture", asdict(self.cfg))}

    def restore_settings(self, s: dict):
        self.name_edit.setText(str(s.get("name", "")))
        self.repeat_chk.setChecked(bool(s.get("repeat", False)))
        self._pending = dict(s)

    @staticmethod
    def _config_from(d: dict) -> LogicConfig | None:
        if not isinstance(d, dict):
            return None
        names = {f.name for f in fields(LogicConfig)}
        try:
            return LogicConfig(**{k: v for k, v in d.items() if k in names})
        except TypeError:
            return None

    # ---- decoders
    def _fill_decoders(self):
        self.dec_list.clear()
        for d in self.cfg.decoders:
            self.dec_list.addItem(QListWidgetItem(describe_decoder(d)))

    def _add_decoder(self):
        dlg = DecoderDialog(self, max(1, len(self.dig_chks)))
        if dlg.exec() and dlg.result_dict:
            self.cfg.decoders.append(dlg.result_dict)
            self._decoders_changed()

    def _edit_decoder(self):
        i = self.dec_list.currentRow()
        if i < 0:
            return
        dlg = DecoderDialog(self, max(1, len(self.dig_chks)), self.cfg.decoders[i])
        if dlg.exec() and dlg.result_dict:
            self.cfg.decoders[i] = dlg.result_dict
            self._decoders_changed()

    def _remove_decoder(self):
        i = self.dec_list.currentRow()
        if i >= 0:
            del self.cfg.decoders[i]
            self._decoders_changed()

    def _decoders_changed(self):
        self._fill_decoders()
        self.settings_changed.emit()
        if self.capture is not None and not self._busy:
            self.decode_now()

    # ================================================================ actions
    def _run_clicked(self):
        if not self._busy:
            self.capture_now()
        elif self._busy_mode == "manual" and self._busy == "waiting":
            self.run_btn.setEnabled(False)
            self.worker.submit(lambda d: d.stop_capture())
        else:
            self.abort()

    def capture_now(self):
        if not self.connected or self._busy:
            return
        cfg = self.collect()
        if not cfg.digital and not cfg.analog:
            self.on_error("Turn on at least one channel")
            return
        if cfg.mode == "trigger" and cfg.trig_channel not in cfg.digital:
            self.on_error(f"Trigger channel D{cfg.trig_channel} isn't turned on")
            return
        self.cfg = cfg
        self._busy_mode = cfg.mode
        self._set_busy("waiting")
        self.worker.request_capture(cfg)

    def abort(self):
        self.repeat_chk.setChecked(False)
        if self._busy:
            self.worker.abort_capture()

    def output_off(self):
        """Esc: nothing to switch off, but stop a running capture loop."""
        self.abort()

    def decode_now(self):
        if not self.connected or self.capture is None or self._busy:
            return
        if not self.cfg.decoders:
            self.capture.table_header, self.capture.table_rows = [], []
            self._fill_table()
            return
        self.table_note.setText("Decoding…")
        self.worker.submit_result("decode", lambda d, dec: d.decode(dec),
                                  [dict(x) for x in self.cfg.decoders])

    def save_sal(self):
        if self.capture is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save capture", time.strftime("logic_%Y%m%d_%H%M%S.sal"),
            "Logic 2 capture (*.sal)")
        if path:
            self.worker.submit_result("save", lambda d, p: (d.save(p), p)[1], path)

    def export_csv(self):
        if self.capture is None:
            return
        path = QFileDialog.getExistingDirectory(self, "Folder for digital.csv / analog.csv")
        if path:
            self.worker.submit_result("csv", lambda d, p: (d.export_csv(p), p)[1], path)

    def save_table(self):
        if self.capture is None or not self.capture.table_header:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save decoded data", time.strftime("decoded_%Y%m%d_%H%M%S.csv"), "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(self.capture.table_header)
            w.writerows(self.capture.table_rows)
        self.message.emit(f"Saved {len(self.capture.table_rows)} rows to {path}")

    # ================================================================ worker slots
    def on_connected(self, family, caps: LogicCaps, state):
        self.caps = caps
        self.idn_label.setText(f"{caps.idn}\n{caps.note}")
        self.idn_label.setStyleSheet("color:#c9ccd1;")
        self._make_channel_boxes(caps.digital, caps.analog)
        for combo, rates in ((self.d_rate, caps.digital_rates), (self.a_rate, caps.analog_rates)):
            combo.blockSignals(True)
            combo.clear()
            for r in rates:
                combo.addItem(fmt_rate(r), float(r))
            combo.blockSignals(False)
        self.a_rate.setVisible(caps.analog > 0)
        self.a_rate_label.setVisible(caps.analog > 0)
        self.thresh.clear()
        for v in caps.thresholds:
            self.thresh.addItem(f"{v:g} V", v)
        self.thresh.setVisible(bool(caps.thresholds))
        self.thresh_label.setVisible(bool(caps.thresholds))
        self.title_changed.emit(self._title())

    def on_state(self, st: LogicConfig):
        if self._pending is not None:
            restored = self._config_from(self._pending.get("capture"))
            self._pending = None
            if restored:
                restored.digital = [c for c in restored.digital if c < self.caps.digital]
                restored.analog = [c for c in restored.analog if c < self.caps.analog]
                if restored.digital or restored.analog:
                    if restored.digital_rate not in self.caps.digital_rates:
                        restored.digital_rate = st.digital_rate
                    if restored.analog_rate not in self.caps.analog_rates:
                        restored.analog_rate = st.analog_rate
                    if restored.threshold not in self.caps.thresholds:
                        restored.threshold = st.threshold
                    st = restored
        self.show_config(st)

    def _on_capture_status(self, status: str):
        if status == "idle":
            self._set_busy("")
        elif status in ("waiting", "transferring"):
            self._set_busy(status)

    def on_error(self, text: str):
        super().on_error(text)
        if self._busy and not self.worker.capturing:
            self._set_busy("")              # arming failed
            self.repeat_chk.setChecked(False)
        if self.capture is not None and self.table_note.text() == "Decoding…":
            self.table_note.setText("Decoding failed; see the console.")

    def _on_capture(self, res: LogicCapture):
        res.t_app = now()
        self.capture = res
        for e in res.errors:
            self.on_error(e)
        self._show_capture(fit=True)
        self._fill_table()
        n = sum(d.t.size - 1 for d in res.digital.values())
        self.console(f"Capture: {len(res.digital)} digital, {len(res.analog)} analog channels, "
                     f"{n} edges, {len(res.table_rows)} decoded rows")
        if self.repeat_chk.isChecked():
            QTimer.singleShot(150, self.capture_now)

    def _on_job(self, tag: str, value):
        if tag == "decode" and self.capture is not None:
            self.capture.table_header, self.capture.table_rows = value
            self._fill_table()
        elif tag == "save":
            self.message.emit(f"Saved capture to {value}")
        elif tag == "csv":
            self.message.emit(f"Logic 2 exported CSV into {value}")

    # ================================================================ busy state
    def _set_busy(self, state: str):
        if state and not self._busy:
            self._busy_since = time.monotonic()
            self.busy_timer.start(200)
        self._busy = state
        if not state:
            self.busy_timer.stop()
            self.status_label.setText("")
        self._show_busy()
        self._style_run()

    def _show_busy(self):
        if not self._busy:
            return
        dt = time.monotonic() - self._busy_since
        if self._busy == "transferring":
            text = "Reading the data from Logic 2…"
        elif self._busy_mode == "trigger":
            text = f"Waiting for the trigger…  {dt:.1f} s"
        elif self._busy_mode == "manual":
            text = f"Recording…  {dt:.1f} s   (Stop to keep it)"
        else:
            text = f"Recording…  {dt:.1f} of {self.cfg.duration:g} s"
        self.status_label.setText(text)

    def _style_run(self):
        busy = bool(self._busy)
        self.run_btn.setEnabled(self.connected and self._busy != "transferring")
        if not busy:
            self.run_btn.setText("▶ Capture")
            self.run_btn.setStyleSheet("QPushButton { font-weight:bold; font-size:15px; }")
        elif self._busy_mode == "manual":
            self.run_btn.setText("■ Stop")
            self.run_btn.setStyleSheet(f"QPushButton {{ color:{C_WARN}; font-weight:bold; "
                                       "font-size:15px; }")
        else:
            self.run_btn.setText("✕ Abort")
            self.run_btn.setStyleSheet(f"QPushButton {{ color:{C_WARN}; font-weight:bold; "
                                       "font-size:15px; }")
        self.abort_btn.setVisible(busy and self._busy_mode == "manual")

    def set_enabled(self, on: bool):
        for cls in (QPushButton, QComboBox, QSpinBox, SafeSpin, QCheckBox, QListWidget):
            for w in self.findChildren(cls):
                w.setEnabled(on)
        for w in (self.trace_chk, self.region_chk, self.filter_edit):
            w.setEnabled(True)
        self._style_run()

    # PanelBase's trend machinery doesn't apply: there is no scrolling trend.
    def _maybe_redraw(self):
        pass

    # ================================================================ drawing
    def _layout_plots(self, digital: bool, analog: bool):
        self.p_dig.setVisible(digital or not analog)
        self.p_an.setVisible(analog)
        # one time axis: under the analog plot when there is one
        ax = self.p_dig.getAxis("bottom")
        ax.setStyle(showValues=not analog)
        self.p_dig.setLabel("bottom", "" if analog else "Time", units=None if analog else "s")
        self.an_table.setVisible(analog)
        self.dig_table.setVisible(digital)

    @staticmethod
    def _fit_table(t: QTableWidget, rows: int, most: int = 6):
        """Tall enough for its rows (scrolls beyond `most`)."""
        h = t.horizontalHeader().height() + 4
        rh = t.verticalHeader().defaultSectionSize()
        t.setFixedHeight(h + rh * max(1, min(rows, most)))

    def _show_capture(self, fit: bool):
        res = self.capture
        for c in list(self.dig_curves.values()):
            self.p_dig.removeItem(c)
        for c in list(self.an_curves.values()):
            self.p_an.removeItem(c)
        self.dig_curves, self.an_curves = {}, {}
        self.an_legend.clear()
        for p, ln in self.trig_lines:
            p.removeItem(ln)
        self.trig_lines = []
        self._layout_plots(bool(res.digital), bool(res.analog))
        small = touch.TOUCH          # 800 px tall: keep room for the lanes
        self._fit_table(self.dig_table, len(res.digital), (2 if small else 4) if res.analog
                        else (3 if small else 8))
        self._fit_table(self.an_table, len(res.analog), 2 if small else 4)
        chans = list(res.digital)
        ticks = []
        for lane, ch in enumerate(chans):
            y0 = -lane * LANE
            ticks.append((y0 + 0.5, f"D{ch}"))
            self.dig_curves[ch] = self.p_dig.plot(pen=pg.mkPen(COLORS[ch % len(COLORS)], width=1))
        self.p_dig.getAxis("left").setTicks([ticks])
        if chans:
            self.p_dig.setYRange(-(len(chans) - 1) * LANE - 0.3, 1.3, padding=0)
        for ch, tr in res.analog.items():
            c = self.p_an.plot(tr.t, tr.v, pen=pg.mkPen(COLORS[ch % len(COLORS)], width=1),
                               name=f"A{ch}")
            c.setClipToView(True)
            c.setDownsampling(auto=True, method="peak")
            self.an_curves[ch] = c
        if res.cfg.mode == "trigger":
            for p in (self.p_dig, self.p_an):
                ln = pg.InfiniteLine(0, angle=90, pen=pg.mkPen("#ffffff", width=1,
                                                               style=Qt.PenStyle.DashLine))
                p.addItem(ln)
                self.trig_lines.append((p, ln))
        a, b = res.span
        dur = b - a
        when = time.strftime("%H:%M:%S", time.localtime(res.wall))
        what = []
        if res.digital:
            what.append(f"D{','.join(map(str, res.digital))} at {fmt_rate(res.cfg.digital_rate)}")
        if res.analog:
            what.append(f"A{','.join(map(str, res.analog))} at {fmt_rate(res.cfg.analog_rate)}")
        trig = "   trigger at 0 s" if res.cfg.mode == "trigger" else ""
        self.info.setText(f"Captured {when} (app time {res.t_app:.1f} s)   "
                          f"{fmt_si(dur, 's')}   " + "   ".join(what) + trig)
        if fit:
            self.fit()
        else:
            self._redraw_view()

    def _view_range(self):
        (x0, x1), _ = self.p_dig.viewRange() if self.p_dig.isVisible() else self.p_an.viewRange()
        return x0, x1

    def _view_changed(self, *_):
        # Redraw at most every ~50 ms while dragging/zooming.
        if not self.view_timer.isActive():
            self.view_timer.start(50)

    def _redraw_view(self):
        res = self.capture
        if res is None:
            return
        x0, x1 = self._view_range()
        bins = max(200, int(self.p_dig.width() or 1000))
        for lane, (ch, tr) in enumerate(res.digital.items()):
            x, y = digital_envelope(tr, x0, x1, bins)
            self.dig_curves[ch].setData(x, y - lane * LANE)
        self._update_measurements()

    def _update_measurements(self):
        res = self.capture
        if res is None:
            return
        if self.region_chk.isChecked():
            a, b = self.region.getRegion()
            self.meas_title.setText(f"Measurements: region  {fmt_si(a, 's')} … {fmt_si(b, 's')}"
                                    f"   Δt {fmt_si(b - a, 's')}   1/Δt {fmt_si(1 / (b - a), 'Hz') if b > a else '—'}")
        else:
            a, b = self._view_range()
            self.meas_title.setText("Measurements: what's in view")
        self.dig_table.setRowCount(len(res.digital))
        for r, (ch, tr) in enumerate(res.digital.items()):
            s = digital_stats(tr, a, b)
            duty = "—" if s["duty"] is None else f"{s['duty'] * 100:.2f} %"
            hi, lo = _span(s["high_min"], s["high_max"]), _span(s["low_min"], s["low_max"])
            vals = [f"D{ch}", _si(s["freq"], "Hz", 6), _si(s["period"], "s"), duty, hi, lo,
                    str(s["edges"]) if s["edges"] else f"none ({'high' if s['level'] else 'low'})"]
            self._set_row(self.dig_table, r, vals, COLORS[ch % len(COLORS)])
        self.an_table.setRowCount(len(res.analog))
        for r, (ch, tr) in enumerate(res.analog.items()):
            s = analog_stats(tr, a, b) or {}
            vals = [f"A{ch}"] + [_si(s.get(k), "V") for k in ("mean", "min", "max", "pp", "rms")]
            self._set_row(self.an_table, r, vals, COLORS[ch % len(COLORS)])

    @staticmethod
    def _set_row(table, r, vals, color):
        for c, text in enumerate(vals):
            it = table.item(r, c)
            if it is None:
                it = QTableWidgetItem()
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                                    if c else Qt.AlignmentFlag.AlignCenter)
                table.setItem(r, c, it)
            it.setText(text)
            if c == 0:
                it.setForeground(QBrush(pg.mkColor(color)))

    # ---- view controls
    def fit(self):
        if self.capture is None:
            return
        a, b = self.capture.span
        if b > a:
            self.p_dig.setXRange(a, b, padding=0.01)
            if not self.p_dig.isVisible():
                self.p_an.setXRange(a, b, padding=0.01)
        self.p_an.enableAutoRange(axis="y")
        if self.region_chk.isChecked():
            w = b - a
            self.region.setRegion((a + 0.4 * w, a + 0.6 * w))
        self._redraw_view()

    def zoom(self, factor: float, center: float | None = None):
        x0, x1 = self._view_range()
        c = (x0 + x1) / 2 if center is None else center
        half = (x1 - x0) * factor / 2
        target = self.p_dig if self.p_dig.isVisible() else self.p_an
        target.setXRange(c - half, c + half, padding=0)

    def pan(self, frac: float):
        x0, x1 = self._view_range()
        d = (x1 - x0) * frac
        target = self.p_dig if self.p_dig.isVisible() else self.p_an
        target.setXRange(x0 + d, x1 + d, padding=0)

    def _region_toggled(self, on: bool):
        if on:
            x0, x1 = self._view_range()
            w = x1 - x0
            self.region.setRegion((x0 + 0.4 * w, x0 + 0.6 * w))
            (self.p_dig if self.p_dig.isVisible() else self.p_an).addItem(self.region)
        else:
            vb = self.region.getViewBox()
            if vb is not None:
                vb.removeItem(self.region)
        self._update_measurements()

    # ---- decoded table
    def _fill_table(self, *_):
        res = self.capture
        header = res.table_header if res else []
        rows = res.table_rows if res else []
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setColumnCount(len(header))
        self.table.setHorizontalHeaderLabels(header)
        needle = self.filter_edit.text().strip().lower()
        shown = [r for r in rows if needle in " ".join(r).lower()] if needle else rows
        self.table.setRowCount(min(len(shown), MAX_TABLE_ROWS))
        for i, row in enumerate(shown[:MAX_TABLE_ROWS]):
            for j, val in enumerate(row):
                self.table.setItem(i, j, QTableWidgetItem(val))
        self.table.resizeColumnsToContents()
        if res is None:
            return
        if not self.cfg.decoders:
            note = "Add a decoder (left) to decode this capture."
        elif not rows:
            note = "The decoders found nothing. Check their channels and bit rate."
        else:
            note = f"{len(rows)} rows" + (f", {len(shown)} match" if needle else "")
            if len(shown) > MAX_TABLE_ROWS:
                note += f" (showing the first {MAX_TABLE_ROWS}; Save… writes all of them)"
        self.table_note.setText(note)

    def _row_clicked(self, r: int, _c: int):
        res = self.capture
        if res is None or "start_time" not in res.table_header:
            return
        try:
            t = float(self.table.item(r, res.table_header.index("start_time")).text())
            dur = 0.0
            if "duration" in res.table_header:
                dur = float(self.table.item(r, res.table_header.index("duration")).text())
        except (AttributeError, ValueError):
            return
        width = max(dur * 6, 20e-6)
        target = self.p_dig if self.p_dig.isVisible() else self.p_an
        target.setXRange(t + dur / 2 - width / 2, t + dur / 2 + width / 2, padding=0)
        self.tabs.setCurrentIndex(0)
