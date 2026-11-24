"""Name, logo, splash screen and About box."""
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from otterbench import about  # noqa: E402
from otterbench.gui import branding_ui as b  # noqa: E402


def test_logo_and_icon():
    assert b.logo(128).width() == 128
    assert len(b.icon().availableSizes()) == 7


def test_splash_goes_by_itself():
    s = b.Splash(_APP)
    s.show()
    s.say("Starting…")
    s.t0 -= 10
    s.done()
    end = time.monotonic() + 2
    while s.isVisible() and time.monotonic() < end:
        _APP.processEvents()
    assert not s.isVisible()
    assert b.splash_pixmap(2.0).width() == 2 * b.W


def test_about_box():
    d = b.AboutDialog()
    text = " ".join(x.text() for x in d.findChildren(QLabel))
    assert about.APP_NAME in text and about.VERSION in text
    assert about.LICENSE in text and "no warranty" in text
    d._copy()
    assert about.APP_NAME in QApplication.clipboard().text()


def test_details_hold_nothing_personal():
    t = about.details()
    assert "Python" in t and str(Path.home()) not in t and "@" not in t


def test_a_copy_without_git(monkeypatch, tmp_path):
    monkeypatch.setattr(about, "ROOT", tmp_path)
    assert about.revision() == "" and about.version_text() == about.VERSION
