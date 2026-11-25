"""A simulated 66321D with a GSM-handset-like pulsed load.

The output is a function of time: setpoint changes are kept as a history,
so waveforms show steps (with a first-order response), triggers can be
found in the signal before/after they happen (pre-trigger data), and the
digitizer, trigger system, transients, status registers, saved states and
the other settings behave like the real instrument closely enough to
exercise every control in the GUI.
"""
from __future__ import annotations

import copy
import time

import numpy as np

from .base import SimBase

V_MAX, I_MAX, OVP_MAX = 15.535, 3.0712, 22.0
RANGES = {"MAX": 3.0712, "1": 1.0, "MIN": 0.02}
R_MIN, R_MAX = -0.04, 1.0

# Load model: 47 ohm idle, 4.7 ohm during a 577 us burst every 4.615 ms.
R_IDLE, R_BURST = 47.0, 4.7
FRAME, BURST = 4.615e-3, 0.577e-3
TAU = 40e-6                      # output step response time constant

WTG = 1 << 5
CV, CCP = 1 << 8, 1 << 10

# name: (kind, default, choices-or-range).  NV = survives *RST.
SETTINGS = {
    "OUTP:PROT:DEL": ("num", 0.08, (0, 2147483.647)),
    "VOLT:PROT:STAT": ("bool", True, None),
    "OUTP:COMP:MODE": ("enum", "HREM", ("LLOC", "HLOC", "LREM", "HREM")),
    "OUTP:TYPE": ("enum", "HIGH", ("LOW", "HIGH")),
    "OUTP:DFI": ("bool", False, None),
    "OUTP:DFI:SOUR": ("enum", "OFF", ("QUES", "OPER", "ESB", "RQS", "OFF")),
    "DIG:DATA": ("int", 0, (0, 7)),
    "SENS:WIND": ("enum", "HANN", ("HANN", "RECT")),
    "SENS:PROT:STAT": ("bool", False, None),
    "SENS:CURR:DET": ("enum", "ACDC", ("ACDC", "DC")),
    "DISP": ("bool", True, None),
    "DISP:MODE": ("enum", "NORM", ("NORM", "TEXT")),
    "DISP:TEXT": ("str", "", None),
    "FORM": ("enum", "ASC", ("ASC", "REAL")),
    "FORM:BORD": ("enum", "NORM", ("NORM", "SWAP")),
    "TRIG:ACQ:SOUR": ("enum", "INT", ("BUS", "INT", "EXT")),
    "TRIG:SOUR": ("enum", "BUS", ("BUS",)),
    "SENS:SWE:POIN": ("int", 2048, (1, 4096)),
    "SENS:SWE:TINT": ("num", 15.6e-6, (15.6e-6, 31200)),
    "SENS:SWE:OFFS:POIN": ("int", 0, (-4095, 2_000_000_000)),
    "SENS:FUNC": ("str", "VOLT", None),
}
for _f in ("CURR", "VOLT", "DVM"):
    SETTINGS[f"TRIG:ACQ:LEV:{_f}"] = ("num", 0.0, (0, 1e3))
    SETTINGS[f"TRIG:ACQ:HYST:{_f}"] = ("num", 0.0, (0, 1e3))
    SETTINGS[f"TRIG:ACQ:SLOP:{_f}"] = ("enum", "POS", ("POS", "NEG", "EITH"))
    SETTINGS[f"TRIG:ACQ:COUN:{_f}"] = ("int", 1, (1, 100))
NV_SETTINGS = {"OUTP:RI:MODE": ("enum", "LATC", ("LATC", "LIVE", "OFF")),
               "OUTP:PON:STAT": ("enum", "RST", ("RST", "RCL0")),
               "DIG:FUNC": ("enum", "RIDF", ("RIDF", "DIG", "TRIG"))}
