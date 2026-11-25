"""Waveforms for a function generator: read from CSV or a scope capture, cut
and resampled to what the generator takes."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MAX_POINTS = 8192
MIN_POINTS = 16


@dataclass
class Wave:
    v: np.ndarray                # volts, evenly spaced
    seconds: float               # one period
    name: str = ""

    @property
    def amplitude(self) -> float:
        return float(self.v.max() - self.v.min())

    @property
    def offset(self) -> float:
        return float(self.v.max() + self.v.min()) / 2

    def unit(self) -> np.ndarray:
        """-1 … +1."""
        return np.clip((self.v - self.offset) / (self.amplitude / 2), -1.0, 1.0)

    def describe(self) -> str:
        return (f"{self.v.size} points, {self.seconds:.6g} s ({1 / self.seconds:.6g} Hz), "
                f"{self.amplitude:.4g} Vpp, offset {self.offset:.4g} V")


def make(t, v, name: str = "", points: int = MAX_POINTS) -> Wave:
    t, v = np.asarray(t, float), np.asarray(v, float)
    ok = np.isfinite(t) & np.isfinite(v)
    t, v = t[ok], v[ok]
    if t.size < 2:
        raise ValueError("at least two points are needed")
    if not np.all(np.diff(t) > 0):
        raise ValueError("the times must go up")
    step = float(t[-1] - t[0]) / (t.size - 1)
    seconds = float(t[-1] - t[0]) + step         # the last sample lasts one step too
    n = int(min(max(t.size, MIN_POINTS), points))
    grid = t[0] + np.arange(n) * seconds / n
    out = np.interp(grid, t, v)
    if float(out.max() - out.min()) < 1e-4:
        raise ValueError("the waveform is flat (less than 0.1 mV)")
    return Wave(out, seconds, name)


def from_csv(path: str, column: int = 1, rate: float = 0.0) -> Wave:
    """Columns: time (s) and volts; or volts alone with a sample rate."""
    rows = []
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            parts = line.replace(";", ",").replace("\t", ",").strip().split(",")
            try:
                rows.append([float(x) if x.strip() else np.nan for x in parts])
            except ValueError:
                if rows:
                    raise ValueError(f"not a number: {line.strip()[:60]!r}") from None
            if len(rows) > 5_000_000:
                raise ValueError("more than 5 million lines")
    if not rows:
        raise ValueError("no numbers in it")
    width = min(len(r) for r in rows)
    a = np.array([r[:width] for r in rows], float)
    if width == 1:
        if rate <= 0:
            raise ValueError("one column only: give the sample rate")
        return make(np.arange(len(a)) / rate, a[:, 0], path)
    if not 1 <= column < width:
        raise ValueError(f"there is no column {column + 1}")
    return make(a[:, 0], a[:, column], path)
