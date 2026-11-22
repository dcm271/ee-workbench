"""Watchdog window: rules that watch one instrument and act on others.

    PPK2 current > 50m for 10 ms → all outputs off
    66321D voltage < 3.0 → turn off "DUT rail"

The rules live in the hub (gui/hub.py), which every tab feeds with its
readings; the main window carries out the action. Rules are kept between
runs (bench.json) and saved in named setups."""
from __future__ import annotations

import re
import time

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                             QMainWindow, QPushButton, QSplitter, QTableWidget, QVBoxLayout,
                             QWidget)

from ..signals import name_matches
from ..watchdog import ACTIONS, Rule
from . import touch
from .hub import hub
from .panel_base import open_panels
from .widgets import C_MUTED, C_WARN

COLS = ["On", "Instrument", "Quantity", "", "Limit", "For (ms)", "Then", "Which",
        "Re-arm (s)", "In HW", "State"]
STATE = len(COLS) - 1
SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6}


def parse_si(text: str) -> float:
    """'50m' → 0.05, '10u' / '10µ' → 1e-5, '3.3' → 3.3, '50 mA' → 0.05."""
    s = text.strip().replace(" ", "")
    m = re.fullmatch(r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)([pnuµmkM]?)[A-Za-zΩ%]*", s)
    if not m:
        raise ValueError(f"'{text}' isn't a number (e.g. 50m, 3.3, 10u)")
    return float(m.group(1)) * SI.get(m.group(2), 1.0)


def fmt_si(v: float) -> str:
    for suf, k in (("m", 1e-3), ("u", 1e-6), ("n", 1e-9)):
        if v != 0 and abs(v) < 1 and abs(v) >= k:
            return f"{v / k:.6g}{suf}"
    return f"{v:.6g}"


