"""The sequencer: run a test across several instruments.

A sequence is a list of steps (Set / Wait / Measure / Capture / Note), run
once, N times, or as a sweep of a variable `x`; every pass makes one row of
results. Step values are expressions, so a sweep can say `x`, `x / 2` or
`3.3 + 0.1 * i`. The Script tab runs Python against the same `bench` object:

    for v in (3.0, 3.3, 3.6):
        bench.set("66321D", "voltage", v)
        bench.wait(0.5)
        i = bench.measure("PPK2", "current", 1.0)
        bench.expect("sleep current", i, hi=10e-6)     # pass / fail
        bench.record(v=v, i=i)

A Measure step can carry limits (min / max, either may be blank); a pass
whose measurements are all inside them is PASS, else FAIL, and with
"stop on first fail" the run ends there (outputs off).

This module knows nothing of Qt: the window hands it one `Inst` per open
instrument tab, which does the actual work on that instrument's own I/O
thread (so a sequence never races the tab's polling).
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass, field

from .accuracy import marginal

# what Set / Measure can do with each kind of instrument (shown in the step editor);
# a generic SCPI tab and a scope tab list their own (Inst.set_names, …)
SET_WHAT = {
    "supply": ["voltage", "current", "output", "ovp", "ocp", "clear_protection",
               "sense_protect"],
    "ppk2": ["voltage", "output"],
    "dmm": [],
    "logic": [],
    "gpio": [],            # the pins configured in the GPIO tab (filled in by the window)
    "fgen": ["function", "frequency", "amplitude", "offset", "duty", "output"],
}
MEASURE_WHAT = {
    "supply": ["voltage", "current", "power", "dvm"],
    "ppk2": ["current", "peak", "charge"],
    "dmm": ["value"],
    "logic": [],
    "gpio": [],
    "fgen": [],
}
# what a Profile step can vary
PROFILE_WHAT = {"supply": ["voltage", "current"], "ppk2": ["voltage"],
                "fgen": ["frequency", "amplitude", "offset", "duty"]}
UNITS = {"voltage": "V", "current": "A", "power": "W", "dvm": "V", "peak": "A", "charge": "C",
         "ovp": "V", "frequency": "Hz", "amplitude": "Vpp", "offset": "V", "duty": "%"}
BLOCKS = ("For", "Repeat", "Retry", "If")        # each ends with an End step
ACTIONS = ["Set", "Wait", "Measure", "Capture", "Profile", "Wait for", "Send", "Command", "Screenshot",
           "Note",
           "For", "Repeat", "Retry", "If", "Else", "End"]
FGEN_FUNCS = ("sine", "square", "ramp", "pulse", "noise", "dc", "arb")
SAFE = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
SAFE.update(abs=abs, min=min, max=max, round=round)


class Stopped(Exception):
    """The sequence was stopped (Stop button, Esc)."""


class SequenceError(Exception):
    pass


class LimitFailed(SequenceError):
    """A limit failed and the plan says to stop on the first failure."""


def _sense_protect(drv, on: bool):
    if not getattr(getattr(drv, "caps", None), "has_sense_protect", False) \
            or not hasattr(drv, "set_sense_protect"):
        raise SequenceError("this supply has no open-sense-lead detection (the 66311B / "
                            "66321B / 66321D do; the 6632A family doesn't)")
    drv.set_sense_protect(on)


def value_of(text, env: dict):
    """A step value: a number, on/off, or an expression in x, i, n and math."""
    if isinstance(text, (int, float)):
        return text
    s = str(text).strip()
    low = s.lower()
    if low in ("on", "true", "yes"):
        return 1.0
    if low in ("off", "false", "no"):
        return 0.0
    if not s:
        return 0.0
    try:
        return float(eval(s, {"__builtins__": {}}, {**SAFE, **env}))   # noqa: S307 - the user's own bench
    except Exception as e:     # noqa: BLE001
        raise SequenceError(f"Can't work out the value '{s}': {e}") from None


@dataclass
class Step:
    action: str = "Set"
    target: str = ""            # instrument name (as listed by the bench)
    what: str = ""              # setting / quantity
    value: str = ""             # value, expression, seconds or note text
    label: str = ""             # results column for Measure / Capture
    lo: str = ""                # Measure / Capture limits (blank: none); expressions
    hi: str = ""


@dataclass
class Plan:
    steps: list = field(default_factory=list)
    loop: str = "once"          # once | repeat | sweep
    count: int = 1
    start: float = 0.0
    stop: float = 1.0
    step: float = 0.1
    off_at_end: bool = True
    stop_on_fail: bool = False
    guard: str = "off"          # off | marginal | strict: readings within ± of a limit

    def passes(self) -> list[tuple[int, float | None]]:
        if self.loop == "repeat":
            return [(i, None) for i in range(max(1, int(self.count)))]
        if self.loop == "sweep":
            if self.step == 0:
                raise SequenceError("The sweep step can't be 0")
            n = int(math.floor((self.stop - self.start) / self.step + 1e-9)) + 1
            if n < 1:
                raise SequenceError("The sweep never reaches its end: check the step's sign")
            if n > 100_000:
                raise SequenceError(f"That sweep is {n} passes; make the step bigger")
            return [(i, self.start + i * self.step) for i in range(n)]
        return [(0, None)]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["steps"] = [asdict(s) if isinstance(s, Step) else s for s in self.steps]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        p = cls(**known)
        # a hand-edited file may say 3.3 rather than "3.3"; the editor wants text
        p.steps = [Step(**{k: "" if v is None else str(v) for k, v in s.items()
                           if k in Step.__dataclass_fields__})
                   for s in d.get("steps", []) if isinstance(s, dict)]
        p.guard = p.guard if p.guard in ("off", "marginal", "strict") else "off"
        p.loop = p.loop if p.loop in ("once", "repeat", "sweep") else "once"
        p.count = int(p.count)
        p.start, p.stop, p.step = float(p.start), float(p.stop), float(p.step)
        p.off_at_end, p.stop_on_fail = bool(p.off_at_end), bool(p.stop_on_fail)
        return p


class Inst:
    """What the bench needs of an instrument tab (the window provides these)."""
    name = ""
    kind = ""
    # Filled in for tabs whose abilities come from a definition (generic SCPI,
    # scopes); None means "use the tables above for this kind".
    set_names: list | None = None
    setting_kinds: dict | None = None        # name -> "choice" (text values)
    measure_names: list | None = None
    profile_names: list | None = None

    def run(self, fn, timeout: float = 30.0):            # fn(driver) on its I/O thread
        raise NotImplementedError

    def reading_after(self, t: float, timeout: float):   # first reading timed after t
        raise NotImplementedError

    def current_between(self, t0: float, t1: float):     # PPK2: (mean A, peak A)
        raise NotImplementedError

    def capture(self, timeout: float):                   # logic: a LogicCapture
        raise NotImplementedError

    def uncertainty(self, what: str, value, r=None) -> float | None:
        """± of a reading, when its accuracy is known (accuracy.py)."""
        return None

    def outputs_off(self):
        pass


def limit_text(lo, hi, unit: str = "") -> str:
    u = f" {unit}" if unit else ""
    if lo is not None and hi is not None:
        return f"{lo:.6g} … {hi:.6g}{u}"
    if lo is not None:
        return f"≥ {lo:.6g}{u}"
    if hi is not None:
        return f"≤ {hi:.6g}{u}"
    return ""


def within(v, lo, hi) -> bool:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return False
    return (lo is None or v >= lo) and (hi is None or v <= hi)


class Bench:
    """The API steps and scripts use. Every wait checks for Stop."""

    def __init__(self, insts: list[Inst], clock, log=print, on_row=None, on_note=None,
                 stop: threading.Event | None = None):
        self._insts = insts
        self._clock = clock
        self._log = log
        self._on_row = on_row or (lambda row: None)
        self._on_note = on_note or (lambda text: None)
        self.stop_event = stop or threading.Event()
        self.rows: list[dict] = []
        self.passed = 0
        self.failed = 0
        self._fails: list[str] = []         # failures since the last record()
        self._marginal: list[str] = []      # passed, but within ± of a limit
        self.guard = "off"                  # off | marginal | strict (Plan.guard)
        self._last_r = None                 # the reading measure() used last
        self._checked = False               # any expect() since the last record()
        self.images: list = []              # (row, instrument, picture)
        self.on_image = None                # called with (picture, instrument)
        self.on_fail = None                 # called with (name, value, lo, hi)

    # ---- finding instruments ------------------------------------------------
    def instruments(self) -> list[str]:
        return [i.name for i in self._insts]

    def inst(self, name: str) -> Inst:
        """By exact name, else the only one whose name contains `name`."""
        from .signals import name_matches, slug
        for i in self._insts:
            if i.name == name:
                return i
        hits = [i for i in self._insts if name_matches(name, i.name)]
        if len(hits) > 1:            # "PPK2" with "PPK2" and "PPK2 · DUT" open: the exact one
            same = [i for i in hits if slug(i.name) == slug(name)]
            hits = same or hits
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise SequenceError(f"No open instrument called '{name}'. Open: "
                                + (", ".join(self.instruments()) or "none"))
        raise SequenceError(f"'{name}' could be " + " or ".join(i.name for i in hits))

    # ---- actions --------------------------------------------------------------
    def check(self):
        if self.stop_event.is_set():
            raise Stopped()

    def wait(self, seconds: float):
        if self.stop_event.wait(max(0.0, float(seconds))):
            raise Stopped()

    def log(self, *args):
        self._log(" ".join(str(a) for a in args))

    def set(self, name: str, what: str, value):
        self.check()
        inst = self.inst(name)
        if inst.kind == "gpio":
            return self.gpio(name, what, value)
        if inst.kind in ("scpi", "scope", "uart"):
            names = inst.set_names or []
            hit = next((n for n in names if n.lower() == str(what).strip().lower()), None)
            if hit is None:
                raise SequenceError(f"{inst.name} can't set '{what}'" + (
                    f" (it can set: {', '.join(names)})" if names else ""))
            if (inst.setting_kinds or {}).get(hit) == "choice":
                v = str(value).strip()
            else:
                v = value_of(value, {})
            self._log(f"set {inst.name} {hit} = {value}")
            inst.run(lambda d: d.set(hit, v))
            return
        if inst.kind == "dmm":
            return self._set_dmm(inst, what, value)
        if inst.kind == "fgen" and what.lower() == "function":
            f = str(value).strip().lower()
            if f not in FGEN_FUNCS:
                raise SequenceError(f"function must be one of {', '.join(FGEN_FUNCS)}")
            self._log(f"set {inst.name} function = {f}")
            inst.run(lambda d: d.set("function", f))
            return
        v = value_of(value, {})
        what = what.lower()
        if inst.kind == "fgen":
            fns = {k: (lambda d, k=k: d.set(k, bool(v) if k == "output" else v))
                   for k in SET_WHAT["fgen"] if k != "function"}
        elif inst.kind == "supply":
            fns = {"voltage": lambda d: d.set_voltage(v), "current": lambda d: d.set_current(v),
                   "output": lambda d: d.set_output(bool(v)), "ovp": lambda d: d.set_ovp(v),
                   "ocp": lambda d: d.set_ocp(bool(v)),
                   "clear_protection": lambda d: d.clear_protection(),
                   "sense_protect": lambda d: _sense_protect(d, bool(v))}
        elif inst.kind == "ppk2":
            fns = {"voltage": lambda d: d.set_voltage(int(round(v * 1000))),
                   "output": lambda d: d.set_dut_power(bool(v))}
        else:
            fns = {}
        if what not in fns:
            raise SequenceError(f"{inst.name} can't set '{what}'" + (
                f" (it can set: {', '.join(fns)})" if fns else ""))
        self._log(f"set {inst.name} {what} = {value}")
        inst.run(fns[what])

    def _set_dmm(self, inst, what: str, value):
        """A meter's own settings (range and function by their labels)."""
        names = inst.set_names or []
        hit = next((n for n in names if n.lower() == str(what).strip().lower()), None)
        if hit is None:
            raise SequenceError(f"{inst.name} can't set '{what}'" + (
                f" (it can set: {', '.join(names)})" if names else " (read-only)"))
        choice = (inst.setting_kinds or {}).get(hit) == "choice"
        v = str(value).strip() if choice else value_of(value, {})
        self._log(f"set {inst.name} {hit} = {value}")
        inst.run(lambda d: dmm_set(d, hit, v))

    def command(self, line: str, match: str = "", timeout: float = 60.0, extra: dict | None = None,
                name: str = "Command"):
        """Run a program (a flasher, a script): `esptool write_flash 0 app.bin
        timeout=120`. No shell: quotes work, pipes and * don't. A non-zero exit
        status, a timeout, or output without `match` (a regex) fails the row.
        Returns the number in the regex's first group, else its text, else None.
        Not in the Remote API: whoever calls that can run programs already."""
        from . import command as cmd
        self.check()
        line, opts = _options(str(line))
        try:
            timeout = float(opts.get("timeout", timeout))
            args = cmd.split(line, extra or {})
        except ValueError as e:
            raise SequenceError(f"Command: {e}") from None
        self._log("$ " + cmd.shown(args))
        res = cmd.run(args, timeout, self.stop_event, self._log)
        if res.stopped:
            raise Stopped()
        if res.problem:
            self.fail(name, res.problem)
            return None
        self.passed_check()
        if not match:
            return None
        found, value = cmd.find(match, res.output)
        if not found:
            self.fail(name, f"no '{match}' in its output")
        return value

    def measure(self, name: str, what: str = "", seconds: float = 0.0) -> float:
        """One reading taken after now; or with seconds > 0 the mean of the
        readings over that long (for a PPK2: of every sample)."""
        self.check()
        inst = self.inst(name)
        can = inst.measure_names if inst.measure_names is not None else \
            MEASURE_WHAT.get(inst.kind, [])
        what = (what or (can[0] if can else "")).lower()
        if what not in [c.lower() for c in can]:
            raise SequenceError(f"{inst.name} can't measure '{what}' (it can measure: "
                                + (", ".join(can) or "nothing") + ")")
        seconds = float(seconds or 0.0)
        t0 = self._clock()
        self._last_r = None
        if inst.kind == "uart":
            # every value printed since now; a line may be a while coming
            end = time.monotonic() + max(60.0, seconds + 10.0)
            while True:
                vals = [x for _, x in inst.run(lambda d: d.values_since(name_of(d, what), t0))]
                if vals and self._clock() >= t0 + seconds:
                    break
                if time.monotonic() > end:
                    raise SequenceError(f"{inst.name} printed no '{what}' in "
                                        f"{max(60.0, seconds + 10.0):g} s")
                self.wait(0.05)
            v = sum(vals) / len(vals)
        elif inst.kind == "scope":
            # from the waveform on screen now (repeated for `seconds`, averaged)
            vals = []
            end = time.monotonic() + seconds
            while True:
                vals.append(float(inst.run(lambda d: d.measure(what), timeout=60.0)))
                if time.monotonic() >= end:
                    break
                self.wait(0.2)
            good = [x for x in vals if not math.isnan(x)]
            v = sum(good) / len(good) if good else float("nan")
        elif inst.kind == "ppk2":
            span = seconds if seconds > 0 else 0.1
            self.wait(span + 0.15)            # let the samples arrive
            mean, peak = inst.current_between(t0, t0 + span)
            v = {"current": mean, "peak": peak, "charge": mean * span}[what]
        else:
            vals = []
            t = t0
            while True:
                r = inst.reading_after(t, timeout=max(10.0, seconds + 10.0))
                if r is None:
                    self.check()
                    raise SequenceError(f"No reading from {inst.name} (is it polling?)")
                vals.append(self._pick(inst, r, what))
                self._last_r = r
                t = r.t
                if seconds <= 0 or t >= t0 + seconds:
                    break
                self.check()
            good = [x for x in vals if x is not None and not math.isnan(x)]
            v = sum(good) / len(good) if good else float("nan")
        self._log(f"{inst.name} {what} = {v:.6g}")
        return v

    def uncertainty(self, name: str, what: str, value) -> float | None:
        """± of a reading by the instrument's accuracy specification; None when
        none was given (calibration log or definition file)."""
        inst = self.inst(name)
        what = (what or (inst.measure_names or MEASURE_WHAT.get(inst.kind) or [""])[0]).lower()
        fn = getattr(inst, "uncertainty", None)
        return None if fn is None else fn(what, value, self._last_r)

    # ---- GPIO ---------------------------------------------------------------------
    def gpio(self, name: str, pin: str, value):
        """Drive a GPIO output: on / off / 1 / 0, or 'pulse 0.1' (seconds)."""
        self.check()
        inst = self.inst(name)
        if inst.kind != "gpio":
            raise SequenceError(f"{inst.name} isn't a GPIO tab")
        s = str(value).strip().lower()
        if s.startswith("pulse"):
            secs = value_of(s[5:].strip() or "0.1", {})
            self._log(f"pulse {inst.name} {pin} for {secs:g} s")
            inst.run(lambda d: d.pulse(pin, secs), timeout=secs + 10)
            return
        on = bool(value_of(value, {}))
        self._log(f"set {inst.name} {pin} = {'on' if on else 'off'}")
        inst.run(lambda d: d.set_pin(pin, on))

    def read_pin(self, name: str, pin: str) -> int:
        self.check()
        inst = self.inst(name)
        if inst.kind != "gpio":
            raise SequenceError(f"{inst.name} isn't a GPIO tab")
        return int(inst.run(lambda d: d.get_pin(pin)))

    def wait_for(self, name: str, pin: str, level=1, timeout: float = 60.0,
                 poll: float = 0.005) -> float:
        """Wait until a GPIO input reads `level`; returns how long that took.
        Fails after `timeout` seconds."""
        want = int(bool(value_of(level, {})))
        t0 = time.monotonic()
        while True:
            if self.read_pin(name, pin) == want:
                took = time.monotonic() - t0
                self._log(f"{pin} = {want} after {took:.3f} s")
                return took
            if time.monotonic() - t0 > timeout:
                raise SequenceError(f"{pin} didn't go {'high' if want else 'low'} within "
                                    f"{timeout:g} s")
            self.wait(poll)

    # ---- text: serial consoles, raw SCPI --------------------------------------------
    def send(self, name: str, text: str):
        """A line to a serial console (its line end is added), or a command to
        a generic SCPI tab. wait_text() then looks at what comes back."""
        self.check()
        inst = self.inst(name)
        if inst.kind not in ("uart", "scpi"):
            raise SequenceError(f"{inst.name} isn't a serial console (or a SCPI instrument)")
        self._log(f"send {inst.name}: {text}")
        if inst.kind == "uart":
            inst.run(lambda d: d.send(str(text)))
        else:
            inst.run(lambda d: d.write(str(text)))

    def _match(self, name: str, regex: str, timeout: float) -> dict | None:
        import re
        self.check()
        inst = self.inst(name)
        if inst.kind != "uart":
            raise SequenceError(f"{inst.name} isn't a serial console")
        try:
            re.compile(regex)
        except re.error as e:
            raise SequenceError(f"'{regex}': {e}") from None
        end = time.monotonic() + float(timeout)
        while True:
            hit = inst.run(lambda d: d.match(regex))
            if hit is not None:
                self._log(f"{inst.name}: {hit['text']}")
                return hit
            if time.monotonic() >= end:
                self._log(f"{inst.name}: nothing matched '{regex}' in {float(timeout):g} s")
                return None
            self.wait(0.02)

    def wait_text(self, name: str, regex: str, timeout: float = 10.0):
        """Wait for a line matching `regex` on a serial console, looking from
        the last send() (or the last match) on. Returns the number in its first
        group, else that group's text, else the line; None if none came."""
        hit = self._match(name, regex, timeout)
        if hit is None:
            return None
        if hit["value"] is not None:
            return hit["value"]
        return next((g for g in hit["groups"] if g is not None), hit["text"])

    def fail(self, name: str, why: str):
        """Make the current row FAIL for a reason that isn't a number."""
        self._checked = True
        self._fails.append(f"{name} ({why})")
        self._log(f"FAIL {name}: {why}")
        if self.on_fail:
            self.on_fail(name, None, None, None)

    def passed_check(self):
        """Something was judged and was fine (the row gets a PASS / FAIL)."""
        self._checked = True

    # ---- profiles --------------------------------------------------------------------
    def profile(self, name: str, what: str, text: str) -> int:
        """Step a setpoint through a profile ('sine 3.0..3.6 period=2 for=20', see
        otterbench.profile); returns when it is done. Returns points sent."""
        from . import profile as prof
        self.check()
        inst = self.inst(name)
        can = inst.profile_names if inst.profile_names is not None else \
            PROFILE_WHAT.get(inst.kind, [])
        what = what.lower() or (can[0] if can else "voltage")
        if what not in can:
            raise SequenceError(f"{inst.name} can't run a profile on '{what}'"
                                + (f" (it can: {', '.join(can)})" if can else ""))
        try:
            p = prof.parse(text)
            p.points()
        except prof.ProfileError as e:
            raise SequenceError(str(e)) from None
        self._log(f"profile {inst.name} {what}: {text}  ({p.length:g} s)")

        def setter(v):
            if inst.kind == "ppk2":
                inst.run(lambda d: d.set_voltage(int(round(v * 1000))))
            elif inst.kind in ("fgen", "scpi"):
                inst.run(lambda d: d.set(what, v))
            elif what == "voltage":
                inst.run(lambda d: d.set_voltage(v))
            else:
                inst.run(lambda d: d.set_current(v))
        try:
            sent = prof.run(p, setter, self.stop_event)
        except prof.ProfileStopped:
            raise Stopped() from None
        self._log(f"profile done: {sent} points")
        return sent

    # ---- limits ------------------------------------------------------------------------
    def expect(self, name: str, value, lo=None, hi=None, u=None) -> bool:
        """Check a value against limits (either may be None). A failure makes
        the current row FAIL; returns whether it passed. u: the value's ±, for
        the plan's guard (a value within ± of a limit is marked, or fails)."""
        lo = None if lo is None or lo == "" else float(lo)
        hi = None if hi is None or hi == "" else float(hi)
        self._checked = True
        ok = within(value, lo, hi)
        if ok and self.guard != "off" and marginal(value, lo, hi, u):
            text = f"{name}={value:.6g} (± {u:.3g} of {limit_text(lo, hi)})"
            self._log(f"within ± of a limit: {text}")
            if self.guard == "strict":
                ok = False
                self._fails.append(text)
                if self.on_fail:
                    self.on_fail(name, value, lo, hi)
            else:
                self._marginal.append(name)
        elif not ok:
            v = "nan" if value is None else f"{value:.6g}"
            self._fails.append(f"{name}={v} (want {limit_text(lo, hi)})")
            self._log(f"FAIL {name} = {v}, want {limit_text(lo, hi)}")
            if self.on_fail:
                self.on_fail(name, value, lo, hi)
        return ok

    @staticmethod
    def _pick(inst, r, what):
        if inst.kind == "dmm":
            return r.value
        if inst.kind == "scpi":
            key = next((k for k in r.values if k.lower() == what), what)
            return r.values.get(key)
        return {"voltage": r.voltage, "current": r.current, "dvm": r.dvm,
                "power": getattr(r, "power", None) if getattr(r, "power", None) is not None
                else (r.voltage or 0.0) * r.current}[what]

    def capture(self, name: str, timeout: float = 120.0):
        """Run one capture with the analyzer tab's settings; returns it."""
        self.check()
        inst = self.inst(name)
        if inst.kind not in ("logic", "scope"):
            raise SequenceError(f"{inst.name} isn't a logic analyzer or an oscilloscope")
        self._log(f"capture on {inst.name}")
        res = inst.capture(timeout)
        if res is None:
            self.check()
            raise SequenceError(f"{inst.name}: the capture didn't finish")
        return res

    def screenshot(self, name: str) -> bytes:
        """The scope's screen: kept for the report and in the session."""
        self.check()
        inst = self.inst(name)
        if inst.kind != "scope":
            raise SequenceError(f"{inst.name} isn't an oscilloscope")
        self._log(f"screenshot of {inst.name}")
        data = inst.run(lambda d: d.screenshot(), timeout=60.0)
        if len(self.images) < MAX_IMAGES:
            self.images.append((len(self.rows) + 1, inst.name, data))
        if self.on_image:
            self.on_image(data, inst.name)
        return data

    def note(self, text: str):
        self._log(f"note: {text}")
        self._on_note(str(text))

    def record(self, **values):
        """One row of results. Rows after an expect() get a result column
        (PASS / FAIL) and a failures column (what failed; empty on a pass)."""
        row = {"t": round(self._clock(), 6), **values}
        if self._checked:
            row["result"] = "FAIL" if self._fails else "PASS"
            # always present, so every judged row has the same columns (one
            # session stream, one CSV layout)
            row["failures"] = "; ".join(self._fails)
            if self.guard == "marginal":
                row["marginal"] = "; ".join(self._marginal)
            if self._fails:
                self.failed += 1
            else:
                self.passed += 1
        self._fails, self._checked, self._marginal = [], False, []
        self.rows.append(row)
        self._on_row(row)

    def outputs_off(self):
        for i in self._insts:
            try:
                i.outputs_off()
            except Exception as e:     # noqa: BLE001
                self._log(f"{i.name}: output off failed: {e}")


