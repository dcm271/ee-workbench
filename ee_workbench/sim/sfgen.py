"""Simulated two-channel SCPI function generator (SIM::FGEN), Keysight 33522B-like;
SIM::SDG simulates a Siglent SDG2042X (BSWV commands)."""
from __future__ import annotations

import re

from .base import SimBase

SCPI = {"SIN": "SIN", "SQU": "SQU", "RAMP": "RAMP", "PULS": "PULS", "NOIS": "NOIS", "DC": "DC"}


class _Ch:
    def __init__(self):
        self.func, self.freq, self.volt, self.offs, self.dcyc, self.out = \
            "SIN", 1000.0, 0.1, 0.0, 50.0, False


class SimFgen(SimBase):
    def __init__(self, siglent: bool = False):
        super().__init__()
        self.siglent = siglent
        self.ch = {1: _Ch(), 2: _Ch()}
        self.err: list[str] = []
        self.log: list[str] = []

    def clear(self):
        self._out.clear()

    def write(self, cmd: str):
        for part in re.split(r"[;\n]+", cmd):
            part = part.strip()
            if part:
                self.log.append(part)
                (self._siglent if self.siglent else self._scpi)(part)

    def _scpi(self, c: str):
        u = c.upper()
        if u == "*IDN?":
            self._out.append("Keysight Technologies,33522B,MY00000000,5.03-3.15-2.00-58-00")
            return
        if u == "SYST:ERR?":
            self._out.append(self.err.pop(0) if self.err else '+0,"No error"')
            return
        m = re.fullmatch(r"OUTP(\d)\??(?:\s+(ON|OFF|1|0))?", u)
        if m:
            ch = self.ch.get(int(m.group(1)))
            if ch is None:
                self.err.append('-114,"Header suffix out of range"')
                return
            if u.endswith("?"):
                self._out.append("1" if ch.out else "0")
            else:
                ch.out = m.group(2) in ("ON", "1")
            return
        m = re.fullmatch(r"SOUR(\d):([A-Z:]+)(\?)?(?:\s+(\S+))?", u)
        if not m:
            self.err.append('-113,"Undefined header"')
            return
        ch = self.ch.get(int(m.group(1)))
        if ch is None:
            self.err.append('-114,"Header suffix out of range"')
            return
        key, q, arg = m.group(2), m.group(3), m.group(4)
        attr = {"FUNC": "func", "FREQ": "freq", "VOLT": "volt", "VOLT:OFFS": "offs",
                "FUNC:SQU:DCYC": "dcyc"}.get(key)
        if attr is None:
            self.err.append('-113,"Undefined header"')
            return
        if q:
            v = getattr(ch, attr)
            self._out.append(v if isinstance(v, str) else f"{v:+.15E}")
        elif attr == "func":
            ch.func = SCPI.get(arg, ch.func)
        else:
            setattr(ch, attr, float(arg))

    def _siglent(self, c: str):
        u = c.upper()
        if u == "*IDN?":
            self._out.append("Siglent Technologies,SDG2042X,SDG2XCAD1R0000,2.01.01.35R3B2")
            return
        m = re.fullmatch(r"C(\d):BSWV\?", u)
        if m:
            ch = self.ch[int(m.group(1))]
            wv = {"SIN": "SINE", "SQU": "SQUARE", "RAMP": "RAMP", "PULS": "PULSE",
                  "NOIS": "NOISE", "DC": "DC"}[ch.func]
            self._out.append(f"C{m.group(1)}:BSWV WVTP,{wv},FRQ,{ch.freq:g}HZ,PERI,"
                             f"{1 / ch.freq:g}S,AMP,{ch.volt:g}V,AMPVRMS,{ch.volt / 2.828:g}Vrms,"
                             f"OFST,{ch.offs:g}V,HLEV,{ch.offs + ch.volt / 2:g}V,LLEV,"
                             f"{ch.offs - ch.volt / 2:g}V,PHSE,0,DUTY,{ch.dcyc:g}")
            return
        m = re.fullmatch(r"C(\d):BSWV\s+(.+)", u)
        if m:
            ch = self.ch[int(m.group(1))]
            items = m.group(2).split(",")
            for k, v in zip(items[0::2], items[1::2]):
                if k == "WVTP":
                    ch.func = {"SINE": "SIN", "SQUARE": "SQU", "RAMP": "RAMP", "PULSE": "PULS",
                               "NOISE": "NOIS", "DC": "DC"}[v]
                else:
                    a = {"FRQ": "freq", "AMP": "volt", "OFST": "offs", "DUTY": "dcyc"}[k]
                    setattr(ch, a, float(re.match(r"[-+\d.eE]+", v).group(0)))
            return
        m = re.fullmatch(r"C(\d):OUTP(\?|\s+(ON|OFF))", u)
        if m:
            ch = self.ch[int(m.group(1))]
            if m.group(2) == "?":
                self._out.append(f"C{m.group(1)}:OUTP {'ON' if ch.out else 'OFF'},LOAD,HZ,"
                                 "PLRT,NOR")
            else:
                ch.out = m.group(3) == "ON"
