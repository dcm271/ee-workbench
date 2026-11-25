"""Instruments that don't speak SCPI, made to look as if they did.

A definition in scpi_defs/ with `protocol = "riden"` (korad, px100, center, w1,
max31855, max31865, scpi-ack) is used through an adapter from this file: a
transport with the usual write() / query() taking short text commands
(`VSET 5`, `VOUT?`), which it turns into the instrument's own frames. So the
generic driver and tab, the sequencer, the watchdog, math and sessions need to
know nothing about Modbus or one-wire.

Protocols are from published sources: Riden RD60xx registers (ShayBox/Riden),
Korad and Center 3xx (libsigrok), PX-100 (misdoro/Electronic_load_px100),
MAX31855 / MAX31865 (Maxim data sheets), w1_therm (the Linux kernel docs), and
the East Tester ET44/ET45 SCPI manual.
"""
from __future__ import annotations

import math
import os
import re
import struct
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from ..transport import TransportError, TransportTimeout

BAD = "9.9E+37"              # SCPI's "no reading": the driver turns it into nan


class Adapter:
    """Text commands in, text answers out. Subclasses implement `handle(cmd)`
    (the answer, or None for a command that has none)."""

    BAUD = 9600
    REPLY_S = 1.0               # how long the instrument may take to answer
    CACHE_S = 0.05              # a block read serves every query of one poll

    def __init__(self, link, definition=None):
        self.link = link
        self.d = definition
        self.timeout_s = 2.0
        self._out: list[str] = []

    # ---- what drivers use --------------------------------------------------
    @contextmanager
    def temp_timeout(self, seconds: float):
        old, self.timeout_s = self.timeout_s, seconds
        try:
            yield
        finally:
            self.timeout_s = old

    def write(self, cmd: str):
        r = self.handle(cmd.strip())
        if r is not None:
            self._out.append(str(r))

    def read(self) -> str:
        if not self._out:
            raise TransportTimeout("No reply (the command has no answer)")
        return self._out.pop(0)

    def query(self, cmd: str) -> str:
        self._out.clear()
        self.write(cmd)
        try:
            return self.read()
        except TransportTimeout:
            raise TransportTimeout(f"No reply to {cmd!r}") from None

    def read_stb(self) -> int:
        return 0

    def clear(self):
        self._out.clear()
        if hasattr(self.link, "reset_input"):
            self.link.reset_input()

    def close(self):
        if hasattr(self.link, "close"):
            self.link.close()

    def handle(self, cmd: str):             # pragma: no cover - abstract
        raise NotImplementedError

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def split(cmd: str) -> tuple[str, str]:
        """'VSET 5' -> ('VSET', '5'); 'VSET?' -> ('VSET?', '')."""
        head, _, arg = cmd.partition(" ")
        return head.upper(), arg.strip()

    @staticmethod
    def unknown(cmd: str):
        raise ValueError(f"unknown command {cmd!r}")

    def recv(self, n: int, timeout: float | None = None) -> bytes:
        """Exactly n bytes, or whatever came before the timeout."""
        end = time.monotonic() + (self.REPLY_S if timeout is None else timeout)
        buf = b""
        while len(buf) < n:
            chunk = self.link.read_available()
            if chunk:
                buf += chunk
            elif time.monotonic() >= end:
                break
            else:
                time.sleep(0.002)
        return buf


def onoff(arg: str) -> bool:
    a = arg.strip().upper()
    if a in ("1", "ON"):
        return True
    if a in ("0", "OFF"):
        return False
    raise ValueError(f"expected ON or OFF, not {arg!r}")


