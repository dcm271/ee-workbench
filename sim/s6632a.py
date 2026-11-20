"""Simulated HP 6632A driving a 10 Ω load (manual Table 6-1 command set)."""
from __future__ import annotations

import re
import time

import numpy as np

from .base import SimBase

V_MAX, I_MIN, I_MAX, OVP_MAX = 20.475, 0.02, 5.1188, 22.0
R_LOAD = 10.0


class Sim6632A(SimBase):
    def __init__(self):
        super().__init__()
        self._rng = np.random.default_rng()
        self._clr()

    def _clr(self):
        self.vset, self.iset, self.out = 0.0, I_MIN, True
        self.ovp, self.ocp = OVP_MAX, False
        self.tripped = 0
        self.err = 0

    def _state(self):
        if not self.out or self.tripped:
            return 0.0, 0.0, 0
        i = self.vset / R_LOAD
        if i > self.iset:
            if self.ocp:
                self.tripped |= 64
                return 0.0, 0.0, 0
            return self.iset * R_LOAD, self.iset, 2
        return self.vset, i, 1

    def write(self, cmd: str):
        for part in re.split(r"[;\r\n]+", cmd):
            part = part.strip().upper()
            if part:
                self._exec(part)

    def _exec(self, part: str):
        m = re.match(r"([A-Z]+\??)\s*(.*)$", part)
        if not m:
            self.err = 10
            return
        hdr, arg = m.groups()
        if hdr in ("VOUT?", "IOUT?"):
            time.sleep(0.03)  # readback conversion time (approximate)

        def num(lo, hi, code):
            try:
                x = float(arg)
            except ValueError:
                self.err = 20
                return None
            if not lo <= x <= hi:
                self.err = code
                return None
            return x

        if hdr == "VSET":
            x = num(0, V_MAX, 42)
            self.vset = self.vset if x is None else x
        elif hdr == "ISET":
            x = num(0, I_MAX, 43)
            self.iset = self.iset if x is None else max(x, I_MIN)
        elif hdr == "OVSET":
            x = num(0, OVP_MAX, 44)
            self.ovp = self.ovp if x is None else x
            if self.out and self.vset > self.ovp:
                self.tripped |= 8
        elif hdr == "OUT":
            self.out = arg.strip() in ("1", "ON")
        elif hdr == "OCP":
            self.ocp = arg.strip() in ("1", "ON")
        elif hdr == "RST":
            self.tripped = 0
        elif hdr == "CLR":
            self._clr()
        elif hdr == "VOUT?":
            v, _, _ = self._state()
            self._out.append(f"{v + self._rng.normal(0, 0.002):7.3f}")
        elif hdr == "IOUT?":
            _, i, _ = self._state()
            self._out.append(f"{i + self._rng.normal(0, 0.0005):7.4f}")
        elif hdr == "STS?":
            _, _, mode = self._state()
            self._out.append(f"{mode | self.tripped | (128 if self.err else 0) | 2048:5d}")
        elif hdr == "ERR?":
            self._out.append(f"{self.err:5d}")
            self.err = 0
        elif hdr == "ID?":
            self._out.append("HP6632A")
        else:
            self.err = 11
