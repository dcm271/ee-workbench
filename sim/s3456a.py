"""Simulated HP 3456A: understands the program codes the driver sends and
answers in the instrument's ASCII format ("+RDDDDDDD+D")."""
from __future__ import annotations

import math
import re
import time

import numpy as np

from .base import SimBase

_TOKEN = re.compile(r"\s*(?:(?P<reg>[\d.]+)ST(?P<r>[A-Z])|(?P<code>[A-Z]+)(?P<n>\d*))", re.I)


class Sim3456A(SimBase):
    def __init__(self):
        super().__init__()
        self._rng = np.random.default_rng()
        self._t0 = time.monotonic()
        self._home()

    def _home(self):
        self.shift, self.func, self.nplc, self.null = 0, 1, 10.0, 0.0
        self.math = 0
        self.autozero = True

    def clear(self):
        super().clear()
        self._home()

    def _truth(self) -> float:
        t = time.monotonic() - self._t0
        f = self.func
        if f == 1:
            return 4.99987 + 0.00030 * math.sin(t / 20)   # a slightly drifty 5 V rail
        if f in (2, 3):
            return 0.12034
        return 1000.27 if f == 4 else 999.981

    def _format(self, x: float) -> str:
        sign = "-" if x < 0 else "+"
        x = abs(x)
        exp = 0 if x == 0 else int(math.floor(math.log10(x) / 3) * 3)
        mant = x / 10 ** exp                      # 1 <= mant < 1000
        digits = 7 if self.nplc >= 10 else 6 if self.nplc >= 1 else 5
        decimals = digits - 1 - int(math.log10(max(mant, 1)))
        # overrange digit '0', then the digits with a floating decimal point
        return f"{sign}0{mant:.{decimals}f}{'+' if exp >= 0 else '-'}{abs(exp)}"

    def write(self, cmd: str):
        for m in _TOKEN.finditer(cmd.replace("W", " ")):
            if m.group("reg"):
                if m.group("r").upper() == "I":
                    self.nplc = float(m.group("reg"))
                continue
            code, n = m.group("code").upper(), m.group("n")
            if code == "H":
                self._home()
            elif code == "S":
                self.shift = int(n or 0)
            elif code == "F":
                self.func = int(n)
            elif code == "M":
                self.math = int(n)
                if self.math == 3:
                    self.null = self._truth()
            elif code == "Z":
                self.autozero = n == "1"
            elif code == "T" and n == "3":
                time.sleep(min(self.nplc / 60 * (2 if self.autozero else 1), 0.5))
                noise = 10 ** -(4 + min(self.nplc, 10) ** 0.3)
                x = self._truth() * (1 + self._rng.normal(0, noise))
                if self.math == 3:
                    x -= self.null
                self._out = [self._format(x)]
                self.stb |= 0o004
            elif code in ("R", "T", "FL", "O", "P", "SO", "RS", "D", "SM", "TE"):
                pass
            else:
                self.stb |= 0o120  # syntax error + RQS
