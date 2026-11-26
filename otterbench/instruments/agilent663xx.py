"""SCPI driver for Agilent/HP 66309B/D, 66311B/D, 66319B/D, 66321B/D mobile-comm DC sources.

Command set from the 66319B/D-66321B/D User's Guide (Language Dictionary,
chapter 8). Behaviour measured on a real 66321D (fw A.03.01) over an NI
GPIB-USB-HS:
  * one bus round trip ~10 ms; MEAS:VOLT?/CURR? ~50 ms at 2048 points.
  * voltage and current are digitized separately (FETC:VOLT? after
    MEAS:CURR? gives +603), so a V+I reading costs two acquisitions.
  * array data leaves the instrument at ~3.7 KB/s whatever the format, so
    arrays are read as float32 (FORM REAL,32 + BORD SWAP: 4 bytes/point)
    instead of ASCII (~12 bytes/point): 1024 points in 1.15 s instead of 2.9 s.
  * scalar queries stay ASCII under FORM REAL.
  * there is no SENS:CURR:RANG:AUTO (nor on a 66309B, fw A.02.04): ranges are
    3 A, 1 A and 20 mA, and "Auto" is done here, in poll(). The 20 mA range
    reads within ~1 uA of zero where the 3 A one is 0.3-0.5 mA out (66309B).
  * the DVM can be triggered but has no array (FETC:ARR:DVM? is undefined).
  * ABOR clears the WTG (waiting-for-trigger) bit within ~70 ms.
  * SENS:LEAD:STAT? (in the manual) is an undefined header on fw A.03.01;
    an open sense lead shows up as the SD bit of the questionable register.

The 66309B/D (66111A-66311B/D-66309B/D User's Guide; every query checked on a
66309B fw A.02.04; output 1 switched with the outputs separate, and the
current ranges set; output 2 switching and coupling not yet) adds
an isolated output 2, 12 V / 1.5 A: VOLT2, CURR2, MEAS:VOLT2?, MEAS:CURR2?.
  * Its outputs are coupled at power-on: OUTP switches both. From fw A.02.04
    INST:COUP:OUTP:STAT NONE separates them; a plain OUTP is then output 1 only.
  * OCP is one switch for both; output 2 has no OVP and no waveforms.
  * VOLT:PROT is a real OVP trip level and VOLT:PROT:STAT switches it, on the
    66311B/D too (on the 66319 / 66321 it is a limit on the setting).
"""
from __future__ import annotations

import time

from ..transport import TransportTimeout
from .base import (CaptureResult, CaptureSpec, SupplyCaps, SupplyPoll, SupplyReading,
                   Setpoints, parse_bool, parse_float as _f)

OPER_WTG = 1 << 5
OPER_CV = 1 << 8
OPER_CC_POS = 1 << 10
OPER_CC_NEG = 1 << 11
OPER_CV2 = 1 << 9
OPER_CC2 = 1 << 12

QUES_FLAGS = {0: "OV", 1: "OCP", 3: "FP-LOCAL", 4: "OT", 5: "SENSE-OPEN", 9: "RI",
              8: "UNREG2", 10: "UNREG", 12: "OCP2", 14: "OVLD"}
QUES_EVENTS = {0: "Over-voltage trip", 1: "Over-current trip", 4: "Over-temperature",
               5: "Open sense lead", 9: "Remote inhibit", 10: "Output unregulated",
               8: "Output 2 unregulated", 12: "Output 2 over-current trip",
               14: "Measurement overload"}

ARRAY_BYTES_PER_S = 3700.0   # measured, instrument-limited
BYTES_PER_POINT = 4.0        # FORM REAL,32
MAX_POINTS = 4096
TINT_MIN = 15.6e-6

