"""Tapes: an instrument's real traffic in a file, played back as a test.

Tap sits between a driver and its link (a VISA session, a serial stream; below
the adapter of a binary protocol) and notes what goes over it while `on`.
Replay answers a driver from such notes, so that the driver code meets what
the real instrument said: a tape made on hardware once is a test for ever.

    tape = load("DP832.tape")
    drv = open_tape("DP832.tape")           # a driver that talks to the tape
    REPLAY::/path/DP832.tape                # the same, as a tab in the app

A tape is text, one JSON value per line: a header, then the events
    ["w", command]                          written
    ["r", command, answer]                  read after that write, or a query
    ["b", command, hex]   ["f", command, [floats]]        block answers
    ["x", command, "timeout" | message]     it didn't answer / the link failed
    ["s", command, status byte]             serial poll
and for a byte stream  ["w", hex], ["r", hex], ["c"] (input discarded).

Playing back goes by command, not by position: the n-th time the driver sends
a command it gets the n-th answer on the tape (then the last one again). So a
driver that asks in another order, or asks for more, still runs; a question
that was never asked of the real instrument is counted in `unknown`.
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .transport import TransportError, TransportTimeout

FORMAT = 1
MAX_EVENTS = 20_000             # per tape; then it stops noting, and says so
MAX_BYTES = 4_000_000
# Streams at 100 kS/s, other programs' objects, and a DUT's own text aren't taped.
NOT_TAPED = ("PPK2", "SALEAE", "SIGROK", "GPIO", "UART")


class TapeError(ValueError):
    pass


def tapeable(model: str) -> bool:
    return model not in NOT_TAPED


def model_of(drv, model: str) -> str:
    """The model key that makes this driver again (after auto-detection)."""
    if model != "auto":
        return model
    key = getattr(getattr(drv, "caps", None), "model_key", "")
    return key or {"663xx": "663xx", "6632A": "6632A", "fgen": "FGEN"}.get(
        getattr(drv, "family", ""), model)


class Tap:
    """Looks like the link it wraps; notes the traffic while `tap_on`."""

    _OWN = ("tap_link", "tap_on", "tap_events", "tap_size", "tap_full", "tap_stream", "tap_last")

    def __init__(self, link):
        put = object.__setattr__
        put(self, "tap_link", link)
        put(self, "tap_on", False)
        put(self, "tap_events", [])
        put(self, "tap_size", 0)
        put(self, "tap_full", False)
        put(self, "tap_stream", hasattr(link, "read_available"))
        put(self, "tap_last", "")          # the last command written

    def __getattr__(self, name):           # only what isn't found on the Tap itself
        return getattr(object.__getattribute__(self, "tap_link"), name)

    def __setattr__(self, name, value):
        if name in Tap._OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self.tap_link, name, value)

    def tap_clear(self):
        self.tap_events, self.tap_size, self.tap_full = [], 0, False

    def _note(self, *event):
        if not self.tap_on or self.tap_full:
            return
        self.tap_size += sum(len(x) if isinstance(x, str) else 8 * len(x)
                             if isinstance(x, list) else 4 for x in event)
        if len(self.tap_events) >= MAX_EVENTS or self.tap_size > MAX_BYTES:
            self.tap_full = True
            return
        self.tap_events.append(list(event))

    # ---- what drivers call --------------------------------------------------
    def write(self, data):
        if self.tap_stream:
            raw = data.encode("latin1") if isinstance(data, str) else bytes(data)
            self._note("w", raw.hex())
        else:
            self.tap_last = data
            self._note("w", data)
        return self.tap_link.write(data)

    def _ask(self, op, cmd, fn, conv=lambda v: v):
        try:
            v = fn()
        except TransportTimeout:
            self._note("x", cmd, "timeout")
            raise
        except Exception as e:
            self._note("x", cmd, str(e) or type(e).__name__)
            raise
        self._note(op, cmd, conv(v))
        return v

    def read(self, *a):
        if self.tap_stream:
            return self.tap_link.read(*a)
        return self._ask("r", self.tap_last, self.tap_link.read)

    def query(self, cmd, *a):
        self.tap_last = cmd
        return self._ask("r", cmd, lambda: self.tap_link.query(cmd, *a))

    def query_binary(self, cmd):
        self.tap_last = cmd
        return self._ask("f", cmd, lambda: self.tap_link.query_binary(cmd),
                         lambda v: [float(x) for x in v])

    def query_raw(self, cmd):
        self.tap_last = cmd
        return self._ask("b", cmd, lambda: self.tap_link.query_raw(cmd), lambda v: bytes(v).hex())

    def query_bytes(self, cmd):
        self.tap_last = cmd
        return self._ask("b", cmd, lambda: self.tap_link.query_bytes(cmd),
                         lambda v: bytes(v).hex())

    def write_bytes(self, data):
        self.tap_last = bytes(data).decode("latin1")
        self._note("w", self.tap_last)
        return self.tap_link.write_bytes(data)

    def read_stb(self):
        v = self.tap_link.read_stb()
        if not self.tap_stream:
            self._note("s", self.tap_last, int(v))
        return v

    def read_available(self):
        data = self.tap_link.read_available()
        if data:
            self._note("r", data.hex())
        return data

    def reset_input(self):
        self._note("c")
        return self.tap_link.reset_input()


def wrap(transport):
    """Tap a transport just opened. Returns (what the driver gets, the Tap);
    the Tap is None when there is nothing to tap (1-Wire and SPI sensors)."""
    link = getattr(transport, "link", None)
    if link is None:
        tap = Tap(transport)
        return tap, tap
    if hasattr(link, "read_available"):
        tap = Tap(link)
        transport.link = tap
        return transport, tap
    return transport, None


# ---------------------------------------------------------------- files
@dataclass
class Tape:
    model: str = ""              # model key the driver was made with
    link: str = ""               # GPIB | USB | TCPIP | serial | simulator
    idn: str = ""
    app: str = ""
    date: str = ""
    stream: bool = False
    truncated: bool = False
    notes: str = ""
    options: dict = field(default_factory=dict)      # of the hardware check, if it was one
    rows: list = field(default_factory=list)         # ... and what it found
    events: list = field(default_factory=list)

    @property
    def simulated(self) -> bool:
        return self.link == "simulator"


HEADER = ("model", "link", "idn", "app", "date", "stream", "truncated", "notes", "options", "rows")


def link_kind(resource: str) -> str:
    up = resource.upper()
    if up.startswith(("SIM", "REPLAY")):
        return "simulator"
    m = re.match(r"(GPIB|USB|TCPIP)", up)
    return m.group(1) if m else "serial"


def save(tape: Tape, path: str, hide_serial: bool = True):
    if hide_serial:
        tape = without_serial(tape)
    head = {"otterbench_tape": FORMAT, **{k: getattr(tape, k) for k in HEADER}}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(head, ensure_ascii=False) + "\n")
        for e in tape.events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def load(path: str) -> Tape:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        head = json.loads(lines[0])
        events = [json.loads(ln) for ln in lines[1:] if ln.strip()]
    except OSError as e:
        raise TapeError(f"Can't read {path}: {e}") from None
    except (ValueError, IndexError):
        raise TapeError(f"{path} isn't a tape (or is damaged)") from None
    if not isinstance(head, dict) or "otterbench_tape" not in head:
        raise TapeError(f"{path} isn't a tape")
    if head["otterbench_tape"] != FORMAT:
        raise TapeError(f"{path} was made by another version (tape format "
                        f"{head['otterbench_tape']})")
    for e in events:
        if not isinstance(e, list) or not e or not isinstance(e[0], str):
            raise TapeError(f"{path} is damaged")
    return Tape(events=events, **{k: head[k] for k in HEADER if k in head})


def serial_in(idn: str) -> str:
    """The serial number in an identity ("" if there is none to hide)."""
    from .calibration import serial_of
    sn = serial_of(idn)
    if not sn:
        m = re.search(r"\bSN:?\s*([A-Za-z0-9\-]{4,})", idn)
        sn = m.group(1) if m else ""
    return sn if len(sn) >= 4 else ""


def _textual(tape: Tape) -> bool:
    """A byte stream of text (no control bytes), so without checksums."""
    raw = [bytes.fromhex(e[1]) for e in tape.events if len(e) > 1]
    return all(c in b"\r\n\t" or c >= 32 for b in raw for c in b)


def hides_serial(tape: Tape) -> bool:
    """Can the serial number be taken out of the traffic? Not from a binary
    protocol: its frames carry checksums, and it may be a plain number there."""
    return not tape.stream or _textual(tape)


def without_serial(tape: Tape) -> Tape:
    """A copy whose serial number is X's (in the traffic too, see hides_serial)."""
    from dataclasses import replace

    sn = serial_in(tape.idn)
    if not sn:
        return tape
    x = "X" * len(sn)

    def text(v):
        return v.replace(sn, x) if isinstance(v, str) else v

    events = [list(e) for e in tape.events]
    if tape.stream:
        if hides_serial(tape):
            events = _merge(events)
            events = [[e[0], bytes.fromhex(e[1]).replace(sn.encode(), x.encode()).hex()]
                      if len(e) > 1 else e for e in events]
    else:
        events = [[text(v) if i else v for i, v in enumerate(e)] if e[0] not in "bf" else e
                  for e in events]
    rows = [{k: text(v) for k, v in r.items()} for r in tape.rows]
    return replace(tape, idn=text(tape.idn), events=events, rows=rows)


