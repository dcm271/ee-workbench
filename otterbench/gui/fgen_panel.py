"""Tab for a function generator (Siglent SDG, Rigol DG, Keysight 33xxx):
waveform, frequency, amplitude, offset and duty per channel, the outputs, and
a Profile tab that sweeps any of them over time (log-spaced frequency steps
for a frequency response, an amplitude ramp, …)."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QSplitter, QTabWidget, QVBoxLayout, QWidget, QFileDialog, QInputDialog, QMessageBox)

from .. import arb
from ..instruments.fgen import FUNCS, FgenState
from . import touch
from .panel_base import PanelBase
from .profile_box import ProfileBox
from .theme import set_role
from .widgets import C_MUTED, SafeSpin

FUNC_TEXT = {"sine": "Sine", "square": "Square", "ramp": "Ramp", "pulse": "Pulse",
             "noise": "Noise", "dc": "DC", "arb": "Arbitrary"}


class FgenPanel(PanelBase):
    KIND = "fgen"

    def __init__(self, resource: str, model: str, baud: int):
        super().__init__(resource, model, baud)
        self.state = FgenState()
        self.ch = 1
        self._armed = False
        self._pending: dict | None = None
        self.worker.poll_enabled = False          # nothing to read back continuously

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        console = self.build_console_tab(
            "SCPI, e.g.  SOUR1:FREQ 1000   (Siglent: C1:BSWV FRQ,1000)", read_button=True)
        # PanelBase's redraw loop expects these (this tab has no trend)
        self.freeze_chk = QCheckBox()
        self.rate_label = QLabel()
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
        self.name_edit.setPlaceholderText("Name, e.g. “clock in”")
        self.name_edit.textChanged.connect(self._name_changed)
        lay.addWidget(self.name_edit)
        lay.addWidget(self._build_channel_group())
        lay.addStretch(1)
        sa = touch.scroll(inner)
        sa.setMinimumWidth(420 if touch.TOUCH else 400)
        split.addWidget(sa)
        self.tabs = QTabWidget()
        self.profile_box = ProfileBox(
            self, {k: (lambda d, v, k=k: d.set(k, v, self._profile_ch))
                   for k in ("frequency", "amplitude", "offset", "duty")},
            {"frequency": "Hz", "amplitude": "Vpp", "offset": "V", "duty": "%"},
            title="Profile  (sweep a setting of the selected channel)")
        self.profile_box.text.insertItem(0, "logsteps 10..100000 n=41 dwell=0.5")
        self.profile_box.text.setCurrentIndex(0)
        self._profile_ch = 1
        self.profile_box.run_btn.pressed.connect(lambda: setattr(self, "_profile_ch", self.ch))
        page = QWidget()
        pv = QVBoxLayout(page)
        pv.addWidget(self.profile_box)
        hint = QLabel("A frequency response: put the generator on the circuit's input and a "
                      "DMM (AC volts) on its output, run logsteps on frequency here, and record "
                      "the DMM — or do both in the Sequencer: a Sweep x with Set frequency = "
                      "10**x and Measure value gives one row per frequency.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C_MUTED};")
        pv.addWidget(hint)
        pv.addStretch(1)
        self.tabs.addTab(touch.scroll(page), "Profile")
        self.tabs.addTab(self._build_arb_tab(), "Arbitrary")
        self.tabs.addTab(console, "Console")
        split.addWidget(self.tabs)
        split.setStretchFactor(1, 1)
        split.setSizes([440, 840] if touch.TOUCH else [420, 1000])
        root.addWidget(split)
        self.set_enabled(False)
        self.start()

    # ---- arbitrary waveforms
    def _build_arb_tab(self):
        import pyqtgraph as pg
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.wave = None
        for text, fn in (("From CSV…", self.arb_csv), ("From a scope tab…", self.arb_scope)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        self.arb_btn = QPushButton("Load into generator")
        set_role(self.arb_btn, "primary")
        self.arb_btn.clicked.connect(self.arb_load)
        row.addWidget(self.arb_btn)
        row.addStretch(1)
        v.addLayout(row)
        self.arb_info = QLabel("CSV: time in seconds and volts, with or without a heading line. "
                               "The waveform repeats; up to 8192 points. The channel's output "
                               "must be off while it is loaded.")
        self.arb_info.setWordWrap(True)
        self.arb_info.setStyleSheet(f"color:{C_MUTED};")
        v.addWidget(self.arb_info)
        self.arb_plot = pg.PlotWidget()
        self.arb_plot.showGrid(x=True, y=True, alpha=0.15)
        self.arb_plot.setLabel("bottom", "Time", units="s")
        self.arb_plot.setLabel("left", "Voltage", units="V")
        touch.plots(self.arb_plot)
        v.addWidget(self.arb_plot, 1)
        return w

    def set_wave(self, wave):
        self.wave = wave
        self.arb_plot.clear()
        self.arb_plot.plot([k * wave.seconds / wave.v.size for k in range(wave.v.size)],
                           list(wave.v))
        self.arb_info.setText(wave.describe())

    def _arb(self, make):
        try:
            self.set_wave(make())
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Waveform", str(e))

    def arb_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Waveform", "", "CSV (*.csv *.txt)")
        if path:
            self._arb(lambda: arb.from_csv(path))

    def arb_scope(self):
        from .panel_base import open_panels
        found = {f"{p.display_name()} CH{ch}": tr for p in open_panels() if p.KIND == "scope"
                 and p.capture is not None for ch, tr in sorted(p.capture.analog.items())}
        if not found:
            QMessageBox.information(self, "Waveform", "Grab a waveform on a scope tab first.")
            return
        name, ok = QInputDialog.getItem(self, "Waveform", "Capture:", list(found), 0, False)
        if ok:
            self._arb(lambda: arb.make(found[name].t, found[name].v, name))

    def arb_load(self):
        if self.wave is None:
            QMessageBox.information(self, "Waveform", "Choose a waveform first.")
            return
        wave, ch = self.wave, self.ch
        self.cmd(lambda d: d.upload(wave, ch))

    def _build_channel_group(self):
        g = QGroupBox("Channel")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        self.ch_group = QButtonGroup(self)
        self.ch_btns = []
        for ch in (1, 2):
            b = QPushButton(f"CH{ch}")
            b.setCheckable(True)
            b.setChecked(ch == 1)
            self.ch_group.addButton(b, ch)
            row.addWidget(b)
            self.ch_btns.append(b)
        self.ch_group.idClicked.connect(self._select)
        v.addLayout(row)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.func = QComboBox()
        for f in FUNCS:
            self.func.addItem(FUNC_TEXT[f], f)
        self.func.activated.connect(lambda *_: self._set("function", self.func.currentData()))
        form.addRow("Waveform", self.func)
        self.freq = self._spin(6, 1.0, " Hz", 1e-6, 1e9, "frequency", "Frequency (Hz)")
        form.addRow("Frequency", self.freq)
        self.ampl = self._spin(4, 0.1, " Vpp", 1e-3, 40.0, "amplitude", "Amplitude (Vpp)")
        form.addRow("Amplitude", self.ampl)
        self.offs = self._spin(4, 0.1, " V", -20.0, 20.0, "offset", "Offset (V)")
        form.addRow("Offset", self.offs)
        self.duty = self._spin(2, 1.0, " %", 0.01, 99.99, "duty", "Duty cycle (%)")
        form.addRow("Duty (square)", self.duty)
        v.addLayout(form)
        self.out_btn = QPushButton("Output OFF")
        self.out_btn.setCheckable(True)
        self.out_btn.setMinimumHeight(52 if touch.TOUCH else 40)
        self.out_btn.clicked.connect(self._out_clicked)
        v.addWidget(self.out_btn)
        hint = QLabel("Amplitude is into the load set on the generator (usually 50 Ω): into "
                      "a high-impedance input you get up to twice the voltage shown.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        v.addWidget(hint)
        return g

    def _spin(self, dec, step, suffix, lo, hi, what, title) -> QDoubleSpinBox:
        s = SafeSpin(dec, step, suffix)
        s.setRange(lo, hi)
        s.editingFinished.connect(lambda w=what, s=s: self._set(w, s.value()))
        touch.attach_keypad(s, title)
        return s

    # ---------------------------------------------------------------- actions
    def _select(self, ch: int):
        self.ch = ch
        self._show()

    def _set(self, what, value):
        ch = self.ch
        cur = self.state.channels[ch - 1] if ch <= len(self.state.channels) else None
        if cur is not None and getattr(cur, what) == value:
            return
        self.cmd(lambda d: d.set(what, value, ch))

    def _out_clicked(self, on: bool):
        if on and touch.TOUCH and not self._armed:
            self._armed = True
            self.out_btn.setChecked(False)
            self.out_btn.setText("Tap again to turn ON")
            QTimer.singleShot(3000, self._disarm)
            return
        self._armed = False
        ch = self.ch
        self.out_btn.setChecked(not on)        # until the instrument confirms
        self.cmd(lambda d: d.set("output", on, ch))

    def _disarm(self):
        if self._armed:
            self._armed = False
            self._show()

    def output_off(self):
        self._armed = False
        self.profile_box.stop()
        if self.connected:
            self.cmd(lambda d: d.set_output(False))

    # ---------------------------------------------------------------- display
    def _show(self):
        if self.ch > len(self.state.channels):
            return
        c = self.state.channels[self.ch - 1]
        for w, v in ((self.freq, c.frequency), (self.ampl, c.amplitude), (self.offs, c.offset),
                     (self.duty, c.duty)):
            w.set_from_instrument(v)
        self.func.setCurrentIndex(max(0, self.func.findData(c.function)))
        self.duty.setEnabled(self.connected and c.function in ("square", "pulse"))
        self.out_btn.setChecked(c.output)
        self.out_btn.setText(f"CH{self.ch} output ON" if c.output else f"CH{self.ch} output OFF")
        set_role(self.out_btn, "danger-solid" if c.output else "")
        for b, ch in zip(self.ch_btns, (1, 2)):
            on = ch <= len(self.state.channels) and self.state.channels[ch - 1].output
            b.setText(f"CH{ch}" + ("  ●" if on else ""))

    def _name_changed(self, text):
        self.title_changed.emit(self.display_name())
        self.settings_changed.emit()

    def base_name(self) -> str:
        model = getattr(self.caps, "model", "") or "FGEN"
        t = self.name_edit.text().strip()
        return f"{model} · {t}" if t else model

    def session_settings(self) -> dict:
        return {"name": self.name_edit.text(), "channel": self.ch}

    def restore_settings(self, s: dict):
        self.name_edit.setText(str(s.get("name", "")))
        try:
            self.ch = int(s.get("channel", 1))
        except (TypeError, ValueError):
            self.ch = 1

    def on_connected(self, family, caps, state):
        self.idn_label.setText(f"{caps.idn}\n{caps.note}")
        self.idn_label.setStyleSheet("color:#c9ccd1;")
        self.ch_btns[1].setVisible(caps.nch > 1)
        self.ch = min(self.ch, caps.nch)
        self.ch_btns[self.ch - 1].setChecked(True)
        self.title_changed.emit(self.display_name())

    def on_state(self, st: FgenState):
        self.state = st
        self._show()

    def set_enabled(self, on: bool):
        for cls in (QPushButton, QComboBox, QDoubleSpinBox):
            for w in self.findChildren(cls):
                w.setEnabled(on)
        self.trace_chk.setEnabled(True)
        self.profile_box.set_enabled(on)
        if on:
            self._show()
