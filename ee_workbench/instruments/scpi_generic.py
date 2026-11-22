"""Generic SCPI instruments, described by a small TOML file instead of code.

A definition says what to send and how to read the answer:

    id = "34461A"                      # model key SCPI:34461A, simulator SIM::34461A
    name = "Keysight 34461A"
    type = "meter"                     # meter | load | supply | source | other
    idn = ["34461A", "34465A"]         # *IDN? containing one of these -> auto-detect
    channels = 1                       # >1: {ch} in a command means each channel
    init = ["*CLS"]                    # sent once after connecting
    error_query = "SYST:ERR?"          # read until it answers 0 / +0 ("" = none)
    off = ["INP OFF"]                  # what "All outputs OFF" sends ({ch}: each channel)

    [[reading]]                        # polled every cycle, one query each
    name = "value"                     # identifier: letters, digits, _
    query = "READ?"
    unit = "V"
    unit_from = "function"             # optional: the unit of that setting's option
    query_from = "function"            # optional: that option's `read` query instead

    [[setting]]
    name = "function"
    kind = "choice"                    # choice | number | bool
    query = "CONF?"                    # answer matched against each option's `reply`
    options = [{label = "DC V", set = "CONF:VOLT:DC AUTO", reply = '^"?VOLT ', unit = "V"}]

    [[setting]]
    name = "nplc"
    kind = "number"
    set = "VOLT:DC:NPLC {value}"       # {value}; bool: {on_off} -> ON/OFF, {1_0} -> 1/0
    query = "VOLT:DC:NPLC?"
    values = [0.02, 0.2, 1, 10, 100]   # optional: offered as a list

Built-in definitions are in instruments/scpi_defs/; your own go in
~/.config/ee-workbench/instruments/*.toml (same format; a file with the same
`id` as a built-in replaces it). `sim` fields only feed the simulator.
"""
from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..transport import TransportTimeout

TYPES = ("meter", "load", "supply", "source", "other")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
OVERLOAD = 9.0e37            # SCPI's "9.9E+37": overload / no reading


class DefinitionError(ValueError):
    pass


# ---------------------------------------------------------------- definitions
@dataclass
class Option:
    label: str
    set: str = ""
    reply: str = ""              # regex searched in the setting's query answer
    unit: str = ""
    read: str = ""               # reading query for this option (query_from)
    sim: str = ""                # what the simulator answers to the query

    def matches(self, answer: str) -> bool:
        if self.reply:
            return re.search(self.reply, answer, re.I) is not None
        return answer.strip().strip('"').lower() == self.label.lower()


@dataclass
class Setting:
    name: str
    label: str = ""
    kind: str = "number"         # number | choice | bool
    set: str = ""
    query: str = ""
    unit: str = ""
    min: float | None = None
    max: float | None = None
    values: list = field(default_factory=list)
    options: list = field(default_factory=list)       # [Option]
    output: bool = False         # the instrument's output / input switch
    ch: int = 0                  # channel this copy belongs to (0: not per channel)
    sim: str = ""                # simulator: initial value

    def option(self, label) -> Option | None:
        return next((o for o in self.options if o.label == label), None)


@dataclass
class Reading:
    name: str
    label: str = ""
    query: str = ""
    unit: str = ""
    unit_from: str = ""
    query_from: str = ""
    ch: int = 0
    sim: str = "0"               # simulator: expression in the settings, t, noise()


@dataclass
class Definition:
    id: str
    name: str
    type: str = "other"
    maker: str = ""
    idn: list = field(default_factory=list)
    channels: int = 1
    init: list = field(default_factory=list)
    error_query: str = "SYST:ERR?"
    off: list = field(default_factory=list)
    poll_ms: int = 0
    notes: str = ""
    tested: str = ""             # hardware it has been checked on ("" = not yet)
    readings: list = field(default_factory=list)      # [Reading], channels expanded
    settings: list = field(default_factory=list)      # [Setting], channels expanded
    path: str = ""               # file it came from
    builtin: bool = True

    @property
    def model_key(self) -> str:
        return "SCPI:" + self.id

    def setting(self, name: str) -> Setting | None:
        return next((s for s in self.settings if s.name == name), None)

    def reading(self, name: str) -> Reading | None:
        return next((r for r in self.readings if r.name == name), None)

    @property
    def has_outputs(self) -> bool:
        return bool(self.off) or any(s.output for s in self.settings)


