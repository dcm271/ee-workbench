"""Simulated FNIRSI DPS-150 on a 10 Ω load, speaking the binary USB protocol
(see instruments/dps150.py). Like the real one it also pushes output
readings unprompted, so the driver's frame parser sees interleaved traffic."""
from __future__ import annotations

import struct
import time
from contextlib import contextmanager

from ..instruments.dps150 import (ALL_BYTES, ALL_FLOATS, ALL_LEN, DEVICE, HOST, START_BAUD,
                                  START_CONNECT, START_GET, START_SET, Reg)

R_LOAD = 10.0
VIN = 24.0


def dframe(cmd: int, data: bytes) -> bytes:
    return bytes([DEVICE, START_GET, cmd, len(data)]) + data + bytes(
        [(cmd + len(data) + sum(data)) & 0xFF])


class SimDPS150:
    def __init__(self):
        self.timeout_s = 2.0
        self.connected = False
        self.vset, self.iset, self.output = 5.0, 1.0, 0
        self.ovp, self.ocp, self.opp, self.otp, self.lvp = 31.0, 5.2, 155.0, 80.0, 4.5
        self.protection = 0
        self._in = bytearray()
        self._out = bytearray()
        self.sent: list[bytes] = []
        self._t_push = time.monotonic()

    @contextmanager
    def temp_timeout(self, seconds):
        old, self.timeout_s = self.timeout_s, seconds
        try:
            yield
        finally:
            self.timeout_s = old

    # ---- physics ------------------------------------------------------------
    @property
    def upper_v(self) -> float:
        return VIN - 0.8

    def _output(self) -> tuple[float, float, int]:
        """(V, I, mode 0 CC / 1 CV) at the load, tripping protection as needed."""
        if not self.output:
            return 0.0, 0.0, 1
        v = min(self.vset, self.upper_v)
        i = v / R_LOAD
        mode = 1
        if i > self.iset:
            i, v, mode = self.iset, self.iset * R_LOAD, 0
        trip = (1 if v > self.ovp else 2 if i >= self.ocp - 1e-6 else
                3 if v * i > self.opp else 0)
        if trip:
            self.protection, self.output = trip, 0
            return 0.0, 0.0, 1
        return v, i, mode

    def _all(self) -> bytes:
        v, i, mode = self._output()
        b = bytearray(ALL_LEN)
        vals = {"vin": VIN, "vset": self.vset, "iset": self.iset, "vout": v, "iout": i,
                "pout": v * i, "temp": 31.5, "ovp": self.ovp, "ocp": self.ocp,
                "opp": self.opp, "otp": self.otp, "lvp": self.lvp, "capacity": 0.0,
                "energy": 0.0, "upper_v": self.upper_v, "upper_i": 5.1}
        for k, off in ALL_FLOATS.items():
            struct.pack_into("<f", b, off, vals[k])
        for k, x in {"brightness": 10, "volume": 5, "metering": 1, "output": self.output,
                     "protection": self.protection, "mode": mode}.items():
            b[ALL_BYTES[k]] = x
        return bytes(b)

    # ---- host -> device -------------------------------------------------------
    def write(self, data: bytes):
        self.sent.append(bytes(data))
        self._in += data
        while len(self._in) >= 5:
            if self._in[0] != HOST:
                del self._in[0]
                continue
            n = 5 + self._in[3]
            if len(self._in) < n:
                break
            pkt, self._in = bytes(self._in[:n]), self._in[n:]
            if (pkt[2] + pkt[3] + sum(pkt[4:-1])) & 0xFF == pkt[-1]:
                self._exec(pkt[1], pkt[2], pkt[4:-1])

    def _exec(self, start: int, cmd: int, data: bytes):
        if start == START_CONNECT:
            self.connected = data[:1] == b"\x01"
            return
        if not self.connected or start == START_BAUD:
            return
        if start == START_GET:
            if cmd == Reg.ALL:
                self._out += dframe(Reg.ALL, self._all())
            elif cmd in (Reg.MODEL_NAME, Reg.HW_VERSION, Reg.FW_VERSION):
                self._out += dframe(cmd, {Reg.MODEL_NAME: b"DPS-150", Reg.HW_VERSION: b"V1.2",
                                          Reg.FW_VERSION: b"V1.0"}[cmd])
            return
        if start != START_SET:
            return
        if cmd == Reg.OUTPUT:
            self.output = data[0]
            if self.output:
                self.protection = 0
            self._out += dframe(Reg.OUTPUT, data)
            return
        x = struct.unpack("<f", data)[0] if len(data) == 4 else None
        attr = {Reg.VOLTAGE_SET: "vset", Reg.CURRENT_SET: "iset", Reg.OVP: "ovp",
                Reg.OCP: "ocp", Reg.OPP: "opp", Reg.OTP: "otp", Reg.LVP: "lvp"}.get(cmd)
        if attr and x is not None:
            setattr(self, attr, x)

    # ---- device -> host ---------------------------------------------------------
    def read_available(self) -> bytes:
        now = time.monotonic()
        if self.connected and now - self._t_push > 0.2:
            self._t_push = now
            v, i, _ = self._output()
            self._out += dframe(Reg.OUTPUT_VIP, struct.pack("<fff", v, i, v * i))
            self._out += dframe(Reg.INPUT_VOLTAGE, struct.pack("<f", VIN))
        out, self._out = bytes(self._out), bytearray()
        return out

    def reset_input(self):
        self._out.clear()

    def clear(self):
        self.reset_input()

    def read_stb(self) -> int:
        return 0

    def close(self):
        self.connected = False
