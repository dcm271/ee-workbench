"""Crash resistance: damaged files, failing writes, bad expressions and
uncaught exceptions must leave the app running (see otterbench/crash.py)."""
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from otterbench import config, crash, mathchannels as mc, setups, watchdog
from otterbench.api import ApiServer
from otterbench.sequencer import Plan
from otterbench.session import SessionReader, SessionWriter


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "otterbench"


# ---------------------------------------------------------------- crash log
def test_crash_report_logs_once_per_minute(cfgdir, monkeypatch):
    monkeypatch.setattr(crash, "_shown", {})
    seen = []
    crash.set_notifier(lambda summary, details: seen.append(summary))
    try:
        for _ in range(50):              # e.g. a timer failing every 200 ms
            try:
                1 / 0
            except ZeroDivisionError as e:
                crash.report(type(e), e, e.__traceback__, where="test")
    finally:
        crash.set_notifier(None)
    assert seen == ["ZeroDivisionError: division by zero"]
    log = crash.log_path().read_text(encoding="utf-8")
    assert log.count("ZeroDivisionError") == 1


def test_crash_log_rotates(cfgdir, monkeypatch):
    monkeypatch.setattr(crash, "MAX_LOG", 100)
    crash.write_log("x" * 200)
    crash.write_log("second")
    assert crash.log_path().with_name("crash.log.1").exists()
    assert "second" in crash.log_path().read_text(encoding="utf-8")


# ---------------------------------------------------------------- start-up files
def test_damaged_last_session_keeps_good_entries(tmp_path):
    p = tmp_path / "last-session.json"
    p.write_text(json.dumps({"open": [{"model": "3456A"}, "junk",
                                      {"resource": "SIM::PPK2", "settings": [1, 2]}],
                             "known": {"x": None, "SIM::6632A": {"resource": "SIM::6632A"}}}), encoding="utf-8")
    s = config.Session(p)
    assert [e.resource for e in s.open] == ["SIM::PPK2"]
    assert s.open[0].settings == {}
    assert list(s.known) == ["SIM::6632A"]


@pytest.mark.parametrize("text", ["[1, 2]", "not json", '{"open": 5, "known": []}'])
def test_unreadable_last_session_is_empty(tmp_path, text):
    p = tmp_path / "last-session.json"
    p.write_text(text, encoding="utf-8")
    s = config.Session(p)
    assert s.open == [] and s.known == {}


def test_session_save_handles_numpy(tmp_path):
    s = config.Session(tmp_path / "ls.json")
    s.save([config.Entry("SIM::PPK2", "PPK2", {"v": np.float64(3.3), "n": np.int32(4)})])
    d = json.loads((tmp_path / "ls.json").read_text(encoding="utf-8"))
    assert d["open"][0]["settings"] == {"v": 3.3, "n": 4}


def test_config_startup_not_a_table(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('startup = 1\ninstrument = "x"\n', encoding="utf-8")
    cfg = config.load_config(p)
    assert cfg.open == "last" and cfg.instruments == []
    assert "table" in cfg.warning


def test_watchdog_load_skips_damaged_rules():
    wd = watchdog.Watchdog()
    bad = wd.load([{"source": "PPK2", "limit": 0.05},
                   {"source": "x", "limit": "fifty"},
                   {"source": "y", "op": "=>"},
                   "junk"])
    assert len(wd.rules) == 1 and len(bad) == 3
    assert wd.load("not a list") == [] and wd.rules == []


def test_channels_load_list_skips_damaged():
    chans = mc.Channel.load_list([{"name": "a", "expr": "ppk2.current"}, 5, None])
    assert [c.name for c in chans] == ["a"]


def test_plan_from_hand_edited_json():
    p = Plan.from_dict({"steps": [{"action": "Set", "value": 3.3, "lo": None}, "junk"],
                        "loop": "forever", "count": "3", "start": "1"})
    assert p.steps[0].value == "3.3" and p.steps[0].lo == ""
    assert p.loop == "once" and p.count == 3 and p.start == 1.0


# ---------------------------------------------------------------- math
def test_math_shape_errors_are_math_errors():
    t = np.linspace(0, 1, 50)
    series = {("ppk2", "current"): (t, t)}
    with pytest.raises(mc.MathError):
        mc.evaluate("ppk2.current[:5]", series)
    with pytest.raises(mc.MathError):
        mc.evaluate("'text'", series, grid=t)


def test_live_math_tick_survives_bad_channel():
    lm = mc.LiveMath()
    t = np.linspace(0, 1, 20)
    lm.feed("PPK2", {"current": (t, np.ones_like(t))})
    lm.channels = [mc.Channel("bad", "ppk2.current[:3]"), mc.Channel("ok", "ppk2.current * 2")]
    out = lm.tick(1.0)
    assert out == {"ok": pytest.approx(2.0)} and "bad" in lm.errors


# ---------------------------------------------------------------- session file
def test_session_stop_never_raises(tmp_path):
    w = SessionWriter()
    w.start(str(tmp_path / "s.otter"))
    w.stream("k", "PPK2", "PPK2", "SIM::PPK2", ["a"])
    w.add("k", 1.0, [1.0])
    w._pending[999] = [[1.0, 2.0]]        # a table that doesn't exist: flush fails
    err = w.stop()
    assert "s_999" in err and not w.active
    assert w.stop() == ""                 # stopping twice is fine


def test_session_reader_rejects_foreign_file(tmp_path):
    p = tmp_path / "other.db"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE meta(key, value)")
    db.commit()
    db.close()
    with pytest.raises(ValueError):
        SessionReader(str(p))
    p.write_text("not a database", encoding="utf-8")
    with pytest.raises(sqlite3.DatabaseError):
        SessionReader(str(p))


# ---------------------------------------------------------------- setups
def test_damaged_setup_rejected_before_anything_closes(cfgdir):
    pytest.importorskip("PyQt6")
    from otterbench.gui.setups_dialog import check_bench
    with pytest.raises(ValueError):
        check_bench({"instruments": [{"model": "PPK2"}]})
    with pytest.raises(ValueError):
        check_bench({"instruments": "SIM::PPK2"})
    assert check_bench({"instruments": [{"resource": " SIM::PPK2 ", "settings": 3}]}) == [
        {"resource": "SIM::PPK2", "model": "auto", "settings": {}}]
    p = setups._file("broken")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        setups.load_setup("broken")


# ---------------------------------------------------------------- remote API
def test_api_rejects_malformed_bodies(cfgdir):
    srv = ApiServer(lambda: None, port=0)
    srv.start()
    try:
        for body in (b"[1, 2]", b'{"method": "set", "args": 5}', b"{bad"):
            req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/call", body,
                                         method="POST",
                                         headers={"Authorization": f"Bearer {srv.token}"})
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(req, timeout=5)
            assert ei.value.code == 400
    finally:
        srv.stop()
    assert not (cfgdir / "api.json").exists()