class Unstopped:
    """`with Unstopped(bench):` lets commands through after a Stop: the
    switch-everything-off part of a test must never be skipped."""

    def __init__(self, bench: "Bench"):
        self.bench = bench

    def __enter__(self):
        self.old = [self.bench.stop_event] + [getattr(i, "stop", None) for i in self.bench._insts]
        fresh = threading.Event()
        self.bench.stop_event = fresh
        for i in self.bench._insts:
            if hasattr(i, "stop"):
                i.stop = fresh
        return self

    def __exit__(self, *_a):
        self.bench.stop_event = self.old[0]
        for i, s in zip(self.bench._insts, self.old[1:]):
            if hasattr(i, "stop"):
                i.stop = s
        return False


def run_plan(plan: Plan, bench: Bench, start: int = 0, extra: dict | None = None,
             on_pass=None):
    """Run the steps; one results row per pass.

    start:   the first pass to run (0-based), to resume after a crash
    extra:   more names for the steps: numbers can be used in values, and any
             value replaces {name} in a Note (e.g. serial -> {serial})
    on_pass: called with the pass index after each pass is recorded"""
    passes = plan.passes()
    tree = parse_blocks(plan.steps)
    extra = dict(extra or {})
    for idx, x in passes:
        if idx < start:
            continue
        env = {k: v for k, v in extra.items() if isinstance(v, (int, float))}
        env.update(i=idx, n=len(passes))
        if x is not None:
            env["x"] = x
        row: dict = {"pass": idx + 1}
        for k, v in extra.items():
            if k in ("serial",):
                row[k] = v
        if x is not None:
            row["x"] = round(x, 12)
        _Pass(plan, bench, idx, x, extra, env, row).run(tree)
        if on_pass is not None:
            on_pass(idx)