# ---------------------------------------------------------------- Riden RD60xx
def crc16(data: bytes) -> int:
    """Modbus RTU."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class RidenScale:
    """Counts per volt / amp / watt, and the limits, of one model."""

    def __init__(self, model_id: int):
        self.id = model_id
        self.v = self.i = self.p = 100
        self.low_range_i = 0             # RD6012P: counts per amp in its 6 A range
        self.vmax = 61.0
        if model_id >= 60241:
            self.name, self.imax = "RD6024", 24.1
        elif 60180 <= model_id <= 60189:
            self.name, self.imax = "RD6018", 18.1
        elif 60125 <= model_id <= 60129:
            self.name, self.imax = "RD6012P", 12.1
            self.v = self.p = self.i = 1000
            self.low_range_i = 10000
        elif 60120 <= model_id <= 60124:
            self.name, self.imax = "RD6012", 12.1
        elif model_id == 60066:
            self.name, self.imax, self.i = "RK6006", 6.1, 1000
        elif model_id == 60065:
            self.name, self.imax = "RD6006P", 6.1
            self.v, self.i, self.p = 1000, 10000, 1000
        elif 60060 <= model_id <= 60064:
            self.name, self.imax, self.i = "RD6006", 6.1, 1000
        else:
            raise TransportError(f"Unknown Riden model id {model_id}: its scale factors aren't "
                                 "known, so its readings would be wrong")


class RidenAdapter(Adapter):
    """Modbus RTU, address 1. Registers 4-20 are read in one go."""

    BAUD = 115200
    REPLY_S = 0.5
    ADDR = 1
    ID, FW, VSET, OVP, OCP, OUT, ISET = 0, 3, 8, 82, 83, 18, 9

    def __init__(self, link, definition=None):
        super().__init__(link, definition)
        self.s: RidenScale | None = None
        self._block: tuple[float, list] | None = None
        self._wh: tuple[float, list] | None = None

    def _xfer(self, pdu: bytes, want: int) -> bytes:
        frame = bytes([self.ADDR]) + pdu
        frame += struct.pack("<H", crc16(frame))
        problem = "no answer"
        for _ in range(3):
            self.link.reset_input()
            self.link.write(frame)
            r = self.recv(want)
            if len(r) >= 5 and r[0] == self.ADDR and r[1] == pdu[0] | 0x80:
                raise TransportError(f"Riden: Modbus error {r[2]}")
            if len(r) >= want and r[0] == self.ADDR and r[1] == pdu[0] \
                    and crc16(r[:want]) == 0:
                return r[:want]
            problem = "no answer" if not r else "garbled answer"
        raise TransportTimeout(f"Riden: {problem} (baud rate and address: 115200 and 1 as "
                               "delivered; see the supply's own menu)")

    def regs(self, start: int, n: int) -> list[int]:
        r = self._xfer(struct.pack(">BHH", 3, start, n), 5 + 2 * n)
        return list(struct.unpack(f">{n}H", r[3:3 + 2 * n]))

    def put(self, reg: int, value: int):
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"{value} doesn't fit the supply's register")
        self._xfer(struct.pack(">BHH", 6, reg, value), 8)
        self._block = None

    def _need(self) -> RidenScale:
        if self.s is None:
            self._id = self.regs(0, 4)
            self.s = RidenScale(self._id[0])
        return self.s

    def block(self) -> list[int]:
        """Registers 0 … 20 (0-3 are zeros: read once, at identify)."""
        now = time.monotonic()
        if self._block is None or now - self._block[0] > self.CACHE_S:
            self._block = (time.monotonic(), [0] * 4 + self.regs(4, 17))
        return self._block[1]

    def extra(self) -> list[int]:
        """Registers 32 … 41: battery mode, external sensor, Ah and Wh."""
        now = time.monotonic()
        if self._wh is None or now - self._wh[0] > self.CACHE_S:
            self._wh = (time.monotonic(), self.regs(32, 10))
        return self._wh[1]

    def iscale(self) -> int:
        s = self._need()
        if s.low_range_i and self.block()[20] == 0:
            return s.low_range_i
        return s.i

    def handle(self, cmd):
        head, arg = self.split(cmd)
        s = self._need()
        if head == "*IDN?":
            sn = self._id[1] << 16 | self._id[2]
            return f"Riden,{s.name},{sn:08d},{self._id[3] / 100:.2f}"
        if head == "VSET":
            return self.put(self.VSET, round(self._lim(arg, s.vmax, "V") * s.v))
        if head == "ISET":
            return self.put(self.ISET, round(self._lim(arg, s.imax, "A") * self.iscale()))
        if head == "OVP":
            return self.put(self.OVP, round(self._lim(arg, s.vmax + 1.5, "V") * s.v))
        if head == "OCP":
            return self.put(self.OCP, round(self._lim(arg, s.imax + 0.3, "A") * self.iscale()))
        if head == "OUT":
            return self.put(self.OUT, int(onoff(arg)))
        if head == "OVP?":
            return f"{self.regs(self.OVP, 1)[0] / s.v:.4f}"
        if head == "OCP?":
            return f"{self.regs(self.OCP, 1)[0] / self.iscale():.4f}"
        if head == "AH?" or head == "WH?":
            x = self.extra()
            hi, lo = (x[6], x[7]) if head == "AH?" else (x[8], x[9])
            return f"{(hi << 16 | lo) / 1000:.3f}"
        if head == "TEMP?":
            x = self.extra()
            v = -x[3] if x[2] else x[3]
            return BAD if abs(v) > 300 else str(v)      # no probe plugged in
        b = self.block()
        ans = {"VSET?": lambda: b[8] / s.v, "ISET?": lambda: b[9] / self.iscale(),
               "VOUT?": lambda: b[10] / s.v, "IOUT?": lambda: b[11] / self.iscale(),
               "POUT?": lambda: b[13] / s.p, "VIN?": lambda: b[14] / 100,
               "PROT?": lambda: b[16], "CC?": lambda: b[17], "OUT?": lambda: b[18],
               "INT?": lambda: -b[5] if b[4] else b[5]}.get(head)
        return self.unknown(cmd) if ans is None else f"{ans():.6g}"

    @staticmethod
    def _lim(arg: str, hi: float, unit: str) -> float:
        v = float(arg)
        if not 0 <= v <= hi:
            raise ValueError(f"{v:g} {unit} is outside this model's 0 … {hi:g} {unit}")
        return v


# ---------------------------------------------------------------- Korad / Tenma
class KoradAdapter(Adapter):
    """ASCII without terminators: the length of each answer is all there is.
    The supply needs about 80 ms between commands."""

    GAP = 0.08
    REPLY_S = 1.0
    LAST = 0.05                 # silence that ends an answer of unknown length

    def __init__(self, link, definition=None):
        super().__init__(link, definition)
        self._ready = 0.0
        self._stat: tuple[float, int] | None = None

    def _send(self, text: str):
        wait = self._ready - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.link.reset_input()
        self.link.write(text.encode("ascii"))
        self._ready = time.monotonic() + self.GAP

    def _ask(self, text: str, n: int) -> bytes:
        self._send(text)
        r = self.recv(n)
        if len(r) < n:
            raise TransportTimeout(f"No reply to {text!r}")
        return r[:n]

    def _status(self) -> int:
        now = time.monotonic()
        if self._stat is None or now - self._stat[0] > self.CACHE_S:
            self._stat = (now, self._ask("STATUS?", 1)[0])
        return self._stat[1]

    def _idn(self) -> str:
        self._send("*IDN?")
        buf = self.recv(1)
        while buf:
            more = self.recv(64, self.LAST)
            if not more:
                break
            buf += more
        text = buf.split(b"\0")[0].decode("latin1").strip()
        if not text:
            raise TransportTimeout("No reply to '*IDN?'")
        return text

    def handle(self, cmd):
        head, arg = self.split(cmd)
        if head == "*IDN?":
            return self._idn()
        if head in ("VSET?", "ISET?", "VOUT?", "IOUT?"):
            r = self._ask(f"{head[:-1]}1?", 5).decode("latin1")
            try:
                # an ISET? answer may have a stray sixth byte: dropped with the
                # next command's reset_input
                return f"{float(r):g}"
            except ValueError:
                raise TransportError(f"Korad: unexpected answer {r!r} to {head}") from None
        if head == "VSET":
            return self._send(f"VSET1:{float(arg):05.2f}")
        if head == "ISET":
            return self._send(f"ISET1:{float(arg):05.3f}")
        if head in ("OUT", "OVP", "OCP"):
            self._send(f"{head}{int(onoff(arg))}")
            self._stat = None
            return None
        bit = {"CV?": 0, "OCP?": 5, "OUT?": 6, "OVP?": 7}.get(head)
        if bit is None:
            return self.unknown(cmd)
        v = self._status() >> bit & 1
        return str(v)


# ---------------------------------------------------------------- Atorch PX-100
class Px100Adapter(Adapter):
    """B1 B2 cmd d1 d2 B6 -> 6F (commands) or CA CB d1 d2 d3 CE CF (queries)."""

    def __init__(self, link, definition=None):
        super().__init__(link, definition)
        self._vi = [0.0, 0.0]

    def _cmd(self, code: int, d1: int = 0, d2: int = 0):
        for _ in range(2):
            self.link.reset_input()
            self.link.write(bytes([0xB1, 0xB2, code, d1, d2, 0xB6]))
            if 0x6F in self.recv(1):
                return
        raise TransportTimeout("The load didn't confirm the command")

    def _ask(self, code: int) -> bytes:
        problem = "no answer"
        for _ in range(2):
            self.link.reset_input()
            self.link.write(bytes([0xB1, 0xB2, code, 0, 0, 0xB6]))
            r = self.recv(7)
            if len(r) >= 7 and r[:2] == b"\xca\xcb" and r[5:7] == b"\xce\xcf":
                return r[2:5]
            problem = "garbled answer" if r else "no answer"
        raise TransportTimeout(f"Load: {problem} (some DL24 firmware doesn't answer the PX-100 "
                               "commands)")

    def _int(self, code: int) -> int:
        return int.from_bytes(self._ask(code), "big")

    @staticmethod
    def _fixed(arg: str, hi: float, unit: str) -> tuple[int, int]:
        v = float(arg)
        if not 0 <= v <= hi:
            raise ValueError(f"{v:g} {unit} is outside 0 … {hi:g} {unit}")
        c = round(v * 100)
        return c // 100, c % 100

    def handle(self, cmd):
        head, arg = self.split(cmd)
        if head == "*IDN?":
            self._int(0x11)                  # it has no ID: just check that it answers
            return "Atorch,PX-100 protocol,,"
        if head == "INP":
            return self._cmd(0x01, int(onoff(arg)), 0)
        if head == "CURR":
            return self._cmd(0x02, *self._fixed(arg, 40, "A"))
        if head == "CUTOFF":
            return self._cmd(0x03, *self._fixed(arg, 200, "V"))
        if head == "TIMER":
            s = int(float(arg))
            if not 0 <= s <= 0xFFFF:
                raise ValueError("the timer is 0 … 65535 s")
            return self._cmd(0x04, s >> 8, s & 0xFF)
        if head == "RESET":
            return self._cmd(0x05)
        if head == "INP?":
            return str(int(self._int(0x10) != 0))
        if head == "VOLT?":
            self._vi[0] = self._int(0x11) / 1000
            return f"{self._vi[0]:.3f}"
        if head == "CURR?":
            self._vi[1] = self._int(0x12) / 1000
            return f"{self._vi[1]:.3f}"
        if head == "POW?":                   # not reported: from the last V and I
            return f"{self._vi[0] * self._vi[1]:.6g}"
        if head in ("TIME?", "TIMER?"):
            h, m, s = self._ask(0x13 if head == "TIME?" else 0x19)
            return str(h * 3600 + m * 60 + s)
        hit = {"AH?": (0x14, 1000), "WH?": (0x15, 1000), "TEMP?": (0x16, 1),
               "SET?": (0x17, 100), "CUTOFF?": (0x18, 100)}.get(head)
        if hit is None:
            return self.unknown(cmd)
        return f"{self._int(hit[0]) / hit[1]:.6g}"


# ---------------------------------------------------------------- Center 309 / K204
class CenterAdapter(Adapter):
    """'A' -> 45 bytes, 02 … 03, four thermocouple channels. Always °C."""

    CACHE_S = 0.15

    def __init__(self, link, definition=None):
        super().__init__(link, definition)
        self._last: tuple[float, list] | None = None

    @staticmethod
    def parse(p: bytes) -> list[float]:
        out = []
        celsius = bool(p[1] & 0x80)
        mode = p[1] >> 1 & 3             # 0 normal, 1 max, 2 min, 3 max-min
        over = {0: p[39], 1: p[41], 2: p[42]}.get(mode, 0)
        for i in range(4):
            v = struct.unpack(">h", p[7 + 2 * i:9 + 2 * i])[0]
            t = v / (1 if p[43] >> i & 1 else 10)
            if over >> i & 1:
                t = math.nan
            elif not celsius:
                t = (t - 32) / 1.8
            out.append(t)
        return out

    def _values(self) -> list[float]:
        now = time.monotonic()
        if self._last is None or now - self._last[0] > self.CACHE_S:
            for _ in range(2):
                self.link.reset_input()
                self.link.write(b"A")
                p = self.recv(45)
                at = p.find(b"\x02")
                if at > 0:
                    p = p[at:] + self.recv(at, 0.2)
                if len(p) >= 45 and p[0] == 2 and p[44] == 3:
                    self._last = (now, self.parse(p))
                    break
            else:
                raise TransportTimeout("No answer from the thermometer")
        return self._last[1]

    def handle(self, cmd):
        head, _ = self.split(cmd)
        if head == "*IDN?":
            self._values()
            return "Center,309 protocol (Voltcraft K204),,"
        m = re.fullmatch(r"T([1-4])\?", head)
        if not m:
            return self.unknown(cmd)
        v = self._values()[int(m.group(1)) - 1]
        return BAD if math.isnan(v) else f"{v:.2f}"


# ---------------------------------------------------------------- DS18B20 (Linux w1)
class W1Link:
    """/sys/bus/w1/devices: the kernel's w1_therm driver does the timing."""

    def __init__(self, base: str = "/sys/bus/w1/devices"):
        self.base = Path(base)

    def sensors(self) -> list[str]:
        try:
            return sorted(p.name for p in self.base.iterdir()
                          if re.match(r"(10|22|28|3b|42)-[0-9a-f]+$", p.name))
        except OSError:
            return []

    def read(self, name: str) -> float:
        """°C, or nan when the sensor didn't answer."""
        try:
            f = self.base / name / "temperature"
            if f.exists():
                return float(f.read_text(encoding="utf-8")) / 1000
            text = (self.base / name / "w1_slave").read_text(encoding="utf-8")
        except (OSError, ValueError):
            return math.nan
        m = re.search(r"t=(-?\d+)", text)
        return float(m.group(1)) / 1000 if m and "YES" in text else math.nan

    def close(self):
        pass


