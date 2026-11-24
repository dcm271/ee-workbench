"""Battery tester window (Tools ▾): charge / rest / discharge cycles (battery.py)."""
from __future__ import annotations

import math
import time
from dataclasses import asdict

import pyqtgraph as pg
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QFormLayout, QGroupBox, QLabel,
                             QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .. import battery as bat
from .. import report
from ..clock import now
from ..session import recorder as session
from . import touch
from .forms import choice, hint, integer, number, select
from .panel_base import open_panels
from .tool_window import InstBox, PortBox, ToolWindow
from .widgets import C_CURR, C_VOLT

COLORS = ["#4fc3f7", "#ffb74d", "#81c784", "#e57373", "#ba68c8", "#fff176", "#90a4ae"]
NUM = ("discharge_a", "cutoff_v", "charge_v", "charge_a", "taper_a", "rest_s", "interval",
       "max_ah", "max_hours", "temp_max", "ir_delay")
HEAD = ["Cycle", "Charge (Ah)", "Discharge (Ah)", "Discharge (Wh)", "Coulombic (%)",
        "R start (mΩ)", "R end (mΩ)", "Open circuit (V)", "Ended by"]


class ReadingBox(InstBox):
    """"tab: reading" of every open tab that measures something."""

    def __init__(self, want, optional=""):
        self.want = want
        super().__init__(lambda p: True, optional)

    def names(self):
        out = []
        for p in open_panels():
            for n in p.measure_names():
                if self.want(p, n):
                    out.append(f"{p.display_name()}: {n}")
        return out


def is_temperature(p, n) -> bool:
    unit = getattr(p, "unit_of", lambda _n: "")(n)
    return unit in ("°C", "C")