def fill(cmd: str, value=None, ch: int = 0) -> str:
    """A command template with its fields filled in."""
    out = cmd.replace("{ch}", str(ch))
    if value is not None:
        if isinstance(value, bool):
            out = out.replace("{on_off}", "ON" if value else "OFF")
            out = out.replace("{1_0}", "1" if value else "0")
            out = out.replace("{value}", "1" if value else "0")
        else:
            out = out.replace("{value}", f"{value:.10g}" if isinstance(value, float)
                              else str(value))
    return out


def _expand(items: list, channels: int, make):
    """One copy per channel of anything whose commands use {ch}."""
    out = []
    for d in items:
        text = " ".join(str(v) for v in d.values())
        if channels > 1 and "{ch}" in text:
            for ch in range(1, channels + 1):
                x = make(d)
                x.name = f"{x.name}{ch}"
                x.label = f"{x.label or d['name']} CH{ch}"
                x.ch = ch
                out.append(x)
        else:
            x = make(d)
            x.label = x.label or d["name"]
            x.ch = 1 if channels <= 1 else 0
            out.append(x)
    return out


def parse_definition(data: dict, path: str = "", builtin: bool = True) -> Definition:
    """Check and build a definition; DefinitionError says what is wrong."""
    def need(cond, msg):
        if not cond:
            raise DefinitionError(f"{path or 'definition'}: {msg}")

    need(isinstance(data, dict), "not a TOML table")
    did = str(data.get("id", "")).strip()
    need(re.match(r"^[A-Za-z0-9_.+-]+$", did or "") is not None,
         "`id` is required: letters, digits, . _ + - (e.g. 34461A)")
    typ = str(data.get("type", "other"))
    need(typ in TYPES, f"`type` must be one of {', '.join(TYPES)}")
    try:
        channels = int(data.get("channels", 1))
    except (TypeError, ValueError):
        channels = 0
    need(1 <= channels <= 16, "`channels` must be 1..16")

    def str_list(key):
        v = data.get(key, [])
        need(isinstance(v, list) and all(isinstance(x, str) for x in v),
             f"`{key}` must be a list of strings")
        return v

    def make_option(o):
        need(isinstance(o, dict) and o.get("label"), "every option needs a `label`")
        extra = set(o) - {"label", "set", "reply", "unit", "read", "sim"}
        need(not extra, f"option {o.get('label')}: unknown key(s) {', '.join(sorted(extra))}")
        if o.get("reply"):
            try:
                re.compile(o["reply"])
            except re.error as e:
                raise DefinitionError(f"{path}: option {o['label']}: bad `reply` regex: {e}") \
                    from None
        return Option(**{k: str(v) for k, v in o.items()})

    def make_setting(d):
        need(isinstance(d, dict) and _IDENT.match(str(d.get("name", ""))),
             "every [[setting]] needs a `name` (letters, digits, _)")
        extra = set(d) - {"name", "label", "kind", "set", "query", "unit", "min", "max",
                          "values", "options", "output", "sim"}
        need(not extra, f"setting {d['name']}: unknown key(s) {', '.join(sorted(extra))}")
        kind = d.get("kind", "number")
        need(kind in ("number", "choice", "bool"),
             f"setting {d['name']}: `kind` must be number, choice or bool")
        opts = [make_option(o) for o in d.get("options", [])]
        need(kind != "choice" or opts, f"setting {d['name']}: a choice needs `options`")
        need(kind == "choice" or d.get("set"), f"setting {d['name']}: needs `set`")
        return Setting(name=d["name"], label=str(d.get("label", "")), kind=kind,
                       set=str(d.get("set", "")), query=str(d.get("query", "")),
                       unit=str(d.get("unit", "")),
                       min=None if d.get("min") is None else float(d["min"]),
                       max=None if d.get("max") is None else float(d["max"]),
                       values=[float(v) for v in d.get("values", [])], options=opts,
                       output=bool(d.get("output", False)), sim=str(d.get("sim", "")))

    def make_reading(d):
        need(isinstance(d, dict) and _IDENT.match(str(d.get("name", ""))),
             "every [[reading]] needs a `name` (letters, digits, _)")
        extra = set(d) - {"name", "label", "query", "unit", "unit_from", "query_from", "sim"}
        need(not extra, f"reading {d['name']}: unknown key(s) {', '.join(sorted(extra))}")
        need(d.get("query") or d.get("query_from"),
             f"reading {d['name']}: needs `query` (or `query_from`)")
        return Reading(name=d["name"], label=str(d.get("label", "")),
                       query=str(d.get("query", "")), unit=str(d.get("unit", "")),
                       unit_from=str(d.get("unit_from", "")),
                       query_from=str(d.get("query_from", "")), sim=str(d.get("sim", "0")))

    rd, st = data.get("reading", []), data.get("setting", [])
    need(isinstance(rd, list) and isinstance(st, list),
         "use [[reading]] and [[setting]] (arrays of tables)")
    need(rd, "at least one [[reading]] is needed")
    readings = _expand(rd, channels, make_reading)
    settings = _expand(st, channels, make_setting)
    names = [x.name for x in readings + settings]
    dup = {n for n in names if names.count(n) > 1}
    need(not dup, f"a reading and a setting can't share a name: {', '.join(sorted(dup))}")
    for r in readings:
        for key in ("unit_from", "query_from"):
            src = getattr(r, key)
            if src:
                base = [s for s in settings if s.name in (src, f"{src}{r.ch}")]
                need(base and base[0].kind == "choice",
                     f"reading {r.name}: `{key}` must name a choice setting")
    try:
        poll_ms = int(data.get("poll_ms", 0))
    except (TypeError, ValueError):
        poll_ms = 0
    return Definition(id=did, name=str(data.get("name", did)), type=typ,
                      maker=str(data.get("maker", "")), idn=str_list("idn"), channels=channels,
                      init=str_list("init"), error_query=str(data.get("error_query",
                                                                     "SYST:ERR?")),
                      off=str_list("off"), poll_ms=poll_ms, notes=str(data.get("notes", "")),
                      tested=str(data.get("tested", "")), readings=readings, settings=settings,
                      path=path, builtin=builtin)


