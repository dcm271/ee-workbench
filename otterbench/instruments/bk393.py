"""Drivers for B&K Precision handheld multimeters with a USB cable: 393 and 393B.

Both are read-only over USB: function and range are chosen on the meter. They
share a name and nothing else.

393 (60,000 counts): talks one way. Once its RS232 key has been pressed it
streams 10-byte frames at 9600 8N1 through an opto-isolated FTDI cable that
needs DTR and RTS asserted (pyserial's default):
    F0 11 FUNC RANGE D0 D1 D2 D3 STATUS 05
  D0..D3  ASCII hex digits, least significant first
  STATUS  0x20 negative, 0x10 overload, 0x01 bit 16 of the count
B&K publishes no protocol. This follows B&K's AK393 PC software (serial
settings, frame check, scale table and status bits) and dstahlke's Linux
driver (gist d0621a061eb8839e2f5f), which agree.

393B (100,000 counts): an APPA 500-series meter (B&K's PC software for it is
APPA's WinDMM500) on a CP210x cable, 9600 8N1, request / reply:
    55 55 CMD LEN DATA[LEN] CHK        CHK = sum of everything before it & 0xFF
Meters with the current protocol answer command 00 with 52 bytes of
information and command 01 with 12 bytes of display. Older ones answer 00 with
everything in 54 bytes. The driver tells them apart by that length. The layouts
follow the libsigrok appa-dmm driver. Only read commands are ever sent: the
same protocol has calibration and firmware-update commands.
"""
from __future__ import annotations

import time

from .base import DmmCaps, DmmConfig, DmmReading

BAUD = 9600
OVERLOAD = 9.9e37               # SCPI's "overload" number

PREFIX = {"n": 1e-9, "µ": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "M": 1e6, "G": 1e9}


def split_unit(native: str) -> tuple[str, float]:
    """'kΩ' -> ('Ω', 1e3); '°C', '%', 'dBm' ... have no prefix."""
    if native[:1] in PREFIX and native[1:] in ("V", "A", "Ω", "F", "Hz", "S", "s", "W"):
        return native[1:], PREFIX[native[0]]
    return native, 1.0


def make_reading(t: float, count: int, decimals: int, native: str, function: str, *,
                 overload: bool = False, negative: bool = False, text: str = "",
                 flags=(), secondary: str = "") -> DmmReading:
    unit, mult = split_unit(native)
    if overload:
        value = -OVERLOAD if negative else OVERLOAD
    elif text:
        value = float("nan")
    else:
        value = (-count if negative else count) * 10.0 ** -decimals * mult
    return DmmReading(t=t, value=value, unit=unit, overload=overload, function=function,
                      native=native, mult=mult, decimals=decimals, text=text,
                      flags=list(flags), secondary=secondary)


# ----------------------------------------------------------------- 393
_V = {0: (4, "V"), 1: (3, "V"), 2: (2, "V"), 3: (1, "V")}
_UA = {0: (2, "µA"), 1: (1, "µA")}
_MA = {0: (3, "mA"), 1: (2, "mA")}
_A = {1: (3, "A")}

# function code -> (name, {range code: (decimals, unit)})
FUNCTIONS_393 = {
    0x01: ("ACV", _V),
    0x02: ("DCV", _V),
    0x03: ("AC mV", {0: (2, "mV")}),
    0x04: ("Ω", {0: (2, "Ω"), 1: (4, "kΩ"), 2: (3, "kΩ"), 3: (2, "kΩ"),
                 4: (4, "MΩ"), 5: (3, "MΩ")}),
    0x05: ("Capacitance", {0: (3, "nF"), 1: (2, "nF"), 2: (1, "nF"), 3: (3, "µF"),
                           4: (2, "µF"), 5: (1, "µF"), 6: (3, "mF")}),
    0x06: ("Frequency", {0: (3, "Hz"), 1: (2, "Hz"), 2: (4, "kHz"), 3: (3, "kHz"),
                         4: (2, "kHz"), 5: (4, "MHz"), 6: (3, "MHz")}),
    0x07: ("AC µA", _UA),
    0x08: ("AC A", _A),
    0x09: ("Temperature", {1: (1, "°C")}),
    0x0B: ("AC+DC V", _V),
    0x0C: ("DC mV", {0: (2, "mV")}),
    0x0D: ("AC+DC mV", {0: (2, "mV")}),
    0x0E: ("Continuity", {0: (2, "Ω")}),
    0x0F: ("Diode", {0: (4, "V")}),
    0x10: ("DC µA", _UA),
    0x11: ("DC mA", _MA),
    0x12: ("DC A", _A),
    0x13: ("AC mA", _MA),
    0x14: ("Temperature", {1: (1, "°F")}),
    0x16: ("Duty cycle", {0: (1, "%")}),
    0x1B: ("AC+DC µA", _UA),
    0x1C: ("AC+DC mA", _MA),
    0x1D: ("AC+DC A", _A),
    0x1E: ("Conductance", {0: (2, "nS")}),
}