class W1Adapter(Adapter):
    def __init__(self, link, definition=None):
        super().__init__(link, definition)
        self.names = getattr(definition, "probes", None) or link.sensors()

    def handle(self, cmd):
        head, _ = self.split(cmd)
        if head == "*IDN?":
            if not self.names:
                raise TransportError(
                    "No 1-Wire temperature sensors found. On a Raspberry Pi add "
                    "dtoverlay=w1-gpio to /boot/firmware/config.txt and reboot; the sensor's "
                    "data wire goes to GPIO4 with a 4.7 kΩ pull-up.")
            return "1-Wire,DS18B20," + " ".join(self.names) + ","
        m = re.fullmatch(r"T(\d+)\?", head)
        if not m or not 1 <= int(m.group(1)) <= len(self.names):
            return self.unknown(cmd)
        v = self.link.read(self.names[int(m.group(1)) - 1])
        return BAD if math.isnan(v) else f"{v:.4f}"


# ---------------------------------------------------------------- SPI converters
class SpiLink:
    """/dev/spidevB.C through ioctl, so no extra Python package is needed."""

    MESSAGE, WR_MODE = 0x40206B00, 0x40016B01       # SPI_IOC_MESSAGE(1), SPI_IOC_WR_MODE

    def __init__(self, paths: list[str], mode: int, hz: int = 1_000_000):
        import fcntl
        self.paths, self.hz, self.fds = list(paths), hz, []
        for p in self.paths:
            try:
                fd = os.open(p, os.O_RDWR)
            except OSError as e:
                self.close()
                hint = " Enable SPI first: sudo raspi-config nonint do_spi 0." \
                    if isinstance(e, FileNotFoundError) else ""
                raise TransportError(f"Could not open {p}: {e.strerror}.{hint}") from None
            self.fds.append(fd)
            fcntl.ioctl(fd, self.WR_MODE, struct.pack("=B", mode))

    def xfer(self, k: int, tx: bytes) -> bytes:
        import ctypes
        import fcntl
        rx = ctypes.create_string_buffer(len(tx))
        buf = ctypes.create_string_buffer(bytes(tx), len(tx))
        msg = struct.pack("=QQIIHBBBBBB", ctypes.addressof(buf), ctypes.addressof(rx), len(tx),
                          self.hz, 0, 8, 0, 0, 0, 0, 0)
        fcntl.ioctl(self.fds[k], self.MESSAGE, msg)
        return rx.raw

    def close(self):
        for fd in self.fds:
            os.close(fd)
        self.fds = []