class WatchdogWindow(QMainWindow):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Watchdog")
        self.resize(1180, 560)
        central = QWidget()
        v = QVBoxLayout(central)
        top = QHBoxLayout()
        self.armed = QCheckBox("Watchdog armed")
        self.armed.setChecked(hub.watchdog.armed)
        self.armed.toggled.connect(self._armed_toggled)
        top.addWidget(self.armed)
        for text, fn in (("＋ Rule", self.add_rule), ("Remove", self.remove_rule),
                         ("Re-arm tripped", self.rearm)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        top.addStretch(1)
        self.status = QLabel("")
        top.addWidget(self.status)
        v.addLayout(top)
        hint = QLabel("Each rule watches every reading of one instrument (a PPK2: every 10 µs "
                      "sample). “For” is how long it must stay past the limit; slower "
                      "instruments are checked once per reading. Limits take SI suffixes: "
                      "50m = 0.05, 10u = 0.00001. A rule that trips stays tripped until "
                      "re-armed (or “Re-arm (s)” later), and its action runs once. Rules "
                      "apply only while the instrument's tab is open and reading. “In HW” "
                      "also sets a supply's own OVP / current limit, so the DUT stays "
                      "protected even if this program stops. “capture” fires the logic "
                      "analyzer / scope tabs named in “Which” (blank: all of them).")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        v.addWidget(hint)
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        # the state is a label widget, which ResizeToContents doesn't see
        hh.setSectionResizeMode(STATE, QHeaderView.ResizeMode.Fixed)
        hh.resizeSection(STATE, 190 if touch.TOUCH else 140)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44 if touch.TOUCH else 34)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        split.addWidget(self.table)
        self.log = QListWidget()
        split.addWidget(self.log)
        split.setSizes([360, 140])
        v.addWidget(split, 1)
        self.setCentralWidget(central)
        for r in hub.watchdog.rules:
            self._add_row(r)
        self._refresh_state()

    # ---------------------------------------------------------------- rows
    def _names(self):
        return [p.display_name() for p in open_panels()
                if p.KIND in ("supply", "ppk2", "dmm", "gpio", "scpi", "logic", "scope")]

    def _add_row(self, rule: Rule):
        r = self.table.rowCount()
        self.table.insertRow(r)
        on = QCheckBox()
        on.setChecked(rule.enabled)
        src = QComboBox()
        src.setEditable(True)
        src.addItems(self._names())
        src.setCurrentText(rule.source)
        qty = QComboBox()
        qty.setEditable(True)
        op = QComboBox()
        op.addItems([">", "<"])
        op.setCurrentText(rule.op)
        lim = QLineEdit(fmt_si(rule.limit))
        lim.setMaximumWidth(110)
        hold = QDoubleSpinBox()
        hold.setRange(0, 600_000)
        hold.setDecimals(2)
        hold.setValue(rule.hold_ms)
        touch.attach_keypad(hold, "Hold (ms)")
        act = QComboBox()
        act.addItems(list(ACTIONS))
        act.setCurrentText(rule.action)
        tgt = QComboBox()
        tgt.setEditable(True)
        tgt.addItems([n for n in self._names()])
        tgt.setCurrentText(rule.target)
        rearm = QDoubleSpinBox()
        rearm.setRange(0, 86_400)
        rearm.setDecimals(1)
        rearm.setSpecialValueText("never")
        rearm.setValue(rule.rearm_s)
        rearm.setToolTip("Re-arm by itself this many seconds after tripping (never: stay "
                         "tripped until you press Re-arm)")
        touch.attach_keypad(rearm, "Re-arm after (s)")
        hw = QCheckBox()
        hw.setChecked(rule.hw)
        hw.setToolTip("Also set the limit in the supply itself (over-voltage protection, or "
                      "current limit + over-current protection), so it holds even if this "
                      "program, the PC or the cable fails. Only for “voltage >” / "
                      "“current >” rules on a power supply.")
        state = QLabel("")
        # long instrument names mustn't push the State column off the edge
        for cb in (src, tgt):
            cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(12)
        for c, w in enumerate((on, src, qty, op, lim, hold, act, tgt, rearm, hw, state)):
            self.table.setCellWidget(r, c, w)

        def fill_qty(*_):
            panel = next((p for p in open_panels()
                          if name_matches(src.currentText(), p.display_name())), None)
            keep = qty.currentText() or rule.quantity
            opts = (panel.quantities() if panel is not None else []) or \
                ["current", "voltage", "power", "value"]
            qty.blockSignals(True)
            qty.clear()
            qty.addItems(opts)
            qty.setCurrentText(keep if keep in opts or not opts else opts[0])
            qty.blockSignals(False)

        def act_changed(*_):
            tgt.setEnabled(act.currentText() in ("turn off", "capture"))
        src.currentTextChanged.connect(fill_qty)
        act.currentTextChanged.connect(act_changed)
        fill_qty()
        qty.setCurrentText(rule.quantity)
        act_changed()
        for w in (on, hw):
            w.toggled.connect(self._commit)
        rearm.valueChanged.connect(self._commit)
        for w in (src, qty, op, act, tgt):
            w.currentTextChanged.connect(self._commit)
        lim.editingFinished.connect(self._commit)
        hold.valueChanged.connect(self._commit)

    def _rule_at(self, r) -> Rule:
        w = self.table.cellWidget
        try:
            limit = parse_si(w(r, 4).text())
            w(r, 4).setStyleSheet("")
        except ValueError:
            w(r, 4).setStyleSheet(f"border-color:{C_WARN};")
            limit = hub.watchdog.rules[r].limit if r < len(hub.watchdog.rules) else 0.0
        return Rule(source=w(r, 1).currentText().strip(), quantity=w(r, 2).currentText().strip(),
                    op=w(r, 3).currentText(), limit=limit, hold_ms=w(r, 5).value(),
                    action=w(r, 6).currentText(), target=w(r, 7).currentText().strip(),
                    enabled=w(r, 0).isChecked(), rearm_s=w(r, 8).value(),
                    hw=w(r, 9).isChecked())

    def _commit(self, *_):
        old = hub.watchdog.rules
        new = []
        for r in range(self.table.rowCount()):
            rule = self._rule_at(r)
            if r < len(old) and old[r] == rule:
                rule = old[r]                  # keep its tripped state
            new.append(rule)
        hub.watchdog.rules = new
        self.main.save_bench_state()
        self.main.apply_hw_limits()
        self._refresh_state()

    def add_rule(self):
        names = self._names()
        ppk = next((n for n in names if "PPK2" in n), names[0] if names else "")
        rule = Rule(ppk, "current", ">", 0.05, 10.0)
        hub.watchdog.rules.append(rule)
        self._add_row(rule)
        self._commit()

    def remove_rule(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)
            self._commit()

    def rearm(self):
        hub.watchdog.rearm()
        self.log.addItem(time.strftime("%H:%M:%S  ") + "re-armed")
        self._refresh_state()

    def _armed_toggled(self, on):
        hub.watchdog.armed = on
        self.main.save_bench_state()
        self.main.apply_hw_limits()
        self._refresh_state()

    def _refresh_state(self):
        n_trip = 0
        for r, rule in enumerate(hub.watchdog.rules):
            if r >= self.table.rowCount():
                break
            lab = self.table.cellWidget(r, STATE)
            if rule.tripped:
                n_trip += 1
                lab.setText(f"  TRIPPED ({rule.trip_value:.4g})")
                lab.setStyleSheet("color:#ff8a80; font-weight:bold;")
            elif not rule.enabled or not hub.watchdog.armed:
                lab.setText("  off")
                lab.setStyleSheet(f"color:{C_MUTED};")
            else:
                hw = self.main.hw_status.get(id(rule), "") if rule.hw else ""
                bad = hw.startswith("not")
                lab.setText("  watching" + ("  · HW not set" if bad else "  · + HW" if hw
                                            else ""))
                lab.setStyleSheet("color:#ffb74d;" if bad else "color:#7ee787;")
                lab.setToolTip(f"In the instrument: {hw}" if hw else "")
        if not hub.watchdog.armed:
            self.status.setText("Disarmed: no rule is checked")
            self.status.setStyleSheet(f"color:{C_WARN};")
        else:
            self.status.setText(f"{n_trip} tripped" if n_trip else "Armed")
            self.status.setStyleSheet("color:#ff8a80;" if n_trip else "color:#7ee787;")

    def on_trip(self, rule, name, text):
        self.log.addItem(time.strftime("%H:%M:%S  ") + text)
        self.log.scrollToBottom()
        self._refresh_state()

    def showEvent(self, e):
        # instrument names may have changed: refresh the choices, keep the text
        for r in range(self.table.rowCount()):
            for c in (1, 7):
                cb = self.table.cellWidget(r, c)
                keep = cb.currentText()
                cb.blockSignals(True)
                cb.clear()
                cb.addItems(self._names())
                cb.setCurrentText(keep)
                cb.blockSignals(False)
        self._refresh_state()
        super().showEvent(e)