def _merge(events: list) -> list:
    """Neighbouring pieces of one answer joined (a number may be split in two)."""
    out: list = []
    for e in events:
        if e[0] == "r" and out and out[-1][0] == "r":
            out[-1] = ["r", out[-1][1] + e[1]]
        else:
            out.append(list(e))
    return out


# ---------------------------------------------------------------- playing back
PACE_S = 0.01                    # a replayed tab's answers


class Replay:
    """A message link (VISA): answers by command."""

    def __init__(self, events: list):
        self.timeout_s = 5.0
        self.pace = 0.0                     # s per answer (a tab: like an instrument's)
        self.unknown: list[str] = []        # asked now, never asked on the tape
        self.repeats = 0
        self._last = ""
        self._q: dict = defaultdict(deque)
        self._old: dict = {}                # (kind, command) -> its last answer
        self._written = set()
        for e in events:
            if e[0] == "w":
                self._written.add(e[1])
            elif e[0] == "s":
                self._q["s", e[1]].append(e)
            else:
                self._written.add(e[1])
                # a failed block query and a failed query look alike on the tape
                self._q["a", e[1]].append(e)

    @contextmanager
    def temp_timeout(self, seconds):
        old, self.timeout_s = self.timeout_s, seconds
        try:
            yield
        finally:
            self.timeout_s = old

    def _next(self, kind: str, cmd: str):
        if self.pace:
            time.sleep(self.pace)
        q = self._q.get((kind, cmd))
        if q:
            self._old[kind, cmd] = q.popleft()
        elif (kind, cmd) in self._old:
            self.repeats += 1
        return self._old.get((kind, cmd))

    def _answer(self, cmd: str, ops: str):
        e = self._next("a", cmd)
        if e is None:
            self.unknown.append(cmd)
            raise TransportTimeout(f"{cmd!r} isn't on the tape")
        if e[0] == "x":
            if e[2] == "timeout":
                raise TransportTimeout(f"No reply to {cmd!r}")
            raise TransportError(e[2])
        if e[0] not in ops:
            raise TransportError(f"{cmd!r}: another kind of answer is on the tape")
        return e[2]

    def write(self, cmd: str):
        self._last = cmd
        if cmd not in self._written:
            self.unknown.append(cmd)

    def read(self) -> str:
        return self._answer(self._last, "r")

    def query(self, cmd: str) -> str:
        self._last = cmd
        return self._answer(cmd, "r")

    def query_binary(self, cmd: str) -> list:
        self._last = cmd
        return list(self._answer(cmd, "f"))

    def query_raw(self, cmd: str) -> bytes:
        self._last = cmd
        return bytes.fromhex(self._answer(cmd, "b"))

    query_bytes = query_raw

    def write_bytes(self, data: bytes):
        self.write(bytes(data).decode("latin1"))

    def read_stb(self) -> int:
        e = self._next("s", self._last)
        return int(e[2]) if e else 0

    def clear(self):
        pass

    def close(self):
        pass


