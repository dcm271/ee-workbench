import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from otterbench import report, setups, wiring  # noqa: E402
from otterbench.session import SessionReader, SessionWriter  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(wiring, "_current", None)
    yield
    wiring._current = None


def photo():
    import base64
    from otterbench.gui.wiring_dialog import to_jpeg
    img = QImage(3000, 1000, QImage.Format.Format_RGB32)
    img.fill(0xff336699)
    data = to_jpeg(img)
    assert data[:3] == b"\xff\xd8\xff" and QImage.fromData(data).width() == 1600
    return base64.b64encode(data).decode()


def test_remembered_and_backed_up():
    w = wiring.Wiring("A → B", photo())
    wiring.set_current(w)
    wiring._current = None
    assert wiring.current() == w
    wiring.set_current(wiring.Wiring("C → D"))
    assert json.loads(wiring.path().with_suffix(".json.old").read_text(encoding="utf-8"))["text"] == "A → B"


@pytest.mark.parametrize("d", [None, [], "x", {"text": 5}, {"photo": "!!"}, {"photo": 3},
                               {"photo": "aGVsbG8="}])
def test_damaged_is_empty(d):
    assert wiring.Wiring.from_dict(d).empty


def test_in_reports_and_sessions(tmp_path):
    wiring.set_current(wiring.Wiring("OUT+ → <J3>", photo()))
    html = report.sequence_report("t", [], [], None, "", [("a", "b")], "Finished")
    assert "OUT+ → &lt;J3&gt;" in html and "data:image/jpeg;base64," in html
    s = SessionWriter()
    p = str(tmp_path / "a.otter")
    s.start(p)
    s.note("x")
    s.image(wiring.current().image, "Scope")
    s.stop()
    wiring.set_current(wiring.Wiring("not-this-one"))       # the recording keeps its own
    r = SessionReader(p)
    assert r.wiring.text == "OUT+ → <J3>" and r.images[0][1] == "Scope"
    r.close()
    html = report.session_report(p)
    assert "OUT+ → &lt;J3&gt;" in html and "not-this-one" not in html and "Pictures" in html
    import sqlite3
    db = sqlite3.connect(p)                          # an older file
    db.execute("DROP TABLE images")
    db.execute("DELETE FROM meta WHERE key = 'wiring'")
    db.commit()
    db.close()
    r = SessionReader(p)
    assert r.images == [] and r.wiring.empty
    r.close()


def test_in_setups():
    from otterbench.config import Config
    from otterbench.gui.main_window import MainWindow
    from otterbench.gui.setups_dialog import apply_bench, capture_bench
    from otterbench.gui.wiring_dialog import WiringDialog
    m = MainWindow([], Config(), None)
    try:
        dlg = WiringDialog(m)
        dlg.text.setPlainText("mine")
        dlg.save()
        setups.save_setup("w", capture_bench(m))
        wiring.set_current(wiring.Wiring("something else"))
        apply_bench(m, setups.load_setup("w"))
        assert wiring.current().text == "mine"
        apply_bench(m, {"instruments": []})
        assert wiring.current().empty
    finally:
        m._confirm_quit = lambda: True
        m.close()
