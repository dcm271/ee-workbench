"""Tab for GPIO pins (the Raspberry Pi's 40-pin header): set each pin to
Output or Input, name it ("relay", "reset", "done"), switch outputs, watch
inputs, and see every used pin on a trend. Sequences and scripts use the
names: Set GPIO relay on, Wait for GPIO done 1, bench.gpio("GPIO", "reset", "pulse 0.1").
"""
from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QPushButton, QSplitter,
                             QTableWidget, QTabWidget, QVBoxLayout, QWidget)

from ..instruments.gpio import HEADER, MODES, PULLS, GpioReading, GpioState, PinConfig
from . import touch
from .panel_base import PanelBase
from .theme import set_role
from .widgets import C_MUTED, SafeSpin, TraceBuffer

COLORS = ["#ffb347", "#7ee787", "#c39bff", "#ef9a9a", "#4fc3f7", "#fff176", "#80cbc4",
          "#f48fb1", "#aed581", "#90caf9"]
NOTES = {2: "I²C SDA (1.8 kΩ pull-up on the board)", 3: "I²C SCL (1.8 kΩ pull-up on the board)",
         14: "UART TX (serial console if enabled)", 15: "UART RX (serial console if enabled)",
         4: "1-Wire default", 18: "PWM0 / PCM CLK", 12: "PWM0", 13: "PWM1", 19: "PWM1",
         7: "SPI0 CE1", 8: "SPI0 CE0", 9: "SPI0 MISO", 10: "SPI0 MOSI", 11: "SPI0 SCLK"}
MODE_TEXT = {"off": "Off", "out": "Output", "in": "Input"}
COLS = ["GPIO", "Pin", "Name", "Mode", "Pull", "Active low", "State", ""]


