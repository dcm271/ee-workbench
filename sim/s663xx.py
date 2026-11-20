"""A simulated 66321D with a GSM-handset-like pulsed load.

Good enough to exercise every control in the GUI without hardware: CV/CC
crossover, OVP/OCP trips, current ranges, pulse statistics, digitized
waveforms, the DVM input and the SCPI error queue.
"""
from __future__ import annotations

import time

import numpy as np

from .base import SimBase

V_MAX, I_MAX, OVP_MAX = 15.535, 3.0712, 22.0
LOW_RANGE = 0.02

# Load model: 47 ohm idle, 4.7 ohm during a 577 us burst every 4.615 ms.
R_IDLE, R_BURST = 47.0, 4.7
FRAME, BURST = 4.615e-3, 0.577e-3


class Sim663xx(SimBase):
    def __init__(self):
        super().__init__()
        self._errs: list[str] = []
        self._rng = np.random.default_rng()
        self._t0 = time.monotonic()
        self._last_i = np.zeros(1)
        self._last_v = np.zeros(1)
        self._rst()

    def _rst(self):
        self.vset, self.iset = 0.0, 0.5
        self.out = False
        self.ovp, self.ocp = OVP_MAX, False
        self.res = 0.0
        self.points, self.tint = 2048, 15.6e-6
        self.rng_low, self.auto = False, False
        self.det, self.window = "ACDC", "HANN"
        self.tripped = 0  # questionable bits latched by protection

    def write(self, cmd: str):
        for part in cmd.split(";"):
            part = part.strip().lstrip(":")
            if part:
                self._exec(part)

    # ---- physics -----------------------------------------------------------
    def _acquire(self, n=None, dt=None):
        n = n or self.points
        dt = dt or self.tint
        t = self._t0 + np.arange(n) * dt + (time.monotonic() - self._t0)
        phase = np.mod(t, FRAME)
        r_load = np.where(phase < BURST, R_BURST, R_IDLE)
        if not self.out:
            v = np.zeros(n)
            i = np.zeros(n)
        else:
            r_tot = r_load + self.res
            i = self.vset / r_tot
            cc = i > self.iset
            i = np.where(cc, self.iset, i)
            v = i * r_load  # output voltage at the terminals
            # A touch of droop/ringing so it looks like a real supply.
            i = i + self._rng.normal(0, 2e-4, n)
            v = v + self._rng.normal(0, 3e-4, n)
            if cc.any() and self.ocp:
                self.out = False
                self.tripped |= 1 << 1
            if v.max() > self.ovp:
                self.out = False
                self.tripped |= 1 << 0
        if self.rng_low and not self.auto:
            i = np.clip(i, -LOW_RANGE * 1.02, LOW_RANGE * 1.02)
        # Acquisition takes real time on the instrument (capped for UX).
        time.sleep(min(n * dt, 0.25))
        self._last_i, self._last_v = i, v
        return v, i

    def _cc_now(self) -> bool:
        return self.out and (self.vset / (R_IDLE + self.res)) > self.iset

    @staticmethod
    def _hilo(a):
        mid = (a.max() + a.min()) / 2
        hi = a[a >= mid]
        lo = a[a < mid]
        return (hi.mean() if hi.size else a.max()), (lo.mean() if lo.size else a.min())

    @staticmethod
    def _num(x: float) -> str:
        return f"{x:+.6E}"

    # ---- SCPI ------------------------------------------------------------
    def _err(self, code, msg):
        self._errs.append(f"{code},\"{msg}\"")

    def _exec(self, part: str):
        hdr, _, arg = part.partition(" ")
        hdr = hdr.upper()
        for noise in (":LEV", ":IMM", ":AMPL", ":DC"):
            hdr = hdr.replace(noise, "")
        arg = arg.strip().upper()
        q = hdr.endswith("?")
        h = hdr.rstrip("?")
        out = self._out.append

        def val(cur, lo, hi):
            if arg in ("MAX", "MAXIMUM"):
                return hi
            if arg in ("MIN", "MINIMUM"):
                return lo
            try:
                x = float(arg)
            except ValueError:
                self._err(-104, "Data type error")
                return cur
            if not lo <= x <= hi:
                self._err(-222, "Data out of range")
                return cur
            return x

        def onoff():
            return arg in ("1", "ON")

        if h == "*IDN":
            out("Agilent Technologies,66321D,SIM0000001,A.03.01 (simulated)")
        elif h == "*RST":
            self._rst()
        elif h == "*CLS":
            self._errs.clear()
        elif h == "*OPC":
            out("1")
        elif h == "SYST:ERR":
            out(self._errs.pop(0) if self._errs else "+0,\"No error\"")
        elif h == "VOLT" and not q:
            self.vset = val(self.vset, 0, V_MAX)
        elif h == "VOLT":
            out(self._num({"MAX": V_MAX, "MIN": 0}.get(arg, self.vset)))
        elif h == "CURR" and not q:
            self.iset = val(self.iset, 0, I_MAX)
        elif h == "CURR":
            out(self._num({"MAX": I_MAX, "MIN": 0}.get(arg, self.iset)))
        elif h in ("OUTP", "OUTP:STAT"):
            if q:
                out("1" if self.out else "0")
            else:
                self.out = onoff() and not self.tripped
                if onoff() and self.tripped:
                    self._err(-221, "Settings conflict; protection tripped")
        elif h == "OUTP:PROT:CLE":
            self.tripped = 0
        elif h == "VOLT:PROT":
            if q:
                out(self._num({"MAX": OVP_MAX, "MIN": 0}.get(arg, self.ovp)))
            else:
                self.ovp = val(self.ovp, 0, OVP_MAX)
        elif h == "VOLT:PROT:STAT":
            out("1") if q else None
        elif h == "CURR:PROT:STAT":
            if q:
                out("1" if self.ocp else "0")
            else:
                self.ocp = onoff()
        elif h == "RES":
            if q:
                out(self._num({"MAX": 1.0, "MIN": -0.04}.get(arg, self.res)))
            else:
                self.res = val(self.res, -0.04, 1.0)
        elif h == "SENS:CURR:RANG:AUTO":
            if q:
                out("1" if self.auto else "0")
            else:
                self.auto = onoff()
        elif h == "SENS:CURR:RANG":
            if q:
                out(self._num(LOW_RANGE if self.rng_low else I_MAX))
            else:
                self.rng_low = val(I_MAX, 0, I_MAX) <= LOW_RANGE if arg not in ("MIN", "MAX") else arg == "MIN"
        elif h == "SENS:CURR:DET":
            if q:
                out(self.det)
            else:
                self.det = arg
        elif h == "SENS:WIND":
            if q:
                out(self.window)
            else:
                self.window = arg
        elif h == "SENS:SWE:POIN":
            if q:
                out(self._num(self.points))
            else:
                self.points = int(val(self.points, 1, 4096))
        elif h == "SENS:SWE:TINT":
            if q:
                out(self._num(self.tint))
            else:
                self.tint = val(self.tint, 15.6e-6, 31200)
        elif h == "SENS:FUNC":
            pass
        elif h == "MEAS:VOLT":
            v, _ = self._acquire()
            out(self._num(v.mean()))
        elif h == "MEAS:CURR":
            _, i = self._acquire()
            out(self._num(i.mean()))
        elif h == "MEAS:VOLT:ACDC":
            v, _ = self._acquire()
            out(self._num(float(np.sqrt((v ** 2).mean()))))
        elif h == "MEAS:CURR:ACDC":
            _, i = self._acquire()
            out(self._num(float(np.sqrt((i ** 2).mean()))))
        elif h.startswith("FETC:CURR"):
            i = self._last_i
            sub = h[len("FETC:CURR"):].lstrip(":")
            hi, lo = self._hilo(i)
            out(self._num({"": i.mean(), "MAX": i.max(), "MIN": i.min(), "HIGH": hi,
                           "LOW": lo, "ACDC": float(np.sqrt((i ** 2).mean()))}.get(sub, i.mean())))
        elif h.startswith("FETC:VOLT"):
            out(self._num(self._last_v.mean()))
        elif h in ("MEAS:DVM", "MEAS:DVM:ACDC"):
            time.sleep(0.03)
            t = time.monotonic() - self._t0
            out(self._num(3.7 + 0.15 * np.sin(t / 8) + self._rng.normal(0, 1e-4)))
        elif h == "MEAS:ARR:CURR":
            _, i = self._acquire()
            out(",".join(f"{x:+.5E}" for x in i))
        elif h == "MEAS:ARR:VOLT":
            v, _ = self._acquire()
            out(",".join(f"{x:+.5E}" for x in v))
        elif h == "STAT:OPER:COND":
            out(str((1 << 10) if self._cc_now() else ((1 << 8) if self.out else 0)))
        elif h == "STAT:QUES:COND":
            out(str(self.tripped))
        else:
            self._err(-113, "Undefined header")