class SpiAdapter(Adapter):
    CHIP, MODE = "", 0

    def __init__(self, link, definition=None):
        super().__init__(link, definition)
        self.n = len(link.paths)

    def index(self, cmd: str, letter: str = "T") -> int:
        m = re.fullmatch(letter + r"(\d+)\?", cmd)
        if not m or not 1 <= int(m.group(1)) <= self.n:
            return -1
        return int(m.group(1)) - 1


class Max31855Adapter(SpiAdapter):
    CHIP, MODE = "MAX31855", 0

    @staticmethod
    def parse(raw: bytes) -> tuple[float, float]:
        """(thermocouple, cold junction) °C; the thermocouple is nan on a fault
        (open, or shorted to ground / supply)."""
        v = int.from_bytes(raw, "big", signed=True)
        cold = (v & 0xFFFF) >> 4
        cold = (cold - 4096 if cold & 0x800 else cold) / 16
        tc = math.nan if v & 0x10007 else (v >> 18) / 4
        return tc, cold

    def handle(self, cmd):
        head, _ = self.split(cmd)
        if head == "*IDN?":
            return f"Maxim,MAX31855,{' '.join(self.link.paths)},"
        k = self.index(head)
        cold = k < 0
        k = self.index(head, "COLD") if cold else k
        if k < 0:
            return self.unknown(cmd)
        raw = self.link.xfer(k, bytes(4))
        if raw in (bytes(4), b"\xff" * 4):       # nothing on the bus
            return BAD
        v = self.parse(raw)[1 if cold else 0]
        return BAD if math.isnan(v) else f"{v:.4f}"