class _Pass:
    """One pass through the steps. A row of results is recorded at the end of
    every For round that measured something, and at the end of the pass."""

    def __init__(self, plan, bench, idx, x, extra, env, row):
        self.plan, self.bench, self.idx, self.x = plan, bench, idx, x
        self.extra, self.env, self.row = dict(extra), env, row
        self.pending = False         # measured since the last row
        self.rows = 0

    def run(self, tree: list):
        self.block(tree)
        if self.pending or not self.rows:
            self.record()

    def record(self):
        failing = bool(self.bench._fails)
        self.bench.record(**self.row)
        self.pending = False
        self.rows += 1
        if failing and self.plan.stop_on_fail:
            raise LimitFailed(f"pass {self.idx + 1} failed: "
                              f"{self.bench.rows[-1].get('failures', '')}")

    def block(self, nodes: list):
        for node in nodes:
            self.bench.check()
            fn = {"For": self._for, "Repeat": self._repeat, "Retry": self._retry,
                  "If": self._if}.get(node.step.action)
            try:
                if fn is not None:
                    fn(node)
                else:
                    self.one(node.step)
            except (Stopped, SequenceError):
                raise
            except Exception as e:      # noqa: BLE001 - say which step
                st = node.step
                raise SequenceError(f"Step {node.k + 1} ({st.action} {st.target} {st.what}): "
                                    f"{e}") from e

    # ---- blocks
    def _for(self, node):
        from .regulator import parse_list
        name = node.step.what.strip()
        outer = dict(self.row)
        try:
            for v in parse_list(node.step.value):
                self.bench.check()
                self.row.clear()
                self.row.update(outer)
                self.row[name] = v
                self.env[name] = v
                self.extra[name] = f"{v:g}"
                self.block(node.body)
                if self.pending:
                    self.record()
        finally:
            self.env.pop(name, None)
            self.extra.pop(name, None)
        # what was measured inside belongs to its own rows
        self.row.clear()
        self.row.update(outer)

    def _repeat(self, node):
        cond, most, wait = repeat_args(node.step.value, self.env)
        for k in range(most):
            self.bench.check()
            self.env["k"] = k
            self.block(node.body)
            if cond and value_of(cond, self.env):
                return
            if k + 1 < most and wait:
                self.bench.wait(wait)
        if cond:
            self.bench.fail(node.step.label or "Repeat", f"'{cond}' not reached in {most} rounds")
            self.pending = True

    def _retry(self, node):
        """Again while a limit fails; only the last try counts."""
        b = self.bench
        tries, wait = retry_args(node.step.value, self.env)
        for k in range(tries):
            fails, checked, row = list(b._fails), b._checked, dict(self.row)
            near = list(b._marginal)
            self.env["k"] = k
            self.block(node.body)
            if len(b._fails) <= len(fails) or k + 1 == tries:
                if tries > 1 and node.step.label:
                    self.row[node.step.label] = k + 1
                return
            b.log(f"retry {k + 2} of {tries}")
            b._fails, b._checked, b._marginal = fails, checked, near
            self.row.clear()
            self.row.update(row)
            if wait:
                b.wait(wait)

    def _if(self, node):
        ok = bool(value_of(node.step.value, self.env))
        self.bench.log(f"if {node.step.value}: {'yes' if ok else 'no'}")
        self.block(node.body if ok else node.other)

    # ---- one step
    def one(self, st: Step):
        bench, row, env, extra, idx, x = (self.bench, self.row, self.env, self.extra, self.idx,
                                          self.x)
        before = dict(row)
        act = st.action
        if act == "Set":
            inst = bench.inst(st.target)
            kind = inst.kind
            raw = kind == "gpio" and str(st.value).strip().lower().startswith("pulse") \
                or kind == "fgen" and st.what.lower() == "function" \
                or (inst.setting_kinds or {}).get(st.what) == "choice"
            bench.set(st.target, st.what, st.value if raw else value_of(st.value, env))
        elif act == "Wait":
            bench.wait(value_of(st.value, env))
        elif act == "Measure":
            kind = bench.inst(st.target).kind
            if kind == "gpio":
                v = bench.read_pin(st.target, st.what)
                col = st.label or f"{st.what}"
            else:
                secs = value_of(st.value, env) if str(st.value).strip() else 0.0
                v = bench.measure(st.target, st.what, secs)
                unit = UNITS.get(st.what.lower(), "") or _unit_of(bench, st)
                col = st.label or f"{st.target} {st.what}" + (f" ({unit})" if unit else "")
            row[col] = v
            u = None if kind == "gpio" else bench.uncertainty(st.target, st.what, v)
            if u is not None:
                row[col + PM] = u
            _limits(bench, st, col, v, env, u)
        elif act == "Capture":
            res = bench.capture(st.target)
            if bench.inst(st.target).kind == "scope":
                col = st.label or f"{st.target} capture"
                row[col] = sum(tr.v.size for tr in res.analog.values())
            else:
                col = st.label or f"{st.target} decoded"
                row[col] = len(res.table_rows)
            if res.offset is not None:
                row[f"{col} t0"] = round(res.offset, 6)
            _limits(bench, st, col, row[col], env)
        elif act == "Profile":
            bench.profile(st.target, st.what, str(st.value)
                          .replace("{x}", "" if x is None else f"{x:g}"))
        elif act == "Send":
            bench.send(st.target, _fill_text(st.value, x, idx, extra))
        elif act == "Wait for" and bench.inst(st.target).kind == "uart":
            regex, timeout = _text_args(_fill_text(st.value, x, idx, extra, braces=False))
            hit = bench._match(st.target, regex, timeout)
            col = st.label or f"{st.target} reply"
            if hit is None:
                # nan keeps a numeric column numeric
                row[col] = float("nan") if "(" in regex else "no reply"
                bench.fail(col, f"no '{regex}' within {timeout:g} s")
            else:
                bench.passed_check()
                v = hit["value"]
                row[col] = v if v is not None else next(
                    (g for g in hit["groups"] if g is not None), hit["text"])
                if v is not None:
                    _limits(bench, st, col, v, env)
        elif act == "Wait for":
            level, timeout = _wait_for_args(st.value, env)
            took = bench.wait_for(st.target, st.what, level, timeout)
            if st.label:
                row[st.label] = round(took, 6)
                _limits(bench, st, st.label, took, env)
        elif act == "Command":
            out = bench.command(st.value, st.what, extra={**extra, "i": idx + 1, **(
                {} if x is None else {"x": f"{x:g}"})}, name=st.label or "Command")
            if out is not None and st.label:
                row[st.label] = out
                _limits(bench, st, st.label, out, env)
        elif act == "Screenshot":
            bench.screenshot(st.target)
        elif act == "Note":
            bench.note(_fill_text(st.value, x, idx, extra))
        else:
            raise SequenceError(f"Unknown action '{act}'")
        if row != before or bench._checked:
            self.pending = True
        for col, v in row.items():
            name = "" if col.endswith(PM) else _var(col)
            if col not in before or before[col] is not v:
                if name and isinstance(v, (int, float)):
                    env[name] = v


