"""Save a report (HTML or PDF) of a sequence run or a session file."""
from __future__ import annotations

import time
import webbrowser
from pathlib import Path

from PyQt6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from ..about import APP_NAME
from .. import report
from ..session import SUFFIX

FILTER = "HTML page (*.html);;PDF (*.pdf)"


def ask_path(parent, default: str) -> str | None:
    path, chosen = QFileDialog.getSaveFileName(parent, "Save report", default, FILTER)
    if not path:
        return None
    if not path.lower().endswith((".html", ".htm", ".pdf")):
        path += ".pdf" if "PDF" in chosen else ".html"
    return path


def write(parent, html: str, path: str):
    try:
        report.save(html, path)
    except Exception as e:      # noqa: BLE001 - missing WeasyPrint, full disk
        QMessageBox.warning(parent, "Report", str(e))
        return
    box = QMessageBox(QMessageBox.Icon.Information, "Report", f"Saved {path}", parent=parent)
    show = box.addButton("Open it", QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Close)
    box.exec()
    if box.clickedButton() is show:
        webbrowser.open(Path(path).resolve().as_uri())


def report_for_session(parent, path: str | None = None, t_range=None, columns=None):
    if path is None:
        path, _ = QFileDialog.getOpenFileName(parent, "Report on a session", "",
                                              f"{APP_NAME} session (*{SUFFIX})")
        if not path:
            return
    title, ok = QInputDialog.getText(parent, "Report", "Title:", text=f"Session {Path(path).stem}")
    if not ok:
        return
    out = ask_path(parent, str(Path(path).with_suffix(".html")))
    if not out:
        return
    try:
        html = report.session_report(path, title, t_range, columns)
    except Exception as e:      # noqa: BLE001
        QMessageBox.warning(parent, "Report", f"Can't read {path}:\n{e}")
        return
    write(parent, html, out)


def default_name(prefix: str) -> str:
    return time.strftime(f"{prefix}_%Y%m%d_%H%M%S.html")
