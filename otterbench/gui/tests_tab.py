"""Tests tab: ready-made procedures that run on top of the normal readings.

* Battery life: logs current/charge/energy for a device on the supply and
  projects how long a real battery would last.
* Battery emulator: makes the output behave like a battery discharging
  (open-circuit voltage follows state of charge, internal resistance).
* Sequencer: a table of voltage/current steps with durations and ramps.
* I-V sweep: steps voltage (or current) and measures the response.

Every test stops on Stop, on ALL OFF (Esc) and on disconnect.
"""
from __future__ import annotations

import csv
import json
import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QFileDialog, QFormLayout, QGroupBox,
                             QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
                             QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from ..instruments.base import SupplyPoll
from . import touch
from .forms import choice, hint, integer, number
from .widgets import C_CURR, C_DVM, C_VOLT, fmt_si, io_errors

# Open-circuit voltage per cell at 0, 10, ... 100 % state of charge.
CHEMISTRIES = {
    "Li-ion / LiPo": [3.00, 3.45, 3.58, 3.65, 3.70, 3.75, 3.80, 3.87, 3.95, 4.05, 4.20],
    "LiFePO4": [2.50, 3.10, 3.20, 3.25, 3.28, 3.30, 3.31, 3.32, 3.33, 3.35, 3.60],
    "NiMH": [1.00, 1.15, 1.20, 1.22, 1.24, 1.25, 1.26, 1.28, 1.30, 1.33, 1.40],
    "Alkaline": [0.90, 1.05, 1.15, 1.20, 1.25, 1.30, 1.35, 1.40, 1.45, 1.50, 1.60],
}
SOC_PTS = np.linspace(0, 100, 11)
TESTS = [("Battery life", "life"), ("Battery emulator", "emu"), ("Sequencer", "seq"),
         ("I-V sweep", "iv")]


def ocv(chem: str, cells: int, soc: float) -> float:
    return float(np.interp(soc, SOC_PTS, CHEMISTRIES[chem])) * cells


