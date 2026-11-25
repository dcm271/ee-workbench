"""Compare two recorded sessions, e.g. the current of a board before and after a
change. Also from the command line, for a script or CI:

    python -m otterbench.compare base.otter new.otter
    python -m otterbench.compare base.otter new.otter --align rise:ppk2.current_A:0.01 \\
        --check "ppk2.current_A mean +5%" --check "ppk2.current_A max +0.02"

A check is SIGNAL STAT and how far it may go up and / or down, in % of the base
or in the signal's own unit. Exit code: 0 all passed, 1 a check failed, 2 it
couldn't be compared.

Signals are matched by tab name and column. Time is put on one axis by the start
of the session, by a note, or by the moment a signal crosses a level. Sequencer
results are compared row by row.
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from dataclasses import dataclass, field

import numpy as np

from .session import SessionReader
from .signals import slug

STATS = ("mean", "min", "max", "p-p", "std", "integral", "duration")
MAX_DIFF_POINTS = 200_000


class CompareError(ValueError):
    pass


@dataclass
class Signal:
    key: str                     # tab.column
    t: np.ndarray                # seconds from the session's reference moment, or row number
    y: np.ndarray
    unit: str = ""
    rows: bool = False           # a sequencer column: t is the row number

    def cut(self, a: float | None, b: float | None) -> "Signal":
        if self.rows or (a is None and b is None):
            return self
        ok = np.ones(self.t.size, bool)
        if a is not None:
            ok &= self.t >= a
        if b is not None:
            ok &= self.t <= b
        return Signal(self.key, self.t[ok], self.y[ok], self.unit)


def unit_of(column: str) -> str:
    m = re.search(r"_([A-Za-zΩ%]{1,3})$", column)
    return m.group(1) if m else ""


def gap_limit(t: np.ndarray) -> float:
    return max(10 * float(np.median(np.diff(t))), 1.0) if t.size > 2 else math.inf


def signals(s: SessionReader) -> dict[str, Signal]:
    """Every numeric column, on the session's own time."""
    out: dict[str, Signal] = {}
    for st in s.streams:
        rows = st.model == "sequence"
        for col, y in st.data.items():
            t = np.arange(1.0, y.size + 1) if rows else st.t
            ok = np.isfinite(y)
            if not ok.any():
                continue
            key, n = f"{slug(st.name)}.{col}", 2
            while key in out:            # the same tab with other columns, or a second run
                key, n = f"{slug(st.name)}_{n}.{col}", n + 1
            out[key] = Signal(key, t[ok], y[ok], unit_of(col), rows)
    return out


# ---------------------------------------------------------------- one time axis
@dataclass
class Align:
    how: str = "start"           # start | note | rise | fall
    text: str = ""               # the note, or the signal
    level: float = 0.0
    shift: float = 0.0           # s, added to the second session

    @classmethod
    def parse(cls, text: str) -> "Align":
        how, _, rest = text.partition(":")
        how = how.strip().lower()
        if how == "start" and not rest:
            return cls()
        if how == "note" and rest.strip():
            return cls("note", rest.strip())
        if how in ("rise", "fall") and ":" in rest:
            sig, _, lev = rest.rpartition(":")
            try:
                return cls(how, sig.strip(), float(lev))
            except ValueError:
                pass
        raise CompareError(f"“{text}” isn't start, note:TEXT, rise:SIGNAL:LEVEL or "
                           "fall:SIGNAL:LEVEL")

    def zero(self, s: SessionReader, sigs: dict[str, Signal], which: str) -> float:
        """The moment (session time) that becomes 0."""
        if self.how == "start":
            return s.span()[0]
        if self.how == "note":
            hit = next((t for t, text in s.notes if self.text.lower() in text.lower()), None)
            if hit is None:
                raise CompareError(f"{which}: no note says “{self.text}”")
            return hit
        sig = sigs.get(self.text)
        if sig is None or sig.rows:
            raise CompareError(f"{which}: there is no signal {self.text}")
        a, b = sig.y[:-1], sig.y[1:]
        hits = np.flatnonzero((a < self.level) & (b >= self.level) if self.how == "rise" else
                              (a > self.level) & (b <= self.level))
        if not hits.size:
            raise CompareError(f"{which}: {self.text} never goes "
                               f"{'above' if self.how == 'rise' else 'below'} {self.level:g}")
        k = int(hits[0])
        f = (self.level - sig.y[k]) / (sig.y[k + 1] - sig.y[k])
        return float(sig.t[k] + f * (sig.t[k + 1] - sig.t[k]))


