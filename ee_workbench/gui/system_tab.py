"""System tab for the 663xx: protection, output response, rear port, front
display, saved states, presets, instrument information and self-test.
Every group is shown only if the connected model supports it."""
from __future__ import annotations

import json
import time

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                             QPushButton, QVBoxLayout, QWidget)

from ..config import config_dir
from . import touch
from .forms import choice, hint, number, select
from .widgets import C_MUTED, C_ON, C_WARN, io_errors

COMP_MODES = {
    "LLOC": ("Low capacitance, local sense",
             "Fast response for loads with little capacitance (< 5 µF). Instrument default."),
    "HLOC": ("High capacitance, local sense",
             "Stable with large load capacitance; slower response."),
    "LREM": ("Low capacitance, remote sense",
             "Like LLOC with sense leads. Use short leads (or long, twisted leads)."),
    "HREM": ("High capacitance, remote sense",
             "Long sense leads and load capacitance above 5 µF (e.g. a phone with "
             "its bypass caps). Unstable with less than 5 µF on the output."),
    "LOW": ("Low capacitance", "Fast response, little load capacitance."),
    "HIGH": ("High capacitance", "Stable with large load capacitance."),
}
RI_MODES = [("Latching", "LATC"), ("Live", "LIVE"), ("Off", "OFF")]
DFI_SOURCES = [("Questionable (protection)", "QUES"), ("Operation", "OPER"),
               ("Event status", "ESB"), ("Service request", "RQS"), ("None", "OFF")]
PORT_FUNCS = [("Remote inhibit / fault", "RIDF"), ("Digital I/O", "DIG"),
              ("External trigger", "TRIG")]
PON_STATES = [("Defaults (*RST)", "RST"), ("Saved state 0", "RCL0")]


