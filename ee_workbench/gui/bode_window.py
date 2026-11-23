"""Bode plot window (Tools ▾): gain and phase against frequency, measured with
a function generator and an oscilloscope (bode.py)."""
from __future__ import annotations

import math
from dataclasses import asdict

import pyqtgraph as pg
from PyQt6.QtWidgets import QCheckBox, QFormLayout, QGroupBox, QLabel, QVBoxLayout, QWidget

from .. import bode, report
from . import touch
from .forms import hint, integer, number
from .tool_window import InstBox, ToolWindow
from .widgets import C_CURR, C_VOLT

HEADER = ["frequency_Hz", "gain_dB", "phase_deg", "vin_peak_V", "vout_peak_V", "residual",
          "note"]


class FreqAxis(pg.AxisItem):
    """Labels 1, 2 and 5 of each decade (1 only over a wide span), as 2k, 100k."""

    def logTickStrings(self, values, scale, spacing):
        lo, hi = self.range
        want = (1,) if hi - lo > 3.2 else (1, 2, 5)
        out = []
        for v in values:
            m = 10 ** (v - math.floor(v + 1e-9))
            out.append(self.text(10 ** v) if any(abs(m - w) < 1e-3 * w for w in want) else "")
        return out

    @staticmethod
    def text(f: float) -> str:
        for s, u in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, "")):
            if f >= s * 0.9999:
                return f"{f / s:.4g}{u}"
        return f"{f:.4g}"


