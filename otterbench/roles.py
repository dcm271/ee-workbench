"""Supplies and electronic loads by what they do, whatever the model: the
regulator and battery tests ask for "a source" and "a load".

A Port is one output or input of an open tab. Written out it is the tab's name,
with "output 2" behind it on a multi-output supply.

Sources: the built-in DC supplies, and SCPI definitions with vset / iset / out.
Loads:   definitions with cc / input (CC mode is selected when there is a mode),
         and the two-quadrant 663xx (they sink), see SinkPort.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .sequencer import Bench, SequenceError

_CHANNELS = ("", "1", "2", "3", "4")


@dataclass
class Found:
    text: str
    name: str
    ch: str
    role: str                    # source | load | sink


def ports(insts) -> list[Found]:
    """Every port of the open instruments."""
    out = []
    for i in insts:
        if i.kind == "supply":
            out.append(Found(i.name, i.name, "", "source"))
            if "output2" in (i.set_names or []):
                out.append(Found(f"{i.name} output 2", i.name, "2", "source"))
            if two_quadrant(i):
                out.append(Found(i.name, i.name, "", "sink"))
        elif i.kind == "scpi":
            sets, reads = set(i.set_names or []), set(i.measure_names or [])
            for c in _CHANNELS:
                if not {"voltage" + c, "current" + c} <= reads:
                    continue
                text = f"{i.name} output {c}" if c else i.name
                if {"vset" + c, "iset" + c, "out" + c} <= sets:
                    out.append(Found(text, i.name, c, "source"))
                elif {"cc" + c, "input" + c} <= sets:
                    out.append(Found(text, i.name, c, "load"))
    return out


def two_quadrant(inst) -> bool:
    caps = getattr(inst, "caps", None) or getattr(getattr(inst, "p", None), "caps", None)
    return bool(getattr(caps, "has_resistance", False))


def find(bench: Bench, text: str, *roles: str) -> "Port":
    hit = next((f for f in ports(bench._insts) if f.text == text and f.role in roles), None)
    if hit is None:
        what = "power supply" if "source" in roles else "electronic load"
        raise SequenceError(f"'{text}' isn't an open {what}" if text else f"Choose a {what}")
    return {"source": Source, "load": Load, "sink": SinkPort}[hit.role](bench, hit)


class Port:
    def __init__(self, bench: Bench, f: Found):
        self.bench, self.name, self.ch, self.text = bench, f.name, f.ch, f.text
        self.inst = bench.inst(f.name)

    def read(self, seconds: float = 0.0) -> tuple[float, float]:
        """(volts, amps) from one reading taken after now, or their means."""
        b = self.bench
        b.check()
        t0 = t = b._clock()
        vs, cs = [], []
        while True:
            r = self.inst.reading_after(t, timeout=max(10.0, seconds + 10.0))
            if r is None:
                b.check()
                raise SequenceError(f"No reading from {self.name} (is it polling?)")
            vs.append(float(b._pick(self.inst, r, "voltage" + self.ch)))
            cs.append(float(b._pick(self.inst, r, "current" + self.ch)))
            t = r.t
            if t >= t0 + seconds:
                return sum(vs) / len(vs), sum(cs) / len(cs)

    def _set(self, what: str, value):
        self.bench.set(self.name, what + self.ch, value)


class Source(Port):
    def __init__(self, bench, f):
        super().__init__(bench, f)
        self._n = {"v": "voltage", "i": "current", "on": "output"} \
            if self.inst.kind == "supply" else {"v": "vset", "i": "iset", "on": "out"}

    def volts(self, v: float):
        self._set(self._n["v"], v)

    def amps(self, a: float):
        self._set(self._n["i"], a)

    def on(self):
        self._set(self._n["on"], 1)

    def off(self):
        self._set(self._n["on"], 0)


class Load(Port):
    """An electronic load in constant current."""
    regulates = True

    def prepare(self, cutoff: float | None = None):
        self.off()
        names = self.inst.set_names or []
        if "function" + self.ch in names:
            self._set("function", self._cc_mode())
        if cutoff is not None and "cutoff" + self.ch in names:
            # the load's own cut-off, in case the computer stops
            self._set("cutoff", cutoff)
            return True
        return False

    def _cc_mode(self) -> str:
        d = self.inst.run(lambda drv: drv.d)
        s = d.setting("function" + self.ch) or d.setting("function")
        hit = next((o.label for o in s.options
                    if re.match(r"^(cc|curr)", o.label, re.I)), None)
        if hit is None:
            raise SequenceError(f"{self.name} has no constant-current mode")
        return hit

    def amps(self, a: float):
        self._set("cc", a)

    def on(self):
        self._set("input", 1)

    def off(self):
        self._set("input", 0)

    def tick(self, i: float):
        pass

    def read(self, seconds: float = 0.0):
        v, i = super().read(seconds)
        return v, abs(i)             # some loads show the current they sink as negative


class SinkPort(Port):
    """A 663xx sinking current. Its sink current is not programmable ("sink
    current does not track the programmed current": 2.8 A at 0 V falling to
    1.2 A at 15 V on the 66321B/D), so the output is a voltage source behind its
    programmable resistance, and tick() trims the voltage towards the wanted
    current after every reading."""
    regulates = False
    OHMS = 0.5

    def __init__(self, bench, f):
        super().__init__(bench, f)
        self.want = 0.0
        self.vset = 0.0
        self.v = 0.0

    @staticmethod
    def limit(v: float) -> float:
        """Most it can sink at v, with 10 % in hand."""
        return 0.9 * max(0.0, 2.8 - 1.6 * v / 15.0)

    def prepare(self, cutoff=None):
        self.off()
        return False

    def amps(self, a: float):
        self.want = a

    def on(self):
        v, _ = self.read()
        if self.want > self.limit(v):
            raise SequenceError(f"{self.name} can sink about {self.limit(v):.2f} A at {v:.2f} V, "
                                f"not {self.want:g} A")
        self.vset = max(0.0, v)         # the battery's own voltage: next to no current flows
        self.inst.run(lambda d: d.set_resistance(self.OHMS))
        self._set("current", 0.1)       # nothing is sourced if the battery is lower
        self._set("voltage", self.vset)
        self._set("output", 1)

    def tick(self, i: float):
        err = self.want - i
        step = max(-0.1, min(0.1, 0.6 * err * self.OHMS))
        if abs(step) > 2e-4:
            self.vset = max(0.0, self.vset - step)
            self._set("voltage", self.vset)

    def off(self):
        self._set("output", 0)
        self.inst.run(lambda d: d.set_resistance(0.0))

    def read(self, seconds: float = 0.0):
        v, i = super().read(seconds)
        self.v = v
        return v, -i                 # sinking is negative at its terminals
