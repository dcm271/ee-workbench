"""A 4-channel oscilloscope simulator that speaks the Rigol, Keysight,
Tektronix or Siglent command dialect (instruments/scopes.py).

  CH1  1 kHz 0-3.3 V square wave (trigger source; a rising edge at t = 0)
  CH2  1 kHz 1 V-peak sine
  CH3  500 Hz 0-2 V triangle
  CH4  1.2 V DC with noise; with a simulated load, a regulator's output (shared.py)
CH1 and CH2 are shown at start. While a simulated function generator drives a
sine, CH1 is that sine and CH2 the same through a 10 kHz low-pass (for the
Bode window). Waveforms are encoded with each maker's
byte format and preamble, so the driver's decoding is exercised for real.
"""
from __future__ import annotations

import re
import time

import numpy as np

from . import shared
from .base import SimBase

_N = r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"
DIVS = {"rigol": 12, "keysight": 10, "tek": 10, "siglent": 14}
POINTS = {"rigol": 1200, "keysight": 1000, "tek": 2000, "siglent": 1400}
IDN = {"rigol": "RIGOL TECHNOLOGIES,DS1104Z,SIM0001,00.04.05",
       "keysight": "KEYSIGHT TECHNOLOGIES,DSOX1204G,SIM0001,02.12",
       "tek": "TEKTRONIX,TBS2104,SIM0001,CF:91.1CT FV:v1.0",
       "siglent": "Siglent Technologies,SDS1104X-E,SIM0001,8.2.6.1.37"}


def signal(ch: int, t: np.ndarray) -> np.ndarray:
    rng = np.random.default_rng()
    d = shared.drive()
    if d is not None and ch in (1, 2):
        f, vpp, offs = d
        h = 1.0 if ch == 1 else shared.filter_response(f)
        dc = offs                              # a low-pass: DC goes through
        v = dc + abs(h) * vpp / 2 * np.sin(2 * np.pi * f * t + np.angle(h))
        return v + rng.normal(0, 2e-4 + 0.002 * abs(h) * vpp / 2, t.size)
    if ch == 1:
        ph = np.mod(t, 1e-3) / 1e-3
        edge = 0.5 * (np.tanh((ph - 0.0) / 0.004) - np.tanh((ph - 0.5) / 0.004)) + \
            0.5 * (1 + np.tanh((ph - 1.0) / 0.004))
        v = 3.3 * np.clip(edge, 0, 1)
    elif ch == 2:
        v = np.sin(2 * np.pi * 1e3 * t)
    elif ch == 3:
        ph = np.mod(t, 2e-3) / 2e-3
        v = 2.0 * (1 - np.abs(2 * ph - 1))
    else:
        return np.full_like(t, 1.2 - 0.004 * shared.LOAD[0]) + rng.normal(0, 5e-4, t.size)
    return v + rng.normal(0, 0.01, t.size)