def rtd_celsius(ohms: float, r0: float) -> float:
    """Callendar–Van Dusen (IEC 60751), inverted."""
    a, b, c = 3.9083e-3, -5.775e-7, -4.183e-12
    x = ohms / r0
    if x >= 1:
        return (-a + math.sqrt(a * a - 4 * b * (1 - x))) / (2 * b)
    t = (x - 1) / a
    for _ in range(20):          # Newton: the below-0 °C polynomial has no closed form
        f = 1 + a * t + b * t * t + c * (t - 100) * t ** 3 - x
        df = a + 2 * b * t + c * (4 * t ** 3 - 300 * t * t)
        t -= f / df
    return t


class Max31865Adapter(SpiAdapter):
    CHIP, MODE = "MAX31865", 1
    SETTLE = 0.075               # bias on + one 50 Hz conversion

    def __init__(self, link, definition=None):
        super().__init__(link, definition)
        self.rtd, self.rref, self.wires = 100.0, 430.0, 4
        self._auto = False

    def _start(self):
        cfg = 0x80 | 0x40 | 0x02 | 0x01 | (0x10 if self.wires == 3 else 0)
        for k in range(self.n):
            self.link.xfer(k, bytes([0x80, cfg]))
        time.sleep(self.SETTLE)
        self._auto = True

    def ohms(self, k: int) -> float:
        if not self._auto:
            self._start()
        raw = self.link.xfer(k, bytes([0x01, 0, 0]))
        code = raw[1] << 8 | raw[2]
        if code & 1:             # the fault bit (open RTD, …) latches: clear it
            self._auto = False
            return math.nan
        adc = code >> 1
        return math.nan if adc in (0, 0x7FFF) else adc / 32768 * self.rref

    def handle(self, cmd):
        head, arg = self.split(cmd)
        if head == "*IDN?":
            return f"Maxim,MAX31865,{' '.join(self.link.paths)},"
        if head in ("RTD", "RREF", "WIRES"):
            v = float(arg)
            if v <= 0:
                raise ValueError(f"{head} must be above 0")
            if head == "RTD":
                self.rtd = v
            elif head == "RREF":
                self.rref = v
            else:
                self.wires = int(v)
                self._auto = False
            return None
        if head in ("RTD?", "RREF?", "WIRES?"):
            return f"{getattr(self, head[:-1].lower()):g}"
        for letter, conv in (("T", lambda r: rtd_celsius(r, self.rtd)), ("R", lambda r: r)):
            k = self.index(head, letter)
            if k >= 0:
                r = self.ohms(k)
                v = math.nan if math.isnan(r) else conv(r)
                return BAD if math.isnan(v) else f"{v:.4f}"
        return self.unknown(cmd)


