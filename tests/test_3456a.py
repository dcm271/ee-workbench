import pytest

from otterbench.instruments.base import DmmConfig
from otterbench.instruments.hp3456a import HP3456A, parse_reading
from otterbench.sim.s3456a import Sim3456A


@pytest.mark.parametrize("raw,val", [
    ("+04.999870+0", 4.99987), ("-0120.3400-3", -0.12034), ("+01.000270+3", 1000.27),
    ("+1.234567E+0", 1.234567), (" +0.5\r", 0.5), ("+05.0,+05.1", 5.0),
    # captured from the real instrument
    ("-1.177060E+0\r\n", -1.17706), ("-01.39843E+0", -1.39843), ("+2.000000E+15", 2e15),
])
def test_parse_reading(raw, val):
    assert parse_reading(raw) == pytest.approx(val)


def make():
    d = HP3456A(Sim3456A())
    d.cfg = DmmConfig(nplc=0.1)  # keep the test fast
    d.identify()
    return d


def test_dcv_and_ohms():
    d = make()
    r = d.poll(0)
    assert r.unit == "V" and abs(r.value - 5.0) < 0.01 and not r.overload
    d.apply(DmmConfig(function="4W Ω", range="1 kΩ", nplc=0.1))
    r = d.poll(0)
    assert r.unit == "Ω" and abs(r.value - 1000) < 1
    assert d.errors() == []


def test_null_and_bad_code():
    d = make()
    d.apply(DmmConfig(nplc=0.1, null=True))
    assert abs(d.poll(0).value) < 0.01
    d.write("Q9X9ZZZ")
    assert d.errors()