# ---------------------------------------------------------------- numbers
def stats(sig: Signal) -> dict[str, float]:
    """The mean is over time (not over readings) and leaves gaps out."""
    t, y = sig.t, sig.y
    out = {k: math.nan for k in STATS}
    if not y.size:
        return out
    out.update({"min": float(y.min()), "max": float(y.max()), "p-p": float(np.ptp(y)),
                "std": float(y.std()), "mean": float(y.mean())})
    if sig.rows:
        return out
    if y.size > 1:
        dt = np.diff(t)
        ok = (dt > 0) & (dt <= gap_limit(t))
        total = float(dt[ok].sum())
        area = float((dt * (y[:-1] + y[1:]) / 2)[ok].sum())
        out["duration"] = float(t[-1] - t[0])
        if total > 0:
            out["mean"], out["integral"] = area / total, area
    return out


def stat_unit(stat: str, unit: str) -> str:
    if stat == "duration":
        return "s"
    if stat == "integral":
        return {"A": "C", "W": "J"}.get(unit, f"{unit}·s" if unit else "")
    return unit


def difference(a: Signal, b: Signal):
    """(t, new - base) where both have readings."""
    if a.y.size < 2 or b.y.size < 2:
        return np.array([]), np.array([])
    lo, hi = max(a.t[0], b.t[0]), min(a.t[-1], b.t[-1])
    t = a.t[(a.t >= lo) & (a.t <= hi)]
    t = t[::max(1, t.size // MAX_DIFF_POINTS)]
    if not t.size:
        return t, t
    d = np.interp(t, b.t, b.y) - np.interp(t, a.t, a.y)
    if not a.rows:
        k = np.clip(np.searchsorted(b.t, t), 1, b.t.size - 1)
        d[(b.t[k] - b.t[k - 1]) > gap_limit(b.t)] = np.nan
    return t, d


# ---------------------------------------------------------------- limits
@dataclass
class Limit:
    signal: str
    stat: str = "mean"
    up: float | None = None      # how far it may rise / fall (None: not judged)
    down: float | None = None
    up_pct: bool = True
    down_pct: bool = True

    @classmethod
    def parse(cls, text: str) -> "Limit":
        p = text.split()
        if len(p) < 3 or p[1] not in STATS:
            raise CompareError(f"“{text}” isn't SIGNAL STAT +RISE and / or -DROP "
                               f"(stat: {', '.join(STATS)})")
        out = cls(p[0], p[1])
        for x in p[2:]:
            m = re.fullmatch(r"([+-]|±|\+-)(\d+\.?\d*(?:e[-+]?\d+)?|\.\d+)(%?)", x, re.I)
            if not m:
                raise CompareError(f"“{x}” isn't a tolerance: +5%, -0.01, ±2%")
            v, pct = float(m.group(2)), bool(m.group(3))
            if m.group(1) in ("+", "±", "+-"):
                out.up, out.up_pct = v, pct
            if m.group(1) in ("-", "±", "+-"):
                out.down, out.down_pct = v, pct
        return out

    def text(self) -> str:
        def one(sign, v, pct):
            return "" if v is None else f" {sign}{v:g}{'%' if pct else ''}"
        return f"{self.signal} {self.stat}" + one("+", self.up, self.up_pct) + \
            one("-", self.down, self.down_pct)

    def check(self, a: float, b: float) -> tuple[bool, str]:
        if math.isnan(a) or math.isnan(b):
            return False, "no value"
        d, ok, why = b - a, True, []
        for v, pct, over, word in ((self.up, self.up_pct, d, "up"),
                                   (self.down, self.down_pct, -d, "down")):
            if v is None:
                continue
            room = abs(a) * v / 100 if pct else v
            if over > room + 1e-12 * max(abs(a), abs(b)):
                ok = False
                why.append(f"{word} {over:.4g}, {room:.4g} allowed")
        return ok, "; ".join(why)


# ---------------------------------------------------------------- the comparison
@dataclass
class Row:
    key: str
    unit: str
    base: dict
    new: dict


@dataclass
class Comparison:
    names: tuple = ("", "")
    base: dict = field(default_factory=dict)         # key -> Signal, on the common axis
    new: dict = field(default_factory=dict)
    only: tuple = ((), ())                           # keys in just one of them
    rows: list = field(default_factory=list)
    checks: list = field(default_factory=list)       # (text, ok, why)
    align: Align = field(default_factory=Align)

    @property
    def verdict(self) -> str:
        return "" if not self.checks else "PASS" if all(c[1] for c in self.checks) else "FAIL"


def compare(a: SessionReader, b: SessionReader, align: Align | None = None,
            t_from: float | None = None, t_to: float | None = None,
            limits: list[Limit] | None = None) -> Comparison:
    align = align or Align()
    sa, sb = signals(a), signals(b)
    za, zb = align.zero(a, sa, "Base"), align.zero(b, sb, "New")
    out = Comparison((a.path, b.path), align=align)
    common = [k for k in sa if k in sb]
    out.only = (tuple(k for k in sa if k not in sb), tuple(k for k in sb if k not in sa))
    for k in common:
        for src, zero, dst in ((sa, za, out.base), (sb, zb - align.shift, out.new)):
            s = src[k]
            dst[k] = s if s.rows else Signal(k, s.t - zero, s.y, s.unit).cut(t_from, t_to)
        out.rows.append(Row(k, sa[k].unit, stats(out.base[k]), stats(out.new[k])))
    by = {r.key: r for r in out.rows}
    for lim in limits or []:
        r = by.get(lim.signal)
        if r is None:
            where = "the base" if lim.signal in out.only[1] else \
                "the new session" if lim.signal in out.only[0] else "either session"
            out.checks.append((lim.text(), False, f"not in {where}"))
        else:
            out.checks.append((lim.text(), *lim.check(r.base[lim.stat], r.new[lim.stat])))
    return out


def percent(a: float, b: float) -> float:
    return 100 * (b - a) / abs(a) if a else (0.0 if b == a else math.nan)


TABLE = ("signal", "stat", "unit", "base", "new", "difference", "percent")


def table(c: Comparison, keys=None) -> list[tuple]:
    out = []
    for r in c.rows:
        if keys is not None and r.key not in keys:
            continue
        for s in STATS:
            a, b = r.base[s], r.new[s]
            if not (math.isnan(a) and math.isnan(b)):
                out.append((r.key, s, stat_unit(s, r.unit), a, b, b - a, percent(a, b)))
    return out


def save_csv(c: Comparison, path: str, keys=None):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(TABLE)
        w.writerows(table(c, keys))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="otterbench-compare",
                                 description="Compare two recorded sessions (.otter).")
    ap.add_argument("base")
    ap.add_argument("new")
    ap.add_argument("--align", default="start", metavar="HOW",
                    help="start (default), note:TEXT, rise:SIGNAL:LEVEL, fall:SIGNAL:LEVEL")
    ap.add_argument("--shift", type=float, default=0.0, help="seconds added to the new session")
    ap.add_argument("--from", dest="t_from", type=float, help="seconds after the aligned moment")
    ap.add_argument("--to", dest="t_to", type=float)
    ap.add_argument("--check", action="append", default=[], metavar="'SIGNAL STAT +5%%'")
    ap.add_argument("--signal", action="append", help="only list this signal (repeatable)")
    ap.add_argument("--csv", help="write the table here")
    ap.add_argument("--report", help="write an HTML / PDF report here")
    a = ap.parse_args(argv)
    readers = []
    try:
        limits = [Limit.parse(x) for x in a.check]
        align = Align.parse(a.align)
        align.shift = a.shift
        for p in (a.base, a.new):
            readers.append(SessionReader(p))
        c = compare(*readers, align, a.t_from, a.t_to, limits)
        keys = set(a.signal or []) | {x.signal for x in limits} or None
        if a.csv:
            save_csv(c, a.csv, keys)
        if a.report:
            from . import report
            report.save(report.compare_report("Comparison", c, keys), a.report)
    except Exception as e:      # noqa: BLE001 - a missing or damaged file, a full disk
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        for r in readers:
            r.close()
    if not c.rows:
        print("error: the sessions have no signal in common", file=sys.stderr)
        return 2
    for key, st, unit, x, y, d, pct in table(c, keys):
        print(f"{key:<28} {st:<9} {x:>12.6g} {y:>12.6g} {unit:<4} {d:>+12.4g} {pct:>+8.2f} %")
    for text, ok, why in c.checks:
        print(("PASS  " if ok else "FAIL  ") + text + (f"   ({why})" if why else ""))
    return 1 if c.verdict == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