# ---------------------------------------------------------------- SCPI with receipts
class ScpiAckAdapter(Adapter):
    """Serial SCPI that also answers every command that isn't a query: East
    Tester ET44 / ET45 ("exec success", "cmd err", "execu err")."""

    REPLY_S = 2.0

    def _line(self, seconds: float | None = None) -> str:
        end = time.monotonic() + (max(self.REPLY_S, self.timeout_s) if seconds is None
                                  else seconds)
        buf = b""
        while time.monotonic() < end:
            chunk = self.link.read_available()
            buf += chunk
            if b"\n" in buf:
                line = buf.split(b"\n", 1)[0].decode("latin1").strip("\r \0")
                if line:
                    return line
                buf = buf.split(b"\n", 1)[1]
            elif not chunk:
                time.sleep(0.002)
        raise TransportTimeout("No reply")

    def handle(self, cmd):
        self.link.reset_input()
        self.link.write(cmd.encode("latin1") + b"\r\n")
        try:
            r = self._line()
        except TransportTimeout:
            raise TransportTimeout(f"No reply to {cmd!r}") from None
        if r.lower().endswith("err"):
            raise ValueError(f"the meter rejected {cmd!r} ({r})")
        return r if "?" in cmd else None


# ---------------------------------------------------------------- OWON SPM
class OwonSpmAdapter(ScpiAckAdapter):
    """OWON SPM supply + multimeter (Kiprim DC310Pro / DC605Pro): SCPI with
    silent sets and a bare "ERR" for a refused command. It sometimes drops a
    query right after a change, and hangs for seconds when commands come without
    a gap. Besides its own commands:

    ALL?   volts, amps, watts, protection (1 OVP, 2 OCP, 3 OTP), mode
           (0 standby, 1 CV, 2 CC, 3 failure), from MEAS:ALL:INFO?
    DMM?   the multimeter's value in V, A, ohm or F, from CONF:ALL?
    """

    BAUD = 115200
    REPLY_S = 0.6
    ERR_S = 0.15                # how long a refusal may take to come
    GAP = 0.02
    SETTLE = 0.9                # changing the multimeter's function
    TRIES = 3

    def __init__(self, link, definition=None):
        super().__init__(link, definition)
        self.func = ""          # the multimeter's function, as last asked for
        self.stale = False      # it was changed at the front panel since
        self.no_info = self.no_all = 0      # refusals in a row: firmware without it

    def _send(self, cmd: str):
        time.sleep(self.GAP)
        self.link.reset_input()
        self.link.write(cmd.encode("latin1") + b"\n")

    def ask(self, cmd: str) -> str:
        for _ in range(self.TRIES):
            self._send(cmd)
            try:
                r = self._line(self.REPLY_S)
            except TransportTimeout:
                continue
            if r.upper() == "ERR":
                raise ValueError(f"the instrument rejected {cmd!r}")
            return r
        raise TransportTimeout(f"No reply to {cmd!r}")

    def put(self, cmd: str):
        self._send(cmd)
        try:
            r = self._line(self.ERR_S)
        except TransportTimeout:
            return
        if r.upper() == "ERR":
            raise ValueError(f"the instrument rejected {cmd!r}")

    def off(self):
        """Until it says it is off."""
        why = "it still says ON"
        for _ in range(4):
            try:
                self.put("OUTP OFF")
                if self.ask("OUTP?").upper() in ("OFF", "0"):
                    return
            except (TransportTimeout, ValueError) as e:
                why = str(e)
        raise TransportError(f"The output could not be switched off ({why})")

    def set_function(self, cmd: str):
        want = cmd[5:].strip().upper()
        self.put(cmd)
        end = time.monotonic() + 2 * self.SETTLE + 0.5
        time.sleep(self.SETTLE)
        while True:
            self.func = self.ask("FUNC?").strip('"').upper()
            if self.func == want:
                self.stale = False
                return
            if time.monotonic() > end:
                raise ValueError(f"the multimeter stayed in {self.func}")
            time.sleep(0.1)

    def info(self) -> str:
        if self.no_info < 3:
            try:
                f = [x.strip() for x in self.ask("MEAS:ALL:INFO?").split(",")]
                if len(f) >= 7:
                    trips = [x.upper() in ("ON", "1") for x in f[3:6]]
                    prot = trips.index(True) + 1 if any(trips) else 0
                    self.no_info = 0
                    return ",".join([*f[:3], str(prot), f[6]])
                raise ValueError("a short answer")
            except ValueError:
                self.no_info += 1
        v, i = float(self.ask("MEAS:VOLT?")), float(self.ask("MEAS:CURR?"))
        return f"{v},{i},{v * i:.6g},{BAD},{BAD}"

    @staticmethod
    def value(text: str) -> str:
        """'+0.0007V', '2.4nF', '+000.26kOhm', 'OL' -> a number in V, A, ohm, F."""
        m = re.match(r"^([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s*(.*)$", text.strip())
        if not m:
            return BAD                    # OL, Open, ----
        unit = m.group(2).replace("\xc2", "")       # UTF-8's lead byte of the micro sign
        k = {"p": -12, "n": -9, "u": -6, "\xb5": -6, "m": -3, "k": 3, "K": 3,
             "M": 6, "G": 9}.get(unit[:1], 0) if len(unit) > 1 else 0
        return f"{float(m.group(1)) * 10 ** k:.9g}"

    def meter(self) -> str:
        func = value = None
        if self.no_all < 3:
            try:
                f = self.ask("CONF:ALL?").split(",")
                func, value = f[0], f[1]
                self.no_all = 0
            except (ValueError, IndexError):
                self.no_all += 1
        if value is None:
            func, _, value = self.ask("CONF?").partition(" ")
        func = func.strip().strip('"').upper()
        if self.func and func != self.func:
            # changed at the front panel: the unit isn't what the caller thinks
            self.stale = True
            return BAD
        return self.value(value)

    def handle(self, cmd):
        up = cmd.upper()
        if up == "ALL?":
            return self.info()
        if up == "DMM?":
            return self.meter()
        if up == "FUNC?":
            self.func = self.ask(cmd).strip('"').upper()
            self.stale = False
            return self.func
        if "?" in cmd:
            return self.ask(cmd)
        if up.startswith("FUNC:"):
            return self.set_function(cmd)
        if up in ("OUTP OFF", "OUTP 0"):
            return self.off()
        return self.put(cmd)