def _var(col: str) -> str:
    """A results column as a name for expressions ("Vout (V)" -> vout)."""
    from .signals import slug
    name = slug(col)
    return "" if name in SAFE or name in ("x", "i", "n", "k", "pass", "_") else name


@dataclass
class Node:
    step: Step
    k: int                       # its place in the list
    body: list = field(default_factory=list)
    other: list = field(default_factory=list)       # If: the Else part


def parse_blocks(steps: list) -> list:
    """The steps as a tree: For / Repeat / Retry / If … (Else …) End."""
    import keyword
    root: list = []
    stack: list = []             # (node, the list being filled)
    cur = root
    for k, st in enumerate(steps):
        where = f"Step {k + 1} ({st.action})"
        if st.action in BLOCKS:
            if st.action == "For":
                v = st.what.strip()
                if not v.isidentifier() or keyword.iskeyword(v) or v in SAFE or \
                        v in ("x", "i", "n", "k", "t", "result", "failures", "serial"):
                    raise SequenceError(f"{where}: give the variable a name of its own, e.g. y")
                if any(n.step.action == "For" and n.step.what.strip() == v for n, _ in stack):
                    raise SequenceError(f"{where}: {v} is used by a For around it")
                from .regulator import parse_list
                try:
                    parse_list(st.value)
                except ValueError as e:
                    raise SequenceError(f"{where}: values like 1, 2, 5 or 0..10 x 11: {e}") \
                        from None
            elif st.action == "If" and not str(st.value).strip():
                raise SequenceError(f"{where}: give a condition, e.g. vout > 3.2")
            elif st.action == "Repeat":
                repeat_args(st.value, None)
            elif st.action == "Retry":
                retry_args(st.value, None)
            node = Node(st, k)
            cur.append(node)
            stack.append((node, cur))
            cur = node.body
        elif st.action == "Else":
            if not stack or stack[-1][0].step.action != "If" or cur is stack[-1][0].other:
                raise SequenceError(f"{where}: there is no If for it")
            cur = stack[-1][0].other
        elif st.action == "End":
            if not stack:
                raise SequenceError(f"{where}: there is nothing to end")
            cur = stack.pop()[1]
        else:
            cur.append(Node(st, k))
    if stack:
        n = stack[-1][0]
        raise SequenceError(f"Step {n.k + 1} ({n.step.action}) has no End")
    return root


