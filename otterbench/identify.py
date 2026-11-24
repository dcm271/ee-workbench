"""What is on this serial port? Tries the protocols the app knows, one by one.

Only ever run when the user asks, and only on the port they chose: a probe
sends bytes, and an unknown device may do anything with them. Every probe is a
read-only question (an ID or a measurement query); nothing that sets, switches
or calibrates is sent. Listening comes first, so a meter or DUT that streams
by itself is recognised before anything is sent.

Ports whose USB descriptors already say what they are (PPK2, DPS-150) are not
probed at all.
"""
from __future__ import annotations

import os
import re
import struct
import time
from dataclasses import dataclass

from .instruments import adapters as ad
from .instruments.bk393 import CMD_INFO, appa_frame, parse_393, parse_appa

IDN = re.compile(r"[\x20-\x7e]{4,}")


@dataclass
class Found:
    resource: str = ""
    model: str = ""              # model key for open_instrument ("" = none fits)
    text: str = ""               # what it said / what it is
    baud: int = 0
    sure: bool = True


class Cancelled(Exception):
    pass


def probeable(resource: str) -> str:
    """The device path, when the resource is a serial port that may be probed."""
    m = re.match(r"(?:ASRL|UART::|[A-Za-z0-9.+-]+::)?(/dev/(?:tty|serial/)[^@:\s]*)", resource)
    if not m:
        return ""
    path = m.group(1)
    return "" if resource.upper().startswith(("PPK2::", "DPS150::")) else path


def by_usb(path: str) -> Found | None:
    """Devices known by their USB IDs alone."""
    from .transport import find_dps150, find_ppk2
    real = os.path.realpath(path)
    data, every = find_ppk2()
    if real in {os.path.realpath(x) for x in every}:
        res = next((d for d in data if os.path.realpath(d) == real), data[0] if data else path)
        return Found(f"PPK2::{res}", "PPK2", "Nordic PPK2 (by its USB ID)")
    for d in find_dps150():
        if os.path.realpath(d) == real:
            return Found(f"DPS150::{d}", "DPS150", "FNIRSI DPS-150 (by its USB ID)")
    return None


def by_idn(idn: str, path: str, baud: int) -> Found:
    """An answer to *IDN? -> the model and the resource to open it with."""
    from .instruments import looks_like_fgen, match_idn, scopes
    at = "" if baud == 9600 else f"@{baud}"
    visa = f"ASRL{path}{at}::INSTR"
    if "XDM" in idn.upper():
        return Found(f"XDM::{path}", "XDM", idn, baud)
    if "663" in idn and ("AGILENT" in idn.upper() or "HEWLETT" in idn.upper()):
        return Found(visa, "663xx", idn, baud)
    if looks_like_fgen(idn):
        return Found(visa, "FGEN", idn, baud)
    sc = scopes.detect_idn(idn)
    if sc:
        return Found(visa, sc, idn, baud)
    d = match_idn(idn)
    if d is not None and d.protocol:
        return Found(f"{d.id}::{path}{'' if baud == (d.baud or ad.ADAPTERS[d.protocol].BAUD) else at}",
                     d.model_key, idn, baud)
    if d is not None:
        return Found(visa, d.model_key, idn, baud)
    return Found(visa, "", idn, baud, sure=False)


def _read(link, seconds: float, done=lambda b: False, stop=None) -> bytes:
    end = time.monotonic() + seconds
    buf = b""
    while time.monotonic() < end:
        if stop is not None and stop.is_set():
            raise Cancelled()
        buf += link.read_available()
        if done(buf):
            break
        time.sleep(0.01)
    return buf


def _text(buf: bytes) -> str:
    """The first readable line of an answer ("" if it isn't text)."""
    if not buf or sum(32 <= b < 127 or b in (9, 10, 13) for b in buf) < 0.9 * len(buf):
        return ""
    for line in re.split(r"[\r\n\0]+", buf.decode("latin1")):
        if IDN.search(line):
            return line.strip()
    return ""


def _chatter(buf: bytes) -> bool:
    """Lines of text nobody asked for: a DUT's console."""
    return buf.count(b"\n") >= 1 and len(buf) >= 8 and bool(_text(buf)) \
        and sum(32 <= b < 127 or b in (9, 10, 13, 27) for b in buf) > 0.97 * len(buf)


