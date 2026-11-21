"""Math channels: traces computed from other instruments' signals.

An expression names signals as  <instrument>.<quantity>  using the
instrument's identifier (signals.slug of its tab name), plus numpy-style math:

    efficiency %      _66321d_out.power / _6632a_in.power * 100
    DUT power         _66321d_dut.voltage * ppk2.current
    smoothed          avg(ppk2.current, 5)            5 s rolling mean
    energy            (integrate on)  _66321d_dut.power          → joules

Signals are put on one time grid (linear interpolation, holding the first /
last value at the ends) and evaluated as arrays, so the same expression works
live (the Math window evaluates the last minute every tick and shows the
newest value) and on a recorded session (session viewer → Math).

Functions: avg(x, s) rolling mean over s seconds, max/min/abs/sqrt/log10/…,
clip(x, lo, hi), where(cond, a, b), dt (grid step), t (time).
"""
from __future__ import annotations

import ast
import math
from collections import deque
from dataclasses import asdict, dataclass

import numpy as np

from .signals import slug

GRID_STEP = 0.05          # s: live evaluation grid
HISTORY = 60.0            # s: how far back live signals are kept


class MathError(ValueError):
    pass


@dataclass
class Channel:
    name: str = "math1"
    expr: str = ""
    unit: str = ""
    integrate: bool = False
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Channel":
        c = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        c.name, c.expr, c.unit = str(c.name), str(c.expr), str(c.unit)
        c.integrate, c.enabled = bool(c.integrate), bool(c.enabled)
        return c

    @classmethod
    def load_list(cls, items) -> list["Channel"]:
        """Channels from bench.json / a setup, skipping any damaged entry."""
        out = []
        for d in items if isinstance(items, list) else []:
            try:
                out.append(cls.from_dict(d))
            except (TypeError, ValueError, AttributeError):
                continue
        return out


class _Inst:
    """inst.quantity attribute access in expressions."""

    def __init__(self, name, sigs):
        self._name, self._sigs = name, sigs

    def __getattr__(self, q):
        if q.startswith("_") and q not in self._sigs:
            raise AttributeError(q)
        try:
            return self._sigs[q]
        except KeyError:
            raise MathError(f"{self._name} has no '{q}' (it has: "
                            + ", ".join(sorted(self._sigs)) + ")") from None


def rolling_mean(t: np.ndarray, y: np.ndarray, seconds: float) -> np.ndarray:
    """Mean of y over the `seconds` before each t (time-based window)."""
    y = np.asarray(y, float)
    good = np.isfinite(y)
    cs = np.concatenate(([0.0], np.cumsum(np.where(good, y, 0.0))))
    cn = np.concatenate(([0], np.cumsum(good)))
    a = np.searchsorted(t, t - float(seconds), side="left")
    b = np.arange(1, t.size + 1)
    n = cn[b] - cn[a]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, (cs[b] - cs[a]) / np.maximum(n, 1), np.nan)