def builtin_dir() -> Path:
    return Path(__file__).with_name("scpi_defs")


def user_dir() -> Path:
    from ..config import config_dir
    return config_dir() / "instruments"


_CACHE: dict | None = None
PROBLEMS: list[str] = []        # files that couldn't be loaded, for the UI


def definitions(reload: bool = False) -> dict[str, Definition]:
    """id -> Definition: the built-ins, then the user's (which win)."""
    global _CACHE
    if _CACHE is not None and not reload:
        return _CACHE
    PROBLEMS.clear()
    out: dict[str, Definition] = {}
    for folder, builtin in ((builtin_dir(), True), (user_dir(), False)):
        try:
            files = sorted(folder.glob("*.toml"))
        except OSError:
            files = []
        for f in files:
            try:
                d = parse_definition(tomllib.loads(f.read_text()), str(f), builtin)
            except (OSError, tomllib.TOMLDecodeError, DefinitionError, TypeError,
                    ValueError) as e:
                PROBLEMS.append(f"{f}: {e}" if str(f) not in str(e) else str(e))
                continue
            out[d.id] = d
    _CACHE = out
    return out


def definition_for(model: str) -> Definition | None:
    """'SCPI:34461A' (or '34461A') -> its definition."""
    key = model[5:] if model.upper().startswith("SCPI:") else model
    defs = definitions()
    return defs.get(key) or next((d for k, d in defs.items() if k.lower() == key.lower()),
                                 None)


def match_idn(idn: str) -> Definition | None:
    up = idn.upper()
    for d in definitions().values():
        if any(x and x.upper() in up for x in d.idn):
            return d
    return None


# ---------------------------------------------------------------- answers
def parse_number(text: str) -> float:
    m = _NUM.search(text or "")
    if not m:
        raise ValueError(f"no number in {text!r}")
    v = float(m.group(0))
    return math.nan if abs(v) >= OVERLOAD else v


def parse_bool(text: str) -> bool:
    t = (text or "").strip().strip('"').upper()
    if t in ("1", "ON", "+1", "TRUE"):
        return True
    if t in ("0", "OFF", "+0", "FALSE"):
        return False
    try:
        return float(t) != 0
    except ValueError:
        raise ValueError(f"not on/off: {text!r}") from None


