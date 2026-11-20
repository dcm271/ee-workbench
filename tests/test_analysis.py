import numpy as np
import pytest

from ee_workbench.analysis import between, histogram, levels, measure, spectrum


def pulses(period=4.615e-3, width=0.577e-3, hi=1.0, lo=0.1, dt=15.6e-6, n=4096, edge=0):
    t = np.arange(n) * dt
    phase = np.mod(t - 0.2e-3, period)
    y = np.where(phase < width, hi, lo)
    if edge:
        k = max(1, int(edge / dt))
        y = np.convolve(y, np.ones(k) / k, mode="same")
    return t, y


def test_levels():
    t, y = pulses()
    hi, lo = levels(y)
    assert hi == pytest.approx(1.0) and lo == pytest.approx(0.1)


def test_pulse_measurements():
    t, y = pulses()
    m = measure(t, y, "A", other_level=3.7)
    assert m["period"] == pytest.approx(4.615e-3, rel=0.01)
    assert m["width"] == pytest.approx(0.577e-3, rel=0.05)
    assert m["duty"] == pytest.approx(0.125, rel=0.06)
    assert m["frequency"] == pytest.approx(216.7, rel=0.01)
    # charge = mean current x record length; energy = charge x 3.7 V
    assert m["charge"] == pytest.approx(m["mean"] * (t[-1] - t[0]), rel=0.01)
    assert m["energy"] == pytest.approx(m["charge"] * 3.7)


def test_rise_time():
    t, y = pulses(edge=100e-6)
    m = measure(t, y)
    # a 100 us moving average gives a ~80 us 10-90 % edge
    assert 50e-6 < m["rise"] < 110e-6 and 50e-6 < m["fall"] < 110e-6


def test_flat_signal_has_no_pulse_metrics():
    t = np.arange(1000) * 1e-4
    y = 2.0 + np.random.default_rng(1).normal(0, 1e-4, t.size)
    m = measure(t, y, "V")
    assert "period" not in m and "width" not in m and "charge" not in m
    assert m["mean"] == pytest.approx(2.0, abs=1e-3)


def test_between_cursors():
    t, y = pulses()
    m = between(t, y, 0.2e-3, 0.2e-3 + 0.5e-3)
    assert m["Δt"] == pytest.approx(0.5e-3) and m["mean"] == pytest.approx(1.0)
    assert m["charge"] == pytest.approx(0.5e-3, rel=0.05)


def test_histogram_and_spectrum():
    t, y = pulses()
    x, frac = histogram(y, 20)
    assert frac.sum() == pytest.approx(1.0) and frac.max() > 0.8
    f, db = spectrum(y, 15.6e-6)
    assert abs(f[np.argmax(db)] - 216.7) < 20