class BodeWindow(ToolWindow):
    TITLE = "Bode plot"
    KEY = "bode"

    def buttons(self):
        return [("Use as reference", self.use_reference), ("Clear reference", self.clear_reference),
                ("Save CSV…", self.save_csv), ("Report…", self.report)]

    def build_left(self):
        self.points: list = []
        self.reference: list = []
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 6, 0)
        g = QGroupBox("Instruments")
        f = QFormLayout(g)
        self.fgen = InstBox(lambda p: p.KIND == "fgen")
        f.addRow("Generator", self.fgen)
        self.fgen_ch = integer("Generator channel", 1, 2, 1)
        f.addRow("Its channel", self.fgen_ch)
        self.scope = InstBox(lambda p: p.KIND == "scope")
        f.addRow("Oscilloscope", self.scope)
        self.ch_in = integer("Scope channel on the input", 1, 8, 1)
        f.addRow("Input on CH", self.ch_in)
        self.ch_out = integer("Scope channel on the output", 1, 8, 2)
        f.addRow("Output on CH", self.ch_out)
        v.addWidget(g)
        g = QGroupBox("Sweep")
        f = QFormLayout(g)
        self.f_start = number("Start frequency", 1e-3, 1e9, 100, 3, " Hz")
        f.addRow("From", self.f_start)
        self.f_stop = number("Stop frequency", 1e-3, 1e9, 100e3, 3, " Hz")
        f.addRow("To", self.f_stop)
        self.per_decade = integer("Points per decade", 1, 200, 10)
        f.addRow("Points / decade", self.per_decade)
        self.amplitude = number("Amplitude", 0.001, 100, 1, 3, " Vpp")
        f.addRow("Amplitude", self.amplitude)
        self.offset = number("Offset", -50, 50, 0, 3, " V")
        f.addRow("Offset", self.offset)
        self.settle = number("Settling time", 0, 60, 0.1, 3, " s")
        f.addRow("Settle", self.settle)
        self.cycles = number("Cycles on screen", 1, 50, 4, 1)
        f.addRow("Cycles shown", self.cycles)
        self.average = integer("Acquisitions per point", 1, 64, 1)
        f.addRow("Average", self.average)
        self.autoscale = QCheckBox("Follow the signal with V/div")
        self.autoscale.setChecked(True)
        f.addRow(self.autoscale)
        self.off_at_end = QCheckBox("Generator output off at the end")
        self.off_at_end.setChecked(True)
        f.addRow(self.off_at_end)
        v.addWidget(g)
        v.addWidget(hint("Probe the circuit's input and output with the two scope channels, "
                         "grounds together. Set the generator's load (50 Ω / High Z) on the "
                         "generator. The scope's settings (V/div, time base, trigger) are "
                         "changed. A reference sweep with both probes on the input takes "
                         "probe and cable differences out of later sweeps."))
        self.log_view = self.make_log()
        self.log_view.setMinimumHeight(110)
        v.addWidget(self.log_view, 1)
        return w

    def build_right(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        v.addWidget(self.result)
        glw = pg.GraphicsLayoutWidget()
        self.p_gain = glw.addPlot(row=0, col=0, axisItems={"bottom": FreqAxis("bottom")})
        self.p_phase = glw.addPlot(row=1, col=0, axisItems={"bottom": FreqAxis("bottom")})
        self.p_phase.setXLink(self.p_gain)
        for p, label, unit in ((self.p_gain, "Gain", "dB"), (self.p_phase, "Phase", "°")):
            p.setLogMode(x=True, y=False)
            p.showGrid(x=True, y=True, alpha=0.2)
            p.setLabel("left", f"{label} ({unit})")
            p.getAxis("left").setWidth(62)
        self.p_phase.setLabel("bottom", "Frequency (Hz)")
        touch.plots(self.p_gain, self.p_phase)
        dim = pg.mkPen("#5b6270", width=1)
        self.old_gain = self.p_gain.plot(pen=dim)
        self.old_phase = self.p_phase.plot(pen=dim)
        self.c_gain = self.p_gain.plot(pen=pg.mkPen(C_VOLT, width=1.8), symbol="o", symbolSize=5,
                                       symbolBrush=C_VOLT, symbolPen=None)
        self.c_phase = self.p_phase.plot(pen=pg.mkPen(C_CURR, width=1.8), symbol="o",
                                         symbolSize=5, symbolBrush=C_CURR, symbolPen=None)
        v.addWidget(glw, 1)
        return w

    # ---- settings
    def current(self) -> bode.BodeSettings:
        return bode.BodeSettings(
            self.fgen.name(), self.fgen_ch.value(), self.scope.name(), self.ch_in.value(),
            self.ch_out.value(), self.f_start.value(), self.f_stop.value(),
            self.per_decade.value(), self.amplitude.value(), self.offset.value(),
            self.settle.value(), self.cycles.value(), self.average.value(),
            self.autoscale.isChecked(), self.off_at_end.isChecked())

    def settings(self):
        return asdict(self.current())

    def restore(self, s):
        self.fgen.choose(s.get("fgen", ""))
        self.scope.choose(s.get("scope", ""))
        for k in ("fgen_ch", "ch_in", "ch_out", "per_decade", "average"):
            if k in s:
                getattr(self, k).setValue(int(s[k]))
        for k in ("f_start", "f_stop", "amplitude", "offset", "settle", "cycles"):
            if k in s:
                getattr(self, k).setValue(float(s[k]))
        for k in ("autoscale", "off_at_end"):
            if k in s:
                getattr(self, k).setChecked(bool(s[k]))

    def used(self):
        return [self.fgen.name(), self.scope.name()]

    # ---- run
    def job(self):
        s = self.current()
        if s.problem():
            self.warn(s.problem())
            return None
        f, g, ph = bode.arrays(self.shown())
        self.old_gain.setData(f, g)
        self.old_phase.setData(f, ph)
        self.points = []
        self.redraw()
        return lambda bench: bode.Sweep(s).run(bench, self.runner.event.emit)

    def on_event(self, p):
        self.points.append(p)
        self.status.setText(f"{len(self.points)} of {len(self.current().frequencies())} points")
        self.redraw()

    def shown(self):
        return bode.normalise(self.points, self.reference) if self.reference else self.points

    def redraw(self):
        pts = self.shown()
        f, g, ph = bode.arrays(pts)
        self.c_gain.setData(f, g)
        self.c_phase.setData(f, ph)
        self.result.setText(("Reference in use.  " if self.reference else "")
                            + bode.describe(bode.analyse(pts)))

    def use_reference(self):
        if len(bode.arrays(self.points)[0]) < 2:
            return self.warn("Measure a sweep first (both probes on the circuit's input)")
        self.reference = list(self.points)
        self.redraw()

    def clear_reference(self):
        self.reference = []
        self.redraw()

    def save_csv(self):
        rows = [["" if isinstance(x, float) and math.isnan(x) else x for x in
                 (p.f, p.gain_db, p.phase, p.vin, p.vout, p.fit, p.note)] for p in self.shown()]
        self.save_rows(HEADER, rows, "bode.csv")

    def report(self):
        pts = self.shown()
        f, g, ph = bode.arrays(pts)
        if f.size < 2:
            return self.warn("Nothing measured yet")
        s = asdict(self.current())
        self.save_report(report.bode_report("Frequency response", f, g, ph,
                                            bode.describe(bode.analyse(pts)), s,
                                            self.inst_list(), bool(self.reference)), "bode")
