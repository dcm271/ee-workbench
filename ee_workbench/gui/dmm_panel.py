"""Tab for a digital multimeter (HP 3456A)."""
from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QFrame, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QScrollArea, QSpinBox, QSplitter, QTabWidget, QVBoxLayout,
                             QWidget)

from ..instruments.base import DmmConfig, DmmReading
from ..instruments.hp3456a import FUNCTIONS, NPLC_CHOICES, ranges_for
from . import touch
from .panel_base import PanelBase
from .widgets import C_DMM, C_MUTED, C_WARN, MONO, Readout, TraceBuffer, fmt_fixed

NPLC_LABELS = {0.01: "0.01 PLC  (fastest, ~4½ digits)", 0.1: "0.1 PLC  (~5½ digits)",
               1: "1 PLC  (~6½ digits)", 10: "10 PLC  (low noise)",
               100: "100 PLC  (slowest, lowest noise)"}
NPLC_DIGITS = {0.01: 5, 0.1: 6, 1: 7, 10: 7, 100: 7}


def _match(value, options):
    """Case/space-insensitive lookup so config.toml can say "2w ohm" or "100mv"."""
    def key(x):
        return str(x).lower().replace(" ", "").replace("ω", "ohm").replace("ohms", "ohm")
    want = key(value) if value is not None else None
    return next((o for o in options if key(o) == want), None)


