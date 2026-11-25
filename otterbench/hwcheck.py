"""Hardware check: is a driver right about the instrument in front of it?

Three levels, each including the one before:
    read      identity, error queue, settings and readings. Sends no settings.
    settings  with every output off: settings are changed, read back and put
              back; a meter's functions and a load's modes are gone through
    output    a supply's output is switched on at a low voltage with NOTHING
              connected, measured, and switched off again

Every step ends PASS, FAIL or SKIP (with why). The steps run one by one on the
instrument's I/O thread, so Esc and Stop act between them, and their traffic
can be taped (tape.py). replay() runs the same check against a tape: the steps
must come out as they did on the hardware.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass, field

from . import tape as tapes
from .instruments.base import DmmConfig, SupplyPoll
from .sequencer import Stopped

READ, SETTINGS, OUTPUT = "read", "settings", "output"
LEVELS = {READ: "Read only", SETTINGS: "Settings (outputs off)",
          OUTPUT: "Output on (nothing connected)"}
ORDER = (READ, SETTINGS, OUTPUT)
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
POLLS = 5


class Skip(Exception):
    """This step doesn't apply; the message says why."""


class Failed(Exception):
    pass


@dataclass
class Options:
    level: str = READ
    volts: float = 1.0           # test voltage
    amps: float = 0.1            # current limit while testing
    functions: bool = True       # go through a meter's functions / a load's modes
    settle: float = 1.0          # after switching an output

    def problem(self) -> str:
        if self.level not in ORDER:
            return f"level must be one of {', '.join(ORDER)}"
        if not 0 < self.volts <= 5:
            return "The test voltage must be above 0 and at most 5 V"
        if not 0 < self.amps <= 1:
            return "The current limit must be above 0 and at most 1 A"
        return ""


def near(got, want, rel: float, floor: float) -> bool:
    return got is not None and math.isfinite(got) and \
        abs(got - want) <= abs(want) * rel + floor


def outputs_on(drv) -> bool | None:
    """None: it can't say. (Asks the instrument.)"""
    if drv.kind == "supply":
        return drv.read_setpoints().output
    if drv.kind == "scpi":
        drv.read_settings()
        vals = [drv.values.get(s.name) for s in drv.d.settings if s.output]
        return True if any(v is True for v in vals) else None if None in vals else False
    if drv.kind == "fgen":
        drv.read_all()
        return any(c.output for c in drv.state.channels)
    return False


def outputs_off(drv):
    if hasattr(drv, "set_output") and drv.kind in ("supply", "scpi", "fgen"):
        drv.set_output(False)


def _g(v) -> str:
    return "?" if v is None else f"{v:.6g}" if isinstance(v, float) else str(v)


