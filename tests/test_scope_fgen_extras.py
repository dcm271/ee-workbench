"""Scope screenshots and masks, generator waveforms."""
import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from otterbench import arb, mask, tape  # noqa: E402
from otterbench.instruments import make_driver, model_for  # noqa: E402
from otterbench.transport import TransportTimeout, open_transport  # noqa: E402


def drv(res):
    d = make_driver(model_for(res), open_transport(res), res)
    d.identify()
    return d


@pytest.mark.parametrize("res", ["SIM::SCOPE", "SIM::DSOX", "SIM::SDS"])
def test_screenshot(res):
    d = drv(res)
    img = QImage.fromData(d.screenshot())
    assert (img.width(), img.height()) == (320, 200)


def test_screenshot_limits(monkeypatch):
    with pytest.raises(RuntimeError, match="Tektronix"):
        drv("SIM::TBS").screenshot()
    d = drv("SIM::SCOPE")
    d.t.query_raw = lambda c: b"hello"
    with pytest.raises(RuntimeError, match="picture"):
        d.screenshot()

    def old(c):              # a model that takes no parameters
        if "PNG" in c:
            raise TransportTimeout(c)
        return b"BM" + bytes(50)
    d.t.query_raw = old
    assert d.screenshot()[:2] == b"BM"


def test_screenshot_is_taped():
    t, tap = tape.wrap(open_transport("SIM::SDS"))
    tap.tap_on = True
    png = t.query_bytes("SCDP")
    assert tape.Replay(tap.tap_events).query_bytes("SCDP") == png


# ---- masks
def test_sliding_max_is_right():
    v = np.random.default_rng(1).normal(size=257)
    for k in (0, 1, 2, 7, 100, 256):
        want = [v[max(0, i - k):i + k + 1].max() for i in range(v.size)]
        assert np.allclose(mask._sliding_max(v, k), want)


def test_mask():
    t = np.linspace(-1e-3, 1e-3, 2001)
    v = np.sign(t)
    m = mask.Mask.around(t, v, 20e-6, 0.1)
    assert m.check(t, v)[0] == 0
    assert m.check(t, np.sign(t - 10e-6))[0] == 0          # the edge may move that much
    assert m.check(t, np.sign(t - 40e-6))[0] > 0
    bad = v.copy()
    bad[1500] = 1.5
    n, judged, which = m.check(t, bad)
    assert (n, judged, int(which.nonzero()[0][0])) == (1, 2001, 1500)
    bad[1500] = np.nan
    assert m.check(t, bad)[0] == 1
    assert m.check(t + 1, v)[1] == 0                       # elsewhere in time
    for args in ((t, v, 0, 0), (t, v, -1, 1), (t, v, 1, 0.1), (t[::-1], v, 0, 1),
                 ([1], [1], 0, 1)):
        with pytest.raises(ValueError):
            mask.Mask.around(*args)
    big = np.linspace(0, 1, 100_001)
    wide = mask.Mask.around(big, np.sin(50 * big), 0, 0.01)
    assert wide.t.size <= mask.MAX_POINTS and wide.check(big, np.sin(50 * big))[0] == 0


def test_mask_files(tmp_path):
    p = str(tmp_path / "m.json")
    t = np.arange(10.0)
    mask.save({2: mask.Mask.around(t, t, 0, 1, "x")}, p)
    m = mask.load(p)
    assert list(m) == [2] and m[2].name == "x" and m[2].check(t, t)[0] == 0
    for bad in ("[]", "{}", '{"a": {}}', '{"1": {"otterbench_mask": 1}}',
                '{"1": {"otterbench_mask": 1, "t": [0, 1], "lo": [2, 2], "hi": [1, 1]}}'):
        open(p, "w").write(bad)
        with pytest.raises(ValueError):
            mask.load(p)


def test_mask_as_a_measurement():
    d = drv("SIM::SCOPE")
    with pytest.raises(ValueError, match="no mask"):
        d.measure("mask1")
    tr = d.fetch_channel(1)
    d.masks[1] = mask.Mask(tr.t, np.full(tr.v.size, -100.0), np.full(tr.v.size, 100.0))
    assert d.measure("mask1") == 0
    d.masks[1] = mask.Mask(tr.t, np.full(tr.v.size, 50.0), np.full(tr.v.size, 100.0))
    assert d.measure("mask1") == tr.v.size


# ---- waveforms
def test_wave():
    t = np.arange(100) * 1e-5
    w = arb.make(t, 2 + np.sin(2 * np.pi * 1000 * t))
    assert w.seconds == pytest.approx(1e-3) and w.offset == pytest.approx(2, abs=0.01)
    assert w.amplitude == pytest.approx(2, rel=0.01)
    assert w.unit().max() == 1 and w.unit().min() == -1
    assert arb.make(np.arange(50_000), np.arange(50_000)).v.size == arb.MAX_POINTS
    assert arb.make([0, 1], [0, 1]).v.size == arb.MIN_POINTS
    for tt, vv in (([0], [1]), ([0, 1, 1], [0, 1, 2]), ([0, 1], [1, 1]), ([0, np.nan], [1, 2])):
        with pytest.raises(ValueError):
            arb.make(tt, vv)


