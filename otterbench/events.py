"""Find bursts in a current trace (radio TX, sensor wake-ups, …) and describe
each: when, how long, peak, average and charge. With a logic capture aligned
to the PPK2 (see align.py), each event can also list the decoded bus traffic
that happened during it.

    evs = find_events(t, i, threshold=1e-3, min_gap=200e-6, min_len=50e-6)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Event:
    t0: float
    t1: float
    peak: float
    mean: float
    charge: float               # C over the event (mean × duration)
    n: int                      # samples
    bus: list = field(default_factory=list)    # decoded rows during it (text)

    @property
    def duration(self) -> float:
        return self.t1 - self.t0


def find_events(t: np.ndarray, i: np.ndarray, threshold: float, min_gap: float = 0.0,
                min_len: float = 0.0, hysteresis: float = 0.0) -> list[Event]:
    """Stretches where i > threshold. Two stretches closer than min_gap are one
    event; events shorter than min_len are dropped. With hysteresis (a
    fraction, e.g. 0.2) an event ends only below threshold × (1 − hysteresis)."""
    t = np.asarray(t, float)
    i = np.asarray(i, float)
    if t.size < 2:
        return []
    dt = float(np.median(np.diff(t)))
    above = i > threshold
    if hysteresis > 0:
        low = threshold * (1 - hysteresis)
        # a Schmitt trigger: on above threshold, off at or below `low`,
        # otherwise whatever it was (forward-fill the last decision)
        ev = np.where(i > threshold, 1, np.where(i <= low, 0, -1)).astype(np.int8)
        ev[0] = max(ev[0], 0)
        idx = np.maximum.accumulate(np.where(ev >= 0, np.arange(i.size), 0))
        above = ev[idx] == 1
    if not above.any():
        return []
    d = np.diff(above.astype(np.int8))
    starts = list(np.flatnonzero(d == 1) + 1)
    ends = list(np.flatnonzero(d == -1) + 1)       # exclusive
    if above[0]:
        starts.insert(0, 0)
    if above[-1]:
        ends.append(above.size)
    runs = list(zip(starts, ends))
    merged: list[list[int]] = []
    for a, b in runs:
        if merged and (t[a] - t[merged[-1][1] - 1]) - dt < min_gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    out = []
    for a, b in merged:
        seg = i[a:b]
        dur = (b - a) * dt
        if dur < min_len:
            continue
        m = float(seg.mean())
        out.append(Event(float(t[a]), float(t[a] + dur), float(seg.max()), m, m * dur, b - a))
    return out


def summary(events: list[Event]) -> dict:
    if not events:
        return {"count": 0}
    d = np.array([e.duration for e in events])
    q = np.array([e.charge for e in events])
    p = np.array([e.peak for e in events])
    out = {"count": len(events), "duration_mean": float(d.mean()), "duration_max": float(d.max()),
           "charge_mean": float(q.mean()), "charge_total": float(q.sum()),
           "peak_max": float(p.max())}
    if len(events) > 1:
        gaps = np.diff([e.t0 for e in events])
        out["interval_mean"] = float(gaps.mean())
    return out


def attach_bus(events: list[Event], rows: list[tuple[float, str]], offset: float,
               pad: float = 0.0):
    """rows: (time in the capture, text). offset: PPK2 time of the capture's
    t = 0 (from the alignment). Puts each row into the event it falls in."""
    if not rows:
        return
    times = np.array([r[0] + offset for r in rows])
    order = np.argsort(times)
    times = times[order]
    for e in events:
        a = int(np.searchsorted(times, e.t0 - pad))
        b = int(np.searchsorted(times, e.t1 + pad, side="right"))
        e.bus = [rows[order[k]][1] for k in range(a, b)]


def histogram(values, bins: int = 30):
    v = np.asarray(values, float)
    v = v[np.isfinite(v)]
    if not v.size:
        return np.array([]), np.array([])
    lo, hi = float(v.min()), float(v.max())
    if hi <= lo:
        hi = lo + (abs(lo) or 1.0) * 1e-3
    counts, edges = np.histogram(v, bins=bins, range=(lo, hi))
    return counts, edges