# ---------------------------------------------------------------- driver
@dataclass
class ScpiCaps:
    idn: str = ""
    model: str = ""
    note: str = ""
    definition: Definition | None = None

    @property
    def model_key(self) -> str:
        return self.definition.model_key if self.definition else ""


@dataclass
class ScpiState:
    values: dict = field(default_factory=dict)      # setting name -> value (None: unknown)


@dataclass
class ScpiReading:
    t: float
    values: dict = field(default_factory=dict)      # reading name -> float (nan: overload)
    units: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


class ScpiDriver:
    kind = "scpi"
    family = "SCPI"

    def __init__(self, transport, definition: Definition):
        self.t = transport
        self.d = definition
        self.log = None
        self.values: dict = {}
        self.caps = ScpiCaps(model=definition.id, definition=definition)

    def _log(self, s):
        if self.log:
            self.log(s)

    def write(self, cmd: str):
        self._log("> " + cmd)
        self.t.write(cmd)

    def query(self, cmd: str) -> str:
        self._log("> " + cmd)
        r = self.t.query(cmd)
        self._log("< " + r)
        return r

    @property
    def cfg(self) -> ScpiState:
        return ScpiState(dict(self.values))

    def errors(self) -> list[str]:
        q = self.d.error_query
        if not q:
            return []
        out = []
        for _ in range(10):
            try:
                with self.t.temp_timeout(2.0):
                    e = self.query(q)
            except TransportTimeout:
                break
            code = e.split(",", 1)[0].strip().strip('"')
            try:
                if int(float(code)) == 0:
                    break
            except ValueError:
                if not e.strip() or "no error" in e.lower():
                    break
            out.append(e.strip())
        return out

    def identify(self) -> ScpiCaps:
        idn = ""
        try:
            with self.t.temp_timeout(3.0):
                idn = self.query("*IDN?")
        except TransportTimeout:
            idn = "(no answer to *IDN?)"
        for c in self.d.init:
            self.write(c)
        self.errors()                 # start with an empty error queue
        self.read_settings()
        note = self.d.notes
        if not self.d.tested:
            note = ("Generic SCPI definition (not yet checked on this model by the project). "
                    + note).strip()
        self.caps = ScpiCaps(idn=idn, model=self.d.id, note=note, definition=self.d)
        return self.caps

    # ---- settings ------------------------------------------------------------
    def read_setting(self, s: Setting):
        if not s.query:
            return self.values.get(s.name)
        ans = self.query(fill(s.query, ch=s.ch))
        if s.kind == "bool":
            return parse_bool(ans)
        if s.kind == "number":
            return parse_number(ans)
        opt = next((o for o in s.options if o.matches(ans)), None)
        return opt.label if opt else None

    def read_settings(self):
        for s in self.d.settings:
            try:
                self.values[s.name] = self.read_setting(s)
            except (TransportTimeout, ValueError):
                self.values.setdefault(s.name, None)

    def set(self, name: str, value):
        s = self.d.setting(name)
        if s is None:
            raise ValueError(f"{self.d.id} has no setting '{name}' (it has: "
                             + ", ".join(x.name for x in self.d.settings) + ")")
        if s.kind == "choice":
            opt = s.option(value) or next((o for o in s.options
                                           if o.label.lower() == str(value).lower()), None)
            if opt is None:
                raise ValueError(f"{name} must be one of "
                                 + ", ".join(o.label for o in s.options))
            self.write(fill(opt.set, ch=s.ch))
            value = opt.label
        elif s.kind == "bool":
            if isinstance(value, str):
                value = parse_bool(value)
            value = bool(value)
            self.write(fill(s.set, value, s.ch))
        else:
            value = float(value)
            if (s.min is not None and value < s.min) or (s.max is not None and value > s.max):
                raise ValueError(f"{name} must be {s.min:g} … {s.max:g} {s.unit}".strip())
            self.write(fill(s.set, value, s.ch))
        self.values[name] = value
        if s.query:
            try:
                self.values[name] = self.read_setting(s)
            except (TransportTimeout, ValueError):
                pass

    def set_output(self, on: bool):
        """All outputs OFF (on=False) sends the definition's `off` commands."""
        if on:
            for s in self.d.settings:
                if s.output:
                    self.set(s.name, True)
            return
        for c in self.d.off:
            if "{ch}" in c:
                for ch in range(1, self.d.channels + 1):
                    self.write(fill(c, ch=ch))
            else:
                self.write(c)
        for s in self.d.settings:
            if s.output:
                self.values[s.name] = False

    # ---- readings ----------------------------------------------------------------
    def _opt(self, base: str, ch: int) -> Option | None:
        s = self.d.setting(base) or self.d.setting(f"{base}{ch}")
        return s.option(self.values.get(s.name)) if s else None

    def unit(self, r: Reading) -> str:
        if r.unit_from:
            o = self._opt(r.unit_from, r.ch)
            if o is not None and o.unit:
                return o.unit
        return r.unit

    def poll(self, t: float, _cfg=None) -> ScpiReading:
        out = ScpiReading(t)
        for r in self.d.readings:
            q = r.query
            if r.query_from:
                o = self._opt(r.query_from, r.ch)
                q = (o.read if o is not None and o.read else q)
            if not q:
                continue
            out.values[r.name] = parse_number(self.query(fill(q, ch=r.ch)))
            out.units[r.name] = self.unit(r)
        return out

    def close(self):
        pass


