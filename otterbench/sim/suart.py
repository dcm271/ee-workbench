"""A simulated DUT console (SIM::UART): prints telemetry twice a second and
answers a few commands (help, ver, led on / led off, adc, slow, crash)."""
from __future__ import annotations

import math
import random
import time
from contextlib import contextmanager


class SimUart:
    resource = "simulated"

    def __init__(self):
        self.timeout_s = 2.0
        self.t0 = time.monotonic()
        self._out = bytearray(b"\r\nboot: demo firmware 1.2.3\r\n")
        self._next = self.t0 + 0.5
        self._later: list[tuple[float, bytes]] = []
        self._in = ""
        self.baud = 115200
        self.lines = {"dtr": True, "rts": True}
        self.sent: list[bytes] = []
        self.led = False

    @contextmanager
    def temp_timeout(self, seconds):
        yield

    def set_baud(self, b: int):
        self.baud = b

    def set_line(self, name: str, on: bool):
        if name == "dtr" and on and not self.lines["dtr"]:       # reset released
            self._out += b"\r\nboot: demo firmware 1.2.3\r\n"
            self.led = False
        self.lines[name] = on

    def write(self, data: bytes):
        self.sent.append(bytes(data))
        self._in += data.decode("utf-8", "replace")
        while "\n" in self._in or "\r" in self._in:
            k = min(i for i in (self._in.find("\n"), self._in.find("\r")) if i >= 0)
            line, self._in = self._in[:k].strip(), self._in[k + 1:]
            if line:
                self._cmd(line)

    def _cmd(self, c: str):
        def say(s, delay=0.0):
            self._later.append((time.monotonic() + delay, s.encode() + b"\r\n"))
        if c == "help":
            say("commands: help ver led adc slow crash")
        elif c == "ver":
            say("version 1.2.3")
        elif c in ("led on", "led off"):
            self.led = c.endswith("on")
            say("OK")
        elif c == "adc":
            say(f"adc={2048 + random.randint(-3, 3)}")
        elif c == "slow":
            say("done", 0.4)
        elif c == "crash":
            say("PANIC: store prohibited at 0x400d1234")
        else:
            say(f"ERR unknown command '{c}'")

    def read_available(self) -> bytes:
        n = time.monotonic()
        while n >= self._next:
            t = self._next - self.t0
            self._out += (f"temp={24 + 2 * math.sin(t / 10):.2f} vbat={3.7 - t / 36000:.3f} "
                          f"led={int(self.led)}\r\n").encode()
            self._next += 0.5
        for due, data in [x for x in self._later if x[0] <= n]:
            self._out += data
            self._later.remove((due, data))
        out, self._out = bytes(self._out), bytearray()
        return out

    def reset_input(self):
        self._out.clear()

    def clear(self):
        self.reset_input()

    def read_stb(self):
        return 0

    def close(self):
        pass