@dataclass
class HardwareCheck:
    """run(fn) calls fn(drv) on the instrument's thread and returns its result."""
    run: object
    caps: object
    opts: Options = field(default_factory=Options)
    sleep: object = time.sleep
    on_row: object = None
    rows: list = field(default_factory=list)

    # ---- steps
    def step(self, name: str, fn):
        t0 = time.monotonic()
        try:
            r = fn()
            status, detail = PASS, "" if r is None else str(r)
        except Skip as e:
            status, detail = SKIP, str(e)
        except Stopped:
            raise
        except Exception as e:      # noqa: BLE001 - any failure is a result here
            status = FAIL
            detail = str(e) if isinstance(e, Failed) else f"{type(e).__name__}: {e}"
        row = {"step": name, "result": status, "detail": detail,
               "ms": round((time.monotonic() - t0) * 1000)}
        self.rows.append(row)
        if self.on_row:
            self.on_row(row)
        return status

    def no_errors(self):
        errs = self.run(lambda d: d.errors())
        if errs:
            raise Failed("instrument error: " + "; ".join(errs))

    def at_least(self, level: str) -> bool:
        return ORDER.index(self.opts.level) >= ORDER.index(level)

    def go(self) -> list[dict]:
        kind = self.run(lambda d: d.kind)
        self.step("Identity", self._identity)
        self.step("Error queue empty", self.no_errors)
        self.step("Read every setting", lambda: self._settings(kind))
        self.step("Readings", self._readings)
        if not self.at_least(SETTINGS):
            return self.rows
        if kind not in ("supply", "scpi", "fgen", "scope", "dmm"):
            self.step("Settings", self._none)
            return self.rows
        if self.step("Outputs are off", self._guard) != PASS:
            return self.rows         # nothing is sent to a live output
        try:
            getattr(self, f"_set_{kind}")()
            if self.at_least(OUTPUT):
                fn = getattr(self, f"_out_{kind}", None)
                if fn is None:
                    self.step("Output", self._none)
                else:
                    fn()
        finally:
            if kind in ("supply", "scpi", "fgen"):
                self.run(outputs_off)
        self.step("Error queue empty at the end", self.no_errors)
        return self.rows

    def _none(self):
        raise Skip("not checked on this kind of instrument")

    # ---- reading
    def _identity(self):
        idn = getattr(self.caps, "idn", "") or ""
        if not idn:
            raise Failed("it gave no identity")
        if "no answer" in idn:
            raise Failed("no answer to *IDN?")
        d = getattr(self.caps, "definition", None)
        if d is not None and d.idn and not any(x.upper() in idn.upper() for x in d.idn):
            raise Failed(f"{idn!r} isn't a model this definition ({d.id}) names: "
                         + ", ".join(d.idn))
        return idn

    def _settings(self, kind: str):
        if kind == "scpi":
            def job(d):
                out, bad = [], []
                for s in d.d.settings:
                    if not s.query:
                        continue
                    try:
                        v = d.read_setting(s)
                    except Exception as e:      # noqa: BLE001
                        bad.append(f"{s.name}: {e}")
                        continue
                    if v is None:
                        bad.append(f"{s.name}: its answer matches none of the options")
                    d.values[s.name] = v
                    out.append(f"{s.name}={_g(v)}")
                return out, bad
            out, bad = self.run(job)
            if bad:
                raise Failed("; ".join(bad))
            if not out:
                raise Skip("it has no settings that can be read")
            return ", ".join(out)
        if kind == "supply":
            s = self.run(lambda d: d.read_setpoints())
            if not getattr(self.caps, "setpoint_readback", True):
                raise Skip("this model can't report its settings")
            got = {k: v for k, v in asdict(s).items()
                   if k in ("voltage", "current", "output", "ovp", "ocp")}
            lost = [k for k in ("voltage", "current", "output") if got[k] is None]
            if lost:
                raise Failed("couldn't read " + ", ".join(lost))
            return ", ".join(f"{k}={_g(v)}" for k, v in got.items())
        if kind == "fgen":
            self.run(lambda d: d.read_all())
            chs = self.run(lambda d: d.cfg).channels
            return "; ".join(f"CH{i}: {c.function} {_g(c.frequency)} Hz {_g(c.amplitude)} Vpp "
                             f"{_g(c.offset)} V {'ON' if c.output else 'off'}"
                             for i, c in enumerate(chs, 1))
        if kind == "scope":
            self.run(lambda d: d.read_state())
            st = self.run(lambda d: d.cfg)
            return f"{st.tscale:g} s/div; " + "; ".join(
                f"CH{i} {'on' if c.on else 'off'} {c.scale:g} V/div"
                for i, c in enumerate(st.channels, 1))
        if kind == "dmm":
            c = self.run(lambda d: getattr(d, "cfg", None))
            if c is None or not hasattr(c, "function"):
                raise Skip("it has no settings to read")
            return f"{c.function}, range {c.range}"
        raise Skip("it has no settings to read")

    def _poll(self):
        cfg = SupplyPoll()

        def job(d):
            r = d.poll(0.0, cfg)
            return r, d.kind
        r, kind = self.run(job)
        errs = getattr(r, "errors", None)
        if errs:
            raise Failed("instrument error: " + "; ".join(errs))
        if kind == "supply":
            return {"V": r.voltage, "A": r.current}
        if kind == "scpi":
            return dict(r.values)
        if kind == "dmm":
            if r.overload or r.text:
                return {}
            return {r.unit or "value": r.value}
        return {}

    def _readings(self):
        t0 = time.monotonic()
        got = [self._poll() for _ in range(POLLS)]
        ms = (time.monotonic() - t0) / POLLS * 1000
        self.poll_ms = round(ms, 1)
        out = []
        for k in got[0]:
            v = [g.get(k) for g in got]
            v = [x for x in v if x is not None and math.isfinite(x)]
            out.append(f"{k}={_g(sum(v) / len(v))}" if v else f"{k}=overload")
        return ", ".join(out) or "answers, nothing to show"

    # ---- settings, outputs off
    def _guard(self):
        on = self.run(outputs_on)
        if on:
            raise Failed("an output is on: switch it off, disconnect what it feeds, and run "
                         "the check again")
        if on is None:
            self.run(outputs_off)
            return "couldn't tell; switched off to be sure"
        return None

    def _set_supply(self):
        o, c = self.opts, self.caps
        volts, amps = min(o.volts, c.v_max), min(o.amps, c.i_max)
        before = self.run(lambda d: d.read_setpoints())
        reads = getattr(c, "setpoint_readback", True)

        def check(fn, attr, want, rel, floor):
            def job(d):
                fn(d)
                return getattr(d.read_setpoints(), attr)

            def body():
                got = self.run(job)
                self.no_errors()
                if not reads:
                    return "accepted (this model can't report it)"
                ok = got == want if isinstance(want, bool) else near(got, want, rel, floor)
                if not ok:
                    raise Failed(f"set {_g(want)}, read back {_g(got)}")
                return f"set {_g(want)}, read back {_g(got)}"
            return body

        try:
            self.step("Set voltage", check(lambda d: d.set_voltage(volts), "voltage", volts,
                                           0.005, 0.02))
            self.step("Set current limit", check(lambda d: d.set_current(amps), "current", amps,
                                                 0.02, 0.01))
            if reads and before.ocp is not None:
                for on in (not before.ocp, before.ocp):
                    self.step(f"Over-current protection {'on' if on else 'off'}",
                              check(lambda d, on=on: d.set_ocp(on), "ocp", on, 0, 0))
            if reads and before.ovp is not None:
                v = min(round(volts + 2.0, 2), c.ovp_max)
                self.step(f"Set {c.ovp_label}", check(lambda d: d.set_ovp(v), "ovp", v, 0.02, 0.1))
                self.step(f"{c.ovp_label} put back", check(
                    lambda d: d.set_ovp(before.ovp), "ovp", before.ovp, 0.02, 0.1))
        finally:
            self._put_back_supply(before)

    def _put_back_supply(self, before):
        def job(d):
            if before.ovp is not None and getattr(self.caps, "setpoint_readback", True):
                d.set_ovp(before.ovp)
            d.set_voltage(before.voltage or 0.0)
            if before.current is not None:
                d.set_current(before.current)
        self.step("Voltage and current put back" if before.voltage is not None
                  else "Voltage left at 0 V", lambda: self.run(job))

    def _set_scpi(self):
        d0 = self.run(lambda d: d.d)
        for s in d0.settings:
            if s.output or not s.query:
                continue
            self.step(f"{s.name}: write what it is", lambda s=s: self._rewrite(s))
            if d0.type == "supply" and s.kind == "number" and \
                    re.match(r"(vset|iset)\d*$", s.name):
                v = self.opts.volts if s.name.startswith("v") else self.opts.amps
                if s.max is not None:
                    v = min(v, s.max)
                if s.min is not None:
                    v = max(v, s.min)
                self.step(f"{s.name}: set and put back", lambda s=s, v=v: self._change(s, v))
            if s.kind == "choice" and self.opts.functions and d0.type in ("meter", "load"):
                self.step(f"{s.name}: every choice", lambda s=s: self._choices(s))

    def _back(self, s, value):
        def job(d):
            d.set(s.name, value)
            return d.read_setting(s)        # d.set ignores a failed read-back
        return self.run(job)

    def _same(self, s, got, want) -> bool:
        if s.kind == "number":
            return near(got, float(want), 0.005, 0.02 if s.unit in ("V", "A") else 1e-9)
        return got == want

    def _rewrite(self, s):
        v = self.run(lambda d: d.read_setting(s))
        if v is None or isinstance(v, float) and not math.isfinite(v):
            raise Skip("its value isn't known")
        if s.kind == "number" and ((s.min is not None and v < s.min)
                                   or (s.max is not None and v > s.max)):
            raise Failed(f"it is {_g(v)}, outside the definition's {_g(s.min)} … {_g(s.max)}")
        got = self._back(s, v)
        self.no_errors()
        if not self._same(s, got, v):
            raise Failed(f"wrote {_g(v)}, read back {_g(got)}")
        return _g(v)

    def _change(self, s, v):
        before = self.run(lambda d: d.read_setting(s))
        try:
            got = self._back(s, v)
            self.no_errors()
            if not self._same(s, got, v):
                raise Failed(f"set {_g(v)}, read back {_g(got)}")
        finally:
            if before is not None:
                self.run(lambda d: d.set(s.name, before))
        self.no_errors()
        return f"set {_g(v)}, read back {_g(got)}, put back {_g(before)}"

    def _choices(self, s):
        before = self.run(lambda d: d.read_setting(s))
        bad, out = [], []
        try:
            for o in s.options:
                try:
                    got = self._back(s, o.label)
                    errs = self.run(lambda d: d.errors())
                    if errs:
                        bad.append(f"{o.label}: {'; '.join(errs)}")
                    elif got != o.label:
                        bad.append(f"{o.label}: read back {_g(got)}")
                    else:
                        self._poll()
                        out.append(o.label)
                except Stopped:
                    raise
                except Exception as e:      # noqa: BLE001
                    bad.append(f"{o.label}: {e}")
        finally:
            if before is not None:
                self.run(lambda d: d.set(s.name, before))
        if bad:
            raise Failed("; ".join(bad) + (f" (fine: {', '.join(out)})" if out else ""))
        return ", ".join(out)

    def _set_dmm(self):
        drv_fns = self.run(lambda d: self._dmm_functions(d))
        if not drv_fns:
            self.step("Functions", lambda: (_ for _ in ()).throw(
                Skip("read-only meter: its own knob chooses")))
            return
        if not self.opts.functions:
            return
        from dataclasses import replace
        before = self.run(lambda d: replace(d.cfg))

        def one(f):
            def job(d):
                d.apply(replace(before, function=f, range=DmmConfig().range) if d.family == "3456A"
                        else replace(before, function=f, range="K type" if f == "Temperature"
                                     else self._auto(d, f), dual=False))
                r = d.poll(0.0)
                return d.cfg.function, r.display() if getattr(r, "native", "") else (
                    "overload" if r.overload else f"{r.value:.6g} {r.unit}")
            got, text = self.run(job)
            self.no_errors()
            if got != f:
                raise Failed(f"set {f}, it is in {got}")
            return text
        try:
            for f in drv_fns:
                self.step(f"Function {f}", lambda f=f: one(f))
        finally:
            self.step("Function put back", lambda: self.run(lambda d: d.apply(before)))

    @staticmethod
    def _auto(d, f):
        from .instruments.xdm import ranges_for
        return ranges_for(f)[0]

    @staticmethod
    def _dmm_functions(d):
        if not hasattr(d, "apply"):
            return []
        if hasattr(d, "functions"):
            return list(d.functions())
        from .instruments.hp3456a import FUNCTIONS
        return list(FUNCTIONS)

    def _set_fgen(self):
        before = self.run(lambda d: d.cfg)

        def put(ch, what, v):
            def job(d):
                d.set(what, v, ch)
                return getattr(d.read_channel(ch), what)

            def body():
                got = self.run(job)
                self.no_errors()
                if not (got == v if isinstance(v, str) else near(got, v, 0.005, 1e-3)):
                    raise Failed(f"set {_g(v)}, read back {_g(got)}")
                return f"set {_g(v)}, read back {_g(got)}"
            return body
        for ch, c in enumerate(before.channels, 1):
            try:
                for what, v in (("function", "sine"), ("frequency", 1234.0), ("amplitude", 0.5),
                                ("offset", 0.1), ("function", "square"), ("duty", 40.0),
                                ("function", "ramp")):
                    self.step(f"CH{ch} {what} = {v}", put(ch, what, v))
            finally:
                def back(d, ch=ch, c=c):
                    for what in ("function", "frequency", "offset", "amplitude"):
                        d.set(what, getattr(c, what), ch)
                self.step(f"CH{ch} put back", lambda: self.run(back))

    def _set_scope(self):
        st = self.run(lambda d: d.cfg)
        ch = next((i for i, c in enumerate(st.channels, 1) if c.on), 1)
        c = st.channels[ch - 1]

        def put(fn, get, v):
            def job(d):
                fn(d, v)
                return get(d.cfg)

            def body():
                got = self.run(job)
                self.no_errors()
                if not near(got, v, 0.01, 1e-12):
                    raise Failed(f"set {_g(v)}, read back {_g(got)}")
                return f"set {_g(v)}, read back {_g(got)}"
            return body

        def tb(d, v): d.set_timebase(scale=v)
        def vs(d, v): d.set_channel(ch, scale=v)
        tnew = 1e-3 if abs(st.tscale - 1e-3) > 1e-5 else 2e-3
        vnew = 0.5 if abs(c.scale - 0.5) > 0.01 else 1.0
        for name, fn, get, new, old in (
                ("Timebase", tb, lambda s: s.tscale, tnew, st.tscale),
                (f"CH{ch} V/div", vs, lambda s: s.channels[ch - 1].scale, vnew, c.scale)):
            self.step(f"{name}: set", put(fn, get, new))
            self.step(f"{name}: put back", put(fn, get, old))

        def grab():
            if not c.on:
                raise Skip("no channel is switched on")
            tr = self.run(lambda d: d.fetch_channel(ch))
            if tr.v.size < 10:
                raise Failed(f"only {tr.v.size} points")
            return f"{tr.v.size} points, {tr.v.min():.4g} … {tr.v.max():.4g} V, {1 / tr.dt:g} S/s"
        self.step(f"Waveform of CH{ch}", grab)

    # ---- outputs on
    def _out_supply(self):
        o, c = self.opts, self.caps
        volts, amps = min(o.volts, c.v_max), min(o.amps, c.i_max)
        before = self.run(lambda d: d.read_setpoints())

        def on():
            def job(d):
                d.set_voltage(volts)
                d.set_current(amps)
                d.set_output(True)
            self.run(job)
            self.sleep(o.settle)
            r = self.run(lambda d: d.poll(0.0, SupplyPoll()))
            self.no_errors()
            return self._judge_on(r.voltage, r.current, r.output, volts, amps, r.flags)

        def off():
            self.run(outputs_off)
            self.sleep(o.settle)
            r = self.run(lambda d: d.poll(0.0, SupplyPoll()))
            if r.output:
                raise Failed("it still says the output is on")
            return f"{_g(r.voltage)} V left"
        try:
            self.step(f"Output on at {volts:g} V", on)
        finally:
            self.step("Output off", off)
            self._put_back_supply(before)

    @staticmethod
    def _judge_on(v, i, on, volts, amps, flags=()):
        if on is False:
            raise Failed("it says the output is off")
        if flags:
            raise Failed("protection: " + ", ".join(flags))
        if not near(v, volts, 0.01, 0.05):
            raise Failed(f"set {volts:g} V, measures {_g(v)} V")
        if i is None or abs(i) > max(0.2 * amps, 0.01):
            raise Failed(f"{_g(i)} A with nothing connected?")
        return f"{_g(v)} V, {_g(i)} A"

    def _out_scpi(self):
        d0 = self.run(lambda d: d.d)
        chs = [n for n in ("", "1", "2", "3", "4") if d0.type == "supply" and all(
            d0.setting(x + n) for x in ("vset", "iset", "out")) and d0.reading("voltage" + n)
            and d0.reading("current" + n)]
        if not chs:
            self.step("Output", lambda: (_ for _ in ()).throw(Skip(
                "only a supply's output is tried; a load or other needs a source")))
        for n in chs:
            self._one_scpi_output(n, d0)

    def _one_scpi_output(self, n: str, d0):
        o = self.opts
        sv = d0.setting("vset" + n)
        volts = min(o.volts, sv.max) if sv.max is not None else o.volts
        before = self.run(lambda d: {k: d.read_setting(d.d.setting(k + n))
                                     for k in ("vset", "iset")})

        def on():
            def job(d):
                d.set("vset" + n, volts)
                d.set("iset" + n, o.amps)
                d.set("out" + n, True)
            self.run(job)
            self.sleep(o.settle)
            r = self.run(lambda d: d.poll(0.0))
            state = self.run(lambda d: d.read_setting(d.d.setting("out" + n)))
            self.no_errors()
            return self._judge_on(r.values["voltage" + n], r.values["current" + n], state,
                                  volts, o.amps)

        def off():
            self.run(lambda d: d.set("out" + n, False))
            self.sleep(o.settle)
            r = self.run(lambda d: d.poll(0.0))
            if self.run(lambda d: d.read_setting(d.d.setting("out" + n))):
                raise Failed("it still says the output is on")
            return f"{_g(r.values['voltage' + n])} V left"

        def back(d):
            for k, v in before.items():
                if v is not None:
                    d.set(k + n, v)
        label = f"Output {n}".strip()
        try:
            self.step(f"{label} on at {volts:g} V", on)
        finally:
            self.step(f"{label} off", off)
            self.step(f"{label}: settings put back", lambda: self.run(back))

    def _out_fgen(self):
        def put(ch, on):
            def job(d):
                d.set("output", on, ch)
                return d.read_channel(ch).output

            def body():
                if self.run(job) is not on:
                    raise Failed(f"it says the output is {'off' if on else 'on'}")
                self.no_errors()
            return body
        for ch in range(1, self.caps.nch + 1):
            try:
                self.step(f"CH{ch} output on", put(ch, True))
            finally:
                self.step(f"CH{ch} output off", put(ch, False))


