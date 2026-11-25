"""Simulated instruments behind the adapters of instruments/adapters.py. They
work on the bytes of the real protocols (frames, CRCs, status bits), so the
adapters' encoding and decoding is exercised for real.

Supplies drive a 50 Ω load, the PX-100 load is on a 12.6 V battery."""
from __future__ import annotations

import math
import random
import struct
import time

from ..instruments.adapters import SPI, crc16


class _Link:
    def __init__(self):
        self._out = bytearray()
        self.sent: list[bytes] = []
        self.t0 = time.monotonic()

    def write(self, data: bytes):
        self.sent.append(bytes(data))
        self.on_bytes(bytes(data))

    def read_available(self) -> bytes:
        out, self._out = bytes(self._out), bytearray()
        return out

    def reset_input(self):
        self._out.clear()

    def close(self):
        pass

    def on_bytes(self, data: bytes):        # pragma: no cover - abstract
        raise NotImplementedError


class SimRiden(_Link):
    LOAD = 50.0

    def __init__(self, model_id: int = 60062):
        super().__init__()
        from ..instruments.adapters import RidenScale
        self.s = RidenScale(model_id)
        self.r = [0] * 120
        self.r[0:4] = [model_id, 0, 12345, 128]
        self.r[5] = 31
        self.r[8], self.r[9] = 5 * self.s.v, 1 * self.s.i
        self.r[14] = 6500
        self.r[35] = 25
        self.r[82], self.r[83] = round(62 * self.s.v), round(6.2 * self.s.i)
        self.r[82] = min(self.r[82], 0xFFFF)

    def _iscale(self):
        return self.s.low_range_i if self.s.low_range_i and self.r[20] == 0 else self.s.i

    def _run(self):
        r, s = self.r, self.s
        vset, iset = r[8] / s.v, r[9] / self._iscale()
        i = min(vset / self.LOAD, iset) if r[18] else 0.0
        v = i * self.LOAD
        if r[18] and v > r[82] / s.v:
            r[16], r[18], v, i = 1, 0, 0.0, 0.0
        r[10], r[11], r[13] = round(v * s.v), round(i * self._iscale()), round(v * i * s.p)
        r[17] = int(bool(r[18]) and vset / self.LOAD > iset)

    def on_bytes(self, f):
        if len(f) < 8 or crc16(f) != 0 or f[0] != 1:
            return
        self._run()
        if f[1] == 3:
            start, n = struct.unpack(">HH", f[2:6])
            if start + n > len(self.r):
                body = bytes([1, 0x83, 2])
            else:
                body = bytes([1, 3, 2 * n]) + struct.pack(f">{n}H", *self.r[start:start + n])
        elif f[1] == 6:
            reg, val = struct.unpack(">HH", f[2:6])
            self.r[reg] = val
            if reg == 18 and val:
                self.r[16] = 0
            body = f[:6]
        else:
            body = bytes([1, f[1] | 0x80, 1])
        self._out += body + struct.pack("<H", crc16(body))


class SimKorad(_Link):
    LOAD = 50.0

    def __init__(self, model: str = "KA3005P"):
        super().__init__()
        self.model = model
        self.v, self.i = 5.0, 1.0
        self.out = self.ovp = self.ocp = False

    def on_bytes(self, data):
        c = data.decode()
        iout = min(self.v / self.LOAD, self.i) if self.out else 0.0
        vout = iout * self.LOAD
        cv = not (self.out and self.v / self.LOAD > self.i)
        if c == "*IDN?":
            self._out += f"KORAD {self.model} V5.5 SN:03379314".encode()
        elif c == "VSET1?":
            self._out += b"%05.2f" % self.v
        elif c == "ISET1?":
            self._out += b"%05.3fK" % self.i         # the real one's stray sixth byte
        elif c == "VOUT1?":
            self._out += b"%05.2f" % vout
        elif c == "IOUT1?":
            self._out += b"%05.3f" % iout
        elif c == "STATUS?":
            self._out.append(int(cv) | 2 | 16 | self.ocp << 5 | self.out << 6 | self.ovp << 7)
        elif c.startswith("VSET1:"):
            self.v = float(c[6:])
        elif c.startswith("ISET1:"):
            self.i = float(c[6:])
        elif c[:3] in ("OUT", "OVP", "OCP") and c[3:] in ("0", "1"):
            setattr(self, c[:3].lower(), c[3:] == "1")


