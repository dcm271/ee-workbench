"""Capture tab for the 663xx digitizer: triggered / pre-triggered waveform
capture, output-step response, cursors, automatic measurements, reference
trace, histogram and spectrum views, CSV save/load."""
from __future__ import annotations

import csv
import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QCheckBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
                             QHBoxLayout, QLabel, QMessageBox, QPushButton, QSizePolicy, QSplitter,
                             QStackedWidget, QVBoxLayout, QWidget)

from .. import analysis
from ..instruments.base import CaptureResult, CaptureSpec
from . import touch
from .forms import choice, hint, integer, number
from .widgets import C_CURR, C_MUTED, C_VOLT, C_WARN, MONO, fmt_si

TRIGGERS = [("Immediate", "IMM"), ("Signal level", "LEVEL"),
            ("External input", "EXT"), ("Output step", "STEP")]
SLOPES = [("Rising", "POS"), ("Falling", "NEG"), ("Either", "EITH")]
SEG_COLORS = ["#4fc3f7", "#ffb347", "#7ee787", "#c39bff", "#ff8a80", "#ffd54f"]
C_REF = "#9e9e9e"
C_CUR1, C_CUR2 = "#ffd54f", "#ff8a80"

TIME_KEYS = ("period", "width", "rise", "fall", "duration")


def _fmt(key: str, v, unit: str) -> str:
    if v is None:
        return "—"
    if key in TIME_KEYS or key == "Δt":
        return fmt_si(v, "s")
    if key in ("frequency", "1/Δt"):
        return fmt_si(v, "Hz")
    if key == "duty":
        return f"{v * 100:.1f} %"
    if key == "charge":
        return fmt_si(v / 3600, "Ah")          # battery units; coulombs in the tooltip
    if key == "energy":
        return fmt_si(v / 3600, "Wh")
    return fmt_si(v, unit, 5)


