"""Tab for an instrument described by a generic SCPI definition (a meter, an
electronic load, a multi-output supply, …): its readings, its settings, a
trend of every reading, and the console. Everything on it comes from the
definition file, so a new instrument needs no new code."""
from __future__ import annotations

import math
import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from ..instruments.scpi_generic import Definition, ScpiReading, ScpiState, definition_for
from . import touch
from .panel_base import PanelBase
from .theme import set_role
from .widgets import C_MUTED, Readout, TraceBuffer, fmt_fixed

COLORS = ["#4fc3f7", "#ffb74d", "#81c784", "#e57373", "#ba68c8", "#fff176"]
KIND_COLOR = {"meter": "#4fc3f7", "load": "#ffb74d", "supply": "#81c784"}


def fmt_value(v: float | None, unit: str, sig: int = 6) -> str:
    """Meter-style: `sig` significant digits, sign always shown, so the
    readout keeps its width (a supply's 0 V output reads +0.0000 mV, not
    a noisy 363.55797 µV)."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "OVLD"
    return fmt_fixed(v, unit, sig) if unit else f"{v:+.{sig - 1}g}"


class ScpiPanel(PanelBase):
    KIND = "scpi"

    def __init__(self, resource: str, model: str, baud: int):
        super().__init__(resource, model, baud)
        self.d: Definition | None = definition_for(model, resource)
        self.values: dict = {}
        self._pending: dict | None = None
        self._widgets: dict = {}           # setting name -> widget
        self._readouts: dict = {}          # reading name -> Readout
        self._units: dict = {}
        self.names = [r.name for r in self.d.readings] if self.d else []
        self.buf = TraceBuffer(1 + len(self.names))

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        console = self.build_console_tab("SCPI command, e.g.  *IDN?   (Enter on a query reads "
                                         "the answer)", read_button=True)
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
        self.name_edit.setPlaceholderText("Tab name, e.g. “DUT load”")
        self.name_edit.textChanged.connect(self._name_changed)
        lay.addWidget(self.name_edit)
        if self.d is None:
            lay.addWidget(QLabel(f"No SCPI definition '{model}'"))
        else:
            lay.addWidget(self._build_readings())
            for g in self._build_settings():
                lay.addWidget(g)
        lay.addWidget(self._build_poll_group())
        lay.addStretch(1)
        sa = touch.scroll(inner)
        sa.setMinimumWidth(420 if touch.TOUCH else 380)
        split.addWidget(sa)
        tabs = QTabWidget()
        tabs.addTab(self._build_trend_tab(), "Trend")
        tabs.addTab(console, "Console")
        split.addWidget(tabs)
        split.setStretchFactor(1, 1)
        split.setSizes([460, 820] if touch.TOUCH else [440, 980])
        root.addWidget(split)
        self.set_enabled(False)
        self.start()

    # ---- building ------------------------------------------------------------------
    def _groups(self, items):
        """[(channel, [items])]: channel 0 = not per channel."""
        out: dict = {}
        for x in items:
            out.setdefault(x.ch if self.d.channels > 1 else 0, []).append(x)
        return sorted(out.items())

    def _build_readings(self):
        g = QGroupBox("Readings")
        grid = QGridLayout(g)
        color = KIND_COLOR.get(self.d.type, "#4fc3f7")
        size = 22
        if self.d.channels > 1 or len(self.d.readings) > 1:
            size = 16                      # two readouts side by side must fit
        row = 0
        for ch, rs in self._groups(self.d.readings):
            if ch:
                lab = QLabel(f"CH{ch}")
                lab.setStyleSheet(f"color:{C_MUTED}; font-weight:bold;")
                grid.addWidget(lab, row, 0, 1, 2)
                row += 1
            for k, r in enumerate(rs):
                base = r.label.removesuffix(f" CH{ch}") if ch else r.label
                ro = Readout(base, COLORS[k % len(COLORS)] if len(rs) > 1 else color,
                             size if len(rs) > 1 else size + 6)
                self._readouts[r.name] = ro
                grid.addWidget(ro, row + k // 2, k % 2)
            row += (len(rs) + 1) // 2
        return g

    def _build_settings(self) -> list:
        boxes = []
        outs = [s for s in self.d.settings if s.output]
        if outs:
            g = QGroupBox({"supply": "Outputs", "load": "Input"}.get(self.d.type, "Output"))
            h = QGridLayout(g)
            for k, s in enumerate(outs):
                b = QPushButton(self._out_text(s, False))
                b.setCheckable(True)
                b.setMinimumHeight(44 if touch.TOUCH else 32)
                b.clicked.connect(lambda on, n=s.name: self._set(n, on))
                self._widgets[s.name] = b
                h.addWidget(b, k // 3, k % 3)
            boxes.append(g)
        rest = [s for s in self.d.settings if not s.output]
        for ch, ss in self._groups(rest):
            g = QGroupBox(f"CH{ch} settings" if ch else "Settings")
            form = QFormLayout(g)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
            for s in ss:
                label = s.label.removesuffix(f" CH{ch}") if ch else s.label
                w = self._setting_widget(s)
                self._widgets[s.name] = w
                form.addRow(label, w)
            boxes.append(g)
        return boxes

    def _out_text(self, s, on: bool) -> str:
        # "Output CH2" -> "CH2" when there are several: three buttons fit a row
        label = s.label.replace("Output ", "") if self.d.channels > 1 and s.ch else s.label
        return f"{label}: {'ON' if on else 'OFF'}"

    def _sig(self) -> int:
        return 5 if self.d is not None and self.d.type in ("supply", "load") else 6

    def _setting_widget(self, s):
        if s.kind == "choice":
            w = QComboBox()
            for o in s.options:
                w.addItem(o.label)
            w.activated.connect(lambda _i, n=s.name, w=w: self._set(n, w.currentText()))
            return w
        if s.kind == "bool":
            w = QCheckBox()
            w.clicked.connect(lambda on, n=s.name: self._set(n, on))
            return w
        if s.values:
            w = QComboBox()
            for v in s.values:
                w.addItem(f"{v:g} {s.unit}".strip(), v)
            w.activated.connect(lambda _i, n=s.name, w=w: self._set(n, w.currentData()))
            return w
        w = QDoubleSpinBox()
        lo = s.min if s.min is not None else -1e9
        hi = s.max if s.max is not None else 1e9
        w.setRange(lo, hi)
        w.setDecimals(4)
        w.setKeyboardTracking(False)
        if s.unit:
            w.setSuffix(" " + s.unit)
        w.setSingleStep(0.1 if (hi - lo) < 1000 else 1.0)
        w.editingFinished.connect(lambda n=s.name, w=w: self._set(n, w.value()))
        touch.attach_keypad(w, s.label, lambda n=s.name, w=w: self._set(n, w.value()))
        return w

    def _build_poll_group(self):
        g = QGroupBox("Polling")
        form = QFormLayout(g)
        self.poll_ms = QSpinBox()
        self.poll_ms.setRange(0, 3_600_000)
        self.poll_ms.setSingleStep(100)
        self.poll_ms.setSuffix(" ms")
        self.poll_ms.setSpecialValueText("none (as fast as possible)")
        self.poll_ms.valueChanged.connect(lambda v: setattr(self.worker, "interval_s", v / 1000))
        if self.d is not None and self.d.poll_ms:
            self.poll_ms.setValue(self.d.poll_ms)
        touch.attach_keypad(self.poll_ms, "Poll delay")
        form.addRow("Poll delay", self.poll_ms)
        form.addRow(self.build_pause_button())
        return g

    def _build_trend_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addLayout(self.build_trend_bar())
        self.glw = pg.GraphicsLayoutWidget()
        self.plots: dict = {}               # base reading name -> PlotItem
        self.curves: dict = {}              # reading name -> curve
        self._plot_of: dict = {}            # reading name -> its key in self.plots
        bases = []
        if self.d is not None:
            for r in self.d.readings:
                b = r.plot or (r.name[:-len(str(r.ch))] if (self.d.channels > 1 and r.ch)
                               else r.name)
                if b == "-":
                    continue                # a status or a total: shown and logged only
                self._plot_of[r.name] = b
                if b not in bases:
                    bases.append(b)
        shared = {b for b in bases if list(self._plot_of.values()).count(b) > 1}
        first = None
        for row, b in enumerate(bases):
            p = self.glw.addPlot(row=row, col=0)
            p.getAxis("left").setWidth(70)
            p.showGrid(x=True, y=True, alpha=0.15)
            p.setClipToView(True)
            p.setDownsampling(auto=True, mode="peak")
            if first is None:
                first = p
            else:
                p.setXLink(first)
            if row == len(bases) - 1:
                p.setLabel("bottom", "Time", units="s")
            if b in shared:
                p.addLegend(offset=(-10, 10))
            self.plots[b] = p
        for k, r in enumerate(self.d.readings if self.d else []):
            b = self._plot_of.get(r.name)
            if b is None:
                continue
            color = COLORS[(r.ch - 1) % len(COLORS)] if self.d.channels > 1 else \
                COLORS[k % len(COLORS)]
            name = None if b not in shared else \
                f"CH{r.ch}" if self.d.channels > 1 else r.label
            self.curves[r.name] = self.plots[b].plot(pen=pg.mkPen(color, width=1.5), name=name)
        if first is not None:
            touch.plots(*self.plots.values())
            self.scrolling_trend(first, *self.plots.values())
        v.addWidget(self.glw, 1)
        return w

    # ---- names the sequencer / watchdog / math see ----------------------------------
    def quantities(self) -> list[str]:
        return list(self.names)

    def set_names(self) -> list[str]:
        return [s.name for s in self.d.settings] if self.d else []

    def setting_kinds(self) -> dict:
        return {s.name: s.kind for s in self.d.settings} if self.d else {}

    def measure_names(self) -> list[str]:
        return list(self.names)

    def profile_names(self) -> list[str]:
        return [s.name for s in self.d.settings if s.kind == "number"] if self.d else []

    def accuracy_keys(self, what, r=None):
        rd = next((x for x in self.d.readings if x.name.lower() == what.lower()), None) \
            if self.d else None
        keys = []
        for frm in ((rd.unit_from, rd.query_from) if rd else ()):
            if frm and self.worker.drv is not None:
                vals = self.worker.drv.values
                label = vals.get(frm) or vals.get(f"{frm}{rd.ch}")
                keys += [f"{what} {label}", label] if label else []
        return keys + [what]

    def builtin_accuracy(self) -> dict:
        if self.d is None:
            return {}
        out = {r.name: r.accuracy for r in self.d.readings if r.accuracy}
        for s in self.d.settings:
            out.update({o.label: o.accuracy for o in s.options if o.accuracy})
        return out

    def unit_of(self, name: str) -> str:
        return self._units.get(name, "")

    # ---- actions ------------------------------------------------------------------------
    def _set(self, name, value):
        self.cmd(lambda d, n, v: d.set(n, v), name, value)

    def output_off(self):
        if self.connected and self.d is not None and self.d.has_outputs:
            self.worker.submit(lambda d: d.set_output(False), refresh=True)

    def _name_changed(self, text):
        self.title_changed.emit(self._title())
        self.settings_changed.emit()

    def _title(self) -> str:
        model = getattr(self.caps, "model", "") or (self.d.id if self.d else self.model)
        t = self.name_edit.text().strip()
        return f"{model} · {t}" if t else model

    def base_name(self) -> str:
        model = getattr(self.caps, "model", "") or (self.d.id if self.d else self.model)
        t = self.user_name()
        return f"{model} · {t}" if t else f"{model} ({self.resource})"

    # ---- remembered settings ------------------------------------------------------------
    def session_settings(self) -> dict:
        if self._pending is not None and not self.connected:
            return dict(self._pending)
        keep = {}
        if self.d is not None:
            for s in self.d.settings:
                # outputs and source levels are never restored (always read back)
                if s.output or self.d.type in ("supply", "load"):
                    continue
                if self.values.get(s.name) is not None:
                    keep[s.name] = self.values[s.name]
        return {"name": self.name_edit.text(), "poll_ms": self.poll_ms.value(),
                "settings": keep}

    def restore_settings(self, settings: dict):
        self._pending = dict(settings)
        self.name_edit.setText(str(settings.get("name", "")))
        try:
            self.poll_ms.setValue(int(settings.get("poll_ms", self.poll_ms.value())))
        except (TypeError, ValueError):
            pass

    def _apply_pending(self):
        s, self._pending = self._pending, None
        if not s or self.d is None:
            return
        for name, v in (s.get("settings") or {}).items():
            st = self.d.setting(name)
            if st is None or st.output or v is None or self.values.get(name) == v:
                continue
            self._set(name, v)

    # ---- worker callbacks -----------------------------------------------------------
    def on_connected(self, family, caps, state):
        self.idn_label.setText(caps.idn + (f"\n{caps.note}" if caps.note else ""))
        self.idn_label.setStyleSheet("color:#c9ccd1;")
        self.title_changed.emit(self._title())
        self._apply_pending()

    def on_state(self, st: ScpiState):
        self.values = dict(st.values)
        for name, w in self._widgets.items():
            v = self.values.get(name)
            if v is None:
                continue
            w.blockSignals(True)
            try:
                if isinstance(w, QPushButton):
                    w.setChecked(bool(v))
                    w.setText(self._out_text(self.d.setting(name), bool(v)))
                    set_role(w, "danger" if v else "")
                elif isinstance(w, QCheckBox):
                    w.setChecked(bool(v))
                elif isinstance(w, QComboBox):
                    i = w.findData(v) if w.findData(v) >= 0 else w.findText(str(v))
                    if i >= 0:
                        w.setCurrentIndex(i)
                elif isinstance(w, QDoubleSpinBox) and not w.hasFocus():
                    w.setValue(float(v))
            finally:
                w.blockSignals(False)
        self.settings_changed.emit()

    def on_reading(self, r: ScpiReading):
        row = [r.t]
        changed = False
        pm = self.pm_due()
        for n in self.names:
            v = r.values.get(n)
            unit = r.units.get(n, "")
            if self._units.get(n) != unit:
                self._units[n] = unit
                changed = True
            ro = self._readouts.get(n)
            if ro is not None:
                ro.set(fmt_value(v, unit, self._sig()))
                if pm:
                    u = None if v is None else self.uncertainty(n, v)
                    ro.set_pm("" if u is None else "± " + fmt_value(u, unit, 2))
            row.append(np.nan if v is None else v)
        if changed:
            self._label_axes()
            if len(self.buf.view()):
                self.clear_trend()          # a meter changed function: new units
        self.buf.append(row)
        if self.recording:
            self.record([time.strftime("%Y-%m-%dT%H:%M:%S"), f"{r.t:.3f}"] + row[1:])

    def _label_axes(self):
        for b, p in self.plots.items():
            unit = next((self._units.get(n, "") for n in self.names
                         if self._plot_of.get(n) == b), "")
            p.setLabel("left", b, units=unit or None)

    def csv_header(self):
        return ["timestamp", "t_s"] + [f"{n}_{self._units.get(n, '')}".rstrip("_")
                                       .replace("Ω", "ohm") for n in self.names]

    def csv_name(self):
        return f"{self.d.id if self.d else 'scpi'}_%Y%m%d_%H%M%S.csv"

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
        for k, n in enumerate(self.names, start=1):
            if n not in self.curves:
                continue
            self.curves[n].setData(t[max(0, start - 1):], d[max(0, start - 1):, k],
                                   connect="finite")
        y = d[start:, 1:]
        if y.size and self.names:
            # mean over the span; σ only for a single reading (a meter)
            parts = []
            for k, n in enumerate(self.names):
                if n not in self.curves:
                    continue
                col = y[:, k]
                col = col[np.isfinite(col)]
                if col.size > 1:
                    u = self._units.get(n, "")
                    text = f"{n} μ {fmt_value(float(col.mean()), u, 5)}"
                    if len(self.names) == 1:
                        text += f"  σ {fmt_value(float(col.std()), u, 3)}"
                    parts.append(text)
            self.trend_stats.setText("   ".join(parts))

    def set_enabled(self, on: bool):
        for cls in (QPushButton, QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox):
            for w in self.findChildren(cls):
                w.setEnabled(on)
        for w in (self.span_combo, self.freeze_chk, self.trace_chk):
            w.setEnabled(True)
        if on:
            self.poll_ms.setEnabled(True)