class SimPx100(_Link):
    def __init__(self):
        super().__init__()
        self.on, self.i, self.cut, self.timer = False, 0.5, 3.0, 0
        self.mah = self.mwh = 0.0
        self.t_on = 0.0
        self._last = time.monotonic()

    def _vi(self):
        now = time.monotonic()
        i = self.i if self.on else 0.0
        v = 12.6 - 0.1 * i
        self.mah += i * (now - self._last) / 3.6
        self.mwh += v * i * (now - self._last) / 3.6
        if self.on:
            self.t_on += now - self._last
        self._last = now
        return v, i

    def on_bytes(self, f):
        if len(f) != 6 or f[:2] != b"\xb1\xb2" or f[5] != 0xB6:
            return
        c, d1, d2 = f[2], f[3], f[4]
        v, i = self._vi()
        if c < 0x10:
            if c == 1:
                self.on = bool(d1)
            elif c == 2:
                self.i = d1 + d2 / 100
            elif c == 3:
                self.cut = d1 + d2 / 100
            elif c == 4:
                self.timer = d1 << 8 | d2
            elif c == 5:
                self.mah = self.mwh = self.t_on = 0.0
            self._out += b"\x6f"
            return

        def hms(s):
            s = int(s)
            return (s // 3600 << 16) | (s // 60 % 60 << 8) | s % 60
        x = {0x10: int(self.on), 0x11: round(v * 1000), 0x12: round(i * 1000),
             0x13: hms(self.t_on), 0x14: round(self.mah), 0x15: round(self.mwh), 0x16: 28,
             0x17: round(self.i * 100), 0x18: round(self.cut * 100),
             0x19: hms(self.timer)}.get(c)
        if x is not None:
            self._out += b"\xca\xcb" + x.to_bytes(3, "big") + b"\xce\xcf"


class SimCenter(_Link):
    """T1 room, T2 a heat sink warming up, T3 and T4 unplugged."""
    celsius = True

    def temps(self):
        t = time.monotonic() - self.t0
        return [23.4 + 0.05 * math.sin(t / 7), 60 - 36 * math.exp(-t / 120)]

    def on_bytes(self, data):
        if data != b"A":
            return
        p = bytearray(45)
        p[0], p[44] = 2, 3
        p[1] = 0x80 if self.celsius else 0
        for i, c in enumerate(self.temps()):
            v = c if self.celsius else c * 1.8 + 32
            struct.pack_into(">h", p, 7 + 2 * i, round(v * 10))
        p[39] = 0b1100
        self._out += p


class SimW1:
    def __init__(self, names):
        self.names, self.t0 = list(names), time.monotonic()

    def sensors(self):
        return list(self.names)

    def read(self, name):
        k = self.names.index(name)
        t = 22.5 + 4 * k + 0.3 * math.sin((time.monotonic() - self.t0) / 20 + k)
        return round(t * 16) / 16            # DS18B20: 1/16 °C steps

    def close(self):
        pass


class SimSpi:
    """One MAX31855 (type K) or MAX31865 (Pt100, 430 Ω reference) per path."""

    def __init__(self, chip: str, paths):
        self.chip, self.paths = chip, list(paths)
        self.tc, self.cold, self.ohms, self.fault = 125.25, 24.5, 138.51, 0
        self.reg0 = 0

    def xfer(self, k, tx):
        if self.chip == "max31855":
            v = (round(self.tc * 4) & 0x3FFF) << 18 | (round(self.cold * 16) & 0xFFF) << 4
            if self.fault:
                v |= 0x10000 | self.fault
            return v.to_bytes(4, "big")
        if tx[0] == 0x80:
            self.reg0 = tx[1]
            if tx[1] & 2:
                self.fault = 0
            return bytes(2)
        adc = round((self.ohms + random.gauss(0, 0.002)) / 430 * 32768)
        code = adc << 1 | int(bool(self.fault)) if self.reg0 & 0xC0 == 0xC0 else 0
        return bytes([0, code >> 8, code & 0xFF])

    def close(self):
        pass


class SimAck(_Link):
    """A SCPI simulator that also answers the commands that aren't queries."""

    def __init__(self, d):
        super().__init__()
        from .sscpi import SimScpi
        self.s = SimScpi(d)

    def on_bytes(self, data):
        for line in data.decode().split("\r\n"):
            if not line:
                continue
            self.s.errors.clear()
            self.s._out.clear()
            self.s.write(line)
            if self.s.errors:
                self._out += b"cmd err\r\n"
            elif "?" in line:
                self._out += self.s.read().encode() + b"\r\n"
            else:
                self._out += b"exec success\r\n"


class SimSpm(_Link):
    """OWON SPM / Kiprim Pro: a supply on 50 ohm, and a multimeter."""
    LOAD = 50.0
    DMM = {"VOLT:DC": "+4.9987V", "VOLT:AC": "+0.0012V", "CURR:DC": "+12.34mA",
           "CURR:AC": "+0.12uA", "RES": "+10.021kOhm", "CAP": "101.2nF", "DIOD": "+0.6123V",
           "CONT": "OL"}

    def __init__(self, idn="KIPRIM,DC310PRO,25010001,FV:V2.1.0", vmax=32.0, imax=10.2):
        super().__init__()
        self.idn, self.vmax, self.imax = idn, vmax, imax
        self.v, self.i, self.ovp, self.ocp, self.out = 5.0, 1.0, vmax, imax, False
        self.func, self.trip = "VOLT:DC", ""
        self.drop = 0            # ignore this many lines
        self.old = False         # firmware without the combined queries
        self.binary_outp = False

    def _num(self, text, hi):
        x = float(text)
        if not 0 <= x <= hi:
            raise ValueError
        return x

    def _line(self, c):
        i = min(self.v / self.LOAD, self.i) if self.out else 0.0
        v = i * self.LOAD
        if self.out and v > self.ovp:
            self.out, self.trip, v, i = False, "OVP", 0.0, 0.0
        cc = self.out and self.v / self.LOAD > self.i
        mode = 3 if self.trip else 0 if not self.out else 2 if cc else 1
        trips = ",".join("ON" if self.trip == t else "OFF" for t in ("OVP", "OCP", "OTP"))
        head, _, arg = c.partition(" ")
        q = {"*IDN?": self.idn, "VOLT?": f"{self.v:.3f}", "CURR?": f"{self.i:.3f}",
             "VOLT:LIM?": f"{self.ovp:.3f}", "CURR:LIM?": f"{self.ocp:.3f}",
             "OUTP?": ("01"[self.out] if self.binary_outp else "ON" if self.out else "OFF"),
             "MEAS:VOLT?": f"{v:.3f}", "MEAS:CURR?": f"{i:.3f}", "FUNC?": self.func,
             "CONF?": f"{self.func} +4.9987E+00"}
        if not self.old:
            q["MEAS:ALL:INFO?"] = f"{v:.3f},{i:.3f},{v * i:.3f},{trips},{mode}"
            q["CONF:ALL?"] = f"{self.func},{self.DMM[self.func]},AUTO,20V"
        if c in q:
            return q[c]
        if head == "VOLT":
            self.v = self._num(arg, self.vmax)
        elif head == "CURR":
            self.i = self._num(arg, self.imax)
        elif head == "VOLT:LIM":
            self.ovp = self._num(arg, self.vmax)
        elif head == "CURR:LIM":
            self.ocp = self._num(arg, self.imax)
        elif c in ("OUTP ON", "OUTP OFF"):
            self.out, self.trip = c == "OUTP ON", ""
        elif head.startswith("FUNC:") and head[5:] in self.DMM:
            self.func = head[5:]
        else:
            raise ValueError
        return None

    def on_bytes(self, data):
        for c in data.decode().split("\n"):
            if not c:
                continue
            if self.drop:
                self.drop -= 1
                continue
            try:
                r = self._line(c)
            except ValueError:
                r = "ERR"
            if r is not None:
                self._out += r.encode() + b"\n"


def sim_link(d):
    p = d.protocol
    if p == "riden":
        return SimRiden()
    if p == "korad":
        return SimKorad(d.id)
    if p == "px100":
        return SimPx100()
    if p == "center":
        return SimCenter()
    if p == "w1":
        return SimW1(d.probes)
    if p in SPI:
        return SimSpi(p, d.probes)
    if p == "scpi-ack":
        return SimAck(d)
    if p == "owon-spm":
        return SimSpm() if d.id == "DC310PRO" else \
            SimSpm("KIPRIM,DC605PRO,25010001,FV:V2.1.0", 62.0, 5.1)
    raise ValueError(f"no simulator for {p}")