ADAPTERS = {"riden": RidenAdapter, "korad": KoradAdapter, "px100": Px100Adapter,
            "center": CenterAdapter, "w1": W1Adapter, "max31855": Max31855Adapter,
            "max31865": Max31865Adapter, "scpi-ack": ScpiAckAdapter,
            "owon-spm": OwonSpmAdapter}
SERIAL = {"riden", "korad", "px100", "center", "scpi-ack", "owon-spm"}
SPI = {"max31855", "max31865"}


# ---------------------------------------------------------------- resources
def parse_resource(resource: str) -> tuple[str, str, int]:
    """'RD60XX::/dev/ttyUSB0@9600' -> ('RD60XX', '/dev/ttyUSB0', 9600); the baud
    rate is 0 when it isn't given."""
    head, _, rest = resource.partition("::")
    m = re.match(r"^(.*)@(\d+)$", rest)
    if m:
        return head, m.group(1), int(m.group(2))
    return head, rest, 0


def probes(protocol: str, resource: str, w1: W1Link | None = None) -> list[str]:
    """The sensors a resource means, for protocols whose channels depend on
    what is wired up ([] = not that kind)."""
    sim = resource.upper().startswith("SIM")
    rest = "" if sim else parse_resource(resource)[1]
    if protocol == "w1":
        if sim:
            return ["28-000000000001", "28-000000000002"]
        named = [x.strip() for x in rest.split(",") if x.strip()]
        return named or (w1 or W1Link()).sensors()
    if protocol in SPI:
        if sim:
            return ["/dev/spidev0.0"]
        return [x.strip() for x in rest.split(",") if x.strip()] or ["/dev/spidev0.0"]
    return []