def identify_port(path: str, open_link=None, progress=None, stop=None,
                  quick: float = 1.0) -> Found | None:
    """Work through the probes; the first hit wins. None: nothing recognised.

    open_link(path, baud) -> a byte link; `quick` scales the waits (tests)."""
    say = progress or (lambda text: None)
    if open_link is None:
        from .transport import SerialStream

        def open_link(p, b):
            return SerialStream(p, baud=b, wait_s=0.005)
        hit = by_usb(path)
        if hit is not None:
            return hit

    def session(baud: int, fn):
        if stop is not None and stop.is_set():
            raise Cancelled()
        link = open_link(path, baud)
        try:
            return fn(link)
        finally:
            link.close()

    def wait(link, s, done=lambda b: False):
        return _read(link, s * quick, done, stop)

    def ask_idn(link, question: bytes = b"*IDN?\r\n"):
        link.reset_input()
        link.write(question)
        buf = wait(link, 0.8, lambda b: b.endswith((b"\n", b"\r")) and len(b) > 4)
        text = _text(buf)
        # an echo of the question isn't an answer
        return "" if text.upper().startswith("*IDN?") or "," not in text and \
            "KORAD" not in text.upper() and len(text) < 8 else text

    def listen(baud):
        def fn(link):
            say(f"Listening at {baud} baud…")
            buf = wait(link, 1.2, lambda b: len(b) > 400)
            if parse_393(bytearray(buf)):
                return Found(f"BK393::{path}", "BK393", "Streams B&K 393 frames", 9600)
            if buf.count(b"\xff\x55\x01") >= 1:
                return Found("", "", "Streams Atorch meter reports (FF 55 …), a protocol this "
                             "app doesn't read", baud, sure=False)
            if _chatter(buf):
                return Found(f"UART::{path}@{baud}", "UART",
                             f"Prints text by itself: “{_text(buf)[:60]}”", baud)
            return None
        return fn

    def fast(link):
        say("*IDN? at 115200 baud…")
        # some (OWON) take a line feed alone, and refuse the question otherwise
        idn = ask_idn(link) or ask_idn(link, b"*IDN?\n")
        if idn:
            return by_idn(idn, path, 115200)
        say("Modbus (Riden) at 115200 baud…")
        link.reset_input()
        frame = bytes([1, 3, 0, 0, 0, 4])
        link.write(frame + struct.pack("<H", ad.crc16(frame)))
        r = wait(link, 0.4, lambda b: len(b) >= 13)[:13]
        if len(r) == 13 and r[:3] == b"\x01\x03\x08" and ad.crc16(r) == 0:
            mid = struct.unpack(">H", r[3:5])[0]
            try:
                return Found(f"RD60XX::{path}", "SCPI:RD60XX",
                             f"Riden {ad.RidenScale(mid).name}", 115200)
            except Exception:          # noqa: BLE001 - another Modbus device
                return Found("", "", f"A Modbus device that isn't a known Riden (id {mid})",
                             115200, sure=False)
        return None

    def slow(link):
        say("*IDN? at 9600 baud…")
        link.reset_input()
        link.write(b"*IDN?")             # Korad: no line end
        buf = wait(link, 0.5, lambda b: len(b) > 60)
        text = _text(buf)
        if text and not text.upper().startswith("*IDN?"):
            return by_idn(text, path, 9600)
        # everyone else: the line end completes the question just sent. Once
        # more in full, for an instrument that the first one only woke up.
        idn = ask_idn(link, b"\r\n") or ask_idn(link)
        if idn:
            return by_idn(idn, path, 9600)
        say("B&K 393B (APPA) at 9600 baud…")
        link.reset_input()
        link.write(appa_frame(CMD_INFO))
        buf = wait(link, 0.6, lambda b: len(b) >= 57)
        if any(c == CMD_INFO and len(data) > 8 for c, data in parse_appa(bytearray(buf))):
            return Found(f"BK393B::{path}", "BK393B", "Answers as a B&K 393B (APPA protocol)",
                         9600)
        say("Atorch load (PX-100) at 9600 baud…")
        link.reset_input()
        link.write(bytes([0xB1, 0xB2, 0x11, 0, 0, 0xB6]))
        r = wait(link, 0.5, lambda b: len(b) >= 7)
        if len(r) >= 7 and r[:2] == b"\xca\xcb" and r[5:7] == b"\xce\xcf":
            return Found(f"PX100::{path}", "SCPI:PX100",
                         f"Atorch PX-100 protocol, {int.from_bytes(r[2:5], 'big') / 1000:g} V",
                         9600)
        say("Thermometer (Center 309) at 9600 baud…")
        link.reset_input()
        link.write(b"A")
        p = wait(link, 0.5, lambda b: len(b) >= 45)
        if len(p) >= 45 and p[0] == 2 and p[44] == 3:
            return Found(f"CENTER309::{path}", "SCPI:CENTER309",
                         "Center 309 / Voltcraft K204 thermometer", 9600)
        return None

    def other(baud):
        def fn(link):
            say(f"*IDN? at {baud} baud…")
            idn = ask_idn(link)
            return by_idn(idn, path, baud) if idn else None
        return fn

    for baud, fn in ((9600, listen(9600)), (115200, listen(115200)), (115200, fast),
                     (9600, slow), (19200, other(19200)), (38400, other(38400)),
                     (57600, other(57600))):
        hit = session(baud, fn)
        if hit is not None:
            return hit
    return None
