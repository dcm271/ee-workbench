"""A "debug pin" every simulator sees at the same moments of app time, as if
one DUT pin were wired to the simulated PPK2's logic input D7 and to channel
D7 of the simulated logic analyzers, so PPK2 alignment can be tried without
hardware. It toggles roughly every 1 ms at uneven times (unlike the other
simulated signals, which are periodic and so can't be told apart by period).
"""
from __future__ import annotations

import time
import weakref

import numpy as np

# The simulated function generator driven last, as if its CH1 went to a filter:
# while its output is on with a sine, the simulated scopes show it on CH1 and
# the filter's output on CH2 (see sscope.signal).
FGEN = None                     # weakref to a SimFgen
FILTER_HZ, FILTER_Q = 10e3, 0.9                # second-order low-pass


def drive():
    """(frequency, Vpp, offset) of the simulated generator, or None."""
    g = FGEN() if FGEN is not None else None
    c = g.ch[1] if g is not None else None
    if c is None or not c.out or c.func != "SIN":
        return None
    return c.freq, c.volt, c.offs


def set_drive(gen):
    global FGEN
    FGEN = weakref.ref(gen)


def filter_response(f: float) -> complex:
    s = 1j * f / FILTER_HZ
    return 1 / (1 + s / FILTER_Q + s * s)


# The simulated electronic loads as if one hung on a 1.2 V regulator whose output
# the simulated scopes show on CH4: the last current, and its last step.
LOAD = [0.0, 0.0, -1e9]         # now, before, when (monotonic)


def load_is(amps: float):
    if amps != LOAD[0]:
        LOAD[:] = [amps, LOAD[0], time.monotonic()]


def regulator_out(t: np.ndarray) -> np.ndarray:
    """Its output around the last step (t = 0): a damped ring, 0.04 V per amp."""
    di = LOAD[0] - LOAD[1]
    dv = np.where(t >= 0, -0.04 * di * np.exp(-t / 60e-6) * np.sin(
        2 * np.pi * np.maximum(t, 0) / 200e-6) * 1.6, 0.0)
    return 1.2 - 0.004 * np.where(t >= 0, LOAD[0], LOAD[1]) + dv


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
