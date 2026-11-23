"""Simulated B&K 393 (streams frames) and 393B (answers APPA requests), both
measuring a slightly drifting 5 V rail. See instruments/bk393.py."""
from __future__ import annotations

import math
import time
from contextlib import contextmanager

from ..instruments.bk393 import (CMD_DISPLAY, CMD_INFO, LEN_LEGACY, S_BIT16, S_NEGATIVE,
                                 S_OVERLOAD, appa_frame, parse_appa)


def frame393(func: int, rng: int, count: int, negative=False, overload=False) -> bytes:
    digits = f"{count & 0xFFFF:04X}"[::-1].encode()
    status = (S_BIT16 if count > 0xFFFF else 0) | (S_NEGATIVE if negative else 0) \
        | (S_OVERLOAD if overload else 0)
    return bytes([0xF0, 0x11, func, rng]) + digits + bytes([status, 0x05])


def rail(t0: float) -> float:
    return 4.9987 + 0.0006 * math.sin((time.monotonic() - t0) / 15)


class _SimSerial:
    def __init__(self):
        self.timeout_s = 2.0
        self._out = bytearray()
        self._t0 = time.monotonic()
        self.sent: list[bytes] = []

    @contextmanager
    def temp_timeout(self, seconds):
        old, self.timeout_s = self.timeout_s, seconds
        try:
            yield
        finally:
            self.timeout_s = old

    def write(self, data: bytes):
        self.sent.append(bytes(data))

    def read_available(self) -> bytes:
        out, self._out = bytes(self._out), bytearray()
        return out

    def reset_input(self):
        self._out.clear()

    def clear(self):
        self.reset_input()

    def read_stb(self) -> int:
        return 0

    def close(self):
        pass


class SimBK393(_SimSerial):
    """DC V, 6 V range, 0.1 mV resolution; a few frames a second."""
    PERIOD = 0.25

    def __init__(self):
        super().__init__()
        self._next = time.monotonic()

    def read_available(self) -> bytes:
        now = time.monotonic()
        if now >= self._next:
            self._next = now + self.PERIOD
            self._out += frame393(0x02, 0, round(rail(self._t0) * 1e4))
        return super().read_available()


def line(raw: int, decimals: int, unit: int, content=0, overload=False) -> bytes:
    """Five bytes of the current protocol's display data."""
    return raw.to_bytes(3, "little", signed=True) + bytes(
        [unit << 3 | decimals, (0x80 if overload else 0) | content])


class SimBK393B(_SimSerial):
    """DC V, 10 V range, 0.1 mV resolution, frequency blank on the second line."""

    def __init__(self, legacy: bool = False):
        super().__init__()
        self.legacy = legacy
        self.display: bytes | None = None       # tests put the 12 bytes here

    def write(self, data: bytes):
        super().write(data)
        for cmd, _ in parse_appa(bytearray(data)):
            count = round(rail(self._t0) * 1e4)
            if self.legacy:
                if cmd == CMD_INFO:
                    self._out += appa_frame(cmd, self._all(count))
            elif cmd == CMD_INFO:
                self._out += appa_frame(cmd, b"393B".ljust(32) + b"SIM00001".ljust(16)
                                        + (0x16).to_bytes(2, "little")
                                        + (102).to_bytes(2, "little"))
            elif cmd == CMD_DISPLAY:
                self._out += appa_frame(cmd, self.display or bytes([0x02, 0x80 | 0x01])
                                        + line(count, 4, 1) + line(0x700000, 0, 0))

    @staticmethod
    def _all(count: int) -> bytes:
        b = bytearray(LEN_LEGACY)
        b[0:10] = b"393B".ljust(10)
        b[10:18] = b"SIM00001"
        b[20:22] = b"\x01\x01"                   # DC V
        b[37:40] = count.to_bytes(3, "big", signed=True)
        b[40] = 1 << 3 | 4
        b[41] = 0x01
        b[46] = 0x80                             # second line blank
        return bytes(b)