class ReplayStream:
    """A serial link: after each write, the bytes that followed it on the tape.
    Bytes before the first write (a meter that only sends) come round again."""

    def __init__(self, events: list):
        self.timeout_s = 2.0
        self.pace = 0.0                     # s per answer
        self.overflow_bytes = 0
        self.unknown: list[str] = []
        self.repeats = 0
        self._q: dict = defaultdict(deque)      # written bytes -> [its chunks, …]
        self._old: dict = {}
        self._free: list = []                   # unasked bytes
        self._at = 0
        self._now: deque = deque()
        self._asked = False
        chunks = self._free
        for e in events:
            if e[0] == "w":
                chunks = []
                self._q[e[1]].append(chunks)
            elif e[0] == "r":
                chunks.append(bytes.fromhex(e[1]))

    @contextmanager
    def temp_timeout(self, seconds):
        old, self.timeout_s = self.timeout_s, seconds
        try:
            yield
        finally:
            self.timeout_s = old

    def write(self, data):
        raw = data.encode("latin1") if isinstance(data, str) else bytes(data)
        key = raw.hex()
        self._asked = True
        if self._q.get(key):
            self._old[key] = self._q[key].popleft()
        elif key in self._old:
            self.repeats += 1
        else:
            self.unknown.append(key)
        self._now = deque(self._old.get(key, []))

    def read_available(self) -> bytes:
        if self.pace:
            time.sleep(self.pace)
        if self._asked:
            return self._now.popleft() if self._now else b""
        if not self._free:
            return b""
        data = self._free[self._at % len(self._free)]
        self._at += 1
        return data

    def read_until(self, marker: bytes, timeout_s: float) -> bytes:
        buf = b""
        while marker not in buf:
            chunk = self.read_available()
            if not chunk:
                break
            buf += chunk
        return buf

    def drain(self, quiet_s: float, max_s: float):
        self._now.clear()

    def reset_input(self):
        self._now.clear()

    def set_baud(self, baud: int):
        pass

    def set_line(self, name: str, on: bool):
        pass

    def read_stb(self) -> int:
        return 0

    def clear(self):
        self.reset_input()

    def close(self):
        pass