def test_wave_from_csv(tmp_path):
    p = tmp_path / "w.csv"
    p.write_text("t_s,ch1_V,ch2_V\n0,0,5\n0.001,1,6\n0.002,0,5\n")
    w = arb.from_csv(str(p))
    assert w.seconds == pytest.approx(0.003) and w.amplitude == pytest.approx(1, rel=0.1)
    assert arb.from_csv(str(p), 2).offset == pytest.approx(5.5, rel=0.02)
    p.write_text("1\n2\n3\n")
    assert arb.from_csv(str(p), rate=1000).seconds == pytest.approx(0.003)
    for text in ("", "a,b\n", "0,1\nx,y\n", "1\n2\n"):
        p.write_text(text)
        with pytest.raises(ValueError):
            arb.from_csv(str(p))
    p.write_text("0,1\n1,2\n")
    with pytest.raises(ValueError):
        arb.from_csv(str(p), 5)


@pytest.mark.parametrize("res,name", [("SIM::FGEN", "OTTER"), ("SIM::SDG", "otter1")])
def test_upload(res, name):
    d = drv(res)
    t = np.arange(200) * 1e-5
    w = arb.make(t, 1 + np.sin(2 * np.pi * 500 * t))
    d.upload(w, 1)
    c = d.read_channel(1)
    assert c.function == "arb" and c.frequency == pytest.approx(500, rel=1e-3)
    assert c.amplitude == pytest.approx(2, rel=1e-3) and c.offset == pytest.approx(1, abs=1e-3)
    assert np.allclose(d.t.waves[name], w.unit(), atol=1e-4)
    d.set("output", True, 1)
    with pytest.raises(ValueError, match="off"):
        d.upload(w, 1)
    d.set("output", False, 1)
    d.set("function", "sine", 1)
    assert d.read_channel(1).function == "sine"


@pytest.mark.parametrize("idn,style", [
    ("Agilent Technologies,33220A,1,1", "user"), ("Rigol Technologies,DG1032Z,1,1", "rigol"),
    ("Keysight Technologies,33611A,1,1", "keysight"), ("Siglent,SDG1032X,1,1", "siglent")])
def test_upload_styles(idn, style):
    from otterbench.instruments.fgen import FunctionGenerator

    class T:
        sent: list = []

        def temp_timeout(self, s):
            from contextlib import nullcontext
            return nullcontext()

        def write(self, c): T.sent.append(c)
        def write_bytes(self, b): T.sent.append(b)
        def clear(self): pass

        def query(self, c):
            if c == "*OPC?":
                return "1"
            return '0,"No error"' if "ERR" in c else "C1:OUTP OFF" if c.startswith("C1") else \
                "0" if "OUTP" in c else "1"
    T.sent = []
    d = FunctionGenerator(T())
    d.caps.idn, d.caps.dialect = idn, "siglent" if style == "siglent" else "scpi"
    assert d.arb_style() == style
    d.upload(arb.make(np.arange(16), np.arange(16)), 1)
    text = " | ".join(c if isinstance(c, str) else c[:20].decode() for c in T.sent)
    assert {"user": "DATA VOLATILE,-1.0", "rigol": "SOUR1:DATA VOLATILE,-1.0",
            "keysight": "SOUR1:DATA:ARB otter,-1.0", "siglent": "C1:WVDT"}[style] in text


# ---- in the app
def test_tabs_and_sequence(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QFileDialog
    from otterbench import report
    from otterbench.config import Config
    from otterbench.gui.main_window import MainWindow
    from otterbench.sequencer import Plan, Step
    from otterbench.session import SessionReader, recorder

    def until(cond, s=15.0):
        end = time.monotonic() + s
        while time.monotonic() < end and not cond():
            _APP.processEvents()
            time.sleep(0.005)
        return cond()
    m = MainWindow([("auto", "SIM::SCOPE"), ("auto", "SIM::FGEN")], Config(), None)
    try:
        assert until(lambda: len(m.panels()) == 2 and all(p.connected for p in m.panels()))
        sc, fg = m.panels()
        sc.grab_now()
        assert until(lambda: sc.capture is not None and not sc._busy)
        ch = sorted(sc.capture.analog)[0]
        sc.mask_from(ch, 0, 50.0)
        assert "PASS" in sc.mask_label.text() and f"mask{ch}" in sc.measure_names()
        fg.set_wave(arb.make(sc.capture.analog[ch].t, sc.capture.analog[ch].v))
        fg.arb_load()
        assert until(lambda: fg.state.channels[0].function == "arb")
        assert fg.func.currentData() == "arb"
        png = str(tmp_path / "s.png")
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (png, ""))
        otter = str(tmp_path / "a.otter")
        recorder.start(otter)
        sc.screenshot()
        assert until(lambda: os.path.exists(png))
        w = m.sequencer()
        name = sc.display_name()
        w._set_plan(Plan([Step("Screenshot", name),
                          Step("Measure", name, f"mask{ch}", "", "outside", "", "0")]))
        w.run()
        assert until(lambda: not w.running() and w.run_btn.isEnabled())
        assert w.outcome == "Finished" and w.rows[0]["result"] == "PASS", w.logbox.toPlainText()
        html = report.sequence_report("t", w.rows, w.columns, None, "", [], "ok",
                                      images=w.bench.images)
        assert html.count("data:image/png") == 1
        recorder.stop()
        r = SessionReader(otter)
        assert len(r.images) == 2
        r.close()
        sc.clear_mask()
        assert sc.mask_label.text() == ""
    finally:
        recorder.stop()
        m._confirm_quit = lambda: True
        m.close()