def for_resource(d, resource: str):
    """The definition with one channel per sensor of this resource. A reading
    whose name ends in '_n' stands for "one of these per sensor"."""
    if d is None or d.protocol not in {"w1"} | SPI:
        return d
    found = probes(d.protocol, resource)
    out = []
    for r in d.readings:
        if not r.name.endswith("_n"):
            out.append(r)
            continue
        for k, name in enumerate(found, start=1):
            short = name.rsplit("-", 1)[-1][-4:] if d.protocol == "w1" else \
                name.removeprefix("/dev/spidev")
            out.append(replace(r, name=f"{r.name[:-2]}{k}", label=f"{r.label} {k} ({short})",
                               query=r.query.replace("{n}", str(k)),
                               sim=r.sim.replace("{n}", str(k))))
    x = replace(d, readings=out)
    x.probes = found
    return x


def open_adapter(d, resource: str, baud: int = 0):
    """The transport for a definition that has a `protocol`."""
    cls = ADAPTERS[d.protocol]
    d = for_resource(d, resource)
    if resource.upper().startswith("SIM"):
        from ..sim.sadapters import sim_link
        a = cls(sim_link(d), d)
        for k, v in (("GAP", 0.0), ("SETTLE", 0.0)):
            if hasattr(a, k):
                setattr(a, k, v)
        return a
    if d.protocol in SERIAL:
        from ..transport import SerialStream, serial_path
        _, path, given = parse_resource(resource)
        if not path.startswith("/"):
            path = serial_path(resource)
        # the resource's own @baud, else the definition's, else the protocol's
        return cls(SerialStream(path, baud=given or d.baud or cls.BAUD, wait_s=0.005), d)
    if d.protocol == "w1":
        return cls(W1Link(), d)
    return cls(SpiLink(probes(d.protocol, resource), cls.MODE), d)


__all__ = ["ADAPTERS", "SERIAL", "SPI", "Adapter", "open_adapter", "for_resource",
           "parse_resource", "crc16", "rtd_celsius"]
