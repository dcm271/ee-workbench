"""Simulated Raspberry Pi GPIO header (SIM::GPIO).

Wired like a little test jig so sequences can be tried without a Pi:
  GPIO20 (out)  ->  GPIO21 (in)       loop-back: 21 reads what 20 drives
  GPIO16 (out)  ->  GPIO12 (in), inverted (an open-collector "LED on" pin)
  GPIO26        a "button" input that is pressed (low) for 0.2 s every 2 s
Other inputs read their pull (1 with pull-up, else 0).
"""
from __future__ import annotations

import time

from ..instruments.gpio import HEADER, PinConfig

LOOPS = {21: (20, False), 12: (16, True)}


class SimGpio:
    simulated = True

    def __init__(self, clock=time.monotonic):
        self.chip = "/dev/gpiochip0"
        self.clock = clock
        self.cfg: dict[int, PinConfig] = {}
        self.phys: dict[int, int] = {}       # physical level of driven outputs
        self.timeout_s = 2.0
        self.calls: list = []

    def info(self):
        return "pinctrl-rp1", 54

    def configure(self, pin: PinConfig, value: bool = False):
        self.calls.append(("configure", pin.gpio, pin.mode))
        if pin.gpio not in HEADER:
            raise RuntimeError(f"GPIO{pin.gpio} is not on the header")
        self.cfg[pin.gpio] = PinConfig(**vars(pin))
        self.phys.pop(pin.gpio, None)
        if pin.mode == "out":
            self.phys[pin.gpio] = int(bool(value)) ^ int(pin.active_low)

    def set(self, gpio, value):
        p = self.cfg[gpio]
        self.calls.append(("set", gpio, bool(value)))
        self.phys[gpio] = int(bool(value)) ^ int(p.active_low)

    def _physical(self, gpio) -> int:
        if gpio in self.phys:
            return self.phys[gpio]
        if gpio in LOOPS:
            src, inv = LOOPS[gpio]
            if src in self.phys:
                return self.phys[src] ^ int(inv)
        if gpio == 26:
            return 0 if (self.clock() % 2.0) < 0.2 else 1
        p = self.cfg.get(gpio)
        return 1 if p is not None and p.pull == "up" else 0

    def get(self, gpio) -> int:
        p = self.cfg[gpio]
        return self._physical(gpio) ^ int(p.active_low)

    def release(self, gpio):
        self.cfg.pop(gpio, None)
        self.phys.pop(gpio, None)

    def clear(self):
        pass

    def read_stb(self):
        return 0

    def close(self):
        self.cfg.clear()
        self.phys.clear()