FRAME_393 = 10
_HEXDIGITS = b"0123456789ABCDEF"
S_OVERLOAD, S_NEGATIVE, S_BIT16 = 0x10, 0x20, 0x01


def parse_393(buf: bytearray) -> list[bytes]:
    """Take every complete frame off the front of `buf` (modified in place);
    an incomplete one at the end stays for the next call."""
    out = []
    i, n = 0, len(buf)
    while n - i >= FRAME_393:
        f = bytes(buf[i:i + FRAME_393])
        if (f[0] == 0xF0 and f[1] == 0x11 and f[9] == 0x05
                and all(c in _HEXDIGITS for c in f[4:8])):
            out.append(f)
            i += FRAME_393
        else:
            i += 1
    del buf[:i]
    return out


def decode_393(f: bytes, t: float) -> DmmReading:
    status = f[8]
    count = int(f[4:8][::-1].decode("ascii"), 16) + ((status & S_BIT16) << 16)
    name, ranges = FUNCTIONS_393.get(f[2], (f"Function {f[2]:#04x}", {}))
    known = ranges.get(f[3] & 0x0F)
    overload = bool(status & S_OVERLOAD)
    if known is None:            # never guess a decimal point
        text = "" if overload else f"unknown range (function {f[2]:02x}, range {f[3]:02x})"
        return make_reading(t, 0, 0, "", name, overload=overload,
                            negative=bool(status & S_NEGATIVE), text=text)
    dec, native = known
    return make_reading(t, count, dec, native, name, overload=overload,
                        negative=bool(status & S_NEGATIVE))


class _Handheld:
    kind = "dmm"

    def __init__(self, transport):
        self.t = transport
        self.caps = DmmCaps()
        self.cfg = DmmConfig()       # the meter's own knob decides; nothing to set
        self.log = None
        self._rx = bytearray()

    def errors(self) -> list[str]:
        return []

    def _trace(self, arrow: str, b: bytes):
        if self.log:
            self.log(f"{arrow} {b.hex(' ')}")


class BK393(_Handheld):
    family = "BK393"
    FIRST_FRAME_S = 4.0
    FRAME_S = 3.0
    HELP = ("No readings from the 393. Press its RS232 key (it turns the computer "
            "interface on), check the USB cable and that the meter hasn't switched itself "
            "off (hold MAX/MIN while turning it on to disable auto power off).")

    def _next_frame(self, timeout_s: float) -> bytes:
        """The newest frame received; waits for one if none has arrived."""
        end = time.monotonic() + timeout_s
        while True:
            self._rx += self.t.read_available()
            frames = parse_393(self._rx)
            if frames:
                self._trace("<", frames[-1])
                return frames[-1]
            if time.monotonic() > end:
                raise TimeoutError(self.HELP)
            time.sleep(0.01)

    def identify(self) -> DmmCaps:
        self.t.reset_input()
        self._rx.clear()
        r = decode_393(self._next_frame(self.FIRST_FRAME_S), 0.0)
        self.caps = DmmCaps(idn="B&K Precision 393 (no ID, verified by its readings)",
                            model="393", note=f"First reading: {r.function} {r.display()}")
        return self.caps

    def poll(self, t: float, _cfg=None) -> DmmReading:
        t0 = time.monotonic()
        f = self._next_frame(self.FRAME_S)
        return decode_393(f, t + time.monotonic() - t0)

    def raw(self, line: str, read: bool = True) -> str:
        """Console: the 393 accepts no commands, so this shows the next frame."""
        f = self._next_frame(self.FRAME_S)
        return f"{f.hex(' ')}   {decode_393(f, 0.0).display()}   (the 393 takes no commands)"