class BatteryWindow(ToolWindow):
    TITLE = "Battery tester"
    KEY = "battery"

    def buttons(self):
        return [("Save samples…", self.save_csv), ("Save cycles…", self.save_cycles),
                ("Report…", self.report)]

    def build_left(self):
        self.samples: list[dict] = []
        self.cycles: list[dict] = []
        self.outcome = ""
        self.run_with = bat.BatSettings()
        self._drawn = 0.0
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 6, 0)
        g = QGroupBox("Instruments")
        f = QFormLayout(g)
        self.load = PortBox("load", "sink", optional="(charge only)")
        f.addRow("Load", self.load)
        self.charger = PortBox("source", optional="(discharge only)")
        f.addRow("Charger", self.charger)
        self.temp = ReadingBox(is_temperature, "(none)")
        f.addRow("Thermometer", self.temp)
        v.addWidget(g)
        g = QGroupBox("Test")
        f = QFormLayout(g)
        self.chem = choice([(k, k) for k in bat.CHEMISTRIES])
        self.chem.activated.connect(lambda *_: self._preset())
        f.addRow("Chemistry", self.chem)
        self.cells = integer("Cells in series", 1, 20, 1)
        self.cells.valueChanged.connect(lambda *_: self._preset())
        f.addRow("Cells in series", self.cells)
        self.start_with = choice([("Charge first", "charge"), ("Discharge first", "discharge")])
        f.addRow("Start", self.start_with)
        self.n_cycles = integer("Cycles", 1, 10000, 1)
        f.addRow("Cycles", self.n_cycles)
        self.end_charged = QCheckBox("Charge again at the end")
        f.addRow(self.end_charged)
        for k, label, lo, hi, val, dec, unit in (
                ("discharge_a", "Discharge current", 0.001, 200, 0.5, 3, " A"),
                ("cutoff_v", "Cut-off voltage", 0.01, 1000, 3.0, 3, " V"),
                ("charge_v", "Charge voltage", 0.01, 1000, 4.2, 3, " V"),
                ("charge_a", "Charge current", 0.001, 200, 0.5, 3, " A"),
                ("taper_a", "Full below", 0.001, 200, 0.05, 3, " A"),
                ("rest_s", "Rest", 0, 86400, 600, 0, " s"),
                ("interval", "Sample every", 0.2, 600, 1, 1, " s"),
                ("ir_delay", "Resistance after", 0.1, 60, 1, 1, " s")):
            setattr(self, k, number(label, lo, hi, val, dec, unit))
            f.addRow(label, getattr(self, k))
        v.addWidget(g)
        g = QGroupBox("Safety")
        f = QFormLayout(g)
        for k, label, lo, hi, val, dec, unit in (
                ("temp_max", "Stop above", -20, 200, 50, 1, " °C"),
                ("max_ah", "Phase ends at (0 = off)", 0, 10000, 0, 3, " Ah"),
                ("max_hours", "Phase ends after", 0.01, 1000, 12, 2, " h")):
            setattr(self, k, number(label, lo, hi, val, dec, unit))
            f.addRow(label, getattr(self, k))
        v.addWidget(g)
        v.addWidget(hint(
            "The limits here are software. A lithium battery also needs its own protection "
            "circuit and a fuse, a surface that can't burn, and someone nearby; a load with its "
            "own cut-off has it set as well. Check that the supply isolates its output when it "
            "is off: one that doesn't will drain the battery while it rests.\n\n"
            "A 66321D can be the load: it sinks, trimmed by its voltage, up to 2.8 A at 0 V "
            "falling to 1.2 A at 15 V. It may be load and charger at once."))
        v.addStretch(1)
        return w

    def build_right(self):
        tabs = QTabWidget()
        w = QWidget()
        v = QVBoxLayout(w)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        v.addWidget(self.result)
        glw = pg.GraphicsLayoutWidget()
        self.p_v = glw.addPlot(row=0, col=0)
        self.p_i = glw.addPlot(row=1, col=0)
        self.p_c = glw.addPlot(row=2, col=0)
        for p, label, unit in ((self.p_v, "Voltage", "V"), (self.p_i, "Current", "A"),
                               (self.p_c, "Voltage", "V")):
            p.showGrid(x=True, y=True, alpha=0.15)
            p.setLabel("left", label, units=unit)
            p.getAxis("left").setWidth(62)
        self.p_i.setXLink(self.p_v)
        self.p_i.setLabel("bottom", "Time", units="s")
        self.p_c.setLabel("bottom", "Discharged (Ah)")
        self.p_c.addLegend(offset=(-10, 10), brush=pg.mkBrush(20, 22, 26, 200))
        touch.plots(self.p_v, self.p_i, self.p_c)
        self.c_v = self.p_v.plot(pen=pg.mkPen(C_VOLT, width=1.5))
        self.c_i = self.p_i.plot(pen=pg.mkPen(C_CURR, width=1.5))
        v.addWidget(glw, 1)
        tabs.addTab(w, "Curves")
        self.table = QTableWidget(0, len(HEAD))
        self.table.setHorizontalHeaderLabels(HEAD)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        tabs.addTab(self.table, "Cycles")
        tabs.addTab(self.make_log(), "Log")
        return tabs

    def _preset(self):
        chg, cut = bat.CHEMISTRIES[self.chem.currentData()]
        if chg:
            self.charge_v.setValue(chg * self.cells.value())
        if cut:
            self.cutoff_v.setValue(cut * self.cells.value())

    # ---- settings
    def current(self):
        return bat.BatSettings(
            load=self.load.name(), charger=self.charger.name(), temp=self.temp.name(),
            start=self.start_with.currentData(), cycles=self.n_cycles.value(),
            end_charged=self.end_charged.isChecked(),
            **{k: getattr(self, k).value() for k in NUM})

    def settings(self):
        return {**asdict(self.current()), "chemistry": self.chem.currentData(),
                "cells": self.cells.value()}

    def restore(self, s):
        for k in ("load", "charger", "temp"):
            getattr(self, k).choose(s.get(k, ""))
        select(self.chem, s.get("chemistry"))
        self.cells.blockSignals(True)
        self.cells.setValue(int(s.get("cells", 1)))
        self.cells.blockSignals(False)
        select(self.start_with, s.get("start"))
        self.n_cycles.setValue(int(s.get("cycles", 1)))
        self.end_charged.setChecked(bool(s.get("end_charged")))
        for k in NUM:
            if k in s:
                getattr(self, k).setValue(float(s[k]))

    def used(self):
        return [self.load.tab(), self.charger.tab(), self.temp.name().partition(": ")[0]]

    # ---- run
    def job(self):
        s = self.current()
        if s.problem():
            self.warn(s.problem())
            return None
        text = []
        if s.charger:
            text.append(f"charge to {s.charge_v:g} V at {s.charge_a:g} A from {s.charger}")
        if s.load:
            text.append(f"discharge to {s.cutoff_v:g} V at {s.discharge_a:g} A into {s.load}")
        if not self.confirm("The battery will " + " and ".join(text) + ".\n\nAre the voltages "
                            "right for this battery?"):
            return None
        self.samples, self.cycles, self.run_with, self.outcome = [], [], s, ""
        self.table.setRowCount(0)
        self.run_name = time.strftime("Battery %H:%M:%S")
        self.redraw()

        def fn(bench):
            bat.BatTest(s).run(bench, lambda x: self.runner.event.emit(("sample", x)),
                               lambda x: self.runner.event.emit(("cycle", x)))
        return fn

    def on_event(self, e):
        kind, x = e
        if kind == "cycle":
            self.cycles.append(x)
            r = self.table.rowCount()
            self.table.insertRow(r)
            vals = [x["cycle"], x["charge_ah"], x["discharge_ah"], x["discharge_wh"],
                    x["coulombic"], 1e3 * x["r_start"], 1e3 * x["r_end"], x["ocv"], x["ended"]]
            for c, val in enumerate(vals):
                text = val if isinstance(val, str) else "—" if math.isnan(val) else f"{val:.5g}"
                self.table.setItem(r, c, QTableWidgetItem(text))
            return
        self.samples.append(x)
        temp = f"   {x['temp']:.1f} °C" if math.isfinite(x["temp"]) else ""
        self.result.setText(f"Cycle {x['cycle']} · {x['phase']}   {x['voltage']:.4f} V   "
                            f"{x['current']:+.4f} A   {x['ah']:.4f} Ah   {x['wh']:.4f} Wh{temp}")
        self.status.setText(f"Running · {x['phase']}")
        if session.active:
            cols = ["cycle", "phase", "voltage_V", "current_A", "charge_Ah", "energy_Wh",
                    "temp_C"]
            key = ("battery", id(self))
            session.stream(key, self.run_name, "battery", "", cols)
            session.add(key, now(), [x["cycle"], x["phase"], x["voltage"], x["current"],
                                     x["ah"], x["wh"],
                                     x["temp"] if math.isfinite(x["temp"]) else None])
        if time.monotonic() - self._drawn > 0.5:
            self.redraw()

    def redraw(self):
        self._drawn = time.monotonic()
        t = [x["t"] for x in self.samples]
        self.c_v.setData(t, [x["voltage"] for x in self.samples])
        self.c_i.setData(t, [x["current"] for x in self.samples])
        self.p_c.clear()
        groups: dict = {}
        for x in self.samples:
            if x["phase"] == "discharge":
                groups.setdefault(x["cycle"], []).append(x)
        for c in sorted(groups)[-len(COLORS):]:
            g = groups[c]
            self.p_c.plot([x["ah"] for x in g], [x["voltage"] for x in g], name=f"cycle {c}",
                          pen=pg.mkPen(COLORS[(c - 1) % len(COLORS)], width=1.6))

    def finished(self, outcome):
        self.outcome = outcome
        self.redraw()

    def save_csv(self):
        self.save_rows(bat.COLUMNS, [[x[c] for c in bat.COLUMNS] for x in self.samples],
                       "battery.csv")

    def save_cycles(self):
        self.save_rows(bat.CYCLE_COLUMNS, [[x[c] for c in bat.CYCLE_COLUMNS]
                                           for x in self.cycles], "battery_cycles.csv")

    def report(self):
        if not self.samples:
            return self.warn("Nothing measured yet")
        self.save_report(report.battery_report(
            "Battery test", self.samples, self.cycles, asdict(self.run_with), self.inst_list(),
            self.outcome or "still running", self.log_view.toPlainText()), "battery")
