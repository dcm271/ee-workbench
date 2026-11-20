"""SCPI driver for Agilent/HP 66311B, 66321B and 66321D mobile-comm DC sources.

Notes measured on a real 66321D (fw A.03.01) over an NI GPIB-USB-HS:
  * one bus round trip ~10 ms; MEAS:VOLT?/CURR? ~50 ms at 2048 points.
  * voltage and current are digitized separately (FETC:VOLT? after
    MEAS:CURR? gives +603), so a V+I reading costs two acquisitions.
  * MEAS:ARR transfers at only ~4 KB/s (instrument-limited), i.e. ~6 s for
    2048 points.
"""
from __future__ import annotations

from ..transport import TransportTimeout
from .base import (SupplyCaps, SupplyPoll, SupplyReading, Setpoints, parse_bool,
                   parse_float as _f)

OPER_CV = 1 << 8
OPER_CC_POS = 1 << 10
OPER_CC_NEG = 1 << 11

QUES_FLAGS = {0: "OV", 1: "OCP", 4: "OT", 9: "RI", 10: "UNREG", 14: "OVLD"}

ARRAY_BYTES_PER_S = 3800.0   # measured
ARRAY_BYTES_PER_POINT = 11.7


class Agilent663xx:
    kind = "supply"
    family = "663xx"

    def __init__(self, transport):
        self.t = transport
        self.caps = SupplyCaps()
        self.log = None
        self._sweep = (2048, 15.6e-6)

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

    # ---- discovery -----------------------------------------------------
    def identify(self) -> SupplyCaps:
        self.t.clear()
        self.t.write("*CLS")
        c = SupplyCaps(has_meas_setup=True, has_waveform=True, has_pulse_stats=True)
        c.idn = self.t.query("*IDN?")
        parts = [p.strip() for p in c.idn.split(",")]
        c.model = parts[1] if len(parts) > 1 else "?"
        try:
            c.v_max = _f(self.t.query("VOLT? MAX"))
            c.i_max = _f(self.t.query("CURR? MAX"))
            c.ovp_max = _f(self.t.query("VOLT:PROT? MAX"))
        except Exception:
            pass
        c.has_dvm = self._supports("MEAS:DVM?")
        c.has_resistance = self._supports("RES?")
        if c.has_resistance:
            try:
                c.r_min = _f(self.t.query("RES? MIN"))
                c.r_max = _f(self.t.query("RES? MAX"))
            except Exception:
                pass
        c.has_auto_range = self._supports("SENS:CURR:RANG:AUTO?")
        self.caps = c
        return c

    def read_setpoints(self) -> Setpoints:
        s = Setpoints()
        s.voltage = _f(self.query("VOLT?"))
        s.current = _f(self.query("CURR?"))
        s.output = parse_bool(self.query("OUTP?"))
        s.ovp = _f(self.query("VOLT:PROT?"))
        s.ocp = parse_bool(self.query("CURR:PROT:STAT?"))
        s.sweep_points = int(_f(self.query("SENS:SWE:POIN?")))
        s.sweep_tint = _f(self.query("SENS:SWE:TINT?"))
        self._sweep = (s.sweep_points, s.sweep_tint)
        try:
            s.detector = self.query("SENS:CURR:DET?").strip().upper()
        except Exception:
            pass
        if self.caps.has_resistance:
            s.resistance = _f(self.query("RES?"))
        if self.caps.has_auto_range and parse_bool(self.query("SENS:CURR:RANG:AUTO?")):
            s.current_range = "AUTO"
        else:
            s.current_range = "LOW" if _f(self.query("SENS:CURR:RANG?")) < 0.1 else "HIGH"
        return s

    # ---- programming ---------------------------------------------------
    def set_voltage(self, v: float):
        self.write(f"VOLT {v:.4f}")

    def set_current(self, a: float):
        self.write(f"CURR {a:.5f}")

    def set_output(self, on: bool):
        self.write(f"OUTP {'ON' if on else 'OFF'}")

    def set_ovp(self, v: float):
        self.write(f"VOLT:PROT {v:.3f}")

    def set_ocp(self, on: bool):
        self.write(f"CURR:PROT:STAT {'ON' if on else 'OFF'}")

    def clear_protection(self):
        self.write("OUTP:PROT:CLE")

    def set_resistance(self, ohms: float):
        self.write(f"RES {ohms:.4f}")

    def set_current_range(self, rng: str):
        rng = rng.upper()
        if rng == "AUTO":
            self.write("SENS:CURR:RANG:AUTO ON")
            return
        if self.caps.has_auto_range:
            self.write("SENS:CURR:RANG:AUTO OFF")
        self.write("SENS:CURR:RANG " + ("MIN" if rng == "LOW" else "MAX"))

    def set_detector(self, det: str):
        self.write(f"SENS:CURR:DET {det.upper()}")

    def set_sweep(self, points: int, tint: float):
        self.write(f"SENS:SWE:POIN {int(points)}")
        self.write(f"SENS:SWE:TINT {tint:.4E}")
        self._sweep = (int(points), tint)

    def reset(self):
        self.write("*RST")
        self.write("*CLS")

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
        qs += ["STAT:OPER:COND?", "STAT:QUES:COND?", "OUTP?"]
        acq = self._sweep[0] * self._sweep[1] * (len(qs) - 3 - (5 if cfg.pulse_stats else 0))
        with self.t.temp_timeout(max(5.0, acq * 2 + 2)):
            vals = self.t.query(";:".join(qs)).split(";")
        if len(vals) != len(qs):
            raise RuntimeError(f"Expected {len(qs)} values, got {len(vals)}: {vals}")
        it = iter(vals)
        v = _f(next(it)) if cfg.measure_voltage else None
        r = SupplyReading(t=t, voltage=v, current=_f(next(it)), mode="--")
        if cfg.pulse_stats:
            r.i_max, r.i_min, r.i_high, r.i_low, r.i_rms = (_f(next(it)) for _ in range(5))
        if dvm:
            r.dvm = _f(next(it))
        oper, ques = int(_f(next(it))), int(_f(next(it)))
        r.output = parse_bool(next(it))
        r.flags = [name for bit, name in QUES_FLAGS.items() if ques & (1 << bit)]
        if not r.output:
            r.mode = "OFF"
        elif oper & OPER_CC_POS:
            r.mode = "CC"
        elif oper & OPER_CC_NEG:
            r.mode = "CC-"
        elif oper & OPER_CV:
            r.mode = "CV"
        return r

    def capture_seconds(self, points: int, tint: float) -> float:
        return points * tint + points * ARRAY_BYTES_PER_POINT / ARRAY_BYTES_PER_S

    def capture(self, what: str, points: int, tint: float) -> list[float]:
        """Digitize a waveform ('CURR' or 'VOLT'); restores the poll sweep."""
        restore = self._sweep
        self.set_sweep(points, tint)
        try:
            with self.t.temp_timeout(self.capture_seconds(points, tint) * 2 + 5):
                raw = self.t.query(f"MEAS:ARR:{what}?")
        finally:
            self.set_sweep(*restore)
        return [float(x) for x in raw.split(",") if x.strip()]
