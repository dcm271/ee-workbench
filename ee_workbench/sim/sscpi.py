"""A simulator for any generic SCPI definition (instruments/scpi_defs).

It understands exactly the commands the definition describes: each setting's
`set` template is turned back into a pattern, so `:SOUR2:VOLT 3.3` stores
vset2 = 3.3, and its `query` answers with the stored value. Readings evaluate
the definition's `sim` expression over the settings (plus t and noise()).
Anything else is answered like a real instrument would: -113 "Undefined
header" in the error queue, and no reply to an unknown query.
"""
from __future__ import annotations

import math
import random
import re
import time

from ..instruments.scpi_generic import Definition, fill, parse_bool
from .base import SimBase

_VALUE = r"(?P<value>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|MIN|MAX|DEF)"
_BOOL = r"(?P<value>ON|OFF|1|0)"


def _pattern(template: str, ch: int) -> re.Pattern:
    t = re.escape(fill(template, ch=ch))
    t = t.replace(re.escape("{value}"), _VALUE).replace(re.escape("{on_off}"), _BOOL)
    t = t.replace(re.escape("{1_0}"), _BOOL)
    t = t.replace(r"\ ", r"\s+")
    return re.compile("^" + t + "$", re.I)


def _sim_reply(opt) -> str:
    if opt.sim:
        return opt.sim
    if opt.reply:
        return re.sub(r"[\^$\\]|\"\?", "", opt.reply)
    return opt.label


class SimScpi(SimBase):
    def __init__(self, d: Definition):
        super().__init__()
        self.d = d
        self.t0 = time.monotonic()
        self.errors: list[str] = []
        self.values: dict = {}
        self.sets = []          # (pattern, setting, option | None)
        self.queries = {}       # normalised query -> ("setting" | "reading", obj)
        for s in d.settings:
            if s.kind == "choice":
                for o in s.options:
                    self.sets.append((_pattern(o.set, s.ch), s, o))
                self.values[s.name] = s.sim or s.options[0].label
            else:
                self.sets.append((_pattern(s.set, s.ch), s, None))
                if s.kind == "bool":
                    self.values[s.name] = parse_bool(s.sim) if s.sim else False
                else:
                    self.values[s.name] = float(s.sim) if s.sim else 0.0
            if s.query:
                self.queries[self._norm(fill(s.query, ch=s.ch))] = ("setting", s)
        for r in d.readings:
            if r.query:
                self.queries[self._norm(fill(r.query, ch=r.ch))] = ("reading", r)
            if r.query_from:
                base = d.setting(r.query_from) or d.setting(f"{r.query_from}{r.ch}")
                for o in base.options:
                    if o.read:
                        self.queries[self._norm(fill(o.read, ch=r.ch))] = ("reading", r)

    @staticmethod
    def _norm(cmd: str) -> str:
        return re.sub(r"\s+", " ", cmd.strip()).upper()

    def _env(self) -> dict:
        env = dict(self.values)
        env.update(t=time.monotonic() - self.t0, noise=lambda sd: random.gauss(0.0, sd),
                   math=math, min=min, max=max, abs=abs)
        return env

    def _reading(self, r) -> str:
        expr = r.sim or "0"
        if r.ch:
            # sim expressions are written once with {ch}: "vset{ch} if out{ch} else 0"
            expr = expr.replace("{ch}", str(r.ch))
        try:
            v = float(eval(expr, {"__builtins__": {}}, self._env()))   # noqa: S307 - our own file
        except Exception as e:      # noqa: BLE001
            raise RuntimeError(f"sim expression of {r.name}: {e}") from None
        return f"{v:+.9E}"

    def _setting_reply(self, s) -> str:
        v = self.values.get(s.name)
        if s.kind == "choice":
            opt = s.option(v) or s.options[0]
            return _sim_reply(opt)
        if s.kind == "bool":
            return "1" if v else "0"
        return f"{float(v):.6g}"

    def write(self, cmd: str):
        cmd = cmd.strip()
        if not cmd:
            return
        n = self._norm(cmd)
        if n == "*IDN?":
            model = self.d.idn[0] if self.d.idn else self.d.id
            self._out.append(f"EE-Workbench-SIM,{model},SIM0001,1.0")
            return
        if n in ("*CLS", "*RST", "SYST:REM", ":SYST:REM") or n.endswith("INIT:CONT OFF") \
                or n.startswith(":FORM:ELEM"):
            if n == "*CLS":
                self.errors.clear()
            return
        if self.d.error_query and n == self._norm(self.d.error_query):
            self._out.append(self.errors.pop(0) if self.errors else '+0,"No error"')
            return
        if "?" in n:
            hit = self.queries.get(n)
            if hit is None:
                self.errors.append('-113,"Undefined header"')
                return
            kind, obj = hit
            self._out.append(self._reading(obj) if kind == "reading" else
                             self._setting_reply(obj))
            return
        for c in self.d.off:
            for ch in range(1, self.d.channels + 1):
                if self._norm(fill(c, ch=ch)) == n:
                    for s in self.d.settings:
                        if s.output and (not s.ch or s.ch == ch or "{ch}" not in c):
                            self.values[s.name] = False
                    return
        for pat, s, opt in self.sets:
            m = pat.match(cmd)
            if not m:
                continue
            if opt is not None:
                self.values[s.name] = opt.label
            elif s.kind == "bool":
                self.values[s.name] = parse_bool(m.group("value"))
            else:
                raw = m.group("value").upper()
                if raw in ("MIN", "MAX", "DEF"):
                    v = {"MIN": s.min, "MAX": s.max}.get(raw)
                    self.values[s.name] = float(v if v is not None else 0.0)
                else:
                    v = float(raw)
                    if (s.min is not None and v < s.min) or (s.max is not None and v > s.max):
                        self.errors.append('-222,"Data out of range"')
                        return
                    self.values[s.name] = v
            return
        self.errors.append('-113,"Undefined header"')