ALIASES = {"TRIG:SEQ2": "TRIG:ACQ", "TRIG:SEQ1": "TRIG:TRAN", "TRIG:TRAN:SOUR": "TRIG:SOUR",
           "DISP:WIND": "DISP", "DISP:STAT": "DISP", "OUTP:STAT": "OUTP",
           "SENS:CURR:RANG:UPP": "SENS:CURR:RANG", "DISP:TEXT:DATA": "DISP:TEXT",
           "SENS:WIND:TYPE": "SENS:WIND", "FORM:DATA": "FORM", "INIT:SEQ2": "INIT:NAME ACQ",
           "INIT:SEQ1": "INIT:NAME TRAN", "TRIG": "TRIG:TRAN", "TRIG:IMM": "TRIG:TRAN"}


class Sim663xx(SimBase):
    def __init__(self):
        super().__init__()
        self._errs: list[str] = []
        self._rng = np.random.default_rng()
        self._t0 = time.monotonic()
        self.nv = {k: d for k, (_, d, _) in NV_SETTINGS.items()}
        self.saved: dict[int, dict] = {}
        self.ques_ev = 0
        self._rst()

    def _now(self) -> float:
        return time.monotonic() - self._t0

    def _rst(self):
        self.cfg = {k: d for k, (_, d, _) in SETTINGS.items()}
        self.vset, self.iset, self.res, self.out = 0.0, 0.1 * I_MAX, 0.0, False
        self.vlim, self.ocp = OVP_MAX, False
        self.rng = "MAX"
        self.trig = {"VOLT": None, "CURR": None, "RES": None}   # None = follows immediate
        self.tripped = 0           # latched questionable condition bits
        self.acq = None            # armed/finished triggered acquisition
        self.tran = False          # transient system initiated
        self.last = ("VOLT", np.zeros(1))
        self.hist = []
        self._push()

    # ---- physics -----------------------------------------------------------
    def _push(self):
        """Record the output state from now on (the waveform model reads this)."""
        self.hist.append((self._now(), self.vset, self.iset, self.res, self.out))
        if len(self.hist) > 500:
            self.hist = self.hist[-250:]

    def _eval(self, t: np.ndarray, noise=True):
        """(voltage, current) at times t (seconds since start)."""
        h = np.array([x[:4] + (float(x[4]),) for x in self.hist])
        ht, hv, hi, hr, ho = h.T
        k = np.clip(np.searchsorted(ht, t, side="right") - 1, 0, len(ht) - 1)
        kp = np.maximum(k - 1, 0)
        target = hv[k] * ho[k]
        prev = hv[kp] * ho[kp]
        settle = 1 - np.exp(-np.maximum(t - ht[k], 0) / TAU)
        v_src = prev + (target - prev) * settle
        r_load = np.where(np.mod(t, FRAME) < BURST, R_BURST, R_IDLE)
        i = v_src / (r_load + hr[k])
        i = np.minimum(i, np.where(ho[k] > 0, hi[k], 0))
        v = i * r_load
        if noise:
            i = i + self._rng.normal(0, 2e-4, t.size)
            v = v + self._rng.normal(0, 3e-4, t.size)
        fs = RANGES[self.rng]
        i = np.clip(i, -fs * 1.02, fs * 1.02)
        return v, i

    def _cc_now(self) -> bool:
        return self.out and self.vset / (R_BURST + self.res) > self.iset

    def _measure(self, func: str) -> np.ndarray:
        n, dt = self.cfg["SENS:SWE:POIN"], self.cfg["SENS:SWE:TINT"]
        time.sleep(min(n * dt, 0.25))     # acquisitions take real time (capped)
        t = self._now() - n * dt + np.arange(n) * dt
        v, i = self._eval(t)
        if self.out and self.ocp and self._cc_now():
            self._trip(1)
        y = i if func == "CURR" else v
        self.last = (func, y)
        return y

    def _trip(self, bit: int):
        self.out = False
        self.tripped |= 1 << bit
        self.ques_ev |= 1 << bit
        self._push()

    def _dvm(self) -> float:
        time.sleep(0.03)
        return 3.7 + 0.15 * np.sin(self._now() / 8) + self._rng.normal(0, 1e-4)

    # ---- trigger system ----------------------------------------------------
    def _init_acq(self):
        c = self.cfg
        func = c["SENS:FUNC"]
        n, dt, off = c["SENS:SWE:POIN"], c["SENS:SWE:TINT"], c["SENS:SWE:OFFS:POIN"]
        count = c.get(f"TRIG:ACQ:COUN:{func}", 1) if func != "DVM" else 1
        if n * count > 4096:
            self._err(-221, "Settings conflict")
            return
        now = self._now()
        self.acq = {"func": func, "n": n, "dt": dt, "off": off, "count": count,
                    "src": c["TRIG:ACQ:SOUR"], "trig": [],
                    "ready": now + max(0, -off) * dt,      # pre-trigger buffer full
                    "searched": now + max(0, -off) * dt, "data": None,
                    "ext_at": now + max(0, -off) * dt + 0.4}

    def _bus_trigger_acq(self):
        a = self.acq
        if a and a["src"] == "BUS" and a["data"] is None and len(a["trig"]) < a["count"]:
            now = self._now()
            if now >= a["ready"]:        # earlier triggers are ignored (manual 7)
                a["trig"].append(now)
                a["ready"] = a["searched"] = now + (a["n"] + a["off"]) * a["dt"]

    def _fire_transient(self):
        if not self.tran:
            return
        self.tran = False
        for k, attr in (("VOLT", "vset"), ("CURR", "iset"), ("RES", "res")):
            if self.trig[k] is not None:
                setattr(self, attr, self.trig[k])
        self._push()

    def _update_acq(self):
        a = self.acq
        if not a or a["data"] is not None:
            return
        now = self._now()
        while len(a["trig"]) < a["count"]:
            if a["src"] == "EXT":
                if now < a["ext_at"]:
                    return
                tt = a["ext_at"]
            elif a["src"] == "INT":
                tt = self._search(a, now)
                if tt is None:
                    return
            else:
                return
            a["trig"].append(tt)
            a["ready"] = a["searched"] = tt + (a["n"] + a["off"]) * a["dt"]
            a["ext_at"] = a["ready"] + 0.4
        end = a["trig"][-1] + (a["n"] + a["off"]) * a["dt"]
        if now < end:
            return
        segs = []
        for tt in a["trig"]:
            t = tt + (a["off"] + np.arange(a["n"])) * a["dt"]
            v, i = self._eval(t)
            segs.append(i if a["func"] == "CURR" else v)
        a["data"] = np.concatenate(segs)
        self.last = (a["func"], a["data"])

    def _search(self, a, now):
        """First level crossing (with hysteresis/slope) between a['searched'] and now."""
        func = a["func"]
        lev = self.cfg[f"TRIG:ACQ:LEV:{func}"]
        hyst = self.cfg[f"TRIG:ACQ:HYST:{func}"] / 2
        slope = self.cfg[f"TRIG:ACQ:SLOP:{func}"]
        start = max(a["searched"], now - 2.0)
        if now <= start:
            return None
        dt = max(a["dt"], (now - start) / 200_000)
        t = np.arange(start, now, dt)
        if t.size < 2:
            return None
        v, i = self._eval(t, noise=False)
        y = i if func == "CURR" else v
        a["searched"] = now
        hits = []
        if slope in ("POS", "EITH"):
            below = np.flatnonzero(y < lev - hyst)
            if below.size:
                above = np.flatnonzero((y > lev + hyst) & (np.arange(y.size) > below[0]))
                if above.size:
                    hits.append(t[above[0]])
        if slope in ("NEG", "EITH"):
            above = np.flatnonzero(y > lev + hyst)
            if above.size:
                below = np.flatnonzero((y < lev - hyst) & (np.arange(y.size) > above[0]))
                if below.size:
                    hits.append(t[below[0]])
        return min(hits) if hits else None

    def _acq_waiting(self) -> bool:
        self._update_acq()
        return bool(self.acq and self.acq["data"] is None)

    # ---- I/O ---------------------------------------------------------------
    def write(self, cmd: str):
        for part in cmd.split(";"):
            part = part.strip().lstrip(":")
            if part:
                self._exec(part)

    @staticmethod
    def _num(x: float) -> str:
        return f"{x:+.5E}"

    def _err(self, code, msg):
        self._errs.append(f"{code:+d},\"{msg}\"")

    def _array(self, y: np.ndarray):
        n = y.size
        bytes_ = n * (4 if self.cfg["FORM"] == "REAL" else 12)
        time.sleep(min(bytes_ / 3700, 0.5))        # slow instrument transfer (capped)
        if self.cfg["FORM"] == "REAL":
            self._bin = [float(x) for x in y]
        else:
            self._out.append(",".join(f"{x:+.5E}" for x in y))

    def _setting(self, table, h, q, arg):
        kind, default, rng = table[h]
        store = self.cfg if table is SETTINGS else self.nv
        if q:
            v = store[h]
            out = {"bool": lambda: "1" if v else "0", "int": lambda: f"{int(v):+d}",
                   "num": lambda: self._num(v), "enum": lambda: v,
                   "str": lambda: f'"{v}"'}[kind]()
            if h == "FORM":
                out += ",+0" if v == "ASC" else ",+32"
            self._out.append(out)
            return
        raw = arg.strip()
        a = raw.upper().split(",")[0].strip()
        if kind == "bool":
            if a not in ("0", "1", "ON", "OFF"):
                return self._err(-104, "Data type error")
            store[h] = a in ("1", "ON")
        elif kind in ("int", "num"):
            try:
                x = {"MAX": rng[1], "MIN": rng[0]}.get(a)
                x = float(a) if x is None else x
            except ValueError:
                return self._err(-104, "Data type error")
            if not rng[0] <= x <= rng[1]:
                return self._err(-222, "Data out of range")
            if h == "SENS:SWE:TINT":
                x = max(15.6e-6, round(x / 15.6e-6) * 15.6e-6)
            store[h] = int(x) if kind == "int" else x
        elif kind == "enum":
            match = [c for c in rng if a.startswith(c)]
            if not match:
                return self._err(-224, "Illegal parameter value")
            store[h] = match[0]
        else:
            store[h] = raw.strip('"').upper() if h == "SENS:FUNC" else raw.strip('"')
            if h == "SENS:FUNC":
                store[h] = {"CURRENT": "CURR", "VOLTAGE": "VOLT"}.get(store[h], store[h])

    def _exec(self, part: str):
        hdr, _, arg = part.partition(" ")
        hdr = hdr.upper()
        for noise in (":IMM", ":AMPL", ":DC", ":SCAL"):
            hdr = hdr.replace(noise, "")
        if hdr.startswith(("VOLT", "CURR", "RES")):
            hdr = hdr.replace(":LEV", "")
        hdr = hdr.replace(":TRIGGERED", ":TRIG")
        q = hdr.endswith("?")
        h = hdr.rstrip("?")
        h = ALIASES.get(h, h)
        if h.startswith("INIT:NAME"):
            arg, h = h.split()[-1] if " " in h else arg, "INIT:NAME"
        A = arg.strip().upper()
        out = self._out.append

        def val(cur, lo, hi):
            if A in ("MAX", "MAXIMUM"):
                return hi
            if A in ("MIN", "MINIMUM"):
                return lo
            try:
                x = float(A)
            except ValueError:
                self._err(-104, "Data type error")
                return cur
            if not lo <= x <= hi:
                self._err(-222, "Data out of range")
                return cur
            return x

        def onoff():
            return A in ("1", "ON")

        if h in SETTINGS:
            self._setting(SETTINGS, h, q, arg)
        elif h in NV_SETTINGS:
            self._setting(NV_SETTINGS, h, q, arg)
        elif h == "*IDN":
            out("Agilent Technologies,66321D,SIM0000001,A.03.01 (simulated)")
        elif h == "*RST":
            self._rst()
        elif h == "*CLS":
            self._errs.clear()
            self.ques_ev = 0
        elif h in ("*OPC", "*TST", "*OPT", "*ESR"):
            if h == "*TST":
                time.sleep(0.8)
            out("0" if h in ("*TST", "*OPT", "*ESR") else "1") if q else None
        elif h == "*SAV":
            self.saved[int(float(A))] = copy.deepcopy(
                {"cfg": self.cfg, "v": self.vset, "i": self.iset, "r": self.res,
                 "out": self.out, "vlim": self.vlim, "ocp": self.ocp, "rng": self.rng})
        elif h == "*RCL":
            s = self.saved.get(int(float(A)))
            if s is None:
                self._rst()
            else:
                self.cfg = copy.deepcopy(s["cfg"])
                self.vset, self.iset, self.res = s["v"], s["i"], s["r"]
                self.out, self.vlim, self.ocp, self.rng = s["out"], s["vlim"], s["ocp"], s["rng"]
                self.acq, self.tran = None, False
                self._push()
        elif h == "*TRG":
            self._bus_trigger_acq()
            self._fire_transient()
        elif h == "SYST:ERR":
            out(self._errs.pop(0) if self._errs else "+0,\"No error\"")
        elif h == "SYST:VERS":
            out("1995.0")
        elif h == "CAL:DATE":
            out("01/15/24")
        elif h == "ABOR":
            self.acq = None
            self.tran = False
            self.trig = {"VOLT": None, "CURR": None, "RES": None}
        elif h == "INIT:NAME":
            if A.startswith("ACQ"):
                self._init_acq()
            elif A.startswith("TRAN"):
                self.tran = True
        elif h == "TRIG:ACQ":
            if self.acq and self.acq["src"] == "BUS":
                self._bus_trigger_acq()
            elif self.acq:            # TRIG:ACQ:IMM works whatever the source
                self.acq["src"] = "BUS"
                self._bus_trigger_acq()
        elif h == "TRIG:TRAN":
            self._fire_transient()
        elif h in ("VOLT:TRIG", "CURR:TRIG", "RES:TRIG"):
            k = h.split(":")[0]
            cur = {"VOLT": self.vset, "CURR": self.iset, "RES": self.res}[k]
            if q:
                out(self._num(self.trig[k] if self.trig[k] is not None else cur))
            else:
                lim = {"VOLT": (0, V_MAX), "CURR": (0, I_MAX), "RES": (R_MIN, R_MAX)}[k]
                self.trig[k] = val(cur, *lim)
        elif h == "VOLT":
            if q:
                out(self._num({"MAX": V_MAX, "MIN": 0}.get(A, self.vset)))
            else:
                x = val(self.vset, 0, V_MAX)
                if x > self.vlim:
                    self._err(-222, "Data out of range (above VOLT:PROT)")
                else:
                    self.vset = x
                    self._push()
        elif h == "CURR":
            if q:
                out(self._num({"MAX": I_MAX, "MIN": 0}.get(A, self.iset)))
            else:
                self.iset = val(self.iset, 0, I_MAX)
                self._push()
        elif h == "RES":
            if q:
                out(self._num({"MAX": R_MAX, "MIN": R_MIN}.get(A, self.res)))
            else:
                self.res = val(self.res, R_MIN, R_MAX)
                self._push()
        elif h == "OUTP":
            if q:
                out("1" if self.out else "0")
            else:
                if onoff() and self.tripped:
                    self._err(-221, "Settings conflict; protection tripped")
                else:
                    self.out = onoff()
                    self._push()
        elif h == "OUTP:PROT:CLE":
            self.tripped = 0
        elif h == "VOLT:PROT":
            if q:
                out(self._num({"MAX": OVP_MAX, "MIN": 0}.get(A, self.vlim)))
            else:
                self.vlim = val(self.vlim, 0, OVP_MAX)
        elif h == "CURR:PROT:STAT":
            if q:
                out("1" if self.ocp else "0")
            else:
                self.ocp = onoff()
        elif h == "SENS:CURR:RANG":
            if q:
                out(self._num({"MAX": I_MAX, "MIN": 0.02}.get(A, RANGES[self.rng])))
            else:
                x = val(I_MAX, 0, I_MAX)
                self.rng = "MIN" if x <= 0.02 else ("1" if x <= 1.0 else "MAX")
        elif h in ("MEAS:VOLT", "MEAS:CURR"):
            out(self._num(self._measure(h[5:]).mean()))
        elif h in ("MEAS:VOLT:ACDC", "MEAS:CURR:ACDC"):
            y = self._measure(h[5:9])
            out(self._num(float(np.sqrt((y ** 2).mean()))))
        elif h in ("MEAS:ARR:CURR", "MEAS:ARR:VOLT"):
            self._array(self._measure(h[-4:]))
        elif h in ("MEAS:DVM", "MEAS:DVM:ACDC", "FETC:DVM", "FETC:DVM:ACDC"):
            out(self._num(self._dvm()))
        elif h in ("FETC:ARR:CURR", "FETC:ARR:VOLT"):
            func = h[-4:]
            deadline = time.monotonic() + self.timeout_s
            while self._acq_waiting() and time.monotonic() < deadline:
                time.sleep(0.01)    # FETCh waits for the acquisition (manual 7)
            if self._acq_waiting():
                return
            if self.last[0] != func:
                return self._err(603, "CURRent or VOLTage fetch incompatible with last acquisition")
            self._array(self.last[1])
        elif h.startswith("FETC:CURR") or h.startswith("FETC:VOLT"):
            func, sub = h[5:9], h[9:].lstrip(":")
            self._update_acq()
            if self.last[0] != func:
                return self._err(603, "CURRent or VOLTage fetch incompatible with last acquisition")
            y = self.last[1]
            mid = (y.max() + y.min()) / 2
            hi = y[y >= mid].mean() if (y >= mid).any() else y.max()
            lo = y[y < mid].mean() if (y < mid).any() else y.min()
            out(self._num({"": y.mean(), "MAX": y.max(), "MIN": y.min(), "HIGH": hi, "LOW": lo,
                           "ACDC": float(np.sqrt((y ** 2).mean()))}.get(sub[:4], y.mean())))
        elif h == "STAT:OPER:COND":
            bits = WTG if (self._acq_waiting() or self.tran) else 0
            if self.out:
                bits |= CCP if self._cc_now() else CV
            out(f"{bits:+d}")
        elif h == "STAT:QUES:COND":
            out(f"{self.tripped:+d}")
        elif h == "STAT:QUES":
            out(f"{self.ques_ev:+d}")
            self.ques_ev = 0
        else:
            self._err(-113, "Undefined header")