def depths(actions: list) -> list[int]:
    """How far each step is indented."""
    out, d = [], 0
    for a in actions:
        if a in ("End", "Else"):
            d = max(0, d - 1)
        out.append(d)
        if a in BLOCKS or a == "Else":
            d += 1
    return out


MAX_ROUNDS = 100_000


def _options(text: str) -> tuple[str, dict]:
    """'… max=5 wait=1' -> ('…', {'max': '5', 'wait': '1'})."""
    import re
    opts = {}
    while True:
        m = re.match(r"^(.*?)\s*\b(max|wait|timeout)=(\S+)\s*$", text, re.S)
        if not m or m.group(2) in opts:
            return text.strip(), opts
        text, opts[m.group(2)] = m.group(1), m.group(3)


def _whole(text, env, what: str, most: int = MAX_ROUNDS) -> int:
    n = value_of(text, env or {})
    if not (n == n and 1 <= n <= most):
        raise SequenceError(f"{what} must be 1 … {most}, not '{text}'")
    return int(n)


def repeat_args(text, env) -> tuple[str, int, float]:
    """'10 wait=1' or 'until v < 0.1 max=100 wait=1' -> (condition, rounds, wait s)."""
    head, o = _options(str(text))
    low = head.lower()
    cond = head[6:].strip() if low.startswith("until ") else ""
    try:
        wait = float(o.get("wait", 0))
        most = _whole(o.get("max", "1000") if cond else head, env, "Repeat: the count")
    except (ValueError, SequenceError) as e:
        if env is None and not cond and not isinstance(e, ValueError) and "work out" in str(e):
            return cond, 1, 0.0          # names something only known while running
        raise SequenceError(f"Repeat: a count like 10, or until vout < 0.1 max=100 ({e})") \
            from None
    if not 0 <= wait <= 86400:
        raise SequenceError("Repeat: wait= is seconds")
    return cond, most, wait


