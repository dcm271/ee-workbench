"""A DUT's serial console: what its firmware prints, on the bench's time axis.

Resource: UART::/dev/ttyUSB0@115200 (8N1). Received text is split into lines,
each stamped with app time (the port is read every 20 ms, so to about that).

Patterns turn lines into numbers: name `vbat`, regex `vbat=([\\d.]+)` makes a
signal `vbat` for the trend, the watchdog, math and Measure. A pattern without
a group is 1 whenever it matches (`panic` <- `PANIC|Guru Meditation`).

For the sequencer: send() marks the place in the incoming text, and match()
looks for a regex from there on, so an answer that arrives before the "Wait
for" step starts is not missed.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field

from ..clock import now as app_now

BAUD = 115200
BAUDS = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 74880, 115200, 230400, 460800, 921600,
         1_000_000, 1_500_000, 2_000_000)
EOLS = {"LF": "\n", "CR LF": "\r\n", "CR": "\r", "none": ""}
PARTIAL_S = 0.15         # an unfinished line this old (a prompt) can be matched too
MAX_LINE = 4096
SET_NAMES = ("baud", "dtr", "rts")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class Pattern:
    name: str
    regex: str
    unit: str = ""

    def problem(self) -> str:
        """What is wrong with it ("" = fine)."""
        if not _NAME.match(self.name):
            return f"“{self.name}”: the name must be letters, digits and _"
        if self.name in ("dir", "text", "timestamp", "t_s", "t"):
            return f"“{self.name}” is taken: use another name"
        try:
            re.compile(self.regex)
        except re.error as e:
            return f"{self.name}: {e}"
        return "" if self.regex else f"{self.name}: the pattern is empty"


@dataclass
class UartLine:
    t: float
    dir: str                 # rx | tx
    text: str
    values: dict = field(default_factory=dict)


@dataclass
class UartCaps:
    idn: str = ""
    model: str = "UART"
    note: str = ""


@dataclass
class UartState:
    baud: int = BAUD
    eol: str = "LF"
    dtr: bool = True
    rts: bool = True
    patterns: list = field(default_factory=list)


@dataclass
class UartReading:
    t: float
    lines: list = field(default_factory=list)        # [UartLine], oldest first
    partial: str = ""                                # the line still coming in
    errors: list = field(default_factory=list)


def value_in(m: re.Match) -> float | None:
    """The number in a match: its first group, or 1 when it has no group."""
    if not m.re.groups:
        return 1.0
    g = next((x for x in m.groups() if x is not None), None)
    if g is None:
        return None
    g = g.strip().replace(",", ".")
    try:
        return float(int(g, 16)) if g.lower().startswith("0x") else float(g)
    except ValueError:
        return None


class UartDriver:
    kind = "uart"
    family = "UART"

    def __init__(self, transport, baud: int = BAUD):
        self.t = transport
        self.log = None
        self.caps = UartCaps()
        self.state = UartState(baud=baud)
        self._buf = bytearray()
        self._last_byte = 0.0
        self._fresh: list[UartLine] = []
        self._rx: deque = deque(maxlen=20000)     # (number, UartLine)
        self._n = 0                               # lines received so far
        self._cursor = 0                          # match() looks from this one on
        self._skip = (-1, "")                     # (line, text of it that match() has passed)
        self._hist: dict[str, deque] = {}         # pattern -> (t, value)
        self._compiled: list = []

    @property
    def cfg(self) -> UartState:
        s = self.state
        return UartState(s.baud, s.eol, s.dtr, s.rts, list(s.patterns))

    def errors(self) -> list:
        return []

    def identify(self) -> UartCaps:
        name = getattr(self.t, "resource", "") or "simulated"
        self.caps = UartCaps(idn=f"Serial console on {name}, {self.state.baud} baud, 8N1")
        return self.caps

    # ---- settings ----------------------------------------------------------
    def set_patterns(self, patterns: list):
        for p in patterns:
            if p.problem():
                raise ValueError(p.problem())
        names = [p.name for p in patterns]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError("two patterns called " + ", ".join(sorted(dup)))
        self.state.patterns = list(patterns)
        self._compiled = [(p.name, re.compile(p.regex)) for p in patterns]
        self._hist = {p.name: self._hist.get(p.name, deque(maxlen=20000)) for p in patterns}

    def set_eol(self, name: str):
        if name not in EOLS:
            raise ValueError(f"line end must be one of {', '.join(EOLS)}")
        self.state.eol = name

    def set(self, name: str, value):
        """By name, for sequence steps: baud, dtr, rts (on = asserted)."""
        n = str(name).lower()
        if n == "baud":
            b = int(float(value))
            if not 50 <= b <= 12_000_000:
                raise ValueError(f"{b} isn't a baud rate")
            self.t.set_baud(b)
            self.state.baud = b
        elif n in ("dtr", "rts"):
            on = bool(float(value))
            self.t.set_line(n, on)
            setattr(self.state, n, on)
        else:
            raise ValueError(f"a serial console can set {', '.join(SET_NAMES)}")

    def pulse(self, line: str, seconds: float = 0.1):
        """Release DTR / RTS and assert it again: resets most boards (Arduino)."""
        self.set(line, 0)
        time.sleep(seconds)
        self.set(line, 1)

    # ---- traffic -----------------------------------------------------------
    def send(self, text: str, raw: bool = False):
        """One line (the line end is added). The place match() looks from."""
        self._take()
        self.mark()
        data = str(text).encode("utf-8") + (b"" if raw else EOLS[self.state.eol].encode())
        self.t.write(data)
        self._fresh.append(UartLine(app_now(), "tx", str(text)))

    def mark(self):
        self._cursor = self._n
        # what is already there of an unfinished line (a prompt) is the past too
        self._skip = (self._n, self.partial())

    def _take(self):
        data = self.t.read_available()
        t = app_now()
        if data:
            self._buf += data
            self._last_byte = time.monotonic()
        while self._buf:
            ends = [i for i in (self._buf.find(b"\n"), self._buf.find(b"\r")) if i >= 0]
            if not ends:
                if len(self._buf) <= MAX_LINE:
                    break
                cut, skip = MAX_LINE, 0
            else:
                cut = min(ends)
                skip = 1
                if self._buf[cut:cut + 1] == b"\r":
                    if cut + 1 == len(self._buf):
                        # maybe half of CR LF: wait a moment for the rest
                        if time.monotonic() - self._last_byte < 0.03:
                            break
                    elif self._buf[cut + 1:cut + 2] == b"\n":
                        skip = 2
            line = bytes(self._buf[:cut])
            del self._buf[:cut + skip]
            if not line and skip == 1 and not self._n:
                continue           # a stray line end at the very start
            self._line(t, line.decode("utf-8", "replace"))

    def _line(self, t: float, text: str):
        text = text.replace("\0", "")
        ln = UartLine(t, "rx", text)
        for name, rx in self._compiled:
            m = rx.search(text)
            v = value_in(m) if m else None
            if v is not None:
                ln.values[name] = v
                self._hist[name].append((t, v))
        self._rx.append((self._n, ln))
        self._n += 1
        self._fresh.append(ln)

    def partial(self) -> str:
        return self._buf.decode("utf-8", "replace")

    def _ahead(self, n: int, text: str) -> str:
        if n == self._skip[0] and text.startswith(self._skip[1]):
            return text[len(self._skip[1]):]
        return text

    def match(self, regex: str) -> dict | None:
        """The first line since the mark that matches: its text, the groups
        and the number in it. The next match() looks after that line."""
        rx = re.compile(regex)
        self._take()
        for n, ln in self._rx:
            if n < self._cursor:
                continue
            m = rx.search(self._ahead(n, ln.text))
            if m:
                self._cursor = n + 1
                return {"t": ln.t, "text": ln.text, "groups": list(m.groups()),
                        "value": value_in(m) if m.groups() else None}
        if self._buf and time.monotonic() - self._last_byte > PARTIAL_S:
            m = rx.search(self._ahead(self._n, self.partial()))   # a prompt: no line end comes
            if m:
                self.mark()
                return {"t": app_now(), "text": self.partial(), "groups": list(m.groups()),
                        "value": value_in(m) if m.groups() else None}
        return None

    def values_since(self, name: str, t: float) -> list:
        """[(t, value)] of a pattern after app time t."""
        self._take()
        if name not in self._hist:
            raise ValueError(f"no pattern called '{name}' on this console")
        return [(a, v) for a, v in self._hist[name] if a > t]

    def poll(self, t: float, _cfg=None) -> UartReading:
        self._take()
        fresh, self._fresh = self._fresh, []
        return UartReading(app_now(), fresh, self.partial())

    def close(self):
        pass


def parse_resource(resource: str) -> tuple[str, int]:
    """'UART::/dev/ttyUSB0@9600' -> ('/dev/ttyUSB0', 9600)."""
    rest = resource.partition("::")[2] if resource.upper().startswith("UART::") else resource
    m = re.match(r"^(.*)@(\d+)$", rest)
    return (m.group(1), int(m.group(2))) if m else (rest, BAUD)
