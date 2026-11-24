"""Driver for OWON XDM bench multimeters: XDM1041, XDM1241 (battery) and XDM2041.

USB (a CH340 serial chip inside) or RS232, 115200 8N1 by default, short ASCII
commands ending in LF, replies ending in CR LF. It looks like SCPI but is its
own flat command set (no READ?, no SYST:ERR?, no *OPC?):

    FUNC1?  -> "VOLT AC"     MEAS1? -> -2.758856E-05     RANGE? -> 50 mV
    AUTO?   -> 1             RATE?  -> S                 MEAS2:SHOW? -> NONe

Sources: OWON's XDM2041 programming manual, TheHWcave's tested XDM1041 command
list (firmware V1.2.0 / V2.1.2), logs of a V4.0.0 and a V4.2.0 XDM1041 and of a
V4.3.0 XDM1241. Firmware quirks handled here:
  - Some versions answer commands (not only queries) with lines of OK / nOK.
    They are dropped, and input is emptied before every query.
  - Queries that don't apply get no answer at all (RANGE? in diode and
    continuity). After a timeout a late answer must not be taken for the next
    one, so the link is resynchronised with *IDN?.
  - K-type temperature: no answers for ~3 s every 10 minutes.
  - Ω, µ and °C are GBK bytes, not UTF-8.
  - RATE: the manual says F/M/L, meters use F/M/S.

Only documented commands are sent, and the console refuses others: owners
report an undocumented one (INT:START) that starts a factory calibration.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace

from ..transport import TransportTimeout
from .base import DmmCaps, DmmReading
from .bk393 import OVERLOAD, split_unit

BAUD = 115200
OVER = 1e9                  # readings at or above this are "overload" (libsigrok's limit)
RATES = {"Slow": "S", "Medium": "M", "Fast": "F"}

# label -> (FUNC1? answer, CONF command, unit, ranges in RANGE n order)
_DCV = ("50 mV", "500 mV", "5 V", "50 V", "500 V", "1000 V")
_ACV = ("500 mV", "5 V", "50 V", "500 V", "750 V")
_AMP = ("500 µA", "5 mA", "50 mA", "500 mA", "5 A", "10 A")
_OHM = ("500 Ω", "5 kΩ", "50 kΩ", "500 kΩ", "5 MΩ", "50 MΩ")
_CAP = ("50 nF", "500 nF", "5 µF", "50 µF", "500 µF", "5 mF", "50 mF")
FUNCTIONS = {
    "DCV": ("VOLT", "CONF:VOLT:DC", "V", _DCV),
    "ACV": ("VOLT AC", "CONF:VOLT:AC", "V", _ACV),
    "DCI": ("CURR", "CONF:CURR:DC", "A", _AMP),
    "ACI": ("CURR AC", "CONF:CURR:AC", "A", _AMP),
    "2W Ω": ("RES", "CONF:RES", "Ω", _OHM),
    "4W Ω": ("FRES", "CONF:FRES", "Ω", _OHM[:3]),
    "Continuity": ("CONT", "CONF:CONT", "Ω", ()),
    "Diode": ("DIOD", "CONF:DIOD", "V", ()),
    "Capacitance": ("CAP", "CONF:CAP", "F", _CAP),
    "Frequency": ("FREQ", "CONF:FREQ", "Hz", ()),
    "Period": ("PER", "CONF:PER", "s", ()),
    "Temperature": ("TEMP", "CONF:TEMP:RTD", "", ()),
}
BY_REPLY = {v[0]: k for k, v in FUNCTIONS.items()}
SENSORS = {"K type": "KITS90", "PT100": "PT100"}

# AUTO stays in the group of ranges it is given: V or mV, A or mA.
AUTO = "AUTO"
AUTO_LOW = {"DCV": "AUTO (mV)", "ACV": "AUTO (mV)", "DCI": "AUTO (mA)", "ACI": "AUTO (mA)"}


def ranges_for(function: str) -> list[str]:
    if function == "Temperature":
        return list(SENSORS)
    ranges = FUNCTIONS[function][3]
    if not ranges:
        return [AUTO]
    low = AUTO_LOW.get(function)
    return [AUTO, *([low] if low else []), *ranges]


@dataclass
class XdmConfig:
    function: str = "DCV"
    range: str = AUTO          # for Temperature: the sensor
    rate: str = "Slow"
    dual: bool = False         # frequency on the second display (ACV, ACI)
    null: bool = False         # (shared with the 3456A panel; not used)


_SYMBOLS = ((b"\xa6\xb8", "Ω"), (b"\xa6\xcc", "µ"), (b"\xaa\xcc", "µ"),
            (b"\xa1\xe6", "°C"), (b"\xa8\x48", "°F"), (b"\xa1\xe3", "°"))


def decode(raw: bytes) -> str:
    for a, b in _SYMBOLS:
        raw = raw.replace(a, b.encode())
    return raw.decode("utf-8", errors="replace").strip()


def norm_range(text: str) -> str:
    """'50 mV', '5KΩ', '500uA' -> a label of the range tables ('' = not one)."""
    m = re.match(r"^\s*(\d+)\s*([a-zA-ZµμΩ]+)\s*$", text.strip().strip('"'))
    if not m:
        return ""
    unit = m.group(2).replace("μ", "µ").replace("Ohm", "Ω").replace("ohm", "Ω")
    if unit[0] == "u":
        unit = "µ" + unit[1:]
    if unit[0] == "K":
        unit = "k" + unit[1:]
    return f"{m.group(1)} {unit}"


def scale(unit: str, value: float, range_label: str) -> tuple[str, float, int]:
    """(native unit, multiplier, decimals) to show `value` like the meter does
    (55,000 counts). Without a range, or one it doesn't fit, follow the value."""
    m = re.match(r"(\d+) (\S+)$", range_label)
    if m:
        native = m.group(2)
        base, mult = split_unit(native)
        if base == unit and abs(value) <= int(m.group(1)) * mult * 1.2:
            return native, mult, max(0, 5 - len(m.group(1)))
    if unit not in ("V", "A", "Ω", "F", "Hz", "s"):
        return unit, 1.0, 1 if unit.startswith("°") or unit == "K" else 4
    for p, mult in (("M", 1e6), ("k", 1e3), ("", 1.0), ("m", 1e-3), ("µ", 1e-6), ("n", 1e-9)):
        if abs(value) >= mult:
            break
    if not value:
        p, mult = "", 1.0
    digits = len(str(int(abs(value) / mult)))
    return p + unit, mult, max(0, 5 - digits)


