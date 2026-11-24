"""Waveform measurements (pure numpy, no Qt): what a scope's "Measure" menu
would show for a captured current or voltage waveform."""
from __future__ import annotations

import numpy as np


def levels(y: np.ndarray) -> tuple[float, float]:
    """High and low levels of a two-level (pulsed) waveform: the mean of the
    samples above / below the 50 % point. Same idea as the 663xx's
    16-bin histogram method, and identical for clean pulses."""
    lo_, hi_ = float(y.min()), float(y.max())
    mid = (lo_ + hi_) / 2
    hi = y[y >= mid]
    lo = y[y < mid]
    return (float(hi.mean()) if hi.size else hi_), (float(lo.mean()) if lo.size else lo_)


def _crossings(t, y, level, rising: bool) -> np.ndarray:
    """Times where y crosses `level` (linear interpolation)."""
    a, b = y[:-1], y[1:]
    idx = np.flatnonzero((a < level) & (b >= level)) if rising else \
        np.flatnonzero((a > level) & (b <= level))
    if not idx.size:
        return np.array([])
    frac = (level - a[idx]) / (b[idx] - a[idx])
    return t[idx] + frac * (t[idx + 1] - t[idx])


def _edge_times(t, y, lo, hi, rising: bool) -> list[float]:
    """10-90 % transition times of each edge."""
    span = hi - lo
    l10, l90 = lo + 0.1 * span, lo + 0.9 * span
    c10 = _crossings(t, y, l10, rising)
    c90 = _crossings(t, y, l90, rising)
    out = []
    first, second = (c10, c90) if rising else (c90, c10)
    for t0 in first:
        later = second[second >= t0]
        if later.size:
            out.append(float(later[0] - t0))
    return out


def measure(t: np.ndarray, y: np.ndarray, unit: str = "A",
            other_level: float | None = None) -> dict:
    """Automatic measurements. `unit` is "A" or "V". For a current waveform,
    `other_level` (the output voltage) turns charge into energy."""
    t = np.asarray(t, float)
    y = np.asarray(y, float)
    m: dict = {}
    if y.size < 2:
        return m
    dur = float(t[-1] - t[0])
    m["mean"] = float(y.mean())
    m["rms"] = float(np.sqrt(np.mean(y ** 2)))
    m["min"], m["max"] = float(y.min()), float(y.max())
    m["pk-pk"] = m["max"] - m["min"]
    m["high"], m["low"] = levels(y)
    m["duration"] = dur
    span = m["high"] - m["low"]
    # Only treat it as a pulse train if the two levels are well separated:
    # by 10 % of the larger level and by 3x the sample-to-sample noise.
    noise = float(np.std(np.diff(y))) / np.sqrt(2)
    if span > 0.1 * max(abs(m["high"]), abs(m["low"]), 1e-12) and span > 3 * noise:
        mid = m["low"] + span / 2
        rises = _crossings(t, y, mid, True)
        falls = _crossings(t, y, mid, False)
        if rises.size >= 2:
            m["period"] = float(np.median(np.diff(rises)))
            m["frequency"] = 1 / m["period"] if m["period"] > 0 else None
        widths = [float(f - r) for r in rises for f in falls[falls > r][:1]]
        if widths:
            m["width"] = float(np.median(widths))
            if "period" in m:
                m["duty"] = m["width"] / m["period"]
        rt = _edge_times(t, y, m["low"], m["high"], True)
        ft = _edge_times(t, y, m["low"], m["high"], False)
        if rt:
            m["rise"] = float(np.median(rt))
        if ft:
            m["fall"] = float(np.median(ft))
    if unit == "A":
        q = float(np.trapezoid(y, t))
        m["charge"] = q                        # coulombs over the record
        if other_level is not None:
            m["energy"] = q * other_level      # joules (constant-voltage approximation)
    return m


def between(t, y, t1: float, t2: float, unit: str = "A",
            other_level: float | None = None) -> dict:
    """Measurements of the part of the record between two cursor times."""
    t = np.asarray(t, float)
    y = np.asarray(y, float)
    a, b = sorted((t1, t2))
    sel = (t >= a) & (t <= b)
    m = {"Δt": b - a, "1/Δt": (1 / (b - a)) if b > a else None,
         "y1": float(np.interp(t1, t, y)), "y2": float(np.interp(t2, t, y))}
    m["Δy"] = m["y2"] - m["y1"]
    if sel.sum() >= 2:
        ys, ts = y[sel], t[sel]
        m.update(mean=float(ys.mean()), rms=float(np.sqrt(np.mean(ys ** 2))),
                 min=float(ys.min()), max=float(ys.max()))
        if unit == "A":
            m["charge"] = float(np.trapezoid(ys, ts))
            if other_level is not None:
                m["energy"] = m["charge"] * other_level
    return m


def histogram(y, bins: int = 64) -> tuple[np.ndarray, np.ndarray]:
    """(bin centres, fraction of time in each bin)."""
    y = np.asarray(y, float)
    counts, edges = np.histogram(y, bins=bins)
    return (edges[:-1] + edges[1:]) / 2, counts / max(1, y.size)


def spectrum(y, dt: float) -> tuple[np.ndarray, np.ndarray]:
    """(frequency, magnitude in dB re 1 unit rms), Hann window, DC removed."""
    y = np.asarray(y, float)
    if y.size < 8:
        return np.array([]), np.array([])
    w = np.hanning(y.size)
    spec = np.fft.rfft((y - y.mean()) * w)
    mag = np.abs(spec) * 2 / w.sum() / np.sqrt(2)          # rms per bin
    f = np.fft.rfftfreq(y.size, dt)
    return f[1:], 20 * np.log10(np.maximum(mag[1:], 1e-15))