# ----------------------------------------------------------------- 393B (APPA 500 series)
CMD_INFO, CMD_DISPLAY = 0x00, 0x01
READ_ONLY = {CMD_INFO, CMD_DISPLAY, 0x03, 0x04}   # information, display, protocol version, battery
LEN_INFO, LEN_DISPLAY, LEN_LEGACY = 52, 12, 54
MAX_DATA = 64

UNITS_APPA = ("", "V", "mV", "A", "mA", "dB", "dBm", "mF", "µF", "nF", "GΩ", "MΩ", "kΩ",
              "Ω", "%", "MHz", "kHz", "Hz", "°C", "°F", "s", "ms", "µs", "ns", "µA", "min",
              "kW", "PF")

FUNCTIONS_APPA = {
    0x01: "ACV", 0x02: "DCV", 0x03: "AC mV", 0x04: "DC mV", 0x05: "Ω", 0x06: "Continuity",
    0x07: "Diode", 0x08: "Capacitance", 0x09: "AC A", 0x0A: "DC A", 0x0B: "AC mA",
    0x0C: "DC mA", 0x0D: "Temperature", 0x0E: "Temperature", 0x0F: "Frequency",
    0x10: "Duty cycle", 0x11: "Frequency (V)", 0x12: "Frequency (mV)",
    0x13: "Frequency (A)", 0x14: "Frequency (mA)", 0x15: "AC+DC V", 0x16: "AC+DC mV",
    0x17: "AC+DC A", 0x18: "AC+DC mA", 0x19: "ACV HFR", 0x1A: "AC mV HFR",
    0x1B: "AC A HFR", 0x1C: "AC mA HFR", 0x1D: "AC µA", 0x1E: "DC µA",
    0x27: "Peak hold V", 0x28: "Peak hold mV", 0x29: "Peak hold A", 0x2A: "Peak hold mA",
    0x2B: "LoZ ACV", 0x2C: "LoZ DCV", 0x2D: "LoZ AC+DC V", 0x2E: "LoZ ACV HFR",
    0x2F: "LoZ frequency", 0x30: "LoZ peak hold V",
}
LEGACY_FUNCTIONS = {
    0x0000: 0x0D, 0x0001: 0x0E, 0x0100: 0x01, 0x0101: 0x02, 0x0102: 0x15, 0x0200: 0x03,
    0x0201: 0x04, 0x0202: 0x16, 0x0300: 0x05, 0x0301: 0x06, 0x0302: 0x08, 0x0303: 0x07,
    0x0400: 0x0B, 0x0401: 0x0C, 0x0402: 0x18, 0x0500: 0x09, 0x0501: 0x0A, 0x0502: 0x17,
    0x0600: 0x0F, 0x0601: 0x10,
}
LEGACY_HFR = {0x01: 0x19, 0x03: 0x1A, 0x09: 0x1B, 0x0B: 0x1C}

# What a display line shows when it isn't the plain reading.
CONTENT = {
    0x01: "Freq", 0x02: "Cycle", 0x03: "Duty", 0x04: "Memory stamp", 0x05: "Store",
    0x06: "Recall", 0x07: "Log", 0x08: "Log recall", 0x09: "Log rate", 0x0A: "REL",
    0x0B: "REL %", 0x0C: "Reference", 0x0D: "MAX", 0x0E: "MIN", 0x0F: "AVG",
    0x10: "Peak max", 0x11: "Peak min", 0x12: "dBm", 0x13: "dB", 0x14: "Auto hold",
    0x15: "Setup", 0x16: "Log stamp", 0x17: "Log max", 0x18: "Log min", 0x19: "Log TP",
    0x1A: "HOLD", 0x1B: "Output",
}

