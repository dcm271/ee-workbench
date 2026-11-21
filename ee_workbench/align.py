"""Line up a logic-analyzer capture with a PPK2 to within a few microseconds.

Both see the same signal: one DUT pin wired to a logic-analyzer channel and to
one of the PPK2's logic inputs D0-D7. The logic analyzer times edges
precisely but on its own clock (t = 0 at its start or trigger); the PPK2
times them to its 10 µs sample period on the app's clock. Finding the offset
that makes the two edge sequences coincide maps the capture onto the app's
time base, so the PPK2's current can be drawn under the decoded traffic.

Method: every edge of the PPK2's record that is close enough in time (the
host's own timing narrows the search to a second or so) is tried as the match
for the first logic edge; each candidate offset is scored by how many logic
edges then land on a PPK2 edge of the same direction within `tol`. The best
score wins and its matched pairs refine the offset (median). If another
offset scores nearly as well the signal is too regular to tell periods apart
(e.g. a plain square wave) and the result says so: use an irregular signal,
such as a UART line or a pin the firmware toggles at uneven times.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

PPK2_DT = 1e-5          # PPK2 sample period
TOL = 1.6 * PPK2_DT     # an edge's position in the PPK2 record is known to ±1 sample
MIN_GAP = 2 * PPK2_DT   # logic edges closer than this can't be seen apart by the PPK2


@dataclass
class Alignment:
    offset: float | None          # app time of the capture's t = 0
    matched: int = 0
    used: int = 0                 # logic edges compared
    spread: float = 0.0           # std of the matched differences, s
    ambiguous: bool = False
    note: str = ""


def edges_from_samples(t: np.ndarray, bits: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Edges of a sampled 0/1 signal: (times, +1 rising / -1 falling). An edge
    is placed halfway between the two samples that differ."""
    bits = bits.astype(np.int8)
    idx = np.flatnonzero(np.diff(bits)) + 1
    return (t[idx - 1] + t[idx]) / 2, np.where(bits[idx] > 0, 1, -1)


def separable(t: np.ndarray, d: np.ndarray, gap: float = MIN_GAP):
    """Drop edges too close to a neighbour for the PPK2 to resolve."""
    if t.size < 2:
        return t, d
    dt = np.diff(t)
    ok = np.ones(t.size, bool)
    ok[1:] &= dt >= gap
    ok[:-1] &= dt >= gap
    return t[ok], d[ok]


def _score(lt, ld, pt, pd, offset, tol):
    """How many logic edges (shifted by offset) have a same-direction PPK2
    edge within tol, and the differences for those that do."""
    x = lt + offset
    i = np.clip(np.searchsorted(pt, x), 1, pt.size - 1)
    left, right = pt[i - 1], pt[i]
    j = np.where(np.abs(x - left) <= np.abs(right - x), i - 1, i)
    diff = pt[j] - x
    ok = (np.abs(diff) <= tol) & (pd[j] == ld)
    return int(ok.sum()), diff[ok]


def align(logic_t: np.ndarray, logic_d: np.ndarray, ppk_t: np.ndarray, ppk_d: np.ndarray,
          lo: float, hi: float, tol: float = TOL, max_edges: int = 400,
          max_candidates: int = 6000) -> Alignment:
    """Offset o with logic_t + o ≈ ppk_t, searched within lo <= o <= hi."""
    lt, ld = separable(np.asarray(logic_t, float), np.asarray(logic_d))
    pt, pd = np.asarray(ppk_t, float), np.asarray(ppk_d)
    if lt.size < 3:
        return Alignment(None, note="Too few edges on the shared channel to align "
                                    "(it needs at least 3 the PPK2 can see).")
    if pt.size < 3:
        return Alignment(None, note="The PPK2 saw no edges on its shared input in that time.")
    if lt.size > max_edges:                  # an even sample is plenty to score with
        pick = np.linspace(0, lt.size - 1, max_edges).astype(int)
        lt, ld = lt[pick], ld[pick]
    # candidates: PPK2 edges that could be the first logic edge
    a, b = np.searchsorted(pt, [lt[0] + lo, lt[0] + hi])
    cand = np.flatnonzero(pd[a:b] == ld[0]) + a
    if cand.size == 0:
        return Alignment(None, used=lt.size,
                         note="No PPK2 edge lines up with the capture's time window. Is the "
                              "PPK2 running, and the right input chosen?")
    if cand.size > max_candidates:
        cand = cand[np.linspace(0, cand.size - 1, max_candidates).astype(int)]
    offsets = pt[cand] - lt[0]
    scores = np.array([_score(lt, ld, pt, pd, o, tol)[0] for o in offsets])
    best = int(np.argmax(scores))
    o = offsets[best]
    n, diffs = _score(lt, ld, pt, pd, o, tol)
    o += float(np.median(diffs))
    n, diffs = _score(lt, ld, pt, pd, o, tol)
    rivals = scores[np.abs(offsets - offsets[best]) > 4 * tol]
    ambiguous = rivals.size > 0 and rivals.max() >= 0.9 * scores[best]
    spread = float(np.std(diffs)) if diffs.size else 0.0
    frac = n / lt.size
    if frac < 0.5:
        return Alignment(None, n, lt.size, spread, ambiguous,
                         f"Only {n} of {lt.size} edges matched: the two may not be on the "
                         "same signal (check the channel and PPK2 input).")
    note = (f"{n} of {lt.size} edges match, ±{max(spread, PPK2_DT / 2) * 1e6:.0f} µs")
    if ambiguous:
        note += (". Warning: the signal is too regular, another period fits about as well; "
                 "use an irregular signal (UART, a debug pin)")
    return Alignment(float(o), n, lt.size, spread, ambiguous, note)
