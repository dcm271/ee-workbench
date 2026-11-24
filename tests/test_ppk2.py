import numpy as np
import pytest

from otterbench.instruments import make_driver, model_for
from otterbench.instruments.ppk2 import (DT, PPK2, SAMPLE_RATE, Calibration, SpikeFilter,
                                           decode_words, voltage_bytes)
from otterbench.sim.sppk2 import SimPPK2

# Captured from a real PPK2 (HW 8682), trimmed to the keys we use.
REAL_META = """Calibrated: 0
R0: 998.95715332031250000000
R1: 102.36305236816406000000
R2: 10.38202285766601600000
R3: 0.97779947519302370000
R4: 0.05648170784115791000
GS0: 0.00000000000000000010
GS1: 153.94396972656250000000
GS2: 24.56081962585449200000
GS3: 3.31852602958679200000
GS4: 0.09761372953653336000
GI0: 1.00000000000000000000
GI1: 0.95724916458129880000
GI2: 0.94877058267593380000
GI3: 0.93684047460556030000
GI4: 0.94640076160430910000
O0: 155.35530090332030000000
O1: 106.94342803955078000000
O2: 91.81430053710938000000
O3: 71.85976409912110000000
O4: 123.35852813720703000000
VDD: 3000
HW: 8682
mode: 2
S0: -0.00000007049199268749
S1: -0.00000055820538591433
S2: -0.00000577518403588329
S3: -0.00006123907223809510
S4: 0.00153499201405793430
I0: 0.00000017910626581852
I1: 0.00000126369002373394
I2: 0.00008376502955798060
I3: -0.00014602243027184159
I4: -0.00601299200206995000
UG0: 1.00000000000000000000
UG1: 1.00000000000000000000
UG2: 1.00000000000000000000
UG3: 1.00000000000000000000
UG4: 1.00000000000000000000
IA: 59
END
"""


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_parse_real_metadata():
    cal = Calibration.parse(REAL_META)
    assert cal.modifiers["R"][4] == pytest.approx(0.05648170784115791)
    assert cal.modifiers["GS"][1] == pytest.approx(153.9439697265625)
    assert cal.modifiers["S"][0] == pytest.approx(-7.049199268749e-8)
    assert cal.modifiers["UG"] == [1.0] * 5
    assert cal.info["VDD"] == "3000" and cal.info["HW"] == "8682" and cal.info["mode"] == "2"


def test_voltage_bytes():
    assert voltage_bytes(800) == bytes([3, 32])        # Nordic's documented baseline
    assert voltage_bytes(3000) == bytes([0x0B, 0xB8])
    assert voltage_bytes(5000) == bytes([0x13, 0x88])
    with pytest.raises(ValueError):
        voltage_bytes(5001)


def test_decode_words():
    w = np.array([39 | (2 << 14) | (5 << 18) | (0xA5 << 24), 0x3FFF | (4 << 14) | (63 << 18)],
                 dtype="<u4")
    adc, rng, cnt, logic = decode_words(w.tobytes())
    assert list(adc) == [39 * 4, 0x3FFF * 4]
    assert list(rng) == [2, 4] and list(cnt) == [5, 63] and list(logic) == [0xA5, 0]


def test_calibration_matches_reference_formula():
    cal = Calibration.parse(REAL_META)
    m = cal.modifiers
    rng = np.array([0, 1, 2, 3, 4])
    adc = np.array([400.0, 9000.0, 20000.0, 30000.0, 50000.0])
    got = cal.amps(adc, rng, 3300)
    for k in range(5):     # scalar transcription of pc-nrfconnect-ppk getAdcResult
        r = rng[k]
        x = (adc[k] - m["O"][r]) * ((1.8 / 163840) / m["R"][r])
        want = m["UG"][r] * (x * (m["GS"][r] * x + m["GI"][r]) + (m["S"][r] * 3.3 + m["I"][r]))
        assert got[k] == pytest.approx(want, rel=1e-12)


def reference_spike_filter(values, ranges):
    """Sample-by-sample port of pc-nrfconnect-ppk's spike filter."""
    alpha, alpha4, samples = 0.18, 0.06, 3
    avg = avg4 = prev_range = None
    after = consec = 0
    out = []
    for adc, rng in zip(values, ranges):
        p_avg, p_avg4 = avg, avg4
        avg = adc if avg is None else alpha * adc + (1 - alpha) * avg
        avg4 = adc if avg4 is None else alpha4 * adc + (1 - alpha4) * avg4
        if prev_range is None:
            prev_range = rng
        if prev_range != rng or after > 0:
            if prev_range != rng:
                consec = 0
                after = samples
            else:
                consec += 1
            if rng == 4:
                if consec < 2:
                    avg, avg4 = p_avg, p_avg4
                adc = avg4
            else:
                adc = avg
            after -= 1
        prev_range = rng
        out.append(adc)
    return np.array(out)