# ---------------------------------------------------------------- results
def verdict(rows: list[dict]) -> str:
    if not rows:
        return ""
    return FAIL if any(r["result"] == FAIL for r in rows) else PASS


def counts(rows: list[dict]) -> str:
    n = {k: sum(r["result"] == k for r in rows) for k in (PASS, FAIL, SKIP)}
    return f"{n[PASS]} passed, {n[FAIL]} failed, {n[SKIP]} skipped"


def summary_line(idn: str, rows: list[dict], level: str, when: str = "") -> str:
    """One line for the compatibility list or an issue. No serial number."""
    sn = tapes.serial_in(idn)
    if sn:
        idn = idn.replace(sn, "…")
    return f"{idn} | {when or time.strftime('%Y-%m-%d')} | {LEVELS[level]} | " \
           f"{verdict(rows)}: {counts(rows)}"


def tested_text(idn: str, level: str, when: str = "") -> str:
    """For a definition's `tested`: model, firmware, date, how far."""
    p = [x.strip() for x in idn.split(",")]
    who = f"{p[1]} fw {p[3]}" if len(p) > 3 else p[0]
    return f"{who}, {when or time.strftime('%Y-%m-%d')}, {level}".replace('"', "'")


def mark_tested(path: str, text: str):
    """Write `tested = "…"` into a definition file, before its tables."""
    import tomllib
    from pathlib import Path

    from .instruments.scpi_generic import parse_definition
    if '"' in text or "\\" in text or not text.isprintable():
        raise ValueError("bad characters in the text")
    p = Path(path)
    old = p.read_text(encoding="utf-8")
    head, sep, rest = old.partition("\n[[")
    line = f'tested = "{text}"'
    if re.search(r"(?m)^tested\s*=.*$", head):
        head = re.sub(r"(?m)^tested\s*=.*$", lambda _m: line, head)
    else:
        head = head.rstrip("\n") + "\n" + line + "\n"
    new = head + sep + rest
    if parse_definition(tomllib.loads(new), path).tested != text:
        raise ValueError("couldn't add the line")
    p.write_text(new, encoding="utf-8")