def retry_args(text, env) -> tuple[int, float]:
    """'3 wait=1' -> (tries, wait s)."""
    head, o = _options(str(text))
    try:
        wait = float(o.get("wait", 0))
        n = _whole(head or "3", env, "Retry: the number of tries", 1000)
    except (ValueError, SequenceError) as e:
        raise SequenceError(f"Retry: a number of tries like 3 wait=1 ({e})") from None
    if not 0 <= wait <= 86400:
        raise SequenceError("Retry: wait= is seconds")
    return n, wait


def _unit_of(bench: Bench, st: Step) -> str:
    fn = getattr(bench.inst(st.target), "unit_of", None)
    try:
        return fn(st.what) if fn else ""
    except Exception:      # noqa: BLE001
        return ""


def _limits(bench: Bench, st: Step, col: str, v, env: dict, u=None):
    lo = value_of(st.lo, env) if str(st.lo).strip() else None
    hi = value_of(st.hi, env) if str(st.hi).strip() else None
    if lo is not None or hi is not None:
        bench.expect(col, v, lo, hi, u)


def _fill_text(text, x, idx: int, extra: dict, braces: bool = True) -> str:
    """{x}, {i} and the extra names ({serial}) in a Note, Send or pattern.
    braces=False (a regex, where {2} means twice): only the extra names."""
    out = str(text)
    if braces:
        out = out.replace("{x}", "" if x is None else f"{x:g}").replace("{i}", str(idx + 1))
    for k, v in extra.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _text_args(text: str) -> tuple[str, float]:
    """'adc=(\\d+) timeout=2' -> (regex, seconds)."""
    import re
    m = re.match(r"^(.*?)\s+timeout=([\d.]+)\s*$", str(text))
    if m:
        return m.group(1), float(m.group(2))
    return str(text).strip(), 10.0