WORD_MIN = 0x700000
WORDS = ("", "FULL", "BEEP", "APO", "B.LIT", "HAZ", "ON", "OFF", "RESET", "START", "VIEW",
         "PAUSE", "FUSE", "PROBE", "DEF", "CLR", "Er", "Er1", "Er2", "Er3", "-----", "-",
         "TEST", "--", "BATT", "DISLT", "NOISE", "FILTR", "PASS", "NULL", "0-20", "4-20",
         "RATE", "SAVE", "LOAD", "YES", "SEND", "AHOLD", "AUTO", "CNTIN", "CAL", "VERSION",
         "OL", "FULL", "HALF", "Lo", "Hi", "DIGIT", "READY", "DISC", "OUTF", "OLA", "OLV",
         "OLVA", "BAD", "TEMP")


def appa_frame(cmd: int, data: bytes = b"") -> bytes:
    f = bytes([0x55, 0x55, cmd, len(data)]) + bytes(data)
    return f + bytes([sum(f) & 0xFF])


def parse_appa(buf: bytearray) -> list[tuple[int, bytes]]:
    """Take every complete, checksum-valid packet off `buf` as (cmd, data)."""
    out = []
    i, n = 0, len(buf)
    while i < n:
        if buf[i] != 0x55:
            i += 1
            continue
        if n - i < 2:
            break
        if buf[i + 1] != 0x55:
            i += 1
            continue
        if n - i < 5:
            break
        ln = buf[i + 3]
        if ln > MAX_DATA:
            i += 1
            continue
        end = i + 5 + ln
        if end > n:
            break
        if sum(buf[i:end - 1]) & 0xFF == buf[end - 1]:
            out.append((buf[i + 2], bytes(buf[i + 4:end - 1])))
            i = end
        else:
            i += 1
    del buf[:i]
    return out


def _line(raw: int, unit_byte: int, overload: bool) -> dict:
    """One display line: count, decimals, unit, and a word instead of a number."""
    code = unit_byte >> 3
    native = UNITS_APPA[code] if code < len(UNITS_APPA) else ""
    text = ""
    if raw >= WORD_MIN:
        k = raw - WORD_MIN
        text = (WORDS[k] if k < len(WORDS) else f"word {k:#x}") or "(blank)"
        if text in ("OL", "OLV", "OLA", "OLVA"):
            overload = True
    return {"count": raw, "decimals": unit_byte & 7, "native": native, "text": text,
            "overload": overload}


def _line_text(d: dict) -> str:
    if d["overload"]:
        return "OL"
    if d["text"]:
        return "" if d["text"] == "(blank)" else d["text"]
    return f"{d['count'] * 10.0 ** -d['decimals']:+.{d['decimals']}f} {d['native']}".rstrip()


def _appa_reading(t, fn: int, auto: bool, main: dict, main_content: int, sub: dict,
                  sub_content: int) -> DmmReading:
    flags = ["AUTO" if auto else "Manual range"]
    if main_content:
        flags.append(CONTENT.get(main_content, f"display {main_content:#x}"))
    s = _line_text(sub)
    if s and sub_content:
        s = f"{CONTENT.get(sub_content, '')} {s}".strip()
    ol = main["overload"]
    return make_reading(t, abs(main["count"]), main["decimals"], main["native"],
                        FUNCTIONS_APPA.get(fn, f"Function {fn:#04x}"),
                        overload=ol, negative=main["count"] < 0,
                        text="" if ol else main["text"], flags=flags, secondary=s)


def decode_display(d: bytes, t: float) -> DmmReading:
    """Reply to command 01 (current protocol)."""
    main = _line(int.from_bytes(d[2:5], "little", signed=True), d[5], bool(d[6] & 0x80))
    sub = _line(int.from_bytes(d[7:10], "little", signed=True), d[10], bool(d[11] & 0x80))
    return _appa_reading(t, d[0] & 0x7F, bool(d[1] & 0x80), main, d[6] & 0x7F,
                         sub, d[11] & 0x7F)