@dataclass
class ReplayResult:
    rows: list
    problems: list               # steps that came out differently ([]: same)
    unknown: list                # questions the tape has no answer to


def _plain(text: str, idn: str) -> str:
    """Without the serial number: a tape's rows may not have it any more."""
    sn = tapes.serial_in(idn)
    return text.replace(sn, "X" * len(sn)) if sn else text


def replay(path: str, tape=None) -> ReplayResult:
    """The taped check again, against the tape."""
    tape = tape or tapes.load(path)
    if not tape.rows:
        raise tapes.TapeError("This tape isn't of a hardware check")
    drv = tapes.open_tape(path, tape)
    caps = drv.identify()
    known = Options.__dataclass_fields__
    opts = Options(**{k: v for k, v in tape.options.items() if k in known})
    chk = HardwareCheck(lambda fn: fn(drv), caps, opts, sleep=lambda s: None)
    rows = chk.go()
    problems = []
    now = {r["step"]: r for r in rows}
    for r in tape.rows:
        n = now.get(r["step"])
        if n is None:
            problems.append(f"{r['step']}: not done any more")
        elif n["result"] != r["result"]:
            problems.append(f"{r['step']}: was {r['result']} ({r['detail']}), now "
                            f"{n['result']} ({n['detail']})")
        elif r["result"] == PASS and _plain(n["detail"], caps.idn) != _plain(r["detail"], caps.idn):
            problems.append(f"{r['step']}: was {r['detail']!r}, now {n['detail']!r}")
    link = getattr(drv.t, "replay", None)
    return ReplayResult(rows, problems, list(getattr(link, "unknown", [])))