PM = " ±"                    # the column with a reading's ±
MAX_IMAGES = 50              # kept for a report


def dmm_set(drv, what: str, value):
    """One setting of a meter whose driver takes its whole configuration."""
    from dataclasses import replace
    cfg = replace(drv.cfg)
    old = getattr(cfg, what)
    if isinstance(old, bool):
        value = bool(value) if not isinstance(value, str) else value_of(value, {}) != 0
    elif isinstance(old, str):
        opts = dmm_options(drv, what)
        hit = next((o for o in opts if o.replace(" ", "").lower() ==
                    str(value).replace(" ", "").lower()), None)
        if hit is None:
            raise ValueError(f"{what} must be one of {', '.join(opts)}")
        value = hit
    else:
        value = type(old)(value)
        if what == "nplc":
            from .instruments.hp3456a import NPLC_CHOICES
            if value not in NPLC_CHOICES:
                raise ValueError("nplc must be one of "
                                 + ", ".join(f"{x:g}" for x in NPLC_CHOICES))
    cfg = replace(cfg, **{what: value})
    if what == "function" and value != old:
        cfg = replace(cfg, range=_dmm_ranges(drv, value)[0])
    drv.apply(cfg)


def _dmm_ranges(drv, function: str) -> list[str]:
    from .instruments import hp3456a, xdm
    return list((hp3456a if drv.family == "3456A" else xdm).ranges_for(function))


