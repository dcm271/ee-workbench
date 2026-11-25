"""Load transient window (Tools ▾): transient.py."""
from __future__ import annotations

from dataclasses import asdict

import pyqtgraph as pg
from PyQt6.QtWidgets import (QAbstractItemView, QFormLayout, QGroupBox, QLabel, QTableWidget,
                             QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .. import report
from .. import transient as tr
from . import touch
from .forms import choice, hint, integer, number, select
from .tool_window import InstBox, PortBox, ToolWindow
from .widgets import C_BAD, C_GOOD

COLORS = ["#4fc3f7", "#ffb74d", "#81c784", "#e57373", "#ba68c8", "#fff176", "#90a4ae"]


class TransientWindow(ToolWindow):
    TITLE = "Load transient test"
    KEY = "transient"

    def buttons(self):
        return [("Save CSV…", self.save_csv), ("Report…", self.report)]

    def build_left(self):
        self.steps: list[tr.Step] = []
        self.outcome = ""
        self.run_with = tr.TransSettings()
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 6, 0)
        g = QGroupBox("Instruments")
        f = QFormLayout(g)
        self.load = PortBox("load", optional="(choose)")
        f.addRow("Load", self.load)
        self.source = PortBox("source", optional="(powered by something else)")
        f.addRow("Input supply", self.source)
        self.scope = InstBox(lambda p: p.KIND == "scope", "(choose)")
        f.addRow("Oscilloscope", self.scope)
        self.ch = choice([(f"CH{n}", n) for n in range(1, 5)])
        f.addRow("Output voltage on", self.ch)
        self.trig_ch = choice([("the output itself", 0)] + [(f"CH{n}", n) for n in range(1, 5)],
                              "Another channel: a current probe, or the load's trigger output")
        self.trig_ch.activated.connect(lambda *_: self._trig_changed())
        f.addRow("Trigger on", self.trig_ch)
        self.trig_mv = number("Trigger distance", 0.1, 10000, 20, 1, " mV")
        self.trig_mv.setToolTip("How far the output moves to trigger: less than the dip, more "
                                "than the ripple")
        f.addRow("Trigger at ±", self.trig_mv)
        self.trig_level = number("Trigger level", -1000, 1000, 0.5, 3, " V")
        f.addRow("Trigger level", self.trig_level)
        v.addWidget(g)
        g = QGroupBox("Step")
        f = QFormLayout(g)
        self.i_low = number("Low current", 0, 1000, 0.1, 3, " A")
        f.addRow("From", self.i_low)
        self.i_high = number("High current", 0, 1000, 1, 3, " A")
        f.addRow("To", self.i_high)
        self.vin = number("Input voltage", 0, 1000, 5, 3, " V")
        f.addRow("Input voltage", self.vin)
        self.iin_limit = number("Input current limit", 0, 100, 2, 3, " A")
        f.addRow("Input limit", self.iin_limit)
        self.vout_nominal = number("Nominal output", 0, 1000, 3.3, 3, " V")
        f.addRow("Nominal output", self.vout_nominal)
        self.vout_max = number("Highest allowed output", 0, 1000, 3.6, 3, " V")
        self.vout_max.setToolTip("Above this (the load's reading) everything is switched off")
        f.addRow("Stop above", self.vout_max)
        self.window_us = number("Time shown", 1, 10e6, 500, 1, " µs")
        f.addRow("Time after the step", self.window_us)
        self.settle = number("Settling time", 0, 60, 0.5, 2, " s")
        f.addRow("Wait between", self.settle)
        self.repeats = integer("Repeats", 1, 100, 3)
        f.addRow("Repeat", self.repeats)
        self.band = number("Band", 0.01, 100, 1, 2, " %")
        self.band.setToolTip("Recovered: inside this much of the nominal output around its "
                             "new level")
        f.addRow("Recovered within ±", self.band)
        v.addWidget(g)
        g = QGroupBox("Limits (0 = not judged)")
        f = QFormLayout(g)
        self.max_deviation = number("Dip / peak", 0, 100, 0, 2, " %")
        f.addRow("Dip / peak ≤", self.max_deviation)
        self.max_rebound = number("Rebound", 0, 100, 0, 2, " %")
        f.addRow("Rebound ≤", self.max_rebound)
        self.max_recovery_us = number("Recovery", 0, 10e6, 0, 1, " µs")
        f.addRow("Recovery ≤", self.max_recovery_us)
        v.addWidget(g)
        v.addWidget(hint("Set the channel's volts/division and offset so that dip and peak fit "
                         "on the screen (AC coupling is fine). The load's own slew rate decides "
                         "how fast the step is. Probe at the output capacitor with a short "
                         "ground."))
        v.addStretch(1)
        self._trig_changed()
        return w

    def _trig_changed(self):
        own = not self.trig_ch.currentData()
        self.trig_mv.setEnabled(own)
        self.trig_level.setEnabled(not own)

    def build_right(self):
        tabs = QTabWidget()
        w = QWidget()
        v = QVBoxLayout(w)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        v.addWidget(self.result)
        glw = pg.GraphicsLayoutWidget()
        self.plots = {}
        for n, (up, name) in enumerate(((True, "Load up"), (False, "Load down"))):
            p = glw.addPlot(row=n, col=0)
            p.showGrid(x=True, y=True, alpha=0.15)
            p.setLabel("left", name, units="V")
            p.setLabel("bottom", "Time", units="s")
            p.getAxis("left").setWidth(62)
            p.addLegend(offset=(-10, 10), brush=pg.mkBrush(20, 22, 26, 200))
            self.plots[up] = p
        touch.plots(*self.plots.values())
        v.addWidget(glw, 1)
        tabs.addTab(w, "Waveforms")
        self.table = QTableWidget(0, len(tr.COLUMNS))
        self.table.setHorizontalHeaderLabels(
            [f"{c} ({tr.UNITS[c]})" if c in tr.UNITS else c for c in tr.COLUMNS])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        tabs.addTab(self.table, "Steps")
        tabs.addTab(self.make_log(), "Log")
        return tabs

    # ---- settings
    NUM = ("trig_mv", "trig_level", "i_low", "i_high", "vin", "iin_limit", "vout_nominal",
           "vout_max", "settle", "band", "max_deviation", "max_rebound")

    def current(self):
        return tr.TransSettings(
            load=self.load.name(), source=self.source.name(), scope=self.scope.name(),
            ch=self.ch.currentData(), trig_ch=self.trig_ch.currentData(),
            repeats=self.repeats.value(), window=self.window_us.value() * 1e-6,
            max_recovery=self.max_recovery_us.value() * 1e-6,
            **{k: getattr(self, k).value() for k in self.NUM})

    def settings(self):
        return asdict(self.current())

    def restore(self, s):
        for k in ("load", "source", "scope"):
            getattr(self, k).choose(s.get(k, ""))
        for k in ("ch", "trig_ch"):
            if k in s:
                select(getattr(self, k), s[k])
        for k in self.NUM:
            if k in s:
                getattr(self, k).setValue(float(s[k]))
        for k, box in (("window", self.window_us), ("max_recovery", self.max_recovery_us)):
            if k in s:
                box.setValue(1e6 * float(s[k]))
        if "repeats" in s:
            self.repeats.setValue(int(s["repeats"]))
        self._trig_changed()

    def used(self):
        return [self.source.tab(), self.load.tab(), self.scope.name()]

    # ---- run
    def job(self):
        s = self.current()
        if s.problem():
            self.warn(s.problem())
            return None
        src = f"{s.source} will be switched on at {s.vin:g} V (limit {s.iin_limit:g} A) and " \
            if s.source else ""
        if not self.confirm(f"{src}{s.load} will step between {s.i_low:g} A and "
                            f"{s.i_high:g} A."):
            return None
        self.steps, self.run_with, self.outcome = [], s, ""
        self.table.setRowCount(0)
        self.redraw()
        return lambda bench: tr.TransTest(s).run(bench, self.runner.event.emit)

    def on_event(self, st):
        self.steps.append(st)
        self.status.setText(f"{len(self.steps)} of {2 * self.run_with.repeats} steps")
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, x in enumerate(st.row().values()):
            self.table.setItem(r, c, QTableWidgetItem(x if isinstance(x, str) else f"{x:.6g}"))
        self.redraw()

    def redraw(self):
        for up, p in self.plots.items():
            p.clear()
            for k, st in enumerate([x for x in self.steps if x.up == up][-len(COLORS):]):
                p.plot(st.t, st.v, pen=pg.mkPen(COLORS[k], width=1.4), name=str(st.n))
        self.summary()

    def summary(self):
        a = tr.summary(self.steps, self.run_with)
        parts = []
        for name, w in a["worst"].items():
            parts.append(f"{'dip' if name == 'up' else 'peak'} {1e3 * w['deviation']:.4g} mV, "
                         f"rebound {1e3 * w['rebound']:.4g} mV, "
                         f"recovery {1e6 * w['recovery']:.4g} µs")
        text = "Worst: " + "   ·   ".join(parts) if parts else ""
        if self.outcome and a["verdict"]:
            col = C_GOOD if a["verdict"] == "PASS" else C_BAD
            bad = "; ".join(t for t, ok in a["checks"] if not ok)
            text = f"<b style='color:{col}'>{a['verdict']}</b>&nbsp; {text}" + \
                (f"<br>{bad}" if bad else "")
        self.result.setText(text)

    def finished(self, outcome):
        self.outcome = outcome
        self.summary()

    def save_csv(self):
        self.save_rows(tr.COLUMNS, [list(x.row().values()) for x in self.steps],
                       "transient.csv")

    def report(self):
        if not self.steps:
            return self.warn("Nothing measured yet")
        a = tr.summary(self.steps, self.run_with)
        self.save_report(report.transient_report("Load transient", self.steps, a,
                                                 asdict(self.run_with), self.inst_list(),
                                                 self.outcome or "still running"), "transient")