def env_names(d: Definition) -> list[str]:
    return [x.name for x in d.readings + d.settings]


def describe_definition(d: Definition) -> str:
    """One paragraph for the docs / the connect dialog."""
    parts = [f"{d.name} ({d.type}"]
    if d.channels > 1:
        parts[0] += f", {d.channels} channels"
    parts[0] += ")"
    parts.append("readings: " + ", ".join(r.name for r in d.readings))
    if d.settings:
        parts.append("settings: " + ", ".join(s.name for s in d.settings))
    return "; ".join(parts)


def self_test(d: Definition) -> list[str]:
    """Run a definition against its own simulator: every setting set and read
    back, every option, a poll, the error queue and the off commands. Returns
    what went wrong ([] = fine). It checks the file is consistent, not that the
    commands are right for the real instrument."""
    from ..sim.sscpi import SimScpi
    probs: list[str] = []
    drv = ScpiDriver(SimScpi(d), d)

    def step(what, fn):
        try:
            fn()
        except Exception as e:      # noqa: BLE001 - report every kind of failure
            probs.append(f"{what}: {e}")

    step("identify", drv.identify)
    step("read settings", drv.read_settings)
    step("poll", lambda: drv.poll(0.0))
    for st in d.settings:
        if st.kind == "choice":
            for o in st.options:
                step(f"{st.name} = {o.label}", lambda st=st, o=o: drv.set(st.name, o.label))
                if not st.query:
                    continue
                try:
                    back = drv.read_setting(st)
                except (TransportTimeout, ValueError) as e:
                    probs.append(f"{st.name}: set {o.label!r}, reading it back failed: {e}")
                    continue
                if back != o.label:
                    probs.append(f"{st.name}: set {o.label!r} but read back {back!r} "
                                 "(check the options' `reply` patterns)")
        else:
            if st.kind == "bool":
                v = True
            else:
                v = st.values[0] if st.values else (st.min or 0) + 0.5
                if st.max is not None:
                    v = min(v, st.max)
            step(f"{st.name} = {v}", lambda st=st, v=v: drv.set(st.name, v))
            if st.query:
                try:
                    back = drv.read_setting(st)
                    if (back != v) if st.kind == "bool" else abs(float(back) - v) > 1e-6 * max(
                            1.0, abs(v)):
                        probs.append(f"{st.name}: set {v} but read back {back!r}")
                except (TransportTimeout, ValueError) as e:
                    probs.append(f"{st.name}: reading it back failed: {e}")
    step("poll after setting", lambda: drv.poll(0.0))
    step("error queue", lambda: probs.extend(f"instrument error: {e}" for e in drv.errors()))
    step("outputs off", lambda: drv.set_output(False))
    return probs


__all__ = ["Definition", "DefinitionError", "ScpiDriver", "ScpiReading", "ScpiState",
           "ScpiCaps", "definitions", "definition_for", "match_idn", "parse_definition",
           "fill", "user_dir", "builtin_dir", "PROBLEMS", "env_names"]
