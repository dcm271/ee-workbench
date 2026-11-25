"""Mask test: is a waveform inside a band around a good one?

A mask is made from a reference trace by widening it by ± seconds and ± volts.
Samples outside the reference's time span aren't judged.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np

MAX_POINTS = 20_000


@dataclass
class Mask:
    t: np.ndarray
    lo: np.ndarray
    hi: np.ndarray
    name: str = ""

    @classmethod
    def around(cls, t, v, dt: float, dv: float, name: str = "") -> "Mask":
        t, v = np.asarray(t, float), np.asarray(v, float)
        if t.size < 2 or t.size != v.size or not np.all(np.diff(t) > 0):
            raise ValueError("the reference trace is too short or not in order")
        if dt < 0 or dv < 0 or dt == 0 and dv == 0:
            raise ValueError("give a tolerance in time or voltage")
        step = float(t[-1] - t[0]) / (t.size - 1)
        k = int(round(dt / step))
        if k >= t.size:
            raise ValueError("the time tolerance is longer than the trace")
        lo, hi = -_sliding_max(-v, k) - dv, _sliding_max(v, k) + dv
        if t.size > MAX_POINTS:              # keep the widest of each group
            n = int(np.ceil(t.size / MAX_POINTS))
            m = t.size // n * n
            lo = lo[:m].reshape(-1, n).min(1)
            hi = hi[:m].reshape(-1, n).max(1)
            t = t[:m].reshape(-1, n).mean(1)
        return cls(t, lo, hi, name)

    def check(self, t, v):
        """Returns (samples outside, samples judged, which ones)."""
        t, v = np.asarray(t, float), np.asarray(v, float)
        inside = (t >= self.t[0]) & (t <= self.t[-1])
        bad = inside & ((v < np.interp(t, self.t, self.lo)) | (v > np.interp(t, self.t, self.hi))
                        | ~np.isfinite(v))
        return int(bad.sum()), int(inside.sum()), bad

    def to_dict(self) -> dict:
        return {"otterbench_mask": 1, "name": self.name, "t": self.t.tolist(),
                "lo": self.lo.tolist(), "hi": self.hi.tolist()}

    @classmethod
    def from_dict(cls, d) -> "Mask":
        try:
            if d["otterbench_mask"] != 1:
                raise ValueError("made by another version")
            t, lo, hi = (np.array(d[k], float) for k in ("t", "lo", "hi"))
        except (KeyError, TypeError) as e:
            raise ValueError(f"not a mask ({e})") from None
        if not (t.ndim == 1 and t.size == lo.size == hi.size >= 2 and np.all(np.diff(t) > 0)
                and np.all(np.isfinite(lo)) and np.all(np.isfinite(hi)) and np.all(lo <= hi)):
            raise ValueError("not a usable mask")
        return cls(t, lo, hi, str(d.get("name", "")))


def _sliding_max(v: np.ndarray, k: int) -> np.ndarray:
    """Maximum over ±k samples (van Herk: the time doesn't grow with k)."""
    if k <= 0:
        return v.copy()
    w = 2 * k + 1
    n = v.size + 2 * k
    pad = (-n) % w
    x = np.concatenate([np.full(k, -np.inf), v, np.full(k + pad, -np.inf)]).reshape(-1, w)
    left = np.maximum.accumulate(x, axis=1).ravel()
    right = np.maximum.accumulate(x[:, ::-1], axis=1)[:, ::-1].ravel()
    return np.maximum(right[:v.size], left[w - 1:w - 1 + v.size])


def save(masks: dict, path: str):
    with open(path, "w") as fh:
        json.dump({str(ch): m.to_dict() for ch, m in masks.items()}, fh)


def load(path: str) -> dict:
    with open(path) as fh:
        d = json.load(fh)
    if not isinstance(d, dict) or not d:
        raise ValueError("not a mask file")
    out = {}
    for ch, m in d.items():
        if not str(ch).isdigit():
            raise ValueError("not a mask file")
        out[int(ch)] = Mask.from_dict(m)
    return out