# ---------------------------------------------------------------- GUI (offscreen)
@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qt = pytest.importorskip("PyQt6.QtWidgets")
    app = qt.QApplication.instance() or qt.QApplication([])
    yield app


def _spin(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def test_exception_in_slot_does_not_abort(qapp, cfgdir, monkeypatch):
    from PyQt6.QtCore import QTimer
    monkeypatch.setattr(crash, "_shown", {})
    old = __import__("sys").excepthook
    crash.install()
    seen = []
    crash.set_notifier(lambda s, d: seen.append(s))
    try:
        before = crash.errors_caught()

        def boom():
            raise RuntimeError("slot failed")
        QTimer.singleShot(0, boom)        # without the hook PyQt6 would abort here
        _spin(qapp, 0.2)
        assert crash.errors_caught() == before + 1
        assert seen == ["RuntimeError: slot failed"]
    finally:
        crash.set_notifier(None)
        __import__("sys").excepthook = old


def test_gpio_poll_failure_offers_reconnect(qapp, cfgdir):
    from otterbench.gui.main_window import MainWindow
    from otterbench.config import Config
    win = MainWindow([("GPIO", "SIM::GPIO")], Config(), None)
    try:
        panel = win.panels()[0]
        deadline = time.monotonic() + 5
        while not panel.connected and time.monotonic() < deadline:
            _spin(qapp, 0.05)
        assert panel.connected
        panel.worker.poll_enabled = False  # what the worker does after a failed read,
        _spin(qapp, 0.1)
        panel.worker.poll_paused.emit()    # then says so (this used to raise
        _spin(qapp, 0.1)                   # AttributeError: GPIO tabs have no pause_btn)
        bar = win.tabs.tabBar()
        btn = bar.tabButton(0, bar.ButtonPosition.LeftSide)
        assert btn is not None and "Reconnect" in btn.text()
        assert win.tabs.tabText(0).startswith("⚠")
        panel.worker.poll_enabled = True   # readings again: the warning goes away
        _spin(qapp, 0.5)
        assert bar.tabButton(0, bar.ButtonPosition.LeftSide) is None
        assert not win.tabs.tabText(0).startswith("⚠")
    finally:
        win.close()
        _spin(qapp, 0.1)


def test_all_outputs_off_survives_a_failing_tab(qapp, cfgdir):
    from otterbench.gui.main_window import MainWindow
    from otterbench.config import Config
    win = MainWindow([("auto", "SIM::66321D"), ("auto", "SIM::6632A")], Config(), None)
    try:
        deadline = time.monotonic() + 5
        while not all(p.connected for p in win.panels()) and time.monotonic() < deadline:
            _spin(qapp, 0.05)
        first, second = win.panels()
        called = []
        first.output_off = lambda: (_ for _ in ()).throw(RuntimeError("bus error"))
        second.output_off = lambda: called.append(True)
        win.all_outputs_off()
        assert called == [True]
        assert "FAILED" in win.status.currentMessage()
    finally:
        win.close()
        _spin(qapp, 0.1)