class SimScope(SimBase):
    def __init__(self, dialect: str = "rigol"):
        super().__init__()
        self.dialect = dialect
        self.on = [True, True, False, False]
        self.scale = [1.0, 0.5, 0.5, 0.5]
        self.offset = [0.0, 0.0, 0.0, 0.0]
        self.tscale = 2e-4
        self.tpos = 0.0
        self.trig_src, self.trig_level, self.trig_slope = 1, 1.5, "POS"
        self.sweep = "AUTO"
        self.running = True
        self.single_at: float | None = None
        self.stepped = False
        self.src = 1
        self.errors: list[str] = []

    # ---- state --------------------------------------------------------------
    def _state(self) -> str:
        if self.single_at is not None:
            if time.monotonic() - self.single_at < 0.15:
                return "wait"
            if self.trig_src == 4:              # waits for a load step that way
                now_a, was, when = shared.LOAD
                want = "NEG" if now_a > was else "POS"
                if when < self.single_at or self.trig_slope[:3] not in (want, "FAL"
                                                                        if want == "NEG" else
                                                                        "RIS"):
                    return "wait"
                self.stepped = True
            self.single_at = None
            self.running = False
        return "run" if self.running else "stop"

    def _trace(self, ch: int):
        n = POINTS[self.dialect]
        span = DIVS[self.dialect] * self.tscale
        dt = span / n
        t0 = -span / 2 - self.tpos
        t = t0 + np.arange(n) * dt
        if ch == 4 and self.stepped and not self.running:
            return t0, dt, shared.regulator_out(t + dt)
        return t0, dt, signal(ch, t)

    # ---- command handling --------------------------------------------------------
    def write(self, cmd: str):
        for part in cmd.split(";"):
            part = part.strip().lstrip(":")
            if part:
                self._one(part)

    def _one(self, c: str):
        u = c.upper()
        if u == "*IDN?":
            return self._out.append(IDN[self.dialect])
        if u in ("*CLS", "*RST", "CHDR SHORT"):
            return self.errors.clear() if u == "*CLS" else None
        if u in {"keysight": ("DISP:DATA? PNG",), "rigol": ("DISP:DATA? ON,OFF,PNG",),
                 "siglent": ("SCDP",)}.get(self.dialect, ()):
            self._bin = self._screen()
            return None
        h = getattr(self, "_" + self.dialect)
        if h(u) is False:
            self.errors.append('-113,"Undefined header"')

    @staticmethod
    def _screen(w: int = 320, h: int = 200) -> bytes:
        """A PNG: a sine on a dark screen."""
        import struct
        import zlib
        px = np.zeros((h, w), np.uint8) + 20
        px[::h // 8, :] = px[:, ::w // 10] = 70
        x = np.arange(w)
        px[(h / 2 + h / 3 * np.sin(x / w * 12.6)).astype(int), x] = 255
        raw = b"".join(b"\0" + r.tobytes() for r in px)

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(
                ">I", zlib.crc32(kind + data))
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(
            ">IIBBBBB", w, h, 8, 0, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(
            b"IEND", b"")

    def _err(self):
        self._out.append(self.errors.pop(0) if self.errors else '0,"No error"')

    def _chan(self, ch, what, val):
        i = ch - 1
        if not 0 <= i < 4:
            return False
        if what == "on":
            self.on[i] = val
        elif what == "scale":
            self.scale[i] = val
        else:
            self.offset[i] = val

    def _rigol_keysight(self, u: str, rigol: bool):
        m = re.match(r"^CHAN(?:NEL)?(\d):(DISP|SCAL|OFFS)\w*(\?|\s+(\S+))$", u)
        if m:
            ch, what = int(m.group(1)), {"DISP": "on", "SCAL": "scale", "OFFS": "offset"}[
                m.group(2)]
            if not 1 <= ch <= 4:
                return False
            if m.group(3) == "?":
                v = [self.on, self.scale, self.offset][["on", "scale", "offset"].index(what)][
                    ch - 1]
                return self._out.append(("1" if v else "0") if what == "on" else f"{v:.6E}")
            arg = m.group(4)
            return self._chan(ch, what, arg in ("1", "ON") if what == "on" else float(arg))
        tim = r"TIM(?:EBASE)?:(SCAL|OFFS|POS)\w*"
        m = re.match("^" + tim + r"(\?|\s+" + _N + ")$", u)
        if m:
            attr = "tscale" if m.group(1) == "SCAL" else "tpos"
            if m.group(2) == "?":
                return self._out.append(f"{getattr(self, attr):.6E}")
            return setattr(self, attr, float(m.group(3)))
        m = re.match(r"^TRIG(?:GER)?:EDGE?:(SOUR|LEV|SLOP)\w*(\?|\s+(\S+))$", u)
        if m:
            k, q, arg = m.group(1), m.group(2) == "?", m.group(3)
            if k == "SOUR":
                if q:
                    return self._out.append(f"CHAN{self.trig_src}")
                self.trig_src = int(arg[-1])
            elif k == "LEV":
                if q:
                    return self._out.append(f"{self.trig_level:.6E}")
                self.trig_level = float(arg)
            else:
                if q:
                    return self._out.append(self.trig_slope)
                self.trig_slope = arg
            return None
        m = re.match(r"^TRIG(?:GER)?:SWE\w*(\?|\s+(\S+))$", u)
        if m:
            if m.group(1) == "?":
                return self._out.append(self.sweep)
            self.sweep = m.group(2)[:4]
            return None
        if u in ("RUN",):
            self.running, self.single_at = True, None
            return None
        if u == "STOP":
            self.running, self.single_at = False, None
            return None
        if u in ("SING", "SINGLE"):
            self.running, self.single_at, self.stepped = True, time.monotonic(), False
            return None
        if u in ("SYST:ERR?", "SYSTEM:ERROR?"):
            return self._err()
        if rigol and u == "TRIG:STAT?":
            s = self._state()
            return self._out.append({"wait": "WAIT", "run": "AUTO" if self.sweep == "AUTO"
                                     else "RUN", "stop": "STOP"}[s])
        if not rigol and u == "OPER:COND?":
            return self._out.append("8" if self._state() != "stop" else "0")
        m = re.match(r"^WAV(?:EFORM)?:SOUR\w*\s+CHAN(?:NEL)?(\d)$", u)
        if m:
            self.src = int(m.group(1))
            return None
        if re.match(r"^WAV:(MODE|FORM|POIN:MODE|UNS)\w*\s+\S+$", u):
            return None
        if u in ("WAV:PRE?", "WAVEFORM:PREAMBLE?"):
            t0, dt, _ = self._trace(self.src)
            yinc = self.scale[self.src - 1] / 25.0
            n = POINTS[self.dialect]
            if rigol:
                yor, yref = self.offset[self.src - 1] / yinc, 127
            else:
                yor, yref = self.offset[self.src - 1], 128    # the volts at mid-screen
            return self._out.append(",".join(f"{x:.6E}" for x in
                                             (0, 0, n, 1, dt, t0, 0, yinc, yor, yref)))
        if u in ("WAV:DATA?", "WAVEFORM:DATA?"):
            _, _, v = self._trace(self.src)
            yinc = self.scale[self.src - 1] / 25.0
            if rigol:
                b = (v + self.offset[self.src - 1]) / yinc + 127
            else:
                b = (v - self.offset[self.src - 1]) / yinc + 128
            self._bin = np.clip(np.round(b), 0, 255).astype(np.uint8).tobytes()
            return None
        return False

    def _rigol(self, u):
        return self._rigol_keysight(u, True)

    def _keysight(self, u):
        return self._rigol_keysight(u, False)

    def _tek(self, u: str):
        m = re.match(r"^SEL(?:ECT)?:CH(\d)(\?|\s+(\S+))$", u)
        if m:
            ch = int(m.group(1))
            if not 1 <= ch <= 4:
                return False
            if m.group(2) == "?":
                return self._out.append("1" if self.on[ch - 1] else "0")
            return self._chan(ch, "on", m.group(3) in ("1", "ON"))
        m = re.match(r"^CH(\d):(SCA|OFFS)\w*(\?|\s+" + _N + ")$", u)
        if m:
            ch, what = int(m.group(1)), "scale" if m.group(2) == "SCA" else "offset"
            if not 1 <= ch <= 4:
                return False
            if m.group(3) == "?":
                v = (self.scale if what == "scale" else self.offset)[ch - 1]
                return self._out.append(f"{v:.6E}")
            return self._chan(ch, what, float(m.group(4)))
        m = re.match(r"^HOR\w*:(SCA|DEL:TIM|RECO)\w*(\?|\s+" + _N + ")$", u)
        if m:
            if m.group(1) == "RECO":
                return self._out.append(str(POINTS["tek"])) if m.group(2) == "?" else None
            attr = "tscale" if m.group(1) == "SCA" else "tpos"
            if m.group(2) == "?":
                return self._out.append(f"{getattr(self, attr):.6E}")
            return setattr(self, attr, float(m.group(3)))
        m = re.match(r"^TRIG:A:EDGE:(SOU|SLO)\w*(\?|\s+(\S+))$", u)
        if m:
            if m.group(1) == "SOU":
                if m.group(2) == "?":
                    return self._out.append(f"CH{self.trig_src}")
                self.trig_src = int(m.group(3)[-1])
            else:
                if m.group(2) == "?":
                    return self._out.append({"POS": "RISE", "NEG": "FALL"}.get(
                        self.trig_slope, self.trig_slope))
                self.trig_slope = {"RIS": "POS", "FALL": "NEG"}.get(m.group(3), m.group(3))
            return None
        m = re.match(r"^TRIG:A:LEV(?::CH\d)?\w*(\?|\s+" + _N + ")$", u)
        if m:
            if m.group(1) == "?":
                return self._out.append(f"{self.trig_level:.6E}")
            self.trig_level = float(m.group(2))
            return None
        m = re.match(r"^TRIG:A:MOD\w*(\?|\s+(\S+))$", u)
        if m:
            if m.group(1) == "?":
                return self._out.append(self.sweep)
            self.sweep = m.group(2)[:4]
            return None
        if u in ("ACQ:STOPA RUNST", "ACQ:STOPA SEQ"):
            self._seq = u.endswith("SEQ")
            return None
        if u == "ACQ:STATE RUN":
            self.running = True
            self.single_at = time.monotonic() if getattr(self, "_seq", False) else None
            self.stepped = False
            return None
        if u == "ACQ:STATE STOP":
            self.running, self.single_at = False, None
            return None
        if u == "ACQ:STATE?":
            return self._out.append("0" if self._state() == "stop" else "1")
        if u == "ALLEV?":
            return self._out.append(self.errors.pop(0) if self.errors else
                                    '0,"No events to report - queue empty"')
        m = re.match(r"^DATA:SOU\w*\s+CH(\d)$", u)
        if m:
            self.src = int(m.group(1))
            return None
        if re.match(r"^DATA:(ENC|WID|STAR|STOP)\w*\s+\S+$", u):
            return None
        ymult = self.scale[self.src - 1] / 25.0
        yoff = -self.offset[self.src - 1] / ymult      # offset: the volts at mid-screen
        t0, dt, v = self._trace(self.src)
        q = {"WFMO:XIN?": dt, "WFMO:XZE?": t0, "WFMO:YMU?": ymult, "WFMO:YOF?": yoff,
             "WFMO:YZE?": 0.0}
        if u in q:
            return self._out.append(f"{q[u]:.6E}")
        if u in ("CURV?", "CURVE?"):
            self._bin = np.clip(np.round(v / ymult + yoff), -128, 127).astype(np.int8).tobytes()
            return None
        return False

    def _siglent(self, u: str):
        m = re.match(r"^C(\d):(TRA|VDIV|OFST)(\?|\s+(\S+))$", u)
        if m:
            ch, k = int(m.group(1)), m.group(2)
            if not 1 <= ch <= 4:
                return False
            what = {"TRA": "on", "VDIV": "scale", "OFST": "offset"}[k]
            if m.group(3) == "?":
                if what == "on":
                    return self._out.append(f"C{ch}:TRA {'ON' if self.on[ch - 1] else 'OFF'}")
                v = (self.scale if what == "scale" else self.offset)[ch - 1]
                return self._out.append(f"C{ch}:{k} {v:.2E}V")
            arg = m.group(4)
            return self._chan(ch, what, arg == "ON" if what == "on" else float(arg.rstrip("V")))
        m = re.match(r"^(TDIV|TRDL)(\?|\s+(\S+))$", u)
        if m:
            attr = "tscale" if m.group(1) == "TDIV" else "tpos"
            if m.group(2) == "?":
                return self._out.append(f"{m.group(1)} {getattr(self, attr):.2E}S")
            return setattr(self, attr, float(m.group(3).rstrip("S")))
        if u == "SARA?":
            return self._out.append(f"SARA {POINTS['siglent'] / (14 * self.tscale):.2E}Sa/s")
        m = re.match(r"^TRSE(\?|\s+EDGE,SR,C(\d).*)$", u)
        if m:
            if m.group(1) == "?":
                return self._out.append(f"TRSE EDGE,SR,C{self.trig_src},HT,OFF")
            self.trig_src = int(m.group(2))
            return None
        m = re.match(r"^C\d:(TRLV|TRSL)(\?|\s+(\S+))$", u)
        if m:
            if m.group(1) == "TRLV":
                if m.group(2) == "?":
                    return self._out.append(f"C{self.trig_src}:TRLV {self.trig_level:.2E}V")
                self.trig_level = float(m.group(3).rstrip("V"))
            else:
                if m.group(2) == "?":
                    return self._out.append(f"C{self.trig_src}:TRSL {self.trig_slope}")
                self.trig_slope = m.group(3)
            return None
        m = re.match(r"^TRMD(\?|\s+(\S+))$", u)
        if m:
            if m.group(1) == "?":
                return self._out.append(f"TRMD {self.sweep}")
            mode = m.group(2)
            if mode == "SINGLE":
                self.single_at = time.monotonic()
                self.stepped = False
                self.running = True
            else:
                self.sweep = mode[:4]
                self.running, self.single_at = True, None
            return None
        if u == "ARM":
            return None
        if u == "STOP":
            self.running, self.single_at = False, None
            return None
        if u == "SAST?":
            s = self._state()
            return self._out.append("SAST " + {"wait": "Ready", "run": "Trig'd",
                                               "stop": "Stop"}[s])
        m = re.match(r"^C(\d):WF\?\s+DAT2$", u)
        if m:
            ch = int(m.group(1))
            _, _, v = self._trace(ch)
            code = (v + self.offset[ch - 1]) * 25.0 / self.scale[ch - 1]
            self._bin = np.clip(np.round(code), -128, 127).astype(np.int8).tobytes()
            return None
        return False