class TestsTab(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.p = panel
        self.active = None              # key of the running test
        self.rows: list[list] = []      # logged data of the running/last test
        self.header: list[str] = []

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        sel = QHBoxLayout()
        self.sel_group = QButtonGroup(self)
        self.sel_group.setExclusive(True)
        for n, (label, key) in enumerate(TESTS):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(n == 0)
            self.sel_group.addButton(b, n)
            sel.addWidget(b)
        self.sel_group.idClicked.connect(self._select)
        v.addLayout(sel)

        body = QHBoxLayout()
        self.pages = QStackedWidget()
        self.pages.addWidget(touch.scroll(self._life()))
        self.pages.addWidget(touch.scroll(self._emu()))
        self.pages.addWidget(touch.scroll(self._seq()))
        self.pages.addWidget(touch.scroll(self._iv()))
        self.pages.setMinimumWidth(330 if touch.TOUCH else 300)
        self.pages.setMaximumWidth(460)
        body.addWidget(self.pages)

        right = QVBoxLayout()
        ctl = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(lambda: self.stop("Stopped"))
        self.stop_btn.setEnabled(False)
        self.save_btn = QPushButton("Save CSV…")
        self.save_btn.clicked.connect(self.save_csv)
        for b in (self.start_btn, self.stop_btn, self.save_btn):
            ctl.addWidget(b)
        ctl.addStretch(1)
        right.addLayout(ctl)
        self.status = QLabel("Choose a test, set it up, press Start.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-weight:bold;")
        right.addWidget(self.status)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.addLegend(offset=(-10, 10))
        touch.plots(self.plot)
        right.addWidget(self.plot, 1)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right.addWidget(self.result)
        body.addLayout(right, 1)
        v.addLayout(body, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.p.worker.job_result.connect(self._on_job)

    def key(self) -> str:
        return TESTS[self.sel_group.checkedId()][1]

    def _select(self, idx):
        if self.active:
            # can't switch while running: keep the running test's page
            self.sel_group.button([k for _, k in TESTS].index(self.active)).setChecked(True)
            return
        self.pages.setCurrentIndex(idx)

    # ================================================================ pages
    def _life(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(hint("Power the device from the supply (set the voltage on the left) and "
                         "log its consumption. Stops after the duration, when the charge is "
                         "used up, or if the voltage drops below the cutoff."))
        g = QGroupBox("Stop after")
        f = QFormLayout(g)
        self.life_dur = number("Duration", 0, 1e6, 10, 1, " min")
        self.life_dur.setSpecialValueText("no limit")
        f.addRow("Duration", self.life_dur)
        self.life_mah = number("Charge limit", 0, 1e6, 0, 1, " mAh")
        self.life_mah.setSpecialValueText("no limit")
        f.addRow("Charge used", self.life_mah)
        self.life_cut = number("Voltage cutoff", 0, 20, 0, 3, " V")
        self.life_cut.setSpecialValueText("off")
        f.addRow("Voltage below", self.life_cut)
        v.addWidget(g)
        g = QGroupBox("Battery to project")
        f = QFormLayout(g)
        self.life_cap = number("Battery capacity", 1, 1e6, 3000, 0, " mAh")
        f.addRow("Capacity", self.life_cap)
        v.addWidget(g)
        self.life_on = QCheckBox("Turn the output on at start")
        self.life_on.setChecked(True)
        self.life_off = QCheckBox("Turn the output off at the end")
        self.life_off.setChecked(True)
        v.addWidget(self.life_on)
        v.addWidget(self.life_off)
        v.addStretch(1)
        return w

    def _emu(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(hint("The output voltage follows the battery's open-circuit voltage as the "
                         "load drains it; the output resistance plays the internal "
                         "resistance. Charging (negative current) raises the charge."))
        g = QGroupBox("Battery")
        f = QFormLayout(g)
        self.emu_chem = choice([(k, k) for k in CHEMISTRIES])
        f.addRow("Chemistry", self.emu_chem)
        self.emu_cells = integer("Cells in series", 1, 12, 1)
        f.addRow("Cells", self.emu_cells)
        self.emu_cap = number("Capacity", 0.1, 1e6, 100, 1, " mAh")
        self.emu_cap.setToolTip("Small capacities make the discharge quick enough to watch")
        f.addRow("Capacity", self.emu_cap)
        self.emu_soc = number("Start charge", 0, 100, 100, 0, " %")
        f.addRow("Start charge", self.emu_soc)
        self.emu_r = number("Internal resistance", 0, 1.0, 0.1, 3, " Ω")
        f.addRow("Internal R", self.emu_r)
        self.emu_ilim = number("Current limit", 0, 3.1, 2.0, 3, " A")
        f.addRow("Current limit", self.emu_ilim)
        v.addWidget(g)
        self.emu_curve = hint("")
        v.addWidget(self.emu_curve)
        for wdg in (self.emu_chem, self.emu_cells):
            (wdg.currentIndexChanged if hasattr(wdg, "currentIndexChanged")
             else wdg.valueChanged).connect(self._emu_hint)
        self._emu_hint()
        self.emu_off = QCheckBox("Turn the output off when empty")
        self.emu_off.setChecked(True)
        v.addWidget(self.emu_off)
        v.addStretch(1)
        return w

    def _emu_hint(self, *_):
        chem, n = self.emu_chem.currentData(), self.emu_cells.value()
        self.emu_curve.setText(f"Full {ocv(chem, n, 100):.2f} V · half {ocv(chem, n, 50):.2f} V"
                               f" · empty {ocv(chem, n, 0):.2f} V")

    def _seq(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(hint("Each step sets the voltage and current limit, ramping over 'Ramp' "
                         "seconds if non-zero, then holds for 'Hold' seconds."))
        self.seq_table = QTableWidget(0, 4)
        self.seq_table.setHorizontalHeaderLabels(["Volts", "Amps", "Hold s", "Ramp s"])
        self.seq_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.seq_table.setMinimumHeight(200)
        v.addWidget(self.seq_table, 1)
        for row in ([3.0, 1.0, 5, 0], [3.7, 1.0, 5, 2], [4.2, 1.0, 5, 0]):
            self._seq_add(row)
        row = QHBoxLayout()
        for text, fn in (("Add", lambda: self._seq_add(None)), ("Remove", self._seq_remove),
                         ("Open…", self._seq_load), ("Save…", self._seq_save)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)
        f = QFormLayout()
        self.seq_rep = integer("Repeat", 1, 100000, 1, " ×")
        f.addRow("Repeat", self.seq_rep)
        v.addLayout(f)
        self.seq_on = QCheckBox("Turn the output on at start")
        self.seq_on.setChecked(True)
        self.seq_off = QCheckBox("Turn the output off at the end")
        self.seq_off.setChecked(True)
        v.addWidget(self.seq_on)
        v.addWidget(self.seq_off)
        return w

    def _seq_add(self, vals):
        if vals is None:
            last = self._seq_steps()[-1:] or [[3.0, 1.0, 5, 0]]
            vals = list(last[0])
        r = self.seq_table.rowCount()
        self.seq_table.insertRow(r)
        for c, x in enumerate(vals):
            self.seq_table.setItem(r, c, QTableWidgetItem(f"{x:g}"))

    def _seq_remove(self):
        rows = sorted({i.row() for i in self.seq_table.selectedIndexes()}, reverse=True) or \
            [self.seq_table.rowCount() - 1]
        for r in rows:
            if r >= 0:
                self.seq_table.removeRow(r)

    def _seq_steps(self) -> list[list[float]]:
        steps = []
        for r in range(self.seq_table.rowCount()):
            try:
                steps.append([float(self.seq_table.item(r, c).text()) for c in range(4)])
            except (AttributeError, ValueError):
                raise ValueError(f"Row {r + 1} has a missing or bad number")
        return steps

    @io_errors("Save sequence")
    def _seq_save(self):
        try:
            steps = self._seq_steps()
        except ValueError as e:
            QMessageBox.warning(self, "Sequence", str(e))
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save sequence", "sequence.json",
                                              "JSON (*.json)")
        if path:
            with open(path, "w") as fh:
                json.dump({"repeat": self.seq_rep.value(),
                           "steps": [dict(zip(("volts", "amps", "hold_s", "ramp_s"), s))
                                     for s in steps]}, fh, indent=2)

    def _seq_load(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open sequence", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path) as fh:
                data = json.load(fh)
            steps = [[float(s["volts"]), float(s["amps"]), float(s.get("hold_s", 1)),
                      float(s.get("ramp_s", 0))] for s in data["steps"]]
        except Exception as e:
            QMessageBox.warning(self, "Sequence", f"Couldn't read {path}:\n{e}")
            return
        self.seq_table.setRowCount(0)
        for s in steps:
            self._seq_add(s)
        self.seq_rep.setValue(int(data.get("repeat", 1)))

    def _iv(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(hint("Steps the output and measures at each step. Sweep voltage for "
                         "diodes/LEDs/resistors; sweep the current limit for a constant-"
                         "current curve."))
        g = QGroupBox("Sweep")
        f = QFormLayout(g)
        self.iv_mode = choice([("Voltage", "V"), ("Current limit", "I")])
        self.iv_mode.currentIndexChanged.connect(self._iv_mode)
        f.addRow("Sweep", self.iv_mode)
        self.iv_from = number("Sweep from", 0, 15.5, 0, 4, " V")
        self.iv_to = number("Sweep to", 0, 15.5, 3, 4, " V")
        self.iv_n = integer("Steps", 2, 1000, 31)
        self.iv_lim = number("Fixed limit", 0, 3.1, 0.1, 4, " A")
        self.iv_dwell = number("Settle time", 0, 60, 0.2, 2, " s")
        f.addRow("From", self.iv_from)
        f.addRow("To", self.iv_to)
        f.addRow("Points", self.iv_n)
        self.iv_lim_lbl = QLabel("Current limit")
        f.addRow(self.iv_lim_lbl, self.iv_lim)
        f.addRow("Settle", self.iv_dwell)
        self.iv_back = QCheckBox("Sweep back down too")
        f.addRow(self.iv_back)
        v.addWidget(g)
        self.iv_off = QCheckBox("Turn the output off at the end")
        self.iv_off.setChecked(True)
        v.addWidget(self.iv_off)
        v.addStretch(1)
        return w

    def _iv_mode(self):
        volt = self.iv_mode.currentData() == "V"
        caps = self.p.caps
        for s, val in ((self.iv_from, 0.0), (self.iv_to, 3.0 if volt else 0.5)):
            s.setSuffix(" V" if volt else " A")
            s.setRange(0, caps.v_max if volt else caps.i_max)
            s.setValue(val)
        self.iv_lim.setSuffix(" A" if volt else " V")
        self.iv_lim.setRange(0, caps.i_max if volt else caps.v_max)
        self.iv_lim.setValue(0.1 if volt else 5.0)
        self.iv_lim_lbl.setText("Current limit" if volt else "Voltage")

    def on_connected(self, caps):
        self._iv_mode()
        self.emu_ilim.setRange(0, caps.i_max)
        self.emu_r.setEnabled(caps.has_resistance)
        self.emu_r.setToolTip("" if caps.has_resistance else
                              "This supply has no programmable output resistance")
        self.emu_r.setRange(0 if not caps.has_resistance else max(0, caps.r_min), caps.r_max)

    # ================================================================ run
    def _confirm_on(self) -> bool:
        if self.p.sp.output:
            return True
        return QMessageBox.question(
            self, "Turn output on", f"This test turns the {self.p.caps.model} output ON.\n\n"
            "Is the load connected and is it safe?") == QMessageBox.StandardButton.Yes

    def start(self):
        if not self.p.connected or self.active:
            return
        key = self.key()
        try:
            ok = {"life": self._start_life, "emu": self._start_emu, "seq": self._start_seq,
                  "iv": self._start_iv}[key]()
        except ValueError as e:
            QMessageBox.warning(self, "Test", str(e))
            return
        if not ok:
            return
        self.result.setText("")
        self.active = key
        self.t_start = time.monotonic()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        for b in self.sel_group.buttons():
            b.setEnabled(b.isChecked())
        self.timer.start()

    def stop(self, why: str, output_off: bool | None = None):
        if not self.active:
            return
        key, self.active = self.active, None
        self.timer.stop()
        if key == "iv":
            self.p.worker.poll_enabled = not self.p.pause_btn.isChecked()
        if output_off is None:
            output_off = {"life": self.life_off, "emu": self.emu_off, "seq": self.seq_off,
                          "iv": self.iv_off}[key].isChecked()
        if output_off and self.p.connected:
            self.p.cmd(lambda d: d.set_output(False))
        if key == "emu" and self.p.connected and self.p.caps.has_resistance:
            self.p.cmd(lambda d: d.set_resistance(0.0))
        self.status.setText(f"{why}  ({time.strftime('%H:%M:%S')})")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        for b in self.sel_group.buttons():
            b.setEnabled(True)
        if why != "Finished":
            self.p.message.emit(f"{self.p.caps.model} test: {why}")

    def abort(self):
        """ALL OFF / disconnect: stop without sending anything more."""
        if self.active:
            self.stop("Stopped by ALL OFF", output_off=False)

    def _new_log(self, header):
        self.header = header
        self.rows = []

    # ---------------------------------------------------------------- life
    def _start_life(self) -> bool:
        if self.life_on.isChecked() and not self._confirm_on():
            return False
        self._new_log(["t_s", "voltage_V", "current_A", "charge_mAh", "energy_mWh"])
        self.q = self.e = 0.0
        self.last = None
        self.p.poll.measure_voltage = True
        if self.life_on.isChecked():
            self.p.cmd(lambda d: d.set_output(True))
        self._plot_setup("Current", "A")
        self.status.setText("Logging…")
        return True

    def _life_reading(self, r):
        if r.current is None:
            return
        t = time.monotonic() - self.t_start
        if self.last is not None:
            dt = t - self.last[0]
            i_avg = (r.current + self.last[1]) / 2
            self.q += i_avg * dt / 3.6                       # mAh
            if r.voltage is not None:
                self.e += i_avg * r.voltage * dt / 3.6       # mWh
        self.last = (t, r.current)
        self.rows.append([t, r.voltage, r.current, self.q, self.e])
        hours = t / 3600
        i_avg = self.q / hours if hours > 0 else float("nan")
        proj = self.life_cap.value() / i_avg if i_avg and i_avg > 0 else float("nan")
        self.result.setText(
            f"Elapsed {self._hms(t)}   average {fmt_si(i_avg / 1000, 'A')}   "
            f"charge {self.q:.3f} mAh   energy {self.e:.3f} mWh\n"
            f"A {self.life_cap.value():.0f} mAh battery would last about "
            f"{self._hms(proj * 3600) if np.isfinite(proj) else '—'}")
        if self.life_dur.value() and t >= self.life_dur.value() * 60:
            self.stop("Finished")
        elif self.life_mah.value() and self.q >= self.life_mah.value():
            self.stop("Finished: charge limit reached")
        elif self.life_cut.value() and r.voltage is not None and r.output and \
                r.voltage < self.life_cut.value():
            self.stop(f"Voltage fell below {self.life_cut.value():.3f} V")

    # ---------------------------------------------------------------- emulator
    def _start_emu(self) -> bool:
        if not self._confirm_on():
            return False
        self.soc = self.emu_soc.value()
        self.last = None
        self.emu_sent = (0.0, None)
        self._new_log(["t_s", "soc_pct", "ocv_V", "voltage_V", "current_A"])
        v0 = ocv(self.emu_chem.currentData(), self.emu_cells.value(), self.soc)
        if v0 > self.p.caps.v_max:
            raise ValueError(f"That battery's voltage ({v0:.2f} V) is above what the supply "
                             f"can do ({self.p.caps.v_max:.2f} V).")
        r_int, ilim = self.emu_r.value(), self.emu_ilim.value()
        caps = self.p.caps

        def setup(d):
            d.set_current(ilim)
            d.set_voltage(v0)
            if caps.has_resistance:
                d.set_resistance(r_int)
            d.set_output(True)
        self.p.cmd(setup)
        self._plot_setup("Voltage", "V")
        self.status.setText("Emulating…")
        return True

    def _emu_reading(self, r):
        if r.current is None:
            return
        t = time.monotonic() - self.t_start
        if self.last is not None:
            dt = t - self.last[0]
            self.soc -= (r.current + self.last[1]) / 2 * dt / 3.6 / self.emu_cap.value() * 100
            self.soc = min(100.0, self.soc)
        self.last = (t, r.current)
        v = ocv(self.emu_chem.currentData(), self.emu_cells.value(), max(0.0, self.soc))
        self.rows.append([t, self.soc, v, r.voltage, r.current])
        sent_t, sent_v = self.emu_sent
        if (sent_v is None or abs(v - sent_v) >= 0.0005) and t - sent_t >= 0.5:
            self.emu_sent = (t, v)
            self.p.worker.submit(lambda d, x: d.set_voltage(x), v)
        self.result.setText(f"Charge {max(0.0, self.soc):.2f} %   open-circuit {v:.4f} V   "
                            f"current {fmt_si(r.current, 'A')}   elapsed {self._hms(t)}")
        if self.soc <= 0:
            self.stop("Finished: battery empty")

    # ---------------------------------------------------------------- sequencer
    def _start_seq(self) -> bool:
        steps = self._seq_steps()
        if not steps:
            raise ValueError("Add at least one step")
        caps = self.p.caps
        for n, (v, i, hold, ramp) in enumerate(steps, 1):
            if not (0 <= v <= caps.v_max and caps.i_min <= i <= caps.i_max and hold >= 0
                    and ramp >= 0):
                raise ValueError(f"Step {n} is outside the supply's range")
        if self.seq_on.isChecked() and not self._confirm_on():
            return False
        self.steps = steps
        self.seq_pos = (0, 0)               # (repeat, step)
        self.seq_t0 = None
        self.seq_from = (self.p.sp.voltage or 0.0, self.p.sp.current or steps[0][1])
        self.seq_last_send = 0.0
        self._new_log(["t_s", "step", "set_V", "set_A", "voltage_V", "current_A"])
        self.seq_set = (None, None)
        if self.seq_on.isChecked():
            v, i = steps[0][0] if steps[0][3] == 0 else self.seq_from[0], steps[0][1]
            self.p.cmd(lambda d, v, i: (d.set_current(i), d.set_voltage(v), d.set_output(True)),
                       v, i)
        self._plot_setup("Voltage", "V")
        return True

    def _seq_tick(self):
        now = time.monotonic()
        rep, k = self.seq_pos
        v1, i1, hold, ramp = self.steps[k]
        if self.seq_t0 is None:
            self.seq_t0 = now
        el = now - self.seq_t0
        if ramp > 0 and el < ramp:
            a = el / ramp
            v = self.seq_from[0] + (v1 - self.seq_from[0]) * a
            i = self.seq_from[1] + (i1 - self.seq_from[1]) * a
        else:
            v, i = v1, i1
        if (v, i) != self.seq_set and (now - self.seq_last_send >= 0.2 or (v, i) == (v1, i1)):
            self.seq_set = (v, i)
            self.seq_last_send = now
            self.p.worker.submit(lambda d, v, i: (d.set_current(i), d.set_voltage(v)), v, i)
        total = len(self.steps)
        self.status.setText(f"Step {k + 1} of {total}, pass {rep + 1} of {self.seq_rep.value()}"
                            f" · {v:.3f} V, {i:.4f} A · {max(0, ramp + hold - el):.1f} s left")
        if el >= ramp + hold:
            self.seq_from = (v1, i1)
            self.seq_t0 = None
            k += 1
            if k >= total:
                k, rep = 0, rep + 1
                if rep >= self.seq_rep.value():
                    self.p.cmd(lambda d: None)        # refresh the setpoints shown
                    self.stop("Finished")
                    return
            self.seq_pos = (rep, k)

    def _seq_reading(self, r):
        t = time.monotonic() - self.t_start
        rep, k = self.seq_pos
        sv, si = self.seq_set
        self.rows.append([t, k + 1, sv, si, r.voltage, r.current])

    # ---------------------------------------------------------------- I-V
    def _start_iv(self) -> bool:
        if not self._confirm_on():
            return False
        a, b, n = self.iv_from.value(), self.iv_to.value(), self.iv_n.value()
        pts = list(np.linspace(a, b, n))
        if self.iv_back.isChecked():
            pts += pts[-2::-1]
        self.iv_pts = pts
        self.iv_k = 0
        self.iv_volt = self.iv_mode.currentData() == "V"
        self._new_log(["set", "voltage_V", "current_A"])
        lim, volt, dwell = self.iv_lim.value(), self.iv_volt, self.iv_dwell.value()
        self.p.worker.poll_enabled = False

        def setup(d):
            if volt:
                d.set_current(lim)
                d.set_voltage(pts[0])
            else:
                d.set_voltage(lim)
                d.set_current(pts[0])
            d.set_output(True)
        self.p.worker.submit(setup)
        self.iv_dwell_s = dwell
        self._iv_next()
        self.plot.clear()
        self.plot.setLabel("bottom", "Voltage", units="V")
        self.plot.setLabel("left", "Current", units="A")
        self.iv_curve = self.plot.plot(pen=pg.mkPen(C_CURR, width=1.5), symbol="o",
                                       symbolSize=5, symbolBrush=C_CURR)
        return True

    def _iv_next(self):
        x, volt, dwell = self.iv_pts[self.iv_k], self.iv_volt, self.iv_dwell_s

        def step(d):
            (d.set_voltage if volt else d.set_current)(x)
            time.sleep(dwell)
            r = d.poll(0, SupplyPoll(measure_voltage=True))
            return x, r.voltage, r.current
        self.p.worker.submit_result("iv", step)
        self.status.setText(f"Point {self.iv_k + 1} of {len(self.iv_pts)}")

    def _on_job(self, tag, value):
        if tag != "iv" or self.active != "iv":
            return
        self.rows.append(list(value))
        d = np.array([[r[1] or np.nan, r[2] or np.nan] for r in self.rows])
        self.iv_curve.setData(d[:, 0], d[:, 1])
        self.iv_k += 1
        if self.iv_k >= len(self.iv_pts):
            self.stop("Finished")
            self.p.cmd(lambda d: None)
            v, i = d[:, 0], d[:, 1]
            ok = np.isfinite(v) & np.isfinite(i) & (np.abs(i) > 1e-6)
            if ok.sum() >= 2:
                r_fit = np.polyfit(i[ok], v[ok], 1)[0]
                self.result.setText(f"{len(self.rows)} points · slope dV/dI ≈ {fmt_si(r_fit, 'Ω')}")
        else:
            self._iv_next()

    # ================================================================ shared
    def on_reading(self, r):
        if not self.active:
            return
        fn = {"life": self._life_reading, "emu": self._emu_reading,
              "seq": self._seq_reading}.get(self.active)
        if fn:
            fn(r)
            self._dirty = True

    def _tick(self):
        if self.active == "seq":
            self._seq_tick()
        if getattr(self, "_dirty", False):
            self._dirty = False
            self._redraw()

    def _plot_setup(self, label, unit):
        self.plot.clear()
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.setLabel("left", label, units=unit)
        self.curves = {}

    def _redraw(self):
        if not self.rows or self.active in (None, "iv"):
            return
        d = np.array(self.rows, dtype=float)
        t = d[:, 0]
        series = {"life": [("current", 2, C_CURR)],
                  "emu": [("output V", 3, C_VOLT), ("open-circuit V", 2, C_DVM)],
                  "seq": [("set V", 2, C_DVM), ("measured V", 4, C_VOLT)]}[self.active]
        for name, col, color in series:
            if name not in self.curves:
                self.curves[name] = self.plot.plot(pen=pg.mkPen(color, width=1.3), name=name)
            self.curves[name].setData(t, d[:, col], connect="finite")

    @staticmethod
    def _hms(s: float) -> str:
        if not np.isfinite(s):
            return "—"
        s = int(s)
        if s >= 86400:
            return f"{s // 86400} d {s % 86400 // 3600} h"
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"

    @io_errors("Save test data")
    def save_csv(self):
        if not self.rows:
            self.p.message.emit("Nothing to save yet.")
            return
        name = time.strftime(f"{self.p.caps.model or 'supply'}_{self.key()}_%Y%m%d_%H%M%S.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Save test data", name, "CSV (*.csv)")
        if path:
            with open(path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(self.header)
                w.writerows([["" if x is None else x for x in r] for r in self.rows])
            self.p.message.emit(f"Saved {path}")