class GpioPanel(PanelBase):
    KIND = "gpio"

    def __init__(self, resource: str, model: str, baud: int):
        super().__init__(resource, model, baud)
        self.state = GpioState()
        self.lines: list[int] = []
        self._pending: dict | None = None
        self._rows: dict[int, dict] = {}       # gpio -> widgets of its row
        self.buf: TraceBuffer | None = None
        self.worker.interval_s = 0.05

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        console = self.build_console_tab("(the GPIO tab has no command console; this is its log)")
        self.raw_line.setEnabled(False)
        split = QSplitter(Qt.Orientation.Vertical if touch.TOUCH else Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        left = QWidget()
        lay = QVBoxLayout(left)
        lay.setContentsMargins(0, 0, 6, 0)
        self.idn_label = QLabel(f"Connecting to {self.resource}…")
        self.idn_label.setWordWrap(True)
        self.idn_label.setStyleSheet(f"color:{C_MUTED};")
        lay.addWidget(self.idn_label)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Name for this tab, e.g. “jig”")
        self.name_edit.textChanged.connect(self._name_changed)
        lay.addWidget(self.name_edit)
        row = QHBoxLayout()
        self.used_only = QCheckBox("Only pins in use")
        self.used_only.toggled.connect(self._filter_rows)
        row.addWidget(self.used_only)
        row.addStretch(1)
        row.addWidget(QLabel("Pulse"))
        self.pulse_len = SafeSpin(3, 0.01, " s", track=False)
        self.pulse_len.setRange(0.001, 30.0)
        self.pulse_len.setValue(0.1)
        touch.attach_keypad(self.pulse_len, "Pulse length (s)")
        row.addWidget(self.pulse_len)
        off = QPushButton("All outputs off")
        off.clicked.connect(self.output_off)
        row.addWidget(off)
        lay.addLayout(row)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44 if touch.TOUCH else 32)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        lay.addWidget(self.table, 1)
        hint = QLabel("3.3 V logic: never connect 5 V to a pin. An output can drive a few mA "
                      "(a relay or LED needs a transistor or a relay board). Most relay boards "
                      "switch on with a low input: tick Active low so “on” means on. "
                      "Pins are released (back to inputs) when this tab closes.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        lay.addWidget(hint)
        split.addWidget(left)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_trend_tab(), "Levels")
        self.tabs.addTab(console, "Log")
        split.addWidget(self.tabs)
        split.setStretchFactor(1, 1)
        split.setSizes([520, 280] if touch.TOUCH else [640, 760])
        root.addWidget(split)
        self.set_enabled(False)
        self.start()

    def _build_trend_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addLayout(self.build_trend_bar())
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=False, alpha=0.15)
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.setMouseEnabled(y=False)
        self.curves: dict[int, pg.PlotDataItem] = {}
        touch.plots(self.plot)
        self.scrolling_trend(self.plot)
        v.addWidget(self.plot, 1)
        return w

    # ---------------------------------------------------------------- table
    def _build_rows(self):
        self.table.setRowCount(len(self.lines))
        for r, g in enumerate(self.lines):
            p = self.state.pins.get(g, PinConfig(g))
            gl = QLabel(f"  GPIO{g}")
            if g in NOTES:
                gl.setToolTip(NOTES[g])
                gl.setText(f"  GPIO{g} ℹ")
            self.table.setCellWidget(r, 0, gl)
            pin = QLabel(f"  {HEADER.get(g, '')}")
            pin.setStyleSheet(f"color:{C_MUTED};")
            self.table.setCellWidget(r, 1, pin)
            name = QLineEdit(p.name)
            name.setPlaceholderText(f"GPIO{g}")
            name.editingFinished.connect(self._apply)
            self.table.setCellWidget(r, 2, name)
            mode = QComboBox()
            for m in MODES:
                mode.addItem(MODE_TEXT[m], m)
            mode.setCurrentIndex(MODES.index(p.mode))
            mode.activated.connect(self._apply)
            self.table.setCellWidget(r, 3, mode)
            pull = QComboBox()
            pull.addItems(list(PULLS))
            pull.setCurrentText(p.pull)
            pull.activated.connect(self._apply)
            self.table.setCellWidget(r, 4, pull)
            al = QCheckBox()
            al.setChecked(p.active_low)
            al.toggled.connect(self._apply)
            self.table.setCellWidget(r, 5, al)
            st = QPushButton("—")
            st.setCheckable(True)
            st.setMinimumWidth(70)
            st.clicked.connect(lambda on, g=g: self._toggle(g, on))
            self.table.setCellWidget(r, 6, st)
            pulse = QPushButton("Pulse")
            pulse.clicked.connect(lambda _, g=g: self._pulse(g))
            self.table.setCellWidget(r, 7, pulse)
            self._rows[g] = {"row": r, "name": name, "mode": mode, "pull": pull, "al": al,
                             "state": st, "pulse": pulse}
        self._refresh_rows()

    def _pins_from_table(self) -> list[PinConfig]:
        out = []
        for g, w in self._rows.items():
            out.append(PinConfig(g, w["mode"].currentData(), w["name"].text().strip(),
                                 w["pull"].currentText(), w["al"].isChecked()))
        return out

    def _apply(self, *_):
        names = [p.name.lower() for p in self._pins_from_table() if p.name]
        if len(names) != len(set(names)):
            self.on_error("Two pins have the same name; names must be unique")
            return
        self.cmd(lambda d, pins: d.configure(pins), self._pins_from_table())
        self.settings_changed.emit()

    def _toggle(self, g, on):
        self.cmd(lambda d: d.set_pin(g, on))

    def _pulse(self, g):
        secs = self.pulse_len.value()
        self.cmd(lambda d: d.pulse(g, secs))

    def output_off(self):
        if self.connected:
            self.cmd(lambda d: d.set_output(False))

    def _refresh_rows(self):
        for g, w in self._rows.items():
            p = self.state.pins.get(g, PinConfig(g))
            mode = p.mode
            w["pull"].setEnabled(self.connected and mode == "in")
            w["al"].setEnabled(self.connected and mode != "off")
            w["state"].setEnabled(self.connected and mode == "out")
            w["pulse"].setEnabled(self.connected and mode == "out")
            if mode == "off":
                w["state"].setText("—")
                w["state"].setChecked(False)
                set_role(w["state"], "")
        self._filter_rows()

    def _filter_rows(self, *_):
        only = self.used_only.isChecked()
        for g, w in self._rows.items():
            p = self.state.pins.get(g, PinConfig(g))
            self.table.setRowHidden(w["row"], only and p.mode == "off")

    # ---------------------------------------------------------------- settings
    def _name_changed(self, text):
        self.title_changed.emit(f"GPIO · {text}" if text else "GPIO")
        self.settings_changed.emit()

    def session_settings(self) -> dict:
        if self._pending is not None and not self.connected:
            return dict(self._pending)
        pins = [vars(p) for p in self.state.pins.values() if p.mode != "off" or p.name]
        return {"name": self.name_edit.text(), "pins": pins,
                "used_only": self.used_only.isChecked()}

    def restore_settings(self, s: dict):
        self._pending = dict(s)
        self.name_edit.setText(str(s.get("name", "")))
        self.used_only.setChecked(bool(s.get("used_only", False)))

    def base_name(self) -> str:
        t = self.name_edit.text().strip()
        return f"GPIO · {t}" if t else "GPIO"

    # ---------------------------------------------------------------- slots
    def on_connected(self, family, caps, state):
        self.idn_label.setText(f"{caps.idn}\n{caps.note}")
        self.idn_label.setStyleSheet("color:#c9ccd1;")
        self.title_changed.emit(self.display_name())
        self.lines = list(caps.lines)
        self.state = state
        self.buf = TraceBuffer(1 + len(self.lines))
        self._build_rows()
        s, self._pending = self._pending, None
        if s and s.get("pins"):
            pins = []
            for d in s["pins"]:
                try:
                    p = PinConfig(**{k: d[k] for k in ("gpio", "mode", "name", "pull",
                                                       "active_low") if k in d})
                except TypeError:
                    continue
                if p.gpio in self.lines and p.mode in MODES and p.pull in PULLS:
                    pins.append(p)
            if pins:
                self.cmd(lambda d, x: d.configure(x), pins)

    def on_state(self, st: GpioState):
        self.state = st
        for g, w in self._rows.items():
            p = st.pins.get(g, PinConfig(g))
            for key, setter in (("name", lambda x, p=p: x.setText(p.name) if not x.hasFocus()
                                  else None),
                                ("mode", lambda x, p=p: x.setCurrentIndex(MODES.index(p.mode))),
                                ("pull", lambda x, p=p: x.setCurrentText(p.pull)),
                                ("al", lambda x, p=p: x.setChecked(p.active_low))):
                w[key].blockSignals(True)
                setter(w[key])
                w[key].blockSignals(False)
            if p.mode == "out":
                on = bool(st.outputs.get(g, False))
                w["state"].setChecked(on)
                w["state"].setText("ON" if on else "off")
                set_role(w["state"], "primary" if on else "")
        self._refresh_rows()
        self._rebuild_curves()
        self.settings_changed.emit()

    def quantities(self) -> list[str]:
        return [f"gpio{g}" for g in self._used()]

    def _used(self) -> list[int]:
        return [g for g in self.lines if self.state.pins.get(g, PinConfig(g)).mode != "off"]

    def _rebuild_curves(self):
        used = self._used()
        for g in list(self.curves):
            if g not in used:
                self.plot.removeItem(self.curves.pop(g))
        ticks = []
        for k, g in enumerate(used):
            if g not in self.curves:
                self.curves[g] = self.plot.plot(pen=pg.mkPen(COLORS[g % len(COLORS)], width=1.5),
                                                stepMode="right")
            ticks.append((-k * 1.5 + 0.5, self.state.pins[g].label))
        self.plot.getAxis("left").setTicks([ticks])
        self.plot.setYRange(-(max(len(used), 1) - 1) * 1.5 - 0.5, 1.5, padding=0)
        self._dirty = True

    def on_reading(self, r: GpioReading):
        for g, lvl in r.levels.items():
            w = self._rows.get(g)
            if w is None:
                continue
            p = self.state.pins.get(g)
            if p is not None and p.mode == "in":
                w["state"].setText("HIGH" if lvl else "low")
                set_role(w["state"], "primary" if lvl else "")
        if self.buf is not None:
            self.buf.append([r.t] + [float(r.levels[g]) if g in r.levels else np.nan
                                     for g in self.lines])
        if self.recording and r.levels:
            used = self._used()
            self.record([time.strftime("%Y-%m-%dT%H:%M:%S"), f"{r.t:.3f}"]
                        + [r.levels.get(g, "") for g in used])

    def csv_header(self):
        return ["timestamp", "t_s"] + [self.state.pins[g].label for g in self._used()]

    def csv_name(self):
        return "GPIO_%Y%m%d_%H%M%S.csv"

    def clear_trend(self):
        if self.buf is not None:
            self.buf.clear()
        super().clear_trend()

    def redraw(self):
        if self.buf is None:
            return
        d = self.buf.view()
        if not len(d):
            return
        span = self.span()
        start = int(np.searchsorted(d[:, 0], d[-1, 0] - span)) if span else 0
        d = d[max(0, start - 1):]
        for k, g in enumerate(self._used()):
            c = self.curves.get(g)
            if c is None:
                continue
            y = d[:, 1 + self.lines.index(g)]
            c.setData(d[:, 0], y - k * 1.5, connect="finite")

    def set_enabled(self, on: bool):
        for cls in (QPushButton, QComboBox, QCheckBox, QLineEdit):
            for w in self.findChildren(cls):
                w.setEnabled(on)
        for w in (self.span_combo, self.freeze_chk, self.trace_chk, self.used_only):
            w.setEnabled(True)
        self.raw_line.setEnabled(False)
        if on:
            self._refresh_rows()
