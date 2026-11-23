"""Simulated OWON XDM meter (see instruments/xdm.py)."""
from __future__ import annotations

import random
import re

from ..instruments.xdm import FUNCTIONS, SENSORS
from .sbk393 import _SimSerial, rail

VALUES = {"ACV": 0.1203, "DCI": 0.0123, "ACI": 0.0021, "2W Ω": 998.7, "4W Ω": 998.6,
          "Continuity": 0.4, "Diode": 0.5874, "Capacitance": 1.002e-7, "Frequency": 1000.02,
          "Period": 1e-3, "Temperature": 23.4}
GBK = {"Ω": b"\xa6\xb8", "µ": b"\xa6\xcc", "°C": b"\xa1\xe6"}


def wire(text: str) -> bytes:
    raw = text.encode()
    for a, b in GBK.items():
        raw = raw.replace(a.encode(), b)
    return raw + b"\r\n"


class SimXDM(_SimSerial):
    """acks=True: the firmware that answers every command with OK / nOK."""

    def __init__(self, model: str = "XDM1041", acks: bool = False):
        super().__init__()
        self.model, self.acks = model, acks
        self.function, self.auto, self.n = "DCV", True, 3
        self.rate, self.dual, self.sensor = "S", False, "KITS90"
        self.value = None              # tests: the raw MEAS1? answer
        self.cmds: list[str] = []

    def write(self, data: bytes):
        super().write(data)
        for line in data.decode("ascii").split("\n"):
            if not line.strip():
                continue
            self.cmds.append(line.strip())
            a = self._do(line.strip())
            if a:
                self._out += wire(a)
            elif self.acks and "?" not in line:
                self._out += b"OK\nnOK\nnOK\n"

    def _range_label(self) -> str:
        r = FUNCTIONS[self.function][3]
        return r[self.n - 1] if r else ""

    def _do(self, cmd: str) -> str:
        u = cmd.upper()
        if u == "*IDN?":
            return f"OWON,{self.model},12345678,V4.2.0,3"
        if u in ("FUNC?", "FUNC1?"):
            return f'"{FUNCTIONS[self.function][0]}"'
        if u == "FUNC2?":
            return '"FREQ"' if self.dual else "NONe"
        if u in ("MEAS?", "MEAS1?"):
            return self.value or f"{self._measure():.6E}"
        if u == "MEAS1:SHOW?":
            return f"{self._measure():.1f}°C" if self.function == "Temperature" else ""
        if u == "MEAS2:SHOW?":
            return "50.001Hz" if self.dual else "NONe"
        if u == "RATE?":
            return self.rate
        if u == "AUTO?":
            return "1" if self.auto else "0"
        if u == "RANGE?":
            if self.function == "Temperature":
                return self.sensor
            return self._range_label().replace("kΩ", "KΩ").replace("µA", "uA")
        if u == "AUTO":
            self.auto = True
        elif m := re.match(r"RANGE (\d)$", u):
            if int(m.group(1)) <= len(FUNCTIONS[self.function][3]):
                self.n, self.auto = int(m.group(1)), False
        elif m := re.match(r"RATE ([SMF])$", u):
            self.rate = m.group(1)
        elif u.startswith("FUNC2 "):
            self.dual = "FREQ" in u
        elif u.startswith("CONF:"):
            self._conf(u)
        return ""

    def _conf(self, u: str):
        head, _, arg = u.partition(" ")
        for label, (_, conf, _, ranges) in FUNCTIONS.items():
            if head == conf and (label != "4W Ω" or self.model.startswith("XDM2")):
                self.function, self.dual, self.auto = label, False, True
                self.n = {"DCV": 3, "ACV": 2, "DCI": 5, "ACI": 5}.get(label, 2)
                if arg in SENSORS.values():
                    self.sensor = arg

    def _measure(self) -> float:
        v = VALUES.get(self.function)
        v = rail(self._t0) if v is None else v
        return v * (1 + random.gauss(0, 5e-5))