class DmmPanel(PanelBase):
    def __init__(self, resource: str, model: str, baud: int):
        super().__init__(resource, model, baud)
        self.cfg = DmmConfig()
        self._pending: dict | None = None   # settings to apply once connected
        self.buf = TraceBuffer(2)   # t, value
        self.unit = "V"

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        console = self.build_console_tab(
            "3456A program codes, e.g.  F1 R4 T3   (Send + read fetches one reading)",
            read_button=True)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        tabs = QTabWidget()
        tabs.addTab(self._build_trend_tab(), "Trend")
        if not touch.TOUCH:
            lay.addWidget(self._build_idn())
        g = self._build_reading_group()
        g.layout().insertWidget(0, self._build_name_edit())
        lay.addWidget(g)
        if touch.TOUCH:
            # Touch: big reading + statistics on the left; configuration in a tab.
            setup = QWidget()
            sv = QVBoxLayout(setup)
            sv.addWidget(self._build_idn())
            sv.addWidget(self._build_config_group())
            sv.addStretch(1)
            tabs.addTab(touch.scroll(setup), "Setup")
        else:
            lay.addWidget(self._build_config_group())
        lay.addStretch(1)
        sa = touch.scroll(inner)
        sa.setMinimumWidth(420 if touch.TOUCH else 400)
        split.addWidget(sa)
        tabs.addTab(console, "Console")
        split.addWidget(tabs)
        split.setStretchFactor(1, 1)
        split.setSizes([440, 840] if touch.TOUCH else [420, 1000])
        root.addWidget(split)
        self.set_enabled(False)
        self.start()

    def _build_idn(self):
        self.idn_label = QLabel(f"Connecting to {self.resource}…")
        self.idn_label.setWordWrap(True)
        self.idn_label.setStyleSheet(f"color:{C_MUTED};")
        return self.idn_label

    def _build_name_edit(self):
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Channel name, e.g. “E3610A output”")
        self.name_edit.textChanged.connect(self._name_changed)
        return self.name_edit

    def _build_reading_group(self):
        g = QGroupBox("Reading")
        gl = QVBoxLayout(g)
        self.readout = Readout("DCV", C_DMM, 34 if touch.TOUCH else 28)
        gl.addWidget(self.readout)
        self.ovld_label = QLabel("")
        self.ovld_label.setStyleSheet(f"color:{C_WARN}; font-weight:bold;")
        gl.addWidget(self.ovld_label)
        stats = QGridLayout()
        self.stat_labels = {}
        for n, key in enumerate(("Mean", "Std dev", "Min", "Max", "Pk-pk", "Count")):
            cap = QLabel(key)
            cap.setStyleSheet(f"color:{C_MUTED};")
            val = QLabel("—")
            val.setFont(QFont(MONO, 12 if touch.TOUCH else 10))
            val.setAlignment(Qt.AlignmentFlag.AlignRight)
            stats.addWidget(cap, n // 2, (n % 2) * 2)
            stats.addWidget(val, n // 2, (n % 2) * 2 + 1)
            self.stat_labels[key] = val
        stats.setColumnStretch(1, 1)
        stats.setColumnStretch(3, 1)
        stats.setHorizontalSpacing(12)
        gl.addLayout(stats)
        hint = QLabel("Statistics cover the trend span shown.")
        hint.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        gl.addWidget(hint)
        self.null_btn = QPushButton("Null")
        self.null_btn.setCheckable(True)
        self.null_btn.setToolTip("Math NULL (M3): subtract the next reading from all later ones. "
                                 "E.g. short the leads, then press Null.")
        self.null_btn.clicked.connect(self._apply)
        gl.addWidget(self.null_btn)
        return g

    def _build_config_group(self):
        g = QGroupBox("Configuration")
        form = QFormLayout(g)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.func_combo = QComboBox()
        self.func_combo.addItems(list(FUNCTIONS))
        self.func_combo.activated.connect(self._func_changed)
        form.addRow("Function", self.func_combo)
        self.range_combo = QComboBox()
        self.range_combo.activated.connect(self._apply)
        form.addRow("Range", self.range_combo)
        self.nplc_combo = QComboBox()
        for n in NPLC_CHOICES:
            self.nplc_combo.addItem(NPLC_LABELS[n], n)
        self.nplc_combo.activated.connect(self._apply)
        self.nplc_combo.setToolTip("Integration time in power-line cycles. Longer = less "
                                   "noise and more digits, but slower.")
        form.addRow("Integration", self.nplc_combo)
        self.az_chk = QCheckBox("Autozero")
        self.az_chk.setToolTip("Measure the zero offset on every reading (halves the speed).")
        self.az_chk.clicked.connect(self._apply)
        self.filt_chk = QCheckBox("AC filter")
        self.filt_chk.setToolTip("3-pole analog filter for noisy DC signals (slower settling).")
        self.filt_chk.clicked.connect(self._apply)
        row = QHBoxLayout()
        row.addWidget(self.az_chk)
        row.addWidget(self.filt_chk)
        form.addRow("", row)
        self.poll_ms = QSpinBox()
        self.poll_ms.setRange(0, 3_600_000)
        self.poll_ms.setSingleStep(100)
        self.poll_ms.setSuffix(" ms")
        self.poll_ms.setSpecialValueText("none (as fast as possible)")
        self.poll_ms.valueChanged.connect(
            lambda v: setattr(self.worker, "interval_s", v / 1000))
        touch.attach_keypad(self.poll_ms, "Poll delay")
        form.addRow("Poll delay", self.poll_ms)
        form.addRow(self.build_pause_button())
        return g

    def _build_trend_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addLayout(self.build_trend_bar())
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.setClipToView(True)
        self.plot.setDownsampling(auto=True, mode="peak")
        self.curve = self.plot.plot(pen=pg.mkPen(C_DMM, width=1.5))
        touch.plots(self.plot)
        v.addWidget(self.plot, 1)
        return w

    # ---- config ------------------------------------------------------------
    def _func_changed(self):
        self._fill_ranges(self.func_combo.currentText(), "AUTO")
        self._apply()

    def _fill_ranges(self, function: str, current: str):
        self.range_combo.clear()
        self.range_combo.addItems(list(ranges_for(function)))
        self.range_combo.setCurrentText(current)

    def _apply(self):
        cfg = DmmConfig(function=self.func_combo.currentText(),
                        range=self.range_combo.currentText(),
                        nplc=self.nplc_combo.currentData(),
                        autozero=self.az_chk.isChecked(),
                        filter=self.filt_chk.isChecked(),
                        null=self.null_btn.isChecked())
        if cfg.function != self.cfg.function or cfg.null != self.cfg.null:
            self.clear_trend()   # mixing units / offsets in one trace is meaningless
        self.cmd(lambda d, c: d.apply(c), cfg)

    def _name_changed(self, text):
        self.title_changed.emit(f"3456A · {text}" if text else "3456A")
        self._update_axis()
        self.settings_changed.emit()

    # ---- remembered settings ---------------------------------------------
    def session_settings(self) -> dict:
        if self._pending is not None and not self.connected:
            return dict(self._pending)    # never connected: keep what we were given
        c = self.cfg
        return {"name": self.name_edit.text(), "function": c.function, "range": c.range,
                "nplc": c.nplc, "autozero": c.autozero, "filter": c.filter}

    def restore_settings(self, settings: dict):
        self._pending = dict(settings)
        self.name_edit.setText(str(settings.get("name", "")))

    def _apply_pending(self):
        s, self._pending = self._pending, None
        if not s:
            return
        func = _match(s.get("function"), FUNCTIONS) or self.cfg.function
        rng = _match(s.get("range"), ranges_for(func)) or "AUTO"
        try:
            nplc = float(s.get("nplc", self.cfg.nplc))
        except (TypeError, ValueError):
            nplc = self.cfg.nplc
        nplc = min(NPLC_CHOICES, key=lambda n: abs(n - nplc))
        cfg = DmmConfig(function=func, range=rng, nplc=nplc,
                        autozero=bool(s.get("autozero", self.cfg.autozero)),
                        filter=bool(s.get("filter", self.cfg.filter)), null=False)
        if cfg != self.cfg:
            self.cmd(lambda d, c: d.apply(c), cfg)

    def _update_axis(self):
        name = self.name_edit.text() or self.cfg.function
        self.plot.setLabel("left", name, units=self.unit or None, color=C_DMM)
        self.readout.caption.setText(
            (self.cfg.function + (" · NULL" if self.cfg.null else "")).upper())

    # ---- slots -------------------------------------------------------------
    def on_connected(self, family, caps, cfg):
        self.idn_label.setText(f"{caps.idn}\n{caps.note}")
        self.idn_label.setStyleSheet("color:#c9ccd1;")
        self.title_changed.emit(f"3456A · {self.name_edit.text()}" if self.name_edit.text()
                                else "3456A")
        self._apply_pending()

    def on_state(self, cfg: DmmConfig):
        self.cfg = cfg
        self.func_combo.setCurrentText(cfg.function)
        self._fill_ranges(cfg.function, cfg.range)
        self.nplc_combo.setCurrentIndex(self.nplc_combo.findData(cfg.nplc))
        self.az_chk.setChecked(cfg.autozero)
        self.filt_chk.setChecked(cfg.filter)
        self.null_btn.setChecked(cfg.null)
        self.unit = FUNCTIONS[cfg.function][1]
        self._update_axis()
        self.settings_changed.emit()

    def on_reading(self, r: DmmReading):
        sig = NPLC_DIGITS.get(self.cfg.nplc, 7)
        if r.overload:
            self.readout.set("OVLD")
            self.ovld_label.setText("Overload: input exceeds the range")
            value = None
        else:
            self.readout.set(fmt_fixed(r.value, r.unit, sig) if r.unit else f"{r.value:+.{sig}g}")
            self.ovld_label.setText("")
            value = r.value
        self.buf.append([r.t, value])
        if self.recorder.active:
            self.recorder.row([time.strftime("%Y-%m-%dT%H:%M:%S"), f"{r.t:.3f}",
                               self.name_edit.text(), self.cfg.function, value, r.unit,
                               int(r.overload)])

    def csv_header(self):
        return ["timestamp", "t_s", "channel", "function", "value", "unit", "overload"]

    def csv_name(self):
        return "3456A_%Y%m%d_%H%M%S.csv"

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
        t, y = d[start:].T
        self.curve.setData(t, y, connect="finite")
        if span:
            self.plot.setXRange(max(0, t[-1] - span), t[-1], padding=0)
        else:
            self.plot.enableAutoRange(axis="x")
        y = y[np.isfinite(y)]
        if y.size:
            sig = NPLC_DIGITS.get(self.cfg.nplc, 7)
            f = (lambda x: fmt_fixed(x, self.unit, sig)) if self.unit else (lambda x: f"{x:+.{sig}g}")
            self.stat_labels["Mean"].setText(f(y.mean()))
            self.stat_labels["Std dev"].setText(f(y.std()) if y.size > 1 else "—")
            self.stat_labels["Min"].setText(f(y.min()))
            self.stat_labels["Max"].setText(f(y.max()))
            self.stat_labels["Pk-pk"].setText(f(y.max() - y.min()))
            self.stat_labels["Count"].setText(str(y.size))

    def set_enabled(self, on: bool):
        for cls in (QPushButton, QComboBox, QSpinBox, QCheckBox):
            for w in self.findChildren(cls):
                w.setEnabled(on)
        for w in (self.span_combo, self.freeze_chk, self.trace_chk):
            w.setEnabled(True)