# (label, value sent with SENS:CURR:RANG, full scale)
RANGES_3 = [("3 A", "MAX", 3.0712), ("1 A", "1", 1.0), ("20 mA", "MIN", 0.02)]
RANGES_2 = [("3 A", "MAX", 3.0712), ("20 mA", "MIN", 0.02)]
AUTO = "Auto"
# Auto: up past UP x the range's full scale (or on overload), down below
# DOWN x the lower range's; after an overload it stays up HOLD_S, so a pulsed
# load whose peaks overflow a range doesn't make it switch on every reading.
UP, DOWN, HOLD_S = 0.95, 0.8, 30.0
OVERFLOW = 9.9e37        # what MEASure returns when a range overflows

DUAL = ("66309", "66319")
OVP_TRIPS = ("66309", "66311", "66111")     # the others: a limit on the setting


def _unquote(s: str) -> str:
    return s.strip().strip('"')


class Agilent663xx:
    kind = "supply"
    family = "663xx"

    def __init__(self, transport):
        self.t = transport
        self.caps = SupplyCaps()
        self.log = None
        self._sweep = (2048, TINT_MIN)    # the poll's digitizer settings
        self._ranges = RANGES_2
        self._auto = True        # choose the current range from the readings
        self._rng: str | None = None     # the range the instrument is on
        self._hold_until = 0.0
        self._capturing: CaptureSpec | None = None
        self._step_from: dict = {}

    # ---- low level ---------------------------------------------------
    def write(self, cmd: str) -> None:
        if self.log:
            self.log(f"> {cmd}")
        self.t.write(cmd)

    def query(self, cmd: str) -> str:
        r = self.t.query(cmd)
        if self.log:
            self.log(f"> {cmd}  < {r}")
        return r

    def queries(self, cmds: list[str]) -> list[str]:
        """Several queries in one bus transaction (saves ~10 ms each)."""
        r = self.query(";:".join(cmds))
        vals = r.split(";")
        if len(vals) != len(cmds):
            raise RuntimeError(f"Expected {len(cmds)} replies, got {len(vals)}: {r!r}")
        return vals

    def errors(self) -> list[str]:
        errs = []
        for _ in range(20):
            e = self.t.query("SYST:ERR?")
            if e.split(",")[0].strip() in ("0", "+0"):
                break
            errs.append(e)
        return errs

    def _supports(self, q: str) -> bool:
        try:
            with self.t.temp_timeout(1.5):
                self.t.query(q)
            ok = True
        except TransportTimeout:
            ok = False
            self.t.clear()
        self.t.write("*CLS")
        return ok

    def _binary_format(self):
        self.t.write("FORM REAL,32;:FORM:BORD SWAP")

    # ---- discovery -----------------------------------------------------
    def identify(self) -> SupplyCaps:
        self.t.clear()
        self.t.write("*CLS")
        c = SupplyCaps(has_meas_setup=True, has_waveform=True, has_pulse_stats=True)
        c.idn = self.t.query("*IDN?")
        parts = [p.strip() for p in c.idn.split(",")]
        c.model = parts[1] if len(parts) > 1 else "?"
        fw = parts[3] if len(parts) > 3 else ""
        c.info = {"Model": c.model, "Serial": parts[2] if len(parts) > 2 else "?",
                  "Firmware": fw}
        try:
            c.v_max = _f(self.t.query("VOLT? MAX"))
            c.i_max = _f(self.t.query("CURR? MAX"))
            c.ovp_max = _f(self.t.query("VOLT:PROT? MAX"))
        except Exception:
            pass
        # each probe of something missing is a timeout (~3 s on GPIB): skip what
        # the manual says these models don't have
        older = c.model.startswith(OVP_TRIPS)
        c.has_dvm = not (older and c.model[5:6] in ("A", "B")) and self._supports("MEAS:DVM?")
        c.has_resistance = not older and self._supports("RES?")
        if c.has_resistance:
            try:
                c.r_min = _f(self.t.query("RES? MIN"))
                c.r_max = _f(self.t.query("RES? MAX"))
            except Exception:
                pass
        # 1 A range on 66321B/D and 66319B/D only (manual table 2-3)
        self._ranges = RANGES_3 if c.model.startswith(("66321", "66319")) else RANGES_2
        c.current_ranges = [(lab, fs) for lab, _, fs in self._ranges]
        c.has_auto_range = not c.model.startswith("66111")    # one range only
        self._auto = c.has_auto_range
        self._rng = None
        c.has_ovp_tracking = self._supports("VOLT:PROT:STAT?")
        if c.has_ovp_tracking and c.model.startswith(OVP_TRIPS):
            c.ovp_is_trip = True
        elif c.has_ovp_tracking:
            c.ovp_label = "Voltage limit"
            c.ovp_tip = ("Highest voltage that can be programmed (VOLT:PROT), from here or "
                         "the front panel. The over-voltage trip itself tracks the setting "
                         "(+2 V) when 'OVP tracking' is on.")
        c.has_prot_delay = self._supports("OUTP:PROT:DEL?")
        c.has_window = self._supports("SENS:WIND?")
        c.has_sense_protect = self._supports("SENS:PROT:STAT?")
        if not older and self._supports("OUTP:COMP:MODE?"):
            c.comp_modes = ["LLOC", "HLOC", "LREM", "HREM"]
        elif self._supports("OUTP:TYPE?"):
            c.comp_modes = ["LOW", "HIGH"] + (["H2"] if c.model.startswith("66309") else [])
        c.has_ri_dfi = self._supports("OUTP:RI:MODE?")
        c.has_port = self._supports("DIG:FUNC?")
        c.has_display = self._supports("DISP:MODE?")
        c.has_trigger = self._supports("TRIG:ACQ:SOUR?")
        c.has_ext_trigger = c.has_trigger and c.has_port and fw >= "A.03.01" \
            and not c.model.startswith(OVP_TRIPS)
        if c.model.startswith(DUAL) and self._supports("VOLT2?"):
            c.has_out2 = True
            try:
                c.v2_max = _f(self.t.query("VOLT2? MAX"))
                c.i2_max = _f(self.t.query("CURR2? MAX"))
            except Exception:
                pass
            c.has_coupling = self._supports("INST:COUP:OUTP:STAT?")
            if not c.has_coupling:
                c.note = ("This firmware always switches both outputs together "
                          "(separate switching needs A.02.04).")
        c.saved_states = 4
        c.has_selftest = True
        for key, q in (("Options", "*OPT?"), ("Calibrated", "CAL:DATE?"),
                       ("SCPI version", "SYST:VERS?")):
            try:
                with self.t.temp_timeout(1.5):
                    c.info[key] = _unquote(self.t.query(q))
            except TransportTimeout:
                self.t.clear()
        self.t.write("*CLS")
        self._binary_format()
        self.caps = c
        return c

    def read_setpoints(self) -> Setpoints:
        c = self.caps
        s = Setpoints()
        qs = ["VOLT?", "CURR?", "OUTP?", "VOLT:PROT?", "CURR:PROT:STAT?",
              "SENS:SWE:POIN?", "SENS:SWE:TINT?", "SENS:CURR:DET?", "SENS:CURR:RANG?"]
        if c.has_resistance:
            qs += ["RES?"]
        r = iter(self.queries(qs))
        s.voltage, s.current = _f(next(r)), _f(next(r))
        s.output = parse_bool(next(r))
        s.ovp = _f(next(r))
        s.ocp = parse_bool(next(r))
        pts, tint = int(_f(next(r))), _f(next(r))
        if self._capturing:
            # the digitizer currently holds the capture's settings
            pts, tint = self._sweep
        else:
            self._sweep = (pts, tint)
        s.sweep_points, s.sweep_tint = pts, tint
        s.detector = next(r).strip().upper()
        fs = _f(next(r))
        self._rng = min(self._ranges, key=lambda x: abs(x[2] - fs))[0]
        s.current_range = AUTO if self._auto else self._rng
        if c.has_resistance:
            s.resistance = _f(next(r))

        extra = []
        if c.has_ovp_tracking:
            extra.append(("ovp_tracking", "VOLT:PROT:STAT?", parse_bool))
        if c.has_prot_delay:
            extra.append(("prot_delay", "OUTP:PROT:DEL?", _f))
        if c.has_window:
            extra.append(("window", "SENS:WIND?", str.strip))
        if c.has_sense_protect:
            extra.append(("sense_protect", "SENS:PROT:STAT?", parse_bool))
        if c.comp_modes:
            q = "OUTP:COMP:MODE?" if "HREM" in c.comp_modes else "OUTP:TYPE?"
            extra.append(("comp_mode", q, str.strip))
        if c.has_ri_dfi:
            extra += [("ri_mode", "OUTP:RI:MODE?", str.strip),
                      ("dfi", "OUTP:DFI?", parse_bool),
                      ("dfi_source", "OUTP:DFI:SOUR?", str.strip),
                      ("pon_state", "OUTP:PON:STAT?", str.strip)]
        if c.has_port:
            extra += [("port_function", "DIG:FUNC?", str.strip),
                      ("port_data", "DIG:DATA?", lambda x: int(_f(x)))]
        if c.has_display:
            extra += [("display_on", "DISP?", parse_bool),
                      ("display_mode", "DISP:MODE?", str.strip)]
        if c.has_trigger:
            extra += [("volt_trig", "VOLT:TRIG?", _f), ("curr_trig", "CURR:TRIG?", _f)]
            if c.has_resistance:
                extra.append(("res_trig", "RES:TRIG?", _f))
        if c.has_out2:
            extra += [("voltage2", "VOLT2?", _f), ("current2", "CURR2?", _f)]
            if c.has_coupling:
                extra += [("output2", "OUTP2?", parse_bool),
                          ("coupled", "INST:COUP:OUTP:STAT?", self._coupled)]
        if extra:
            for (attr, _, conv), val in zip(extra, self.queries([q for _, q, _ in extra])):
                setattr(s, attr, conv(val))
        if c.has_out2 and not c.has_coupling:
            s.output2, s.coupled = s.output, True
        if c.has_display:
            # separate: the text itself could contain ';'
            s.display_text = _unquote(self.query("DISP:TEXT?"))
        return s

    # ---- programming ---------------------------------------------------
    def set_voltage(self, v: float):
        self.write(f"VOLT {v:.4f}")

    def set_current(self, a: float):
        self.write(f"CURR {a:.5f}")

    def set_output(self, on: bool):
        """Output 1; both outputs of a dual unit while they are coupled."""
        self.write(f"OUTP {'ON' if on else 'OFF'}")

    def all_off(self):
        """Every output, coupled or not."""
        self.write("OUTP OFF")
        if self.caps.has_out2:
            self.write("OUTP2 OFF")     # OUTP alone is output 1 when they are separate
            if not self.caps.has_coupling:
                self.errors()           # older firmware: OUTP did both, no OUTP2

    # ---- output 2 (66309 / 66319) ----------------------------------------
    @staticmethod
    def _coupled(s: str) -> bool:
        return s.strip().strip('"').upper().startswith("ALL")

    def _need_out2(self):
        if not self.caps.has_out2:
            raise ValueError(f"the {self.caps.model or 'supply'} has no output 2")

    def set_voltage2(self, v: float):
        self._need_out2()
        self.write(f"VOLT2 {v:.4f}")

    def set_current2(self, a: float):
        self._need_out2()
        self.write(f"CURR2 {a:.5f}")

    def set_output2(self, on: bool):
        """Output 2 alone. ON is refused while the outputs are coupled (it
        would switch output 1 on too); OFF never is."""
        self._need_out2()
        c = self.caps
        if on and (not c.has_coupling or self._coupled(self.query("INST:COUP:OUTP:STAT?"))):
            raise ValueError("outputs 1 and 2 are coupled: switch output 1 on (that does both)"
                             + (", or separate them first" if c.has_coupling else ""))
        self.write(f"OUTP{'2' if c.has_coupling else ''} {'ON' if on else 'OFF'}")

    def set_coupled(self, on: bool):
        self._need_out2()
        if not self.caps.has_coupling:
            raise ValueError("this firmware always switches both outputs together")
        self.write(f"INST:COUP:OUTP:STAT {'ALL' if on else 'NONE'}")

    def set_ovp(self, v: float):
        """VOLT:PROT: on the 663xx this is the programmable voltage limit."""
        self.write(f"VOLT:PROT {v:.3f}")

    def set_ovp_tracking(self, on: bool):
        self.write(f"VOLT:PROT:STAT {'ON' if on else 'OFF'}")

    def set_ocp(self, on: bool):
        self.write(f"CURR:PROT:STAT {'ON' if on else 'OFF'}")

    def set_prot_delay(self, s: float):
        self.write(f"OUTP:PROT:DEL {s:.3f}")

    def clear_protection(self):
        self.write("OUTP:PROT:CLE")

    def set_resistance(self, ohms: float):
        self.write(f"RES {ohms:.4f}")

    def set_current_range(self, label: str):
        """A range, or AUTO: poll() then picks one as the current changes."""
        if label == AUTO and self.caps.has_auto_range:
            self._auto, self._hold_until = True, 0.0
            return
        for lab, arg, _ in self._ranges:
            if lab == label:
                self._auto = False
                self._put_range(lab)
                return
        raise ValueError(f"Unknown range {label!r}")

    def _put_range(self, label: str):
        arg = next(a for lab, a, _ in self._ranges if lab == label)
        self.write(f"SENS:CURR:RANG {arg}")
        self._rng = label

    def _autorange(self, r: SupplyReading, cfg: SupplyPoll, stats: list[str]):
        """Auto: move up at once when the range overflows (and measure again),
        up for the next reading when near its full scale, down when well
        below the lower one's."""
        ranges = sorted(self._ranges, key=lambda x: x[2])
        if self._rng is None:
            fs = _f(self.query("SENS:CURR:RANG?"))
            self._rng = min(ranges, key=lambda x: abs(x[2] - fs))[0]
        i = [lab for lab, _, _ in ranges].index(self._rng)
        top = len(ranges) - 1
        over = abs(r.current) >= OVERFLOW or "OVLD" in r.flags
        if over and i < top:
            while over and i < top:
                i += 1
                self._put_range(ranges[i][0])
                with self.t.temp_timeout(max(5.0, self._sweep[0] * self._sweep[1] * 2 + 2)):
                    vals = self.queries(["MEAS:CURR?"] + stats)
                r.current = _f(vals[0])
                if stats:
                    r.i_max, r.i_min, r.i_high, r.i_low, r.i_rms = (_f(x) for x in vals[1:])
                over = abs(r.current) >= OVERFLOW
            r.flags = [f for f in r.flags if f != "OVLD"]
            r.events = [e for e in r.events if e != QUES_EVENTS[14]]
            self._hold_until = time.monotonic() + HOLD_S
        elif not over:
            peak = max(abs(x) for x in (r.current, r.i_max, r.i_min) if x is not None)
            if peak > UP * ranges[i][2] and i < top:
                self._put_range(ranges[i + 1][0])
            elif i > 0 and time.monotonic() >= self._hold_until:
                fits = [j for j in range(i) if peak < DOWN * ranges[j][2]]
                if fits:
                    self._put_range(ranges[fits[0]][0])
        r.i_range = ranges[i][0]

    def set_detector(self, det: str):
        self.write(f"SENS:CURR:DET {det.upper()}")

    def set_window(self, w: str):
        self.write(f"SENS:WIND {w.upper()}")

    def set_sweep(self, points: int, tint: float):
        self.write(f"SENS:SWE:POIN {int(points)};:SENS:SWE:TINT {tint:.4E}")
        self._sweep = (int(points), tint)

    def set_sense_protect(self, on: bool):
        self.write(f"SENS:PROT:STAT {'ON' if on else 'OFF'}")

    def set_comp_mode(self, mode: str):
        if mode in ("LOW", "HIGH", "H2"):
            self.write(f"OUTP:TYPE {mode}")
        else:
            self.write(f"OUTP:COMP:MODE {mode}")

    def set_ri_mode(self, mode: str):
        self.write(f"OUTP:RI:MODE {mode}")

    def set_dfi(self, on: bool, source: str):
        self.write(f"OUTP:DFI:SOUR {source};:OUTP:DFI {'ON' if on else 'OFF'}")

    def set_port_function(self, func: str):
        """RIDF | DIG | TRIG. Writes non-volatile memory: don't call in a loop."""
        self.write(f"DIG:FUNC {func}")

    def set_port_data(self, value: int):
        self.write(f"DIG:DATA {int(value) & 7}")

    def set_pon_state(self, state: str):
        """RST | RCL0. Writes non-volatile memory."""
        self.write(f"OUTP:PON:STAT {state}")

    def set_display(self, on: bool):
        self.write(f"DISP {'ON' if on else 'OFF'}")

    def show_text(self, text: str):
        text = text.replace('"', "'")[:14]
        self.write(f'DISP:TEXT "{text}";:DISP:MODE TEXT')

    def show_normal(self):
        self.write("DISP:MODE NORM")

    def save_state(self, n: int):
        self.write(f"*SAV {int(n)}")

    def recall_state(self, n: int):
        self.write(f"*RCL {int(n)}")
        self._binary_format()

    def self_test(self) -> tuple[bool, list[str]]:
        with self.t.temp_timeout(60):
            code = int(_f(self.query("*TST?")))
        return code == 0, self.errors()

    def reset(self):
        self.write("*RST")
        self.write("*CLS")
        self._binary_format()

    # ---- output transients ----------------------------------------------
    def _set_trig_levels(self, step: dict):
        cmds = []
        if step.get("VOLT") is not None:
            cmds.append(f"VOLT:TRIG {step['VOLT']:.4f}")
        if step.get("CURR") is not None:
            cmds.append(f"CURR:TRIG {step['CURR']:.5f}")
        if step.get("RES") is not None and self.caps.has_resistance:
            cmds.append(f"RES:TRIG {step['RES']:.4f}")
        if not cmds:
            raise ValueError("Give at least one level to step to")
        self.write(";:".join(cmds))

    def step_output(self, step: dict):
        """Change the output to the given levels with a transient trigger
        (all of V, I and R change at the same instant)."""
        self._set_trig_levels(step)
        self.write("INIT:NAME TRAN;:TRIG:TRAN")

    # ---- measurement ---------------------------------------------------
    def poll(self, t: float, cfg: SupplyPoll) -> SupplyReading:
        """One bus transaction for everything (saves ~10 ms per query)."""
        qs = []
        if cfg.measure_voltage:
            qs.append("MEAS:VOLT?")
        qs.append("MEAS:CURR?")
        if cfg.pulse_stats:
            # FETCh re-uses the current acquisition; must precede the DVM read.
            qs += ["FETC:CURR:MAX?", "FETC:CURR:MIN?", "FETC:CURR:HIGH?",
                   "FETC:CURR:LOW?", "FETC:CURR:ACDC?"]
        dvm = cfg.dvm and self.caps.has_dvm
        if dvm:
            qs.append("MEAS:DVM?")
        # STAT:QUES? is the latched event register (reading clears it): it
        # catches trips that come and go between polls.
        c = self.caps
        out2 = c.has_out2 and cfg.out2
        if out2:
            qs += ["MEAS:VOLT2?", "MEAS:CURR2?"]
        qs += ["STAT:OPER:COND?", "STAT:QUES:COND?", "STAT:QUES?", "OUTP?"]
        if c.has_coupling:
            qs += ["OUTP2?", "INST:COUP:OUTP:STAT?"]
        acqs = (1 if cfg.measure_voltage else 0) + 1 + (1 if dvm else 0)
        acq = self._sweep[0] * self._sweep[1] * acqs + (2 * 2048 * TINT_MIN if out2 else 0)
        with self.t.temp_timeout(max(5.0, acq * 2 + 2)):
            vals = self.queries(qs)
        it = iter(vals)
        v = _f(next(it)) if cfg.measure_voltage else None
        r = SupplyReading(t=t, voltage=v, current=_f(next(it)), mode="--")
        if cfg.pulse_stats:
            r.i_max, r.i_min, r.i_high, r.i_low, r.i_rms = (_f(next(it)) for _ in range(5))
        if dvm:
            r.dvm = _f(next(it))
        if out2:
            r.voltage2, r.current2 = _f(next(it)), _f(next(it))
        oper, ques, ques_ev = (int(_f(next(it))) for _ in range(3))
        r.output = parse_bool(next(it))
        if c.has_coupling:
            r.output2, r.coupled = parse_bool(next(it)), self._coupled(next(it))
        elif c.has_out2:
            r.output2, r.coupled = r.output, True
        if c.has_out2:
            r.mode2 = "OFF" if not r.output2 else "CC" if oper & OPER_CC2 else \
                "CV" if oper & OPER_CV2 else "--"
        r.flags = [name for bit, name in QUES_FLAGS.items() if ques & (1 << bit)]
        r.events = [name for bit, name in QUES_EVENTS.items() if ques_ev & (1 << bit)]
        if not r.output:
            r.mode = "OFF"
        elif oper & OPER_CC_POS:
            r.mode = "CC"
        elif oper & OPER_CC_NEG:
            r.mode = "CC-"
        elif oper & OPER_CV:
            r.mode = "CV"
        if self._auto and self.caps.has_auto_range and not self._capturing:
            self._autorange(r, cfg, qs[qs.index("MEAS:CURR?") + 1:][:5] if cfg.pulse_stats else [])
        else:
            r.i_range = self._rng or ""
        return r

    # ---- waveform capture (non-blocking: arm -> fire? -> done? -> fetch) ----
    @staticmethod
    def transfer_seconds(points: int) -> float:
        return points * BYTES_PER_POINT / ARRAY_BYTES_PER_S

    def capture_seconds(self, points: int, tint: float) -> float:
        return points * tint + self.transfer_seconds(points)

    @staticmethod
    def validate(spec: CaptureSpec) -> CaptureSpec:
        spec.points = max(1, min(MAX_POINTS, int(spec.points)))
        spec.count = max(1, min(100, int(spec.count), MAX_POINTS // spec.points))
        if spec.trigger in ("IMM", "STEP"):
            spec.count = 1          # one software trigger: one segment
        spec.pre = max(0, min(spec.points - 1, int(spec.pre)))
        spec.tint = max(TINT_MIN, float(spec.tint))
        if spec.what not in ("CURR", "VOLT"):
            raise ValueError("what must be CURR or VOLT")
        if spec.trigger not in ("IMM", "LEVEL", "EXT", "STEP"):
            raise ValueError(f"Unknown trigger {spec.trigger}")
        return spec

    def arm(self, spec: CaptureSpec) -> float:
        """Configure and initiate a capture. Returns the seconds to wait before
        firing (IMM/STEP: the pre-trigger buffer must fill first; samples
        offered before that are ignored by the instrument), or -1 when the
        instrument triggers itself (LEVEL/EXT)."""
        spec = self.validate(spec)
        w = spec.what
        cmds = ["ABOR", f'SENS:FUNC "{w}"', f"SENS:SWE:POIN {spec.points}",
                f"SENS:SWE:TINT {spec.tint:.4E}", f"SENS:SWE:OFFS:POIN {-spec.pre}",
                f"TRIG:ACQ:COUN:{w} {spec.count}"]
        if spec.trigger == "LEVEL":
            cmds += [f"TRIG:ACQ:LEV:{w} {spec.level:.5f}", f"TRIG:ACQ:HYST:{w} {spec.hyst:.5f}",
                     f"TRIG:ACQ:SLOP:{w} {spec.slope}", "TRIG:ACQ:SOUR INT"]
        elif spec.trigger == "EXT":
            cmds += ["TRIG:ACQ:SOUR EXT"]
        else:
            cmds += ["TRIG:ACQ:SOUR BUS"]
        if self._auto and self.caps.has_auto_range:
            # waveforms can have peaks the readings don't show: the top range
            top = max(self._ranges, key=lambda x: x[2])
            cmds.append(f"SENS:CURR:RANG {top[1]}")
            self._rng = top[0]
        self._binary_format()
        self.write(";:".join(cmds))
        self._step_from = {}
        if spec.trigger == "STEP":
            qs = ["VOLT?", "CURR?"] + (["RES?"] if self.caps.has_resistance else [])
            vals = [_f(x) for x in self.queries(qs)] + [None]
            self._step_from = {"VOLT": vals[0], "CURR": vals[1], "RES": vals[2]}
            self._set_trig_levels(spec.step)
            self.write("INIT:NAME TRAN")
        self.write("INIT:NAME ACQ")
        self._capturing = spec
        if spec.trigger in ("IMM", "STEP"):
            return spec.pre * spec.tint + 0.02
        return -1.0

    def fire(self):
        """Bus trigger. For STEP, *TRG fires the output transient and the
        acquisition at the same instant."""
        spec = self._capturing
        if spec is None:
            return
        self.write("*TRG" if spec.trigger == "STEP" else "TRIG:ACQ")

    def capture_done(self) -> bool:
        return not int(_f(self.query("STAT:OPER:COND?"))) & OPER_WTG

    def fetch(self) -> CaptureResult:
        spec = self._capturing
        if spec is None:
            raise RuntimeError("No capture in progress")
        w = spec.what
        n = spec.points * spec.count
        try:
            with self.t.temp_timeout(self.transfer_seconds(n) * 2 + 5):
                data = self.t.query_binary(f"FETC:ARR:{w}?")
            vals = self.queries([f"FETC:{w}?", f"FETC:{w}:ACDC?", f"FETC:{w}:MAX?",
                                 f"FETC:{w}:MIN?", f"FETC:{w}:HIGH?", f"FETC:{w}:LOW?"])
            stats = dict(zip(("dc", "rms", "max", "min", "high", "low"), map(_f, vals)))
            restored = {}
            if spec.trigger == "STEP" and spec.restore_after_step:
                restored = {k: v for k, v in self._step_from.items()
                            if v is not None and spec.step.get(k) is not None}
                if restored:
                    self.step_output(restored)
        finally:
            self._finish()
        return CaptureResult(spec=spec, data=data, stats=stats, restored=restored,
                             t_done=time.time())

    def abort(self):
        try:
            self.write("ABOR")
        finally:
            self._finish()

    def _finish(self):
        """Put the digitizer back the way the poll expects it."""
        self._capturing = None
        p, t = self._sweep
        self.write(f"SENS:SWE:POIN {p};:SENS:SWE:TINT {t:.4E};:SENS:SWE:OFFS:POIN 0;"
                   ":TRIG:ACQ:COUN:CURR 1;:TRIG:ACQ:COUN:VOLT 1;:TRIG:ACQ:SOUR INT")

    def capture(self, what: str, points: int, tint: float) -> list[float]:
        """Blocking immediate capture (scripts/tests)."""
        wait = self.arm(CaptureSpec(what=what, points=points, tint=tint))
        time.sleep(max(0.0, wait))
        self.fire()
        deadline = time.monotonic() + points * tint + 5
        while not self.capture_done():
            if time.monotonic() > deadline:
                self.abort()
                raise TransportTimeout("Capture did not complete")
            time.sleep(0.02)
        return self.fetch().data