V2_MAX, I2_MAX, R2 = 12.25, 1.52, 10.0
CV2, CC2 = 1 << 9, 1 << 12


class Sim66309(Sim663xx):
    """A 66309B: no DVM, output resistance, 1 A range or external trigger; a
    real OVP; and output 2, feeding 10 ohm. Before fw A.02.04 (coupling=False)
    the outputs can't be separated."""

    def __init__(self, coupling: bool = True):
        self.coupling = coupling
        super().__init__()

    def _rst(self):
        super()._rst()
        self.cfg["OUTP:TYPE"] = "LOW"
        self.v2, self.i2, self.out2, self.coupled = 0.0, 0.1 * I2_MAX, False, True
        self.saved2 = getattr(self, "saved2", {})

    def _out2_now(self) -> tuple[float, float]:
        i = min(self.v2 / R2, self.i2) if self.out2 else 0.0
        return i * R2, i

    def _cc2(self) -> bool:
        return self.out2 and self.v2 / R2 > self.i2

    def _check(self):
        """Protection trips."""
        if self.out and self.cfg["VOLT:PROT:STAT"] and self.vset > self.vlim:
            self._trip(0)
        if self.ocp and self._cc2():
            self._trip(12)

    def _trip(self, bit: int):
        out = self.out
        super()._trip(bit)
        if bit == 12:
            self.out2 = False
            self.out = out and not self.coupled
        elif self.coupled:
            self.out2 = False
        self._push()

    def _exec(self, part: str):
        hdr, _, arg = part.partition(" ")
        hdr, A = hdr.upper(), arg.strip().upper()
        q, h = hdr.endswith("?"), hdr.rstrip("?")
        out = self._out.append

        def level(cur, hi):
            try:
                x = {"MAX": hi, "MIN": 0.0}.get(A)
                x = float(A) if x is None else x
            except ValueError:
                self._err(-104, "Data type error")
                return cur
            if not 0 <= x <= hi:
                self._err(-222, "Data out of range")
                return cur
            return x

        if h == "*IDN":
            out(f"HEWLETT-PACKARD,66309B,SIM0000002,A.02.{'04' if self.coupling else '00'}")
        elif h.startswith(("RES", "OUTP:COMP")) or ":DVM" in h or A == "EXT":
            self._err(-113, "Undefined header")
        elif h == "OUTP:TYPE":
            if q:
                out(self.cfg[h])
            elif A in ("LOW", "HIGH", "H2"):
                self.cfg[h] = A
            else:
                self._err(-224, "Illegal parameter value")
        elif h in ("VOLT2", "CURR2"):
            k, hi = ("v2", V2_MAX) if h == "VOLT2" else ("i2", I2_MAX)
            if q:
                out(self._num({"MAX": hi, "MIN": 0.0}.get(A, getattr(self, k))))
            else:
                setattr(self, k, level(getattr(self, k), hi))
        elif h in ("MEAS:VOLT2", "MEAS:CURR2"):
            time.sleep(0.032)
            out(self._num(self._out2_now()[h == "MEAS:CURR2"] + self._rng.normal(0, 1e-4)))
        elif h == "VOLT" and not q:
            self.vset = level(self.vset, V_MAX)
            self._push()
        elif h == "INST:COUP:OUTP:STAT" and self.coupling:
            if q:
                out("ALL" if self.coupled else "NONE")
            elif A in ("ALL", "NONE"):
                self.coupled = A == "ALL"
            else:
                self._err(-224, "Illegal parameter value")
        elif h == "OUTP2" and self.coupling:
            if q:
                out("1" if self.out2 else "0")
            elif self.coupled:
                self._both(A)
            elif A in ("1", "ON") and self.tripped:
                self._err(-221, "Settings conflict; protection tripped")
            else:
                self.out2 = A in ("1", "ON")
        elif h == "OUTP" and not q and self.coupled:
            self._both(A)
        elif h == "STAT:OPER:COND":
            super()._exec(part)
            if self.out2:
                self._out[-1] = f"{int(self._out[-1]) | (CC2 if self._cc2() else CV2):+d}"
        elif h in ("*SAV", "*RCL"):
            super()._exec(part)
            n = int(float(A))
            if h == "*SAV":
                self.saved2[n] = (self.v2, self.i2, self.out2, self.coupled)
            elif n in self.saved2:
                self.v2, self.i2, self.out2, self.coupled = self.saved2[n]
        elif h == "SENS:CURR:RANG" and not q:
            super()._exec(part)
            self.rng = "MAX" if self.rng == "1" else self.rng
        else:
            super()._exec(part)
        self._check()

    def _both(self, A: str):
        super()._exec(f"OUTP {A}")
        self.out2 = self.out