class CaptureTab(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.p = panel
        self.res: CaptureResult | None = None
        self.x = self.y = None          # displayed record (concatenated)
        self.ref = None                 # (x, y, label)
        self.running = False
        self.busy = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        side = touch.scroll(self._build_settings())
        side.setMinimumWidth(285 if touch.TOUCH else 270)
        split.addWidget(side)
        split.addWidget(self._build_view())
        split.setStretchFactor(1, 1)
        split.setSizes([310, 900])
        root.addWidget(split)
        self._trigger_changed()
        self._update_span()

    # ================================================================ build
    def _build_settings(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 6, 0)
        g = QGroupBox("Capture")
        f = QFormLayout(g)
        f.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.what = choice([("Current", "CURR"), ("Voltage", "VOLT")])
        self.what.currentIndexChanged.connect(self._what_changed)
        f.addRow("Signal", self.what)
        self.trigger = choice(TRIGGERS)
        self.trigger.currentIndexChanged.connect(self._trigger_changed)
        f.addRow("Trigger", self.trigger)
        self.points = integer("Points per capture", 16, 4096, 1024)
        self.points.valueChanged.connect(self._update_span)
        f.addRow("Points", self.points)
        self.tint = number("Sample interval", 15.6, 1e6, 15.6, 1, " µs", 15.6)
        self.tint.valueChanged.connect(self._update_span)
        f.addRow("Interval", self.tint)
        self.span = hint("")
        f.addRow(self.span)
        v.addWidget(g)

        g = QGroupBox("Trigger")
        tv = QVBoxLayout(g)
        self.pages = QStackedWidget()
        self.pages.addWidget(hint("Captures as soon as you press Single or Run."))
        # level
        lw = QWidget()
        lf = QFormLayout(lw)
        lf.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.level = number("Trigger level", 0, 3.1, 0.5, 4, " A")
        row.addWidget(self.level, 1)
        auto = QPushButton("50 %")
        auto.setToolTip("Midway between the high and low levels of the last capture")
        auto.clicked.connect(self._auto_level)
        row.addWidget(auto)
        lf.addRow("Level", row)
        self.slope = choice(SLOPES)
        lf.addRow("Edge", self.slope)
        self.hyst = number("Hysteresis", 0, 3.1, 0.02, 4, " A")
        self.hyst.setToolTip("The signal must cross the whole band (level ± half this) to "
                             "trigger, so noise doesn't.")
        lf.addRow("Hysteresis", self.hyst)
        self.pages.addWidget(lw)
        # external
        ew = QWidget()
        ev = QVBoxLayout(ew)
        ev.setContentsMargins(0, 0, 0, 0)
        ev.addWidget(hint("Triggers on a falling edge or contact closure at the rear-panel "
                          "control port (pin 3)."))
        self.ext_warn = QLabel("")
        self.ext_warn.setWordWrap(True)
        self.ext_warn.setStyleSheet(f"color:{C_WARN};")
        ev.addWidget(self.ext_warn)
        self.ext_fix = QPushButton("Set the port to 'External trigger'")
        self.ext_fix.clicked.connect(self._set_port_trigger)
        ev.addWidget(self.ext_fix)
        self.pages.addWidget(ew)
        # output step
        sw = QWidget()
        sg = QGridLayout(sw)
        sg.setContentsMargins(0, 0, 0, 0)
        sg.addWidget(hint("Steps the output and captures the response at the same instant."),
                     0, 0, 1, 2)
        self.step_v_chk = QCheckBox("Voltage")
        self.step_v_chk.setChecked(True)
        self.step_v = number("Step voltage to", 0, 15.5, 4.0, 3, " V")
        self.step_i_chk = QCheckBox("Current limit")
        self.step_i = number("Step current limit to", 0, 3.1, 1.0, 4, " A")
        self.step_r_chk = QCheckBox("Resistance")
        self.step_r = number("Step output resistance to", -0.04, 1.0, 0.0, 3, " Ω")
        for n, (chk, spin) in enumerate(((self.step_v_chk, self.step_v),
                                         (self.step_i_chk, self.step_i),
                                         (self.step_r_chk, self.step_r)), start=1):
            sg.addWidget(chk, n, 0)
            sg.addWidget(spin, n, 1)
        self.step_back = QCheckBox("Put the levels back afterwards")
        self.step_back.setChecked(True)
        sg.addWidget(self.step_back, 4, 0, 1, 2)
        now = QPushButton("Step now (no capture)")
        now.clicked.connect(self._step_now)
        sg.addWidget(now, 5, 0, 1, 2)
        self.pages.addWidget(sw)
        tv.addWidget(self.pages)

        self.common = QWidget()
        cf = QFormLayout(self.common)
        cf.setContentsMargins(0, 0, 0, 0)
        cf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.pre = integer("Pre-trigger", 0, 95, 25, " %")
        self.pre.setToolTip("How much of the record is before the trigger")
        cf.addRow("Pre-trigger", self.pre)
        self.count = integer("Segments", 1, 100, 1)
        self.count.setToolTip("Capture this many triggers in a row (points × segments ≤ 4096)")
        self.count.valueChanged.connect(self._update_span)
        cf.addRow("Segments", self.count)
        self.common_form = cf
        tv.addWidget(self.common)
        v.addWidget(g)

        btns = QGridLayout()
        self.single_btn = QPushButton("Single")
        self.single_btn.setToolTip("Capture once")
        self.single_btn.clicked.connect(lambda: self.start(False))
        self.run_btn = QPushButton("Run")
        self.run_btn.setToolTip("Capture again and again until Stop")
        self.run_btn.clicked.connect(lambda: self.start(True))
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        btns.addWidget(self.single_btn, 0, 0)
        btns.addWidget(self.run_btn, 0, 1)
        btns.addWidget(self.stop_btn, 0, 2)
        v.addLayout(btns)
        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-weight:bold;")
        v.addWidget(self.status)
        v.addWidget(hint("Readings on the left pause while a capture is armed: the "
                         "supply has one digitizer."))
        v.addStretch(1)
        return w

    def _build_view(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(6, 0, 0, 0)
        bar = QHBoxLayout()
        self.view = choice([("Time", "time"), ("Histogram", "hist"), ("Spectrum", "fft")],
                           "Histogram: share of time at each level. Spectrum: FFT (Hann).")
        self.view.currentIndexChanged.connect(self.redraw)
        bar.addWidget(self.view)
        self.cursors_chk = QCheckBox("Cursors")
        self.cursors_chk.toggled.connect(self._toggle_cursors)
        bar.addWidget(self.cursors_chk)
        self.overlay_chk = QCheckBox("Overlay")
        self.overlay_chk.setChecked(True)
        self.overlay_chk.setToolTip("Draw multiple segments on top of each other")
        self.overlay_chk.toggled.connect(self.redraw)
        bar.addWidget(self.overlay_chk)
        bar.addStretch(1)
        if touch.TOUCH:
            v.addLayout(bar)
            bar = QHBoxLayout()
        self.ref_btn = QPushButton("Reference")
        self.ref_btn.setToolTip("Show this capture in grey behind the following ones")
        self.ref_btn.clicked.connect(self._toggle_ref)
        bar.addWidget(self.ref_btn)
        save = QPushButton("Save…")
        save.setToolTip("Save the capture as CSV, or the plot as PNG")
        save.clicked.connect(self.save)
        bar.addWidget(save)
        load = QPushButton("Open…")
        load.setToolTip("Open a capture saved as CSV")
        load.clicked.connect(self.load)
        bar.addWidget(load)
        v.addLayout(bar)

        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.addLegend(offset=(-10, 10))
        touch.plots(self.plot)
        v.addWidget(self.plot, 1)
        self.c1 = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen(C_CUR1, width=2),
                                  label="1", labelOpts={"position": 0.95, "color": C_CUR1})
        self.c2 = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen(C_CUR2, width=2),
                                  label="2", labelOpts={"position": 0.95, "color": C_CUR2})
        for c in (self.c1, self.c2):
            c.sigPositionChanged.connect(self._update_cursor_text)
        self.t0_line = pg.InfiniteLine(angle=90, pen=pg.mkPen(C_MUTED, style=Qt.PenStyle.DashLine))
        self.lev_line = pg.InfiniteLine(angle=0, pen=pg.mkPen(C_WARN, style=Qt.PenStyle.DashLine))

        self.meas_head = QLabel("Press Single to capture.")
        self.meas_head.setWordWrap(True)
        self.meas_head.setStyleSheet(f"color:{C_MUTED};")
        v.addWidget(self.meas_head)
        self.meas_grid = QGridLayout()
        self.meas_grid.setHorizontalSpacing(10)
        self.meas_grid.setVerticalSpacing(1)
        v.addLayout(self.meas_grid)
        self.meas_cells = []
        self.cur_text = QLabel("")
        self.cur_text.setFont(QFont(MONO, 10 if touch.TOUCH else 9))
        self.cur_text.setWordWrap(True)
        self.cur_text.setVisible(False)
        v.addWidget(self.cur_text)
        return w

    # ================================================================ settings
    def unit(self) -> str:
        return "A" if self.what.currentData() == "CURR" else "V"

    def _what_changed(self):
        u = self.unit()
        hi = self.p.caps.i_max if u == "A" else self.p.caps.v_max
        for s, val in ((self.level, 0.5 if u == "A" else 3.0),
                       (self.hyst, 0.02 if u == "A" else 0.05)):
            s.setSuffix(" " + u)
            s.setRange(0, hi)
            s.setValue(val)
        self.redraw()

    def _trigger_changed(self):
        trig = self.trigger.currentData()
        idx = [t for _, t in TRIGGERS].index(trig)
        # size the stack to the visible page only (no gap under short pages)
        for k in range(self.pages.count()):
            self.pages.widget(k).setSizePolicy(
                QSizePolicy.Policy.Preferred,
                QSizePolicy.Policy.Preferred if k == idx else QSizePolicy.Policy.Ignored)
        self.pages.setCurrentIndex(idx)
        self.pages.adjustSize()
        self.common.setVisible(trig != "IMM")
        self.common_form.setRowVisible(self.count, trig in ("LEVEL", "EXT"))
        if trig == "EXT":
            self._check_port()
        self._update_span()

    def _check_port(self):
        func = getattr(self.p.sp, "port_function", None)
        bad = func is not None and func != "TRIG"
        self.ext_warn.setText("The rear port is set to " + {"RIDF": "RI/DFI", "DIG": "digital I/O"}
                              .get(func or "", str(func)) + ", not external trigger." if bad else "")
        self.ext_fix.setVisible(bad)

    def _set_port_trigger(self):
        if QMessageBox.question(
                self, "Rear port", "Set the rear control port to 'External trigger'?\n\n"
                "This replaces remote inhibit / discrete fault (or digital I/O) on that port, "
                "and is stored in the supply's non-volatile memory.") == \
                QMessageBox.StandardButton.Yes:
            self.p.cmd(lambda d: d.set_port_function("TRIG"))

    def on_state(self, sp):
        if self.trigger.currentData() == "EXT":
            self._check_port()

    def _update_span(self, *_):
        n, dt = self.points.value(), self.tint.value() * 1e-6
        maxc = max(1, 4096 // n)
        if self.count.maximum() != min(100, maxc):
            self.count.setMaximum(min(100, maxc))
        segs = self.count.value() if self.trigger.currentData() in ("LEVEL", "EXT") else 1
        xfer = n * segs * 4 / 3700
        self.span.setText(f"{fmt_si(n * dt, 's')} per capture · transfer ≈ {xfer:.1f} s")

    def _auto_level(self):
        if self.y is None:
            self.p.message.emit("Capture once first, then 50 % sets the level from it.")
            return
        hi, lo = analysis.levels(self.y)
        self.level.setValue((hi + lo) / 2)
        self.hyst.setValue(max(abs(hi - lo) * 0.1, 0.001))

    def spec(self) -> CaptureSpec:
        trig = self.trigger.currentData()
        n = self.points.value()
        s = CaptureSpec(what=self.what.currentData(), points=n, tint=self.tint.value() * 1e-6,
                        trigger=trig)
        if trig != "IMM":
            s.pre = int(n * self.pre.value() / 100)
            s.count = self.count.value() if trig in ("LEVEL", "EXT") else 1
        if trig == "LEVEL":
            s.level, s.slope, s.hyst = self.level.value(), self.slope.currentData(), self.hyst.value()
        if trig == "STEP":
            s.step = self._step_levels()
            s.restore_after_step = self.step_back.isChecked()
        return s

    def _step_levels(self) -> dict:
        step = {}
        if self.step_v_chk.isChecked():
            step["VOLT"] = self.step_v.value()
        if self.step_i_chk.isChecked():
            step["CURR"] = self.step_i.value()
        if self.step_r_chk.isChecked() and self.p.caps.has_resistance:
            step["RES"] = self.step_r.value()
        return step

    def _step_now(self):
        step = self._step_levels()
        if step:
            self.p.cmd(lambda d, s: d.step_output(s), step)

    # ================================================================ run
    def start(self, run: bool):
        if not self.p.connected:
            return
        spec = self.spec()
        if spec.trigger == "STEP" and not spec.step:
            self.p.message.emit("Tick at least one level to step.")
            return
        if spec.trigger == "EXT" and getattr(self.p.sp, "port_function", "TRIG") != "TRIG":
            self.p.message.emit("Set the rear port to 'External trigger' first.")
            return
        self.running = run
        self.busy = True
        self._buttons()
        self.p.worker.request_capture(spec)

    def stop(self):
        self.running = False
        if self.busy:
            self.p.worker.abort_capture()
        self.busy = False
        self._buttons()
        self.status.setText("Stopped")

    def _buttons(self):
        idle = not self.busy and not self.running
        self.single_btn.setEnabled(idle and self.p.connected)
        self.run_btn.setEnabled(idle and self.p.connected)
        self.stop_btn.setEnabled(not idle)
        self.run_btn.setText("Running…" if self.running else "Run")

    def on_status(self, st: str):
        spec = self.spec()
        text = {"armed": "Filling the pre-trigger buffer…",
                "waiting": ("Waiting for the trigger…" if spec.trigger in ("LEVEL", "EXT")
                            else "Capturing…"),
                "transferring": f"Transferring {spec.points * max(1, spec.count)} points…",
                }.get(st)
        if text:
            self.status.setText(text)
            self.status.setStyleSheet(f"font-weight:bold; color:{C_WARN if st == 'waiting' and spec.trigger in ('LEVEL', 'EXT') else '#c9ccd1'};")
        if st == "idle" and self.busy and not self.running:
            self.busy = False
            self._buttons()

    def on_capture(self, res: CaptureResult):
        self.res = res
        self.busy = False
        self.status.setStyleSheet("font-weight:bold;")
        self.status.setText("Captured " + time.strftime("%H:%M:%S", time.localtime(res.t_done))
                            + (f" · restored {', '.join(res.restored)}" if res.restored else ""))
        self._set_record(res.spec, np.asarray(res.data, float))
        if self.running:
            self.busy = True
            self.p.worker.request_capture(self.spec())
        self._buttons()

    def on_error(self):
        if self.busy or self.running:
            self.running = False
            self.busy = False
            self._buttons()
            self.status.setText("Stopped (error, see Console)")

    # ================================================================ display
    def _set_record(self, spec: CaptureSpec, y: np.ndarray):
        n = spec.points
        segs = max(1, y.size // n) if n else 1
        self.spec_shown = spec
        self.segments = [y[k * n:(k + 1) * n] for k in range(segs)] if segs > 1 else [y]
        self.seg_x = (np.arange(len(self.segments[0])) - spec.pre) * spec.tint
        self.x = np.concatenate([self.seg_x + k * n * spec.tint for k in range(len(self.segments))])
        self.y = np.concatenate(self.segments)
        lo, hi = self.seg_x[0], self.seg_x[-1]
        if not (lo <= self.c1.value() <= hi and lo <= self.c2.value() <= hi) or \
                self.c1.value() == self.c2.value():
            self.c1.setPos(lo + (hi - lo) * 0.25)
            self.c2.setPos(lo + (hi - lo) * 0.75)
        self.redraw()

    def redraw(self, *_):
        p = self.plot.getPlotItem()
        p.clear()
        p.legend.clear() if p.legend else None
        if self.y is None:
            return
        spec = self.spec_shown
        u = "A" if spec.what == "CURR" else "V"
        color = C_CURR if u == "A" else C_VOLT
        mode = self.view.currentData()
        p.setLogMode(x=False, y=False)
        if mode == "time":
            p.setLabel("bottom", "Time from trigger" if spec.trigger != "IMM" else "Time",
                       units="s")
            p.setLabel("left", "Current" if u == "A" else "Voltage", units=u)
            if self.ref is not None:
                p.plot(self.ref[0], self.ref[1], pen=pg.mkPen(C_REF, width=1), name=self.ref[2])
            if len(self.segments) > 1 and self.overlay_chk.isChecked():
                for k, seg in enumerate(self.segments):
                    p.plot(self.seg_x, seg, pen=pg.mkPen(SEG_COLORS[k % len(SEG_COLORS)], width=1),
                           name=f"#{k + 1}" if k < 6 else None)
            else:
                p.plot(self.x, self.y, pen=pg.mkPen(color, width=1.3), name="capture")
            if spec.trigger != "IMM":
                self.t0_line.setPos(0)
                p.addItem(self.t0_line)
            if spec.trigger == "LEVEL":
                self.lev_line.setPos(spec.level)
                p.addItem(self.lev_line)
            if self.cursors_chk.isChecked():
                p.addItem(self.c1)
                p.addItem(self.c2)
        elif mode == "hist":
            xs, frac = analysis.histogram(self.y, 64)
            width = (xs[1] - xs[0]) if xs.size > 1 else 1
            p.addItem(pg.BarGraphItem(x=xs, height=frac * 100, width=width * 0.9,
                                      brush=pg.mkBrush(color)))
            p.setLabel("bottom", "Current" if u == "A" else "Voltage", units=u)
            p.setLabel("left", "Time at level", units="%")
        else:
            f, db = analysis.spectrum(self.segments[0], spec.tint)
            p.plot(f, db, pen=pg.mkPen(color, width=1.2))
            p.setLogMode(x=True, y=False)
            p.setLabel("bottom", "Frequency", units="Hz")
            p.setLabel("left", f"dB re 1 {u} rms")
        p.enableAutoRange()
        self._update_measurements()
        self._update_cursor_text()

    def _other_level(self):
        """Output voltage, to turn a current capture's charge into energy."""
        return self.p.last_voltage if self.spec_shown.what == "CURR" else None

    def _update_measurements(self):
        spec = self.spec_shown
        u = "A" if spec.what == "CURR" else "V"
        seg = self.segments[0]
        m = analysis.measure(self.seg_x, seg, u, self._other_level())
        order = ["mean", "rms", "max", "min", "high", "low", "pk-pk", "period", "frequency",
                 "width", "duty", "rise", "fall", "charge", "energy"]
        items = [(k, _fmt(k, m[k], u)) for k in order if k in m]
        tips = {"charge": f"{m.get('charge', 0):.6g} C over the record",
                "energy": f"{m.get('energy', 0):.6g} J (charge × output voltage)"}
        cols = 3 if touch.TOUCH else 4
        for w in self.meas_cells:
            self.meas_grid.removeWidget(w)
            w.hide()
            w.deleteLater()
        self.meas_cells = []
        font = QFont(MONO, 11 if touch.TOUCH else 10)
        for n, (k, text) in enumerate(items):
            r, c = divmod(n, cols)
            name = QLabel(k)
            name.setStyleSheet(f"color:{C_MUTED};")
            name.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val = QLabel(text)
            val.setFont(font)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if k in tips:
                val.setToolTip(tips[k])
            self.meas_grid.addWidget(name, r, 2 * c)
            self.meas_grid.addWidget(val, r, 2 * c + 1)
            self.meas_cells += [name, val]
        head = (f"{len(seg)} points × {fmt_si(spec.tint, 's')}"
                + (f", segment 1 of {len(self.segments)}" if len(self.segments) > 1 else ""))
        if self.res is not None and self.res.stats:
            st = self.res.stats
            head += f" · supply's own: dc {fmt_si(st['dc'], u, 5)}, rms {fmt_si(st['rms'], u, 5)}"
        self.meas_head.setText(head)

    def _toggle_cursors(self, on):
        self.cur_text.setVisible(on)
        if on and self.x is not None:
            a, b = self.seg_x[0], self.seg_x[-1]
            self.c1.setPos(a + (b - a) * 0.25)
            self.c2.setPos(a + (b - a) * 0.75)
        self.redraw()

    def _update_cursor_text(self, *_):
        if not self.cursors_chk.isChecked() or self.y is None or self.view.currentData() != "time":
            return
        u = "A" if self.spec_shown.what == "CURR" else "V"
        t1, t2 = self.c1.value(), self.c2.value()
        m = analysis.between(self.seg_x, self.segments[0], t1, t2, u, self._other_level())
        line1 = [f"1: {fmt_si(t1, 's')} {fmt_si(m['y1'], u, 5)}",
                 f"2: {fmt_si(t2, 's')} {fmt_si(m['y2'], u, 5)}",
                 f"Δt {_fmt('Δt', m['Δt'], u)}", f"1/Δt {_fmt('1/Δt', m['1/Δt'], u)}",
                 f"Δ {fmt_si(m['Δy'], u, 4)}"]
        line2 = [f"{k} {_fmt(k, m[k], u)}" for k in ("mean", "rms", "max", "min", "charge",
                                                    "energy") if k in m]
        self.cur_text.setText("Cursors  " + "   ".join(line1) + "\nBetween  " + "   ".join(line2))

    def _toggle_ref(self):
        if self.ref is not None:
            self.ref = None
            self.ref_btn.setText("Reference")
        elif self.x is not None:
            self.ref = (self.seg_x.copy(), self.segments[0].copy(),
                        "reference " + time.strftime("%H:%M:%S"))
            self.ref_btn.setText("Clear ref.")
        self.redraw()

    # ================================================================ files
    def save(self):
        if self.y is None:
            return
        spec = self.spec_shown
        base = time.strftime(f"{self.p.caps.model or 'capture'}_{spec.what.lower()}_%Y%m%d_%H%M%S")
        path, flt = QFileDialog.getSaveFileName(self, "Save capture", base + ".csv",
                                                "CSV data (*.csv);;PNG image (*.png)")
        if not path:
            return
        if path.lower().endswith(".png") or flt.startswith("PNG"):
            from pyqtgraph.exporters import ImageExporter
            if not path.lower().endswith(".png"):
                path += ".png"
            ImageExporter(self.plot.getPlotItem()).export(path)
        else:
            unit = "current_A" if spec.what == "CURR" else "voltage_V"
            with open(path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["t_s", "segment", unit])
                for k, seg in enumerate(self.segments):
                    for t, val in zip(self.seg_x, seg):
                        w.writerow([f"{t:.7e}", k + 1, f"{val:.6e}"])
            with open(path, "a") as fh:
                fh.write(f"# trigger={spec.trigger} points={spec.points} interval_s={spec.tint}"
                         f" pre={spec.pre} level={spec.level} slope={spec.slope}\n")
        self.p.message.emit(f"Saved {path}")

    def load(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open capture", "", "CSV (*.csv)")
        if not path:
            return
        try:
            rows = []
            with open(path, newline="") as fh:
                rd = csv.reader(fh)
                header = next(rd)
                for r in rd:
                    if r and not r[0].startswith("#"):
                        rows.append(r)
            a = np.array(rows, dtype=float)
            t = a[:, 0]
            seg = a[:, 1].astype(int) if a.shape[1] > 2 else np.ones(len(a), int)
            y = a[:, -1]
            n = int((seg == seg[0]).sum())
            dt = float(np.median(np.diff(t[:n]))) if n > 1 else 15.6e-6
            pre = int(round(-t[0] / dt)) if t[0] < 0 else 0
            what = "CURR" if "current" in header[-1].lower() else "VOLT"
            spec = CaptureSpec(what=what, points=n, tint=dt, pre=pre,
                               trigger="LEVEL" if pre else "IMM", count=int(seg.max()))
        except Exception as e:
            QMessageBox.warning(self, "Open capture", f"Couldn't read {path}:\n{e}")
            return
        self.res = None
        self.status.setText(f"Opened {path.split('/')[-1]}")
        self._set_record(spec, y)
