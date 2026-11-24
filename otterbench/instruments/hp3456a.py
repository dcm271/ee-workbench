"""Driver for the HP 3456A 6½-digit DMM (1980s HP-IB, pre-SCPI program codes).

Codes from the 3456A Operating & Service Manual §III and M. Hepperle's
quick-reference card:
  Functions  S0/S1 shift + F1..F5   Ranges R1 (auto) .. R9
  Trigger    T1 int, T2 ext, T3 single, T4 hold
  Autozero Z0/Z1, Filter FL0/FL1, Math M0 off / M3 null
  Registers  <value>ST<reg>, e.g. 10STI = integrate 10 power-line cycles
  Output     ASCII "+RDDDDDDD+D" (sign, overrange digit, digits with a
             floating decimal point, signed exponent), CR LF terminated.
The instrument has no ID query; status comes from a serial poll.
"""
from __future__ import annotations

import re
import time

from .base import DmmCaps, DmmConfig, DmmReading

FUNCTIONS = {  # label: (code, unit)
    "DCV": ("S0F1", "V"),
    "ACV": ("S0F2", "V"),
    "ACV+DCV": ("S0F3", "V"),
    "2W Ω": ("S0F4", "Ω"),
    "4W Ω": ("S0F5", "Ω"),
    "OC 2W Ω": ("S1F4", "Ω"),
    "OC 4W Ω": ("S1F5", "Ω"),
    "DCV/DCV ratio": ("S1F1", ""),
}

VOLT_RANGES = {"AUTO": "R1", "100 mV": "R2", "1 V": "R3", "10 V": "R4",
               "100 V": "R5", "1000 V": "R6"}
OHM_RANGES = {"AUTO": "R1", "100 Ω": "R2", "1 kΩ": "R3", "10 kΩ": "R4",
              "100 kΩ": "R5", "1 MΩ": "R6", "10 MΩ": "R7", "100 MΩ": "R8",
              "1 GΩ": "R9"}

NPLC_CHOICES = [0.01, 0.1, 1, 10, 100]
LINE_HZ = 60.0

# Serial-poll status byte (octal masks from the manual)
STB_DATA_READY = 0o004
STB_ERROR = 0o020      # illegal instrument state / internal error / syntax error
STB_PROG_ERR = 0o040

_ASCII = re.compile(r"^([+-]?[\d.]+)(?:E?([+-]\d+))?$", re.I)


def ranges_for(function: str) -> dict[str, str]:
    return OHM_RANGES if FUNCTIONS[function][1] == "Ω" else VOLT_RANGES


def parse_reading(s: str) -> float:
    """Parse '+01.234567+0', '+1.234567E+0' or plain '1.23' style output."""
    s = s.strip().split(",")[0]
    m = _ASCII.match(s)
    if not m:
        raise ValueError(f"Unrecognized 3456A reading {s!r}")
    mant = float(m.group(1))
    return mant * 10 ** int(m.group(2)) if m.group(2) else mant


class HP3456A:
    kind = "dmm"
    family = "3456A"

    def __init__(self, transport):
        self.t = transport
        self.caps = DmmCaps()
        self.cfg = DmmConfig()
        self.log = None

    def write(self, cmd: str) -> None:
        if self.log:
            self.log(f"> {cmd}")
        self.t.write(cmd)

    def conversion_s(self) -> float:
        t = self.cfg.nplc / LINE_HZ
        if self.cfg.autozero:
            t *= 2
        if self.cfg.filter:
            t += 0.6  # analog filter settling
        return t + 0.01

    def errors(self) -> list[str]:
        stb = self.t.read_stb()
        out = []
        if stb & STB_ERROR:
            out.append("3456A: illegal instrument state or syntax error "
                       f"(status byte {stb:#o})")
        if stb & STB_PROG_ERR:
            out.append(f"3456A: program memory error (status byte {stb:#o})")
        return out

    def identify(self) -> DmmCaps:
        self.t.clear()           # device clear = front-panel CLEAR
        self.write("H")          # home: known default state
        self.apply(self.cfg)
        try:
            v = self.read_value()
        except Exception as e:
            raise RuntimeError("No reading from the 3456A. Check the address "
                               "(rear-panel switches, factory default 22).") from e
        self.caps = DmmCaps(
            idn="HP 3456A (no ID query, verified by taking a reading)", model="3456A",
            note=f"First reading {v:+.6g}")
        return self.caps

    def apply(self, cfg: DmmConfig):
        code, _ = FUNCTIONS[cfg.function]
        rng = ranges_for(cfg.function).get(cfg.range, "R1")
        nplc = f"{cfg.nplc:g}"
        self.write(" ".join([
            code, rng, "M3" if cfg.null else "M0", "T4",
            "Z1" if cfg.autozero else "Z0", "FL1" if cfg.filter else "FL0",
            f"{nplc}STI",
        ]))
        self.cfg = DmmConfig(**vars(cfg))

    def read_value(self) -> float:
        self.write("T3")  # single trigger
        # Don't sit on the bus while integrating: other instruments share it.
        time.sleep(max(0.0, self.conversion_s() - 0.01))
        with self.t.temp_timeout(max(3.0, self.conversion_s() * 3 + 1)):
            raw = self.t.read()
        return parse_reading(raw)

    def poll(self, t: float, _cfg=None) -> DmmReading:
        v = self.read_value()
        unit = FUNCTIONS[self.cfg.function][1]
        return DmmReading(t=t, value=v, unit=unit, overload=abs(v) >= 9.0e9)