# What the console may send: the first word of the documented measuring commands.
_ROOTS = re.compile(
    r"^:?(?:SENSE?:)?(\*IDN\?|MEAS\w*|CONF\w*|FUNC\w*|TEMP\w*|CONT|CONTINUITY|CALC|CALCULATE|"
    r"VOLT\w*|CURR\w*|F?RES|F?RESISTANCE|CAP\w*|FREQ\w*|PER\w*|DIOD\w*|AUTO|RATE|RANGE\d?|"
    r"BEEP\w*|SYST\w*:(BEEP\w*|DATE|TIME|LOC\w*|REM\w*))(?=$|[\s:?])", re.I)


def allowed(cmd: str) -> bool:
    return not re.search(r"[;\r\n]", cmd) and _ROOTS.match(cmd.strip()) is not None


class OwonXDM:
    kind = "dmm"
    family = "XDM"
    TIMEOUT = 5.0              # K type: silent for ~3 s every 10 minutes
    OPTIONAL = 1.0             # queries some functions / firmwares don't answer
    GAP = 0.1                  # between commands: no *OPC? to wait on
    SETTLE = 0.4               # after a change the first reading may be the old one
    SLOW_S = 1.0               # how often range, rate and second display are asked
    HELP = ("No answer from the meter. Check the USB cable and port, and that Utility > "
            "Port > Baud on the meter is 115200 (the default).")

    def __init__(self, transport):
        self.t = transport
        self.caps = DmmCaps()
        self.cfg = XdmConfig()
        self.log = None
        self._rx = bytearray()
        self._silent: set[tuple] = set()     # (function, query) that get no answer
        self._range = ""                     # last RANGE? as a label
        self._unit_t = ""                    # temperature unit on the display
        self._second = ""
        self._slow_at = 0.0

    # ---- link ----------------------------------------------------------------
    def _log(self, s: str):
        if self.log:
            self.log(s)

    def _line(self, timeout_s: float) -> str:
        """The next line that isn't an OK / nOK acknowledgement."""
        end = time.monotonic() + timeout_s
        while True:
            self._rx += self.t.read_available()
            while b"\n" in self._rx:
                raw, _, rest = bytes(self._rx).partition(b"\n")
                self._rx = bytearray(rest)
                text = decode(raw.replace(b"\r", b"").lstrip(b"\x00"))
                if text and text not in ("OK", "nOK", "NOK"):
                    return text
            if time.monotonic() > end:
                raise TransportTimeout("No reply")
            time.sleep(0.002)

    def _empty(self):
        self.t.reset_input()
        self._rx.clear()

    def write(self, cmd: str):
        self._log("> " + cmd)
        self.t.write((cmd + "\n").encode("ascii", errors="replace"))

    def query(self, cmd: str, timeout_s: float | None = None) -> str:
        self._empty()
        self.write(cmd)
        try:
            r = self._line(self.TIMEOUT if timeout_s is None else timeout_s)
        except TransportTimeout:
            raise TransportTimeout(f"No reply to {cmd!r}. {self.HELP}") from None
        self._log("< " + r)
        return r

    def _resync(self):
        """Throw away answers that come late: read up to the one to *IDN?."""
        self._empty()
        self.write("*IDN?")
        end = time.monotonic() + self.TIMEOUT
        while time.monotonic() < end:
            if self._line(end - time.monotonic()).count(",") >= 3:
                return
        raise TransportTimeout(self.HELP)

    def _ask(self, cmd: str, fn: str) -> str:
        """A query that may go unanswered in function `fn` ('' then)."""
        if (fn, cmd) in self._silent:
            return ""
        try:
            return self.query(cmd, self.OPTIONAL)
        except TransportTimeout:
            t0 = time.monotonic()
            self._resync()
            if time.monotonic() - t0 < 0.5:     # not the K-type pause: it ignored us
                self._silent.add((fn, cmd))
            return ""

    # ---- identify / state ----------------------------------------------------
    def identify(self) -> DmmCaps:
        self._empty()
        idn = ""
        for _ in range(2):              # bytes left over in its buffer spoil the first
            try:
                idn = self.query("*IDN?", 2.0)
                break
            except TransportTimeout:
                continue
        if not idn:
            raise TimeoutError(self.HELP)
        f = [x.strip() for x in idn.split(",")]
        model = f[1] if len(f) > 1 else idn
        if re.match(r"XDM3", model, re.I):
            raise RuntimeError(f"{model} uses a different command set (standard SCPI). Choose "
                               "the generic “Any SCPI meter” definition for it.")
        if len(f) < 4:
            raise RuntimeError(f"This doesn't look like an OWON XDM meter: {idn!r}")
        note = "" if re.match(r"XDM[12]\d", model, re.I) else \
            "Not a known OWON XDM model; treated as an XDM1041."
        self.caps = DmmCaps(idn=idn, model=model, note=note)
        self.read_state()
        return self.caps

    def functions(self) -> list[str]:
        four = re.match(r"XDM2", self.caps.model, re.I)
        return [k for k in FUNCTIONS if four or k != "4W Ω"]

    def _function(self) -> str:
        raw = " ".join(self.query("FUNC1?").strip('"').upper().split())
        if raw not in BY_REPLY:
            raise RuntimeError(f"Unexpected answer to FUNC1?: {raw!r}")
        return BY_REPLY[raw]

    def _read_slow(self, fn: str):
        """Range, rate and second display (asked about once a second)."""
        self._slow_at = time.monotonic()
        cfg = replace(self.cfg, function=fn, dual=False)
        self._range, self._second = "", ""
        rate = self._ask("RATE?", fn).upper()[:1]
        cfg.rate = {"S": "Slow", "L": "Slow", "M": "Medium", "F": "Fast"}.get(rate, cfg.rate)
        if fn == "Temperature":
            cfg.range = "PT100" if "PT100" in self._ask("RANGE?", fn).upper() else "K type"
            shown = self._ask("MEAS1:SHOW?", fn)
            m = re.search(r"(°C|°F|K)\s*$", shown)
            if m:
                self._unit_t = m.group(1)
        elif FUNCTIONS[fn][3]:
            self._range = norm_range(self._ask("RANGE?", fn))
            if self._ask("AUTO?", fn).startswith("1"):
                low = split_unit(self._range.partition(" ")[2])[1] < 1 if self._range else False
                cfg.range = AUTO_LOW[fn] if low and fn in AUTO_LOW else AUTO
            elif self._range in FUNCTIONS[fn][3]:
                cfg.range = self._range
        else:
            cfg.range = AUTO
        if fn in ("ACV", "ACI") and "FREQ" in self._ask("FUNC2?", fn).upper():
            cfg.dual = True
            # as displayed: MEAS2? is wrongly scaled in older firmware
            self._second = self._ask("MEAS2:SHOW?", fn)
        self.cfg = cfg

    def read_state(self) -> XdmConfig:
        self._read_slow(self._function())
        return self.cfg

    def apply(self, want: XdmConfig):
        fn = want.function
        if fn not in self.functions():
            raise ValueError(f"The {self.caps.model} has no function {fn!r}")
        if want.range not in ranges_for(fn):
            raise ValueError(f"{fn} has no range {want.range!r}")
        have = replace(self.cfg)
        conf, ranges = FUNCTIONS[fn][1], FUNCTIONS[fn][3]
        if fn == "Temperature":
            if (fn, want.range) != (have.function, have.range):
                self.write(f"{conf} {SENSORS[want.range]}")
        elif fn != have.function or want.range != have.range:
            if fn != have.function or want.range == AUTO:
                self.write(f"{conf} AUTO" if ranges else conf)
                time.sleep(self.GAP)
            if want.range in ranges:
                self.write(f"RANGE {ranges.index(want.range) + 1}")
            elif want.range != AUTO:            # AUTO within the mV / mA ranges
                self.write("RANGE 1")
                time.sleep(self.GAP)
                self.write("AUTO")
        if want.rate != have.rate or fn != have.function:
            time.sleep(self.GAP)
            self.write(f"RATE {RATES[want.rate]}")
        if fn in ("ACV", "ACI") and (want.dual != have.dual or fn != have.function):
            time.sleep(self.GAP)
            self.write('FUNC2 "FREQ"' if want.dual else 'FUNC2 "NONe"')
        time.sleep(self.SETTLE)
        self.read_state()

    def errors(self) -> list[str]:
        return []

    # ---- reading ---------------------------------------------------------------
    def poll(self, t: float, _cfg=None) -> DmmReading:
        t0 = time.monotonic()
        for _ in range(4):
            fn = self._function()
            if fn != self.cfg.function or time.monotonic() - self._slow_at > self.SLOW_S:
                if fn != self.cfg.function:     # its knob was turned: skip the old reading
                    time.sleep(self.SETTLE)
                self._read_slow(fn)
            # The combined form MEAS? answers two values when dual is on.
            raw = self.query("MEAS1?")
            if self._function() == fn:          # still the same: the unit is right
                break
        return self._reading(t + time.monotonic() - t0, fn, raw)

    def _reading(self, t: float, fn: str, raw: str) -> DmmReading:
        unit = self._unit_t if fn == "Temperature" else FUNCTIONS[fn][2]
        auto = self.cfg.range in (AUTO, AUTO_LOW.get(fn)) and bool(FUNCTIONS[fn][3])
        r = DmmReading(t=t, value=float("nan"), unit=unit, function=fn, native=unit,
                       secondary=self._second, cfg=replace(self.cfg),
                       flags=["AUTO"] if auto else [])
        try:
            v = float(raw)
        except ValueError:
            if re.search(r"over|\bOL\b", raw, re.I):
                v = OVERLOAD
            else:
                r.text = raw
                return r
        if abs(v) >= OVER:
            r.overload = True
            r.value = OVERLOAD if v > 0 else -OVERLOAD
            return r
        r.value = v
        if fn in ("Continuity", "Diode"):       # fixed range, never in mΩ / mV
            r.decimals = 2 if fn == "Continuity" else 4
        else:
            r.native, r.mult, r.decimals = scale(unit, v, self._range)
        return r

    def raw(self, line: str, read: bool = True) -> str:
        cmd = line.strip()
        if not allowed(cmd):
            raise ValueError("Only documented measuring commands can be sent, one per line: "
                             "owners report undocumented ones that start a calibration.")
        self._slow_at = 0.0                    # it may have changed something
        if not (read or cmd.endswith("?")):
            self.write(cmd)
            return ""
        try:
            return self.query(cmd, 2.0)
        except TransportTimeout:
            self._resync()
            return "(no answer)"

    def close(self):
        try:
            self.write("SYST:LOC")             # leave the front panel unlocked
        except Exception:                      # noqa: BLE001 - unplugged
            pass
