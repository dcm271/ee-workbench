"""663xx current auto-range, done by the driver (the supply has none over GPIB)."""
import pytest

from otterbench.instruments import agilent663xx as a
from otterbench.instruments.agilent663xx import AUTO, Agilent663xx
from otterbench.instruments.base import CaptureSpec, SupplyPoll
from otterbench.sim.s663xx import Sim66309, Sim663xx

# the simulated load: 47 ohm, with 4.7 ohm bursts (a phone transmitting)


def make(sim=Sim66309):
    d = Agilent663xx(sim())
    d.identify()
    d.read_setpoints()
    return d


def polls(d, n, cfg=None):
    return [d.poll(0, cfg or SupplyPoll(out2=False)) for _ in range(n)][-1]


def test_auto_is_the_default():
    d = make()
    assert d.caps.has_auto_range and d.read_setpoints().current_range == AUTO


def test_down_to_20_ma_for_a_small_current():
    d = make()
    d.set_voltage(0.05), d.set_current(1), d.set_output(True)
    assert d.poll(0, SupplyPoll(out2=False)).i_range == "3 A"      # read on 3 A, then down
    r = polls(d, 2)
    assert r.i_range == "20 mA" and d.t.rng == "MIN"
    assert r.current == pytest.approx(0.05 / 47 + 0.05 / 4.7 * 0.125, rel=0.1)
    assert d.errors() == []


def test_an_overflow_is_measured_again_on_the_higher_range():
    d = make()
    d.set_voltage(0.05), d.set_current(1), d.set_output(True)
    polls(d, 2)
    d.set_voltage(1.0)             # 213 mA bursts: far over 20 mA
    r = d.poll(0, SupplyPoll(out2=False, pulse_stats=True))
    assert r.i_range == "3 A" and abs(r.current) < 1 and r.current > 0.02
    assert r.i_max > 0.15 and "OVLD" not in r.flags and "Measurement overload" not in r.events
    # it stays up: back down only after the hold
    d.set_voltage(0.05)
    assert polls(d, 3).i_range == "3 A"
    d._hold_until = 0
    assert polls(d, 2).i_range == "20 mA"


def test_up_near_full_scale(monkeypatch):
    d = make()
    d.set_output(True)
    polls(d, 2)
    assert d._rng == "20 mA"
    monkeypatch.setattr(a, "UP", 0.0001)
    d.set_voltage(0.05)
    r = d.poll(0, SupplyPoll(out2=False))
    assert r.i_range == "20 mA" and d._rng == "3 A"     # this reading fine, the next one up


def test_three_ranges_on_a_66321d():
    d = make(Sim663xx)
    d.set_voltage(1.0), d.set_current(3), d.set_output(True)
    r = polls(d, 3)
    assert r.i_range == "1 A"          # 213 mA bursts: over 20 mA, under 1 A
    d.set_output(False)
    d._hold_until = 0
    assert polls(d, 2).i_range == "20 mA"


def test_a_chosen_range_stays():
    d = make()
    d.set_current_range("20 mA")
    d.set_voltage(1.0), d.set_output(True)
    r = polls(d, 3)
    assert r.i_range == "20 mA" and "OVLD" in r.flags and r.current > 1e37
    assert d.read_setpoints().current_range == "20 mA"
    d.set_current_range(AUTO)
    assert polls(d, 1).i_range == "3 A" and d.read_setpoints().current_range == AUTO


def test_a_capture_is_on_the_top_range():
    d = make()
    polls(d, 2)
    assert d._rng == "20 mA"
    d.arm(CaptureSpec(points=64))
    assert d.t.rng == "MAX"
    d.abort()
    assert d.errors() == []
