import time

import numpy as np
import pytest

from ee_workbench.align import align, edges_from_samples
from ee_workbench.clock import now
from ee_workbench.instruments import make_driver
from ee_workbench.instruments.logic import LogicConfig
from ee_workbench.sim.shared import shared_edges, shared_level
from ee_workbench.sim.ssigrok import SimSigrokCli

RNG = np.random.default_rng(3)


def ppk_view(edges, t0, t1):
    """What a PPK2 sampling at 100 kHz sees of a signal with these edges."""
    t = np.arange(t0, t1, 1e-5)
    bits = (np.searchsorted(edges, t, side="right") % 2).astype(np.uint8)
    return edges_from_samples(t, bits)


def dirs(n):
    return np.where(np.arange(n) % 2 == 0, 1, -1)


def test_irregular_signal_aligns_within_a_sample():
    e = np.cumsum(RNG.uniform(30e-6, 2e-3, 300))
    true = 123.4567891
    lt = e[e < 0.2]
    pt, pd = ppk_view(e + true, true - 2, true + 1)
    r = align(lt, dirs(lt.size), pt, pd, true - 0.8, true + 0.6)
    assert r.offset == pytest.approx(true, abs=10e-6) and not r.ambiguous
    assert r.matched == r.used


def test_square_wave_is_flagged_ambiguous():
    e = np.arange(1, 300) * 0.5e-3
    pt, pd = ppk_view(e + 50, 48, 51)
    r = align(e[e < 0.1], dirs((e < 0.1).sum()), pt, pd, 49.5, 50.5)
    assert r.ambiguous and "too regular" in r.note


def test_unrelated_signals_refuse():
    a = np.cumsum(RNG.uniform(30e-6, 2e-3, 300))
    b = np.cumsum(RNG.uniform(30e-6, 2e-3, 300))
    pt, pd = ppk_view(b + 50, 48, 51)
    r = align(a[:80], dirs(80), pt, pd, 49, 51)
    assert r.offset is None and "matched" in r.note
    assert align(a[:2], dirs(2), pt, pd, 49, 51).offset is None


def test_shared_pin_is_consistent():
    init, e = shared_edges(10.0, 10.05)
    assert 40 <= e.size <= 60
    lv = shared_level(np.array([10.0, *(e[:3] + 1e-7)]))
    assert list(lv) == [init, 1 - init, init, 1 - init]


def test_sim_sigrok_capture_aligns_with_shared_pin():
    """The simulated analyzer's D7 lined up against the pin as a PPK2 sees it."""
    d = make_driver("SIGROK", SimSigrokCli(speed=1), "SIM::SIGROK")
    d.identify()
    try:
        d.arm(LogicConfig(digital=[7], digital_rate=1e6, duration=0.05))
        while not d.capture_done():
            time.sleep(0.005)
        res = d.fetch()
    finally:
        d.close()
    tr = res.digital[7]
    t = np.arange(res.t_armed - 1, now() + 0.2, 1e-5)
    pt, pd = edges_from_samples(t, shared_level(t))
    r = align(tr.t[1:], np.where(tr.level[1:] == 1, 1, -1), pt, pd,
              res.t_armed - 1, res.t_app + 0.5)
    assert r.offset is not None and not r.ambiguous
    # the simulator put capture time 0 at app time app0, just after arming
    assert abs(r.offset - res.t_armed) < 0.01