def decode_legacy(d: bytes, t: float) -> DmmReading:
    """Reply to command 00 on meters with the older protocol."""
    def line(k):
        status = d[k + 4]
        raw = int.from_bytes(d[k:k + 3], "big", signed=True)
        if status & 0x80:                       # blank
            raw = WORD_MIN
        elif status & 0x40:                     # a word; number 0 means "Er"
            raw = WORD_MIN + raw if raw else WORD_MIN + 0x10
        content = status & 0x1F
        return _line(raw, d[k + 3], bool(status & 0x20)), content - 1 if content > 1 else 0

    fn = LEGACY_FUNCTIONS.get(int.from_bytes(d[20:22], "big"), 0)
    if d[24] & 0x10:
        fn = LEGACY_HFR.get(fn, fn)
    main, mc = line(37)
    sub, sc = line(42)
    return _appa_reading(t, fn, not d[24] & 1, main, mc, sub, sc)


def _ascii(b: bytes) -> str:
    return "".join(chr(c) for c in b if 32 <= c < 127).strip()


class BK393B(_Handheld):
    family = "BK393B"
    REPLY_S = 1.0
    HELP = ("No answer from the 393B. Check the USB cable is seated on the back of the "
            "meter, the meter is on (auto power off?) and no other program has the port.")

    def __init__(self, transport):
        super().__init__(transport)
        self.legacy = False

    def query(self, cmd: int, timeout_s: float | None = None) -> bytes:
        if cmd not in READ_ONLY:
            raise ValueError(f"command {cmd:02x} is not a read command")
        self.t.reset_input()
        self._rx.clear()
        pkt = appa_frame(cmd)
        self._trace(">", pkt)
        self.t.write(pkt)
        end = time.monotonic() + (timeout_s or self.REPLY_S)
        while True:
            self._rx += self.t.read_available()
            for c, data in parse_appa(self._rx):
                if c == 0x70:
                    raise RuntimeError(f"The 393B refused command {cmd:02x}")
                if c == cmd:
                    self._trace("<", appa_frame(c, data))
                    return data
            if time.monotonic() > end:
                raise TimeoutError(self.HELP)
            time.sleep(0.005)

    def _retry(self, cmd: int) -> bytes:
        try:
            return self.query(cmd)
        except TimeoutError:
            return self.query(cmd)      # the optical link drops a packet now and then

    def identify(self) -> DmmCaps:
        info = self._retry(CMD_INFO)
        if len(info) == LEN_INFO:
            self.legacy = False
            name, serial = _ascii(info[:32]), _ascii(info[32:48])
            fw = int.from_bytes(info[50:52], "little")
            extra = f"serial {serial or '?'}  fw {fw / 100:.2f}"
        elif len(info) == LEN_LEGACY:
            self.legacy = True
            name, serial = _ascii(info[:10]), _ascii(info[10:18])
            extra = f"serial {serial or '?'}  (older APPA protocol)"
        else:
            raise RuntimeError(f"Unexpected answer from the meter ({len(info)} bytes); "
                               "is this a 393B?")
        r = self.poll(0.0)
        self.caps = DmmCaps(idn=f"B&K Precision 393B  [{name or 'no name'}]  {extra}",
                            model="393B", note=f"First reading: {r.function} {r.display()}")
        return self.caps

    def poll(self, t: float, _cfg=None) -> DmmReading:
        t0 = time.monotonic()
        want = LEN_LEGACY if self.legacy else LEN_DISPLAY
        data = self._retry(CMD_INFO if self.legacy else CMD_DISPLAY)
        if len(data) != want:
            raise RuntimeError(f"Unexpected display data from the 393B ({len(data)} bytes)")
        t += time.monotonic() - t0
        return decode_legacy(data, t) if self.legacy else decode_display(data, t)

    def raw(self, line: str, read: bool = True) -> str:
        """Console: one read command as hex (00 information, 01 display, 03 protocol
        version, 04 battery). Anything else is refused: the protocol also carries
        calibration and firmware-update commands."""
        try:
            b = bytes.fromhex(line.replace("0x", ""))
        except ValueError:
            b = b""
        if len(b) != 1 or b[0] not in READ_ONLY:
            return "read commands only:  00 information, 01 display, 03 version, 04 battery"
        return self.query(b[0]).hex(" ")