def dmm_options(drv, what: str) -> list[str]:
    from .instruments import hp3456a, xdm
    if what == "function":
        return list(drv.functions()) if hasattr(drv, "functions") else list(hp3456a.FUNCTIONS)
    if what == "range":
        return _dmm_ranges(drv, drv.cfg.function)
    return list(xdm.RATES) if what == "rate" else []


def name_of(drv, what: str) -> str:
    """A pattern's own spelling (names are matched without case)."""
    return next((p.name for p in drv.state.patterns if p.name.lower() == what), what)


def _wait_for_args(text, env) -> tuple[float, float]:
    """'1', 'low', '1 timeout=5' -> (level, timeout s)."""
    level, timeout = 1.0, 60.0
    for w in str(text).split():
        if w.lower().startswith("timeout="):
            timeout = value_of(w.split("=", 1)[1], env)
        elif w.lower() in ("high", "low"):
            level = 1.0 if w.lower() == "high" else 0.0
        else:
            level = value_of(w, env)
    return level, timeout


def run_script(code: str, bench: Bench, extra: dict | None = None):
    """Python in the Script tab: `bench` plus math, time and numpy as np."""
    import numpy as np
    g = {"__name__": "__sequence__", "bench": bench, "math": math, "time": time, "np": np,
         "print": bench.log, **(extra or {})}
    exec(compile(code, "<script>", "exec"), g)      # noqa: S102 - the user's own bench