def resample(t: np.ndarray, v: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """v on the grid. A signal sampled more densely than the grid is averaged
    over each grid step (a 100 kS/s current must not be point-sampled every
    50 ms: its bursts would alias); a sparser one is interpolated."""
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    if t.size < 2:
        return np.full(grid.shape, v[0] if v.size else np.nan)
    if grid.size < 2:
        return np.interp(grid, t, v)
    step = float(np.median(np.diff(grid)))
    if float(np.median(np.diff(t))) * 2 >= step:
        return np.interp(grid, t, v)
    good = np.isfinite(v)
    cs = np.concatenate(([0.0], np.cumsum(np.where(good, v, 0.0))))
    cn = np.concatenate(([0], np.cumsum(good)))
    a = np.searchsorted(t, grid - step, side="right")
    b = np.searchsorted(t, grid, side="right")
    n = cn[b] - cn[a]
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(n > 0, (cs[b] - cs[a]) / np.maximum(n, 1), np.nan)
    # outside the data (or an empty bin): the nearest value, like interp
    gap = ~np.isfinite(out)
    if gap.any():
        out[gap] = np.interp(grid[gap], t, v)
    return out


def names_in(expr: str) -> set[tuple[str, str]]:
    """(instrument id, quantity) pairs an expression uses."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise MathError(f"Syntax error: {e.msg}") from None
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            out.add((node.value.id, node.attr))
    return out


def evaluate(expr: str, series: dict, grid: np.ndarray | None = None,
             step: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """series: {(inst id, quantity): (t, v)}. Returns (grid, values)."""
    if not expr.strip():
        raise MathError("Empty expression")
    used = names_in(expr)
    missing = [f"{i}.{q}" for i, q in used if (i, q) not in series and i not in ("np", "math")]
    if missing:
        raise MathError("Unknown signal: " + ", ".join(sorted(missing)))
    keys = [k for k in used if k in series]
    if grid is None:
        if not keys:
            raise MathError("The expression uses no signals")
        lo = max(series[k][0][0] for k in keys)
        hi = min(series[k][0][-1] for k in keys)
        if step is None:
            # the finest of the signals' own steps, but at most 20 000 points
            steps = [np.median(np.diff(series[k][0])) for k in keys if len(series[k][0]) > 1]
            step = min(steps) if steps else 1.0
            if hi > lo:
                step = max(step, (hi - lo) / 20_000)
        if hi < lo:
            raise MathError("The signals don't overlap in time")
        grid = np.arange(lo, hi + step / 2, step) if hi > lo else np.array([lo])
    insts: dict[str, dict] = {}
    for (i, q) in keys:
        t, v = series[(i, q)]
        t = np.asarray(t, float)
        v = np.asarray(v, float)
        insts.setdefault(i, {})[q] = resample(t, v, grid)
    dt = float(np.median(np.diff(grid))) if grid.size > 1 else GRID_STEP

    def avg(x, seconds):
        return rolling_mean(grid, np.broadcast_to(np.asarray(x, float), grid.shape), seconds)

    env = {k: getattr(np, k) for k in ("sqrt", "log", "log10", "exp", "abs", "sin", "cos",
                                       "minimum", "maximum", "clip", "where", "pi")}
    env.update(max=np.maximum, min=np.minimum, avg=avg, t=grid, dt=dt, math=math, np=np)
    env.update({i: _Inst(i, s) for i, s in insts.items()})
    try:
        with np.errstate(all="ignore"):
            y = eval(compile(expr, "<math>", "eval"), {"__builtins__": {}}, env)  # noqa: S307
        y = np.broadcast_to(np.asarray(y, float), grid.shape).astype(float)
    except MathError:
        raise
    except Exception as e:      # noqa: BLE001 - a typo must never reach the GUI as a crash
        raise MathError(f"{type(e).__name__}: {e}") from None
    return grid, y


def integrate(t: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Running integral (trapezoid), NaNs counted as 0."""
    y = np.nan_to_num(np.asarray(y, float))
    if t.size < 2:
        return np.zeros_like(y)
    seg = (y[1:] + y[:-1]) / 2 * np.diff(t)
    return np.concatenate(([0.0], np.cumsum(seg)))


class LiveMath:
    """Keeps the last minute of every signal and evaluates channels on it."""

    def __init__(self):
        self.hist: dict[tuple[str, str], deque] = {}
        self.channels: list[Channel] = []
        self.totals: dict[str, float] = {}       # integrate: running total
        self.last_t: dict[str, float] = {}
        self.errors: dict[str, str] = {}

    def feed(self, inst_name: str, signals: dict):
        iid = slug(inst_name)
        for q, (t, v) in signals.items():
            d = self.hist.setdefault((iid, q), deque())
            if len(t) > 50:
                # a PPK2 block: keep 1 ms means (the grid is 50 ms anyway)
                step = float(np.median(np.diff(t)))
                k = max(1, int(round(1e-3 / step))) if step > 0 else 1
                n = (len(t) // k) * k
                t = np.asarray(t[:n]).reshape(-1, k).mean(1)
                v = np.asarray(v[:n], float).reshape(-1, k).mean(1)
            d.extend(zip(np.asarray(t, float).tolist(), np.asarray(v, float).tolist()))
            cutoff = d[-1][0] - HISTORY
            while d and d[0][0] < cutoff:
                d.popleft()

    def available(self) -> list[str]:
        return sorted(f"{i}.{q}" for i, q in self.hist)

    def series(self) -> dict:
        out = {}
        for k, d in self.hist.items():
            if d:
                a = np.array(d)
                out[k] = (a[:, 0], a[:, 1])
        return out

    def tick(self, now: float) -> dict[str, float]:
        """Newest value of each enabled channel (integrated ones: the total)."""
        series = self.series()
        out = {}
        grid = np.arange(now - HISTORY, now + GRID_STEP / 2, GRID_STEP)
        for ch in self.channels:
            if not ch.enabled or not ch.expr.strip():
                continue
            try:
                _, y = evaluate(ch.expr, series, grid)
                v = float(y[-1])
                self.errors.pop(ch.name, None)
            except MathError as e:
                self.errors[ch.name] = str(e)
                continue
            except Exception as e:      # noqa: BLE001 - one channel must not stop the others
                self.errors[ch.name] = f"{type(e).__name__}: {e}"
                continue
            if ch.integrate:
                last = self.last_t.get(ch.name)
                if last is not None and math.isfinite(v):
                    self.totals[ch.name] = self.totals.get(ch.name, 0.0) + v * (now - last)
                self.last_t[ch.name] = now
                v = self.totals.get(ch.name, 0.0)
            out[ch.name] = v
        return out

    def reset_totals(self):
        self.totals.clear()
        self.last_t.clear()