@pytest.mark.parametrize("window,rel", [(SpikeFilter.WINDOW, 1e-9), (800, 1e-14)])
def test_spike_filter_matches_reference_across_chunks(window, rel):
    """Default window: within truncation error. A long window: exact, which
    proves the vectorised logic (exclusions, chunk carry-over) is right."""
    g = np.random.default_rng(3)
    n = 20000
    ranges = np.repeat(g.integers(0, 5, 400), g.integers(1, 90, 400))[:n]
    values = g.normal(1e-3, 2e-4, ranges.size) * (1 + ranges)
    want = reference_spike_filter(values, ranges)
    f = type("F", (SpikeFilter,), {"WINDOW": window})()
    cuts = np.sort(g.choice(np.arange(1, ranges.size), 25, replace=False))
    got = np.concatenate([f(v, r) for v, r in zip(np.split(values, cuts), np.split(ranges, cuts))])
    assert np.max(np.abs(got - want) / np.abs(want)) < rel


def make(clock=None):
    clock = clock or FakeClock()
    sim = SimPPK2(clock=clock)
    d = make_driver("auto", sim, "SIM::PPK2")
    d.identify()
    return d, sim, clock


def run(d, clock, seconds, step=0.05):
    out = []
    t = 0.0
    for _ in range(int(round(seconds / step))):
        clock.t += step
        t += step
        out.append(d.poll(t))
    return out


def test_identify_and_model():
    assert model_for("PPK2::/dev/ttyACM0") == "PPK2"
    assert model_for("SIM::PPK2") == "PPK2"
    d, sim, _ = make()
    assert isinstance(d, PPK2)
    assert "HW 9173" in d.caps.idn
    assert d.state.mode == "source" and d.state.voltage_mv == 3000
    assert d.state.dut_power is None and d.state.measuring


def test_source_mode_stream():
    d, sim, clock = make()
    d.set_mode("source")
    d.set_voltage(3300)
    assert sim.vdd_mv == 3300
    d.set_dut_power(True)
    rs = run(d, clock, 1.0)
    cur = np.concatenate([r.current for r in rs])
    assert cur.size == pytest.approx(SAMPLE_RATE, abs=10)
    assert d.lost_total == 0
    # Sim DUT at 3.3 V: 3 µA sleep, 400 µA ticks, ~6-9 mA radio; average ≈ 0.25 mA
    t = np.arange(cur.size) / SAMPLE_RATE        # the simulator's own time base
    sleep = cur[(np.mod(t, 0.01) > 1e-3) & (np.mod(t, 0.1) > 5e-3)]
    assert np.median(sleep) == pytest.approx(3.3e-6, rel=0.05)
    assert 0.2e-3 < cur.mean() < 0.35e-3
    assert cur.max() == pytest.approx(9.9e-3, rel=0.05)    # spike filter removed 3x spikes
    # timestamps are contiguous across blocks
    for a, b in zip(rs, rs[1:]):
        if a.n and b.n:
            assert b.t0 == pytest.approx(a.t0 + a.n * DT, abs=1e-9)
    # logic: D0 high during radio events
    ph = np.mod(t, 0.1)
    lg = np.concatenate([r.logic for r in rs])
    radio, idle = (ph > 1e-4) & (ph < 2.4e-3), (ph > 3e-3) & (ph < 0.0999)
    assert np.all(lg[radio] & 1) and not np.any(lg[idle] & 1)


def test_dut_power_off_reads_zero():
    d, sim, clock = make()
    d.set_mode("source")
    d.set_dut_power(False)
    cur = np.concatenate([r.current for r in run(d, clock, 0.3)])
    assert abs(cur.mean()) < 0.1e-6


def test_lost_samples_counted():
    d, sim, clock = make()
    d.set_dut_power(True)
    run(d, clock, 0.2)
    sim.drop_next = 10
    run(d, clock, 0.2)
    assert d.lost_total == 10


def test_ampere_mode_voltage_not_sent():
    d, sim, clock = make()
    d.set_mode("ampere")
    sim.sent.clear()
    d.set_voltage(1800)
    assert sim.sent == [] and d.state.voltage_mv == 1800
    cur = np.concatenate([r.current for r in run(d, clock, 0.5)])
    assert cur.mean() > 0.1e-3           # ampere mode: DUT runs on its own supply


def test_stop_and_restart():
    d, sim, clock = make()
    d.set_dut_power(True)
    run(d, clock, 0.1)
    d.stop()
    assert all(r.n == 0 for r in run(d, clock, 0.2))
    d.start()
    assert sum(r.n for r in run(d, clock, 0.2)) == pytest.approx(0.2 * SAMPLE_RATE, abs=10)
