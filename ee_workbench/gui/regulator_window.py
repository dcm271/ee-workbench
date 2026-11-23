"""Regulator test window (Tools ▾): input voltage × load sweeps (regulator.py)."""
from __future__ import annotations

from dataclasses import asdict

import pyqtgraph as pg
from PyQt6.QtWidgets import (QAbstractItemView, QFormLayout, QGroupBox, QLabel, QLineEdit,
                             QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .. import regulator as reg
from .. import report
from . import touch
from .forms import hint, number
from .tool_window import InstBox, PortBox, ToolWindow
from .widgets import C_BAD, C_GOOD

COLORS = ["#4fc3f7", "#ffb74d", "#81c784", "#e57373", "#ba68c8", "#fff176", "#90a4ae"]
PLOTS = [("Efficiency", "efficiency", "%"), ("Output voltage", "vout", "V"), ("Loss", "loss", "W")]


class RegulatorWindow(ToolWindow):
    TITLE = "Regulator test"
    KEY = "regulator"

    def buttons(self):
        return [("Save CSV…", self.save_csv), ("Report…", self.report)]

    def build_left(self):
        self.rows: list[dict] = []
        self.outcome = ""
        self.run_with = reg.RegSettings()
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 6, 0)
        g = QGroupBox("Instruments")
        f = QFormLayout(g)
        self.source = PortBox("source", optional="(choose)")
        f.addRow("Input supply", self.source)
        self.load = PortBox("load", optional="(choose)")
        f.addRow("Load", self.load)
        meter = (lambda p: p.KIND in ("dmm", "scpi"))
        self.vin_meter = InstBox(meter, "(the supply's own reading)")
        f.addRow("Vin meter", self.vin_meter)
        self.vout_meter = InstBox(meter, "(the load's own reading)")
        f.addRow("Vout meter", self.vout_meter)
        v.addWidget(g)
        g = QGroupBox("Sweep")
        f = QFormLayout(g)
        self.vin = QLineEdit("5, 9, 12")
        f.addRow("Input voltages (V)", self.vin)
        self.iload = QLineEdit("0, 0.1, 0.25, 0.5, 1")
        self.iload.setToolTip("5, 9, 12    or    4.5 … 16 step 0.5    or    0..2 x 11")
        self.vin.setToolTip(self.iload.toolTip())
        f.addRow("Load currents (A)", self.iload)
        self.vout_nominal = number("Nominal output", 0, 1000, 3.3, 3, " V")
        f.addRow("Nominal output", self.vout_nominal)
        self.vout_max = number("Highest allowed output", 0, 1000, 3.6, 3, " V")
        self.vout_max.setToolTip("Above this everything is switched off at once")
        f.addRow("Stop above", self.vout_max)
        self.iin_limit = number("Input current limit", 0.001, 100, 2, 3, " A")
        f.addRow("Input limit", self.iin_limit)
        self.settle = number("Settling time", 0, 60, 0.5, 2, " s")
        f.addRow("Settle", self.settle)
        self.average = number("Averaging time", 0, 60, 0.5, 2, " s")
        f.addRow("Average", self.average)
        v.addWidget(g)
        g = QGroupBox("Limits (0 = not judged)")
        f = QFormLayout(g)
        self.tolerance = number("Output tolerance", 0, 100, 0, 2, " %")
        f.addRow("Output ±", self.tolerance)
        self.min_efficiency = number("Lowest efficiency", 0, 100, 0, 1, " %")
        f.addRow("Efficiency ≥", self.min_efficiency)
        self.max_load_reg = number("Load regulation", 0, 100, 0, 3, " %")
        f.addRow("Load regulation ≤", self.max_load_reg)
        self.max_line_reg = number("Line regulation", 0, 100, 0, 3, " %")
        f.addRow("Line regulation ≤", self.max_line_reg)
        v.addWidget(g)
        v.addWidget(hint("Efficiency is only as good as the voltages: connect the supply's and "
                         "the load's sense leads at the board, or use meters there. The "
                         "efficiency limit applies at the highest load current."))
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
        self.plots = {}
        for n, (label, key, unit) in enumerate(PLOTS):
            p = glw.addPlot(row=n, col=0)
            p.showGrid(x=True, y=True, alpha=0.15)
            p.setLabel("left", label, units=unit if unit != "%" else None)
            if unit == "%":
                p.setLabel("left", "Efficiency (%)")
            p.getAxis("left").setWidth(62)
            if n == 0:
                p.addLegend(offset=(-10, -10), brush=pg.mkBrush(20, 22, 26, 200))
            else:
                p.setXLink(self.plots["efficiency"])
            self.plots[key] = p
        self.plots["loss"].setLabel("bottom", "Output current", units="A")
        touch.plots(*self.plots.values())
        v.addWidget(glw, 1)
        tabs.addTab(w, "Curves")
        self.table = QTableWidget(0, len(reg.COLUMNS))
        self.table.setHorizontalHeaderLabels(
            [f"{c} ({reg.UNITS[c]})" if c in reg.UNITS else c for c in reg.COLUMNS])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        tabs.addTab(self.table, "Points")
        tabs.addTab(self.make_log(), "Log")
        return tabs

    # ---- settings
    NUM = ("iin_limit", "vout_nominal", "vout_max", "settle", "average", "min_efficiency",
           "max_load_reg", "max_line_reg", "tolerance")

    def current(self):
        return reg.RegSettings(self.source.name(), self.load.name(), self.vout_meter.name(),
                               self.vin_meter.name(), self.vin.text(), self.iload.text(),
                               **{k: getattr(self, k).value() for k in self.NUM})

    def settings(self):
        return asdict(self.current())

    def restore(self, s):
        for k in ("source", "load", "vin_meter", "vout_meter"):
            getattr(self, k).choose(s.get(k, ""))
        for k in ("vin", "iload"):
            if s.get(k):
                getattr(self, k).setText(str(s[k]))
        for k in self.NUM:
            if k in s:
                getattr(self, k).setValue(float(s[k]))

    def used(self):
        return [self.source.tab(), self.load.tab(), self.vin_meter.name(),
                self.vout_meter.name()]

    # ---- run
    def job(self):
        s = self.current()
        if s.problem():
            self.warn(s.problem())
            return None
        if not self.confirm(f"{s.source} will be switched on at {s.vin} V (limit "
                            f"{s.iin_limit:g} A) and {s.load} will draw up to "
                            f"{max(reg.parse_list(s.iload)):g} A."):
            return None
        self.rows, self.run_with, self.outcome = [], s, ""
        self.table.setRowCount(0)
        self.redraw()
        return lambda bench: reg.RegTest(s).run(bench, self.runner.event.emit)

    def on_event(self, row):
        self.rows.append(row)
        self.status.setText(f"{len(self.rows)} of {self.run_with.points()} points")
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, k in enumerate(reg.COLUMNS):
            x = row[k]
            self.table.setItem(r, c, QTableWidgetItem(x if isinstance(x, str) else f"{x:.6g}"))
        self.redraw()

    def redraw(self):
        for _, key, _u in PLOTS:
            p = self.plots[key]
            p.clear()
            for n, vin in enumerate(sorted({r["vin_set"] for r in self.rows})):
                g = sorted((r for r in self.rows if r["vin_set"] == vin),
                           key=lambda r: r["iout"])
                col = COLORS[n % len(COLORS)]
                p.plot([r["iout"] for r in g], [r[key] for r in g], connect="finite",
                       pen=pg.mkPen(col, width=1.8), symbol="o", symbolSize=5, symbolBrush=col,
                       symbolPen=None, name=f"{vin:g} V in")
        self.summary()

    def summary(self):
        a = reg.analyse(self.rows, self.run_with)
        parts = []
        if "best" in a:
            b = a["best"]
            parts.append(f"Best efficiency {b['efficiency']:.1f} % at {b['vin_set']:g} V, "
                         f"{b['iload_set']:g} A")
        if a["load"]:
            w = max(a["load"], key=lambda x: abs(x["percent"]))
            parts.append(f"load regulation {w['percent']:.3f} % at {w['vin']:g} V")
        if a["line"]:
            w = max(a["line"], key=lambda x: abs(x["percent"]))
            parts.append(f"line regulation {w['percent']:.3f} % at {w['iload']:g} A")
        text = "   ·   ".join(parts)
        if self.outcome and a["verdict"]:
            col = C_GOOD if a["verdict"] == "PASS" else C_BAD
            why = [t for t, ok in a["checks"] if not ok]
            bad = "; ".join(why[:3]) + (f"; and {len(why) - 3} more" if len(why) > 3 else "")
            text = f"<b style='color:{col}'>{a['verdict']}</b>&nbsp; {text}" + \
                (f"<br>{bad}" if bad else "")
        self.result.setText(text)

    def finished(self, outcome):
        self.outcome = outcome
        self.summary()

    def save_csv(self):
        self.save_rows(reg.COLUMNS, [[r[c] for c in reg.COLUMNS] for r in self.rows],
                       "regulator.csv")

    def report(self):
        if not self.rows:
            return self.warn("Nothing measured yet")
        a = reg.analyse(self.rows, self.run_with)
        self.save_report(report.regulator_report("Regulator characterization", self.rows, a,
                                                 asdict(self.run_with), self.inst_list(),
                                                 self.outcome or "still running"), "regulator")
