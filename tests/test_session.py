import numpy as np
import pytest

from otterbench.instruments.logic import (AnalogTrace, DigitalTrace, LogicCapture,
                                            LogicConfig)
from otterbench.session import SessionReader, SessionWriter


def capture(offset=None):
    res = LogicCapture(LogicConfig(digital=[0, 3], analog=[1], digital_rate=1e6),
                       {0: DigitalTrace(0, np.array([-1e-3, 0, 2e-3]), np.array([0, 1, 0], np.uint8), 5e-3),
                        3: DigitalTrace(3, np.array([-1e-3]), np.array([1], np.uint8), 5e-3)},
                       {1: AnalogTrace(1, -1e-3, 1e-5, np.linspace(0, 1, 600))},
                       t_app=12.0, wall=0.0, elapsed=0.5, t_armed=11.4, offset=offset,
                       offset_note="aligned" if offset else "")
    res.table_header = ["name", "type", "start_time", "duration", "data"]
    res.table_rows = [["u", "uart", "0.000100000", "0.000080000", "48"]]
    res.extra["ppk2"] = {"name": "PPK2", "t": np.array([0.0, 1e-5]), "i": np.array([1e-3, 2e-3])}
    return res


def test_write_and_read_back(tmp_path):
    path = str(tmp_path / "s.otter")
    w = SessionWriter()
    w.start(path)
    key = object()
    w.stream(key, "66321D · bench", "66321D", "GPIB0::5::INSTR",
             ["voltage_V", "current_A", "mode", "dvm_V"])
    w.add(key, 10.0, [5.0, 0.1, "CV", None])
    w.add(key, 10.5, [5.0, 0.2, "CV", None])
    pk = object()
    w.stream(pk, "PPK2", "PPK2", "SIM::PPK2", ["current_A", "logic"])
    w.add_many(pk, [[10.0, 1e-3, 0], [10.001, 2e-3, 128]])
    w.note("power on", 10.2)
    w.capture(capture(offset=11.45), "Logic · uart")
    w.capture(capture(), "Logic · uart")
    w.stop()

    r = SessionReader(path)
    sup, ppk = r.streams
    np.testing.assert_allclose(sup.t, [10.0, 10.5])
    np.testing.assert_allclose(sup.data["current_A"], [0.1, 0.2])
    assert "mode" not in sup.data and np.isnan(sup.data["dvm_V"]).all()
    np.testing.assert_allclose(ppk.data["logic"], [0, 128])
    assert r.notes == [(10.2, "power on")]
    aligned, rough = r.captures
    assert aligned.aligned and aligned.t0 == 11.45 and not rough.aligned and rough.t0 == 11.4
    res = aligned.load()
    assert set(res.digital) == {0, 3} and res.digital[0].end == 5e-3
    np.testing.assert_allclose(res.analog[1].v[-1], 1.0, rtol=1e-6)
    assert res.table_rows[0][4] == "48" and res.extra["ppk2"]["i"][1] == pytest.approx(2e-3)
    assert aligned.span == pytest.approx((11.45 - 1e-3, 11.45 + 5e-3))
    assert r.span()[0] == 10.0
    r.close()


def test_appending_keeps_the_first_time_base(tmp_path, monkeypatch):
    path = str(tmp_path / "s.otter")
    w = SessionWriter()
    w.start(path)
    w.note("first", 1.0)
    w.stop()
    import otterbench.session as S
    monkeypatch.setattr(S, "APP_T0", S.APP_T0 + 100.0)     # "a run of the app started 100 s later"
    w.start(path)
    w.note("second", 1.0)
    w.stop()
    notes = SessionReader(path).notes
    assert notes[0] == (1.0, "first") and notes[1][0] == pytest.approx(101.0, abs=0.01)


def test_not_a_session(tmp_path):
    import sqlite3
    p = tmp_path / "x.otter"
    sqlite3.connect(p).execute("CREATE TABLE meta(key, value)").connection.commit()
    with pytest.raises(ValueError):
        SessionReader(str(p))