def replay_path(resource: str) -> str:
    return resource[len("REPLAY::"):] if resource.upper().startswith("REPLAY::") else resource


def open_replay(path: str, tape: Tape | None = None):
    """What open_transport() gives for REPLAY::path."""
    tape = tape or load(path)
    link = (ReplayStream if tape.stream else Replay)(tape.events)
    if tape.model.startswith("SCPI:"):
        from .instruments.scpi_generic import definition_for
        d = definition_for(tape.model)
        if d is None:
            raise TapeError(f"The tape is of {tape.model}, which has no definition here")
        if d.protocol:
            from .instruments.adapters import ADAPTERS
            a = ADAPTERS[d.protocol](link, d)
            for k in ("GAP", "SETTLE"):
                if hasattr(a, k):
                    setattr(a, k, 0.0)
            a.replay = link
            return a
    link.replay = link
    return link


def open_tape(path: str, tape: Tape | None = None):
    """A driver talking to a tape (identify() not called yet)."""
    from .instruments import make_driver
    tape = tape or load(path)
    t = open_replay(path, tape)
    drv = make_driver(tape.model, t, "")
    for k in ("GAP", "SETTLE"):
        if hasattr(drv, k):
            setattr(drv, k, 0.0)
    return drv


def new_tape(tap: Tap | None, connect: list, model: str, resource: str, idn: str) -> Tape:
    from .about import VERSION
    return Tape(model=model, link=link_kind(resource), idn=idn, app=VERSION,
                date=time.strftime("%Y-%m-%d"), stream=bool(tap and tap.tap_stream),
                truncated=bool(tap and tap.tap_full),
                events=list(connect) + list(tap.tap_events if tap else []))