class SystemTab(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.p = panel
        self.alarm_since = {}
        cols = QHBoxLayout(self)
        left = QVBoxLayout()
        cols.addLayout(left, 1)
        if touch.TOUCH:
            right = left        # 1280x800: one scrolling column fits better
        else:
            right = QVBoxLayout()
            cols.addLayout(right, 1)

        left.addWidget(self._protection())
        left.addWidget(self._alarms())
        left.addWidget(self._response())
        left.addWidget(self._events(), 1)
        right.addWidget(self._port())
        right.addWidget(self._display())
        right.addWidget(self._states())
        right.addWidget(self._info())
        right.addStretch(1)

    # ================================================================ build
    def _protection(self):
        g = self.g_prot = QGroupBox("Protection")
        f = self.prot_form = QFormLayout(g)
        self.ovp_track = QCheckBox("Over-voltage trip, 2 V above the setting")
        self.ovp_track.setToolTip("Turns the output off if the output goes more than about 2 V "
                                  "above the voltage setting (e.g. a load back-driving it).")
        self.ovp_track.clicked.connect(lambda on: self.p.cmd(lambda d, x: d.set_ovp_tracking(x), on))
        f.addRow(self.ovp_track)
        row = QHBoxLayout()
        self.delay = number("Protection delay", 0, 2147483, 0.08, 3, " s")
        self.delay.setToolTip("How long current limit must last before OCP trips "
                              "(also delays the trip after a programming change). Default 0.08 s.")
        row.addWidget(self.delay, 1)
        b = QPushButton("Set")
        b.clicked.connect(lambda: self.p.cmd(lambda d, x: d.set_prot_delay(x), self.delay.value()))
        row.addWidget(b)
        self.delay_row = QWidget()
        self.delay_row.setLayout(row)
        f.addRow("OCP delay", self.delay_row)
        self.sense_prot = QCheckBox("Open sense-lead detection")
        self.sense_prot.setToolTip("Turns the output off if a remote-sense lead comes "
                                   "disconnected.")
        self.sense_prot.clicked.connect(lambda on: self.p.cmd(lambda d, x: d.set_sense_protect(x), on))
        f.addRow(self.sense_prot)
        return g

    def _alarms(self):
        g = QGroupBox("Software limits (checked by this app)")
        grid = QGridLayout(g)
        self.al_i = QCheckBox("Output off if current above")
        self.al_i_val = number("Current alarm", 0, 3.1, 1.0, 4, " A")
        self.al_v = QCheckBox("Output off if voltage below")
        self.al_v_val = number("Voltage alarm", 0, 15.5, 3.0, 3, " V")
        self.al_t = number("Alarm time", 0, 3600, 1.0, 1, " s")
        grid.addWidget(self.al_i, 0, 0)
        grid.addWidget(self.al_i_val, 0, 1)
        grid.addWidget(self.al_v, 1, 0)
        grid.addWidget(self.al_v_val, 1, 1)
        grid.addWidget(QLabel("for longer than"), 2, 0, Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self.al_t, 2, 1)
        grid.addWidget(hint("Uses the readings, so it is only as fast as the poll rate. For "
                            "fast protection use OCP."), 3, 0, 1, 2)
        return g

    def _response(self):
        g = self.g_comp = QGroupBox("Output response")
        v = QVBoxLayout(g)
        self.comp = choice([])
        self.comp.activated.connect(self._set_comp)
        v.addWidget(self.comp)
        self.comp_hint = hint("")
        v.addWidget(self.comp_hint)
        return g

    def _events(self):
        g = QGroupBox("Protection events")
        v = QVBoxLayout(g)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Trips and warnings reported by the supply appear here.")
        self.log.setMinimumHeight(70)
        v.addWidget(self.log)
        return g

    def _port(self):
        g = self.g_port = QGroupBox("Rear control port")
        f = QFormLayout(g)
        self.port_func = choice(PORT_FUNCS)
        self.port_func.setToolTip("What pins 1-3 of the rear control connector do. "
                                  "Stored in non-volatile memory.")
        self.port_func.activated.connect(self._set_port_func)
        f.addRow("Function", self.port_func)
        self.ri_mode = choice(RI_MODES, "Remote inhibit (pin 3 pulled low turns the output off).\n"
                                        "Latching: stays off until cleared. Live: follows "
                                        "the input.")
        self.ri_mode.activated.connect(
            lambda: self.p.cmd(lambda d, x: d.set_ri_mode(x), self.ri_mode.currentData()))
        self.ri_row = f.rowCount()
        f.addRow("Remote inhibit", self.ri_mode)
        dfi = QHBoxLayout()
        self.dfi = QCheckBox("Fault output")
        self.dfi.setToolTip("Discrete fault indicator: pins 1-2 signal when the chosen "
                            "status event happens")
        self.dfi_src = choice(DFI_SOURCES)
        self.dfi.clicked.connect(self._set_dfi)
        self.dfi_src.activated.connect(self._set_dfi)
        dfi.addWidget(self.dfi)
        dfi.addWidget(self.dfi_src, 1)
        self.dfi_w = QWidget()
        self.dfi_w.setLayout(dfi)
        f.addRow("Fault", self.dfi_w)
        io = QHBoxLayout()
        self.bit0 = QCheckBox("Out 0 (pin 1)")
        self.bit1 = QCheckBox("Out 1 (pin 2)")
        for b in (self.bit0, self.bit1):
            b.clicked.connect(self._set_bits)
            io.addWidget(b)
        self.in_lbl = QLabel("In: ?")
        io.addWidget(self.in_lbl)
        rd = QPushButton("Read")
        rd.setToolTip("Read the input pin (pin 3)")
        rd.clicked.connect(lambda: self.p.cmd(lambda d: None))
        io.addWidget(rd)
        self.io_w = QWidget()
        self.io_w.setLayout(io)
        f.addRow("Digital I/O", self.io_w)
        self.port_form = f
        return g

    def _display(self):
        g = self.g_disp = QGroupBox("Front-panel display")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        self.disp_on = QCheckBox("Display on")
        self.disp_on.clicked.connect(lambda on: self.p.cmd(lambda d, x: d.set_display(x), on))
        row.addWidget(self.disp_on)
        self.disp_text = QLineEdit()
        self.disp_text.setMaxLength(14)
        self.disp_text.setPlaceholderText("Message (14 chars)")
        self.disp_text.returnPressed.connect(self._show_text)
        row.addWidget(self.disp_text, 1)
        b = QPushButton("Show")
        b.clicked.connect(self._show_text)
        row.addWidget(b)
        b = QPushButton("Normal")
        b.setToolTip("Back to showing the readings")
        b.clicked.connect(lambda: self.p.cmd(lambda d: d.show_normal()))
        row.addWidget(b)
        v.addLayout(row)
        return g

    def _states(self):
        g = self.g_states = QGroupBox("Saved setups")
        grid = QGridLayout(g)
        grid.addWidget(QLabel("In the supply:"), 0, 0)
        self.save_btns, self.rcl_btns = [], []
        row = QHBoxLayout()
        for n in range(4):
            b = QPushButton(f"Recall {n}")
            b.clicked.connect(lambda _, k=n: self._recall(k))
            self.rcl_btns.append(b)
            row.addWidget(b)
        grid.addLayout(row, 0, 1)
        row = QHBoxLayout()
        for n in range(4):
            b = QPushButton(f"Save {n}")
            b.clicked.connect(lambda _, k=n: self._save(k))
            self.save_btns.append(b)
            row.addWidget(b)
        grid.addLayout(row, 1, 1)
        self.pon = choice(PON_STATES, "What the supply does when it is switched on. "
                                      "Stored in non-volatile memory.")
        self.pon.activated.connect(self._set_pon)
        grid.addWidget(QLabel("At power-on:"), 2, 0)
        grid.addWidget(self.pon, 2, 1)
        grid.addWidget(QLabel("In this app:"), 3, 0)
        row = QHBoxLayout()
        self.preset = choice([])
        row.addWidget(self.preset, 1)
        for text, fn in (("Apply", self._apply_preset), ("Save…", self._save_preset),
                         ("Delete", self._delete_preset)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        grid.addLayout(row, 3, 1)
        grid.addWidget(hint("App presets hold voltage, current limit, voltage limit, OCP "
                            "and output resistance; they never turn the output on."), 4, 0, 1, 2)
        self._load_presets()
        return g

    def _info(self):
        g = QGroupBox("Instrument")
        v = QVBoxLayout(g)
        self.info = QLabel("")
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.info)
        row = QHBoxLayout()
        self.test_btn = QPushButton("Self-test")
        self.test_btn.setToolTip("Runs the supply's self-test (output off, a few seconds)")
        self.test_btn.clicked.connect(self._self_test)
        row.addWidget(self.test_btn)
        b = QPushButton("Read error queue")
        b.clicked.connect(self._errors)
        row.addWidget(b)
        b = QPushButton("Reset (*RST)")
        b.clicked.connect(self.p.reset_instrument)
        row.addWidget(b)
        v.addLayout(row)
        return g

    # ================================================================ state
    def on_connected(self, caps):
        self.g_prot.setVisible(caps.has_ovp_tracking or caps.has_prot_delay or
                               caps.has_sense_protect)
        self.ovp_track.setVisible(caps.has_ovp_tracking)
        self.prot_form.setRowVisible(self.delay_row, caps.has_prot_delay)
        self.sense_prot.setVisible(caps.has_sense_protect)
        self.g_comp.setVisible(bool(caps.comp_modes))
        self.comp.clear()
        for m in caps.comp_modes:
            self.comp.addItem(COMP_MODES.get(m, (m, ""))[0], m)
        self.g_port.setVisible(caps.has_port or caps.has_ri_dfi)
        self.g_disp.setVisible(caps.has_display)
        self.g_states.setVisible(True)
        for k, (s, r) in enumerate(zip(self.save_btns, self.rcl_btns)):
            r.setVisible(k < caps.saved_states)
            s.setVisible(0 < k < caps.saved_states)
        self.pon.setVisible(caps.has_ri_dfi)
        self.test_btn.setVisible(caps.has_selftest)
        self.al_i_val.setRange(0, caps.i_max)
        self.al_v_val.setRange(0, caps.v_max)
        info = caps.info or {"Model": caps.model, "Identity": caps.idn}
        self.info.setText("\n".join(f"{k}: {v}" for k, v in info.items()))

    def on_state(self, sp):
        if sp.ovp_tracking is not None:
            self.ovp_track.setChecked(sp.ovp_tracking)
        if sp.prot_delay is not None and not self.delay.hasFocus():
            self.delay.setValue(sp.prot_delay)
        if sp.sense_protect is not None:
            self.sense_prot.setChecked(sp.sense_protect)
        if sp.comp_mode:
            select(self.comp, sp.comp_mode[:4].upper())
            self.comp_hint.setText(COMP_MODES.get(self.comp.currentData(), ("", ""))[1])
        if sp.port_function:
            func = sp.port_function[:4].upper()
            select(self.port_func, func)
            ri = func == "RIDF"
            self.ri_mode.setEnabled(ri and self.p.connected)
            self.dfi_w.setEnabled(ri and self.p.connected)
            self.io_w.setEnabled(func == "DIG" and self.p.connected)
        if sp.ri_mode:
            select(self.ri_mode, sp.ri_mode[:4].upper())
        if sp.dfi is not None:
            self.dfi.setChecked(sp.dfi)
        if sp.dfi_source:
            select(self.dfi_src, sp.dfi_source[:4].upper())
        if sp.port_data is not None:
            self.bit0.setChecked(bool(sp.port_data & 1))
            self.bit1.setChecked(bool(sp.port_data & 2))
            self.in_lbl.setText(f"In: {'high' if sp.port_data & 4 else 'low'}")
        if sp.pon_state:
            select(self.pon, sp.pon_state[:4].upper())
        if sp.display_on is not None:
            self.disp_on.setChecked(sp.display_on)
        if sp.display_text is not None and not self.disp_text.hasFocus():
            self.disp_text.setText(sp.display_text if sp.display_mode == "TEXT" else "")

    def on_reading(self, r):
        for ev in r.events:
            self.log_event(ev)
            self.p.message.emit(f"{self.p.caps.model}: {ev}")
        if not r.output:
            self.alarm_since.clear()
            return
        checks = []
        if self.al_i.isChecked() and r.current is not None:
            checks.append(("I", r.current > self.al_i_val.value(),
                           f"current {r.current:.4f} A above {self.al_i_val.value():.4f} A"))
        if self.al_v.isChecked() and r.voltage is not None:
            checks.append(("V", r.voltage < self.al_v_val.value(),
                           f"voltage {r.voltage:.3f} V below {self.al_v_val.value():.3f} V"))
        for key, bad, text in checks:
            if not bad:
                self.alarm_since.pop(key, None)
                continue
            since = self.alarm_since.setdefault(key, r.t)
            if r.t - since >= self.al_t.value():
                self.alarm_since.clear()
                self.p.cmd(lambda d: d.set_output(False))
                self.log_event(f"Software limit: {text} — output turned OFF")
                self.p.message.emit(f"{self.p.caps.model}: output OFF, {text}")
                break

    def log_event(self, text):
        self.log.appendPlainText(time.strftime("%H:%M:%S  ") + text)

    def set_enabled(self, on):
        self.on_state(self.p.sp)

    # ================================================================ actions
    def _set_comp(self):
        m = self.comp.currentData()
        if m == "HREM" and QMessageBox.question(
                self, "Output response", "High-capacitance remote mode needs more than 5 µF "
                "across the output, or the supply may oscillate.\n\nUse it?") != \
                QMessageBox.StandardButton.Yes:
            select(self.comp, (self.p.sp.comp_mode or "")[:4].upper())
            return
        self.p.cmd(lambda d, x: d.set_comp_mode(x), m)

    def _set_port_func(self):
        f = self.port_func.currentData()
        if QMessageBox.question(
                self, "Rear control port", f"Set the rear port to '{self.port_func.currentText()}'?"
                "\n\nThis is stored in the supply's non-volatile memory.") != \
                QMessageBox.StandardButton.Yes:
            select(self.port_func, (self.p.sp.port_function or "")[:4].upper())
            return
        self.p.cmd(lambda d, x: d.set_port_function(x), f)

    def _set_dfi(self, *_):
        self.p.cmd(lambda d, on, s: d.set_dfi(on, s), self.dfi.isChecked(), self.dfi_src.currentData())

    def _set_bits(self, *_):
        self.p.cmd(lambda d, x: d.set_port_data(x),
                   int(self.bit0.isChecked()) | (int(self.bit1.isChecked()) << 1) | 4)

    def _set_pon(self):
        if QMessageBox.question(
                self, "Power-on state", f"At power-on use: {self.pon.currentText()}?\n\n"
                "Saved state 0 can include OUTPUT ON. This is stored in non-volatile "
                "memory.") != QMessageBox.StandardButton.Yes:
            select(self.pon, (self.p.sp.pon_state or "")[:4].upper())
            return
        self.p.cmd(lambda d, x: d.set_pon_state(x), self.pon.currentData())

    def _show_text(self):
        self.p.cmd(lambda d, x: d.show_text(x), self.disp_text.text())

    def _save(self, n):
        if QMessageBox.question(self, "Save setup", f"Overwrite setup {n} in the supply with "
                                "the current settings?") == QMessageBox.StandardButton.Yes:
            self.p.cmd(lambda d, x: d.save_state(x), n)
            self.log_event(f"Saved setup {n}")

    def _recall(self, n):
        if QMessageBox.question(self, "Recall setup", f"Recall setup {n}?\n\nThis can change "
                                "the output voltage and may turn the output ON.") == \
                QMessageBox.StandardButton.Yes:
            self.p.cmd(lambda d, x: d.recall_state(x), n)
            self.log_event(f"Recalled setup {n}")

    def _self_test(self):
        if QMessageBox.question(self, "Self-test", "Run the self-test? The output is turned "
                                "off.") != QMessageBox.StandardButton.Yes:
            return
        self.test_btn.setEnabled(False)
        self.test_btn.setText("Testing…")
        self.p.worker.submit_result("selftest", lambda d: d.self_test(), refresh=True)

    def _errors(self):
        self.p.worker.submit_result("errors", lambda d: d.errors())

    def on_result(self, tag, value):
        if tag == "selftest":
            ok, errs = value
            self.test_btn.setEnabled(True)
            self.test_btn.setText("Self-test")
            text = "Self-test passed." if ok else "Self-test FAILED:\n" + "\n".join(errs)
            self.log_event(text.replace("\n", " "))
            (QMessageBox.information if ok else QMessageBox.warning)(self, "Self-test", text)
        elif tag == "errors":
            text = "\n".join(value) if value else "No errors."
            self.log_event("Error queue: " + text.replace("\n", "; "))
            QMessageBox.information(self, "Error queue", text)

    # ================================================================ presets
    @staticmethod
    def _preset_path():
        return config_dir() / "presets.json"

    def _read_presets(self) -> dict:
        try:
            return json.loads(self._preset_path().read_text())
        except Exception:
            return {}

    def _load_presets(self):
        self.preset.clear()
        for name in sorted(self._read_presets()):
            self.preset.addItem(name, name)

    @io_errors("Save preset")
    def _save_preset(self):
        sp = self.p.sp
        name, ok = QInputDialog.getText(self, "Save preset", "Name:")
        if not ok or not name.strip():
            return
        data = self._read_presets()
        data[name.strip()] = {"voltage": sp.voltage, "current": sp.current, "ovp": sp.ovp,
                              "ocp": sp.ocp, "resistance": sp.resistance}
        path = self._preset_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
        self._load_presets()
        select(self.preset, name.strip())

    @io_errors("Delete preset")
    def _delete_preset(self):
        name = self.preset.currentData()
        if not name:
            return
        data = self._read_presets()
        data.pop(name, None)
        self._preset_path().write_text(json.dumps(data, indent=2))
        self._load_presets()

    def _apply_preset(self):
        name = self.preset.currentData()
        pr = self._read_presets().get(name or "")
        if not pr:
            return
        caps = self.p.caps

        def job(d, pr):
            if pr.get("ovp") is not None:
                d.set_ovp(min(pr["ovp"], caps.ovp_max))
            if pr.get("voltage") is not None:
                d.set_voltage(min(pr["voltage"], caps.v_max))
            if pr.get("current") is not None:
                d.set_current(min(pr["current"], caps.i_max))
            if pr.get("ocp") is not None:
                d.set_ocp(pr["ocp"])
            if pr.get("resistance") is not None and caps.has_resistance:
                d.set_resistance(pr["resistance"])
        self.p.cmd(job, pr)
        self.log_event(f"Applied preset '{name}'")
