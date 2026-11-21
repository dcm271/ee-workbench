"""A "debug pin" every simulator sees at the same moments of app time, as if
one DUT pin were wired to the simulated PPK2's logic input D7 and to channel
D7 of the simulated logic analyzers, so PPK2 alignment can be tried without
hardware. It toggles roughly every 1 ms at uneven times (unlike the other
simulated signals, which are periodic and so can't be told apart by period).
"""
from __future__ import annotations

import numpy as np

SHARED_CH = 7
STEP = 1e-3


def _jitter(k: np.ndarray) -> np.ndarray:
    """A fixed pseudo-random 0 .. 0.8 STEP for each k (splitmix64-style mixing)."""
    x = k.astype(np.uint64) + np.uint64(0x9E3779B97F4A7C15)
    with np.errstate(over="ignore"):
        x = (x ^ (x >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        x = (x ^ (x >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        x ^= x >> np.uint64(31)
    return (x >> np.uint64(11)).astype(np.float64) / 2.0 ** 53 * 0.8 * STEP


def shared_edges(a: float, b: float) -> tuple[int, np.ndarray]:
    """(level at a, edge times in (a, b]) in app time. Edge k is at
    k*STEP + jitter(k); the level after edge k is k % 2 ... odd k high."""
    k0, k1 = int(np.floor(a / STEP)) - 1, int(np.ceil(b / STEP)) + 1
    k = np.arange(k0, k1 + 1, dtype=np.int64)
    t = k * STEP + _jitter(k)
    before = t <= a
    init = int(k[before][-1] % 2) if before.any() else 0
    return init, t[(t > a) & (t <= b)]


def shared_level(t: np.ndarray) -> np.ndarray:
    """0/1 level of the pin at app times t."""
    if t.size == 0:
        return np.zeros(0, np.uint8)
    init, e = shared_edges(float(t[0]) - 1e-9, float(t[-1]))
    return ((init + np.searchsorted(e, t, side="right")) % 2).astype(np.uint8)
