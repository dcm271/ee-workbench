"""Setups: save the whole bench under a name and recall it.

A setup holds the open instruments (resource, model, each tab's settings:
names, ranges, decoders, pins…), the sequence (steps, script, limits), the
watchdog rules and the math channels. Recalling replaces the open tabs
with the setup's; outputs are never switched on by a recall."""
from __future__ import annotations

import time

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
                             QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout)

from .. import setups
from ..config import norm_model
from ..mathchannels import Channel
from .hub import hub
from .theme import set_role
from .widgets import C_MUTED


def capture_bench(main) -> dict:
    seq = main.sequencer()
    return {"instruments": [{"resource": p.resource, "model": norm_model(p.model),
                             "settings": p.session_settings()} for p in main.panels()],
            "sequence": seq.state(),
            "watchdog": hub.watchdog.to_list(),
            "watchdog_armed": hub.watchdog.armed,
            "math": [c.to_dict() for c in hub.math.channels]}


def check_bench(d) -> list[dict]:
    """The setup's instrument entries, or ValueError if it is damaged. Checked
    before anything is closed, so a bad file leaves the bench as it was."""
    if not isinstance(d, dict):
        raise ValueError("not a setup (expected a JSON object)")
    insts = d.get("instruments", [])
    if not isinstance(insts, list):
        raise ValueError("'instruments' must be a list")
    out = []
    for k, e in enumerate(insts):
        if not isinstance(e, dict) or not str(e.get("resource", "")).strip():
            raise ValueError(f"instrument {k + 1} has no resource")
        settings = e.get("settings") or {}
        out.append({"resource": str(e["resource"]).strip(),
                    "model": str(e.get("model", "auto")),
                    "settings": settings if isinstance(settings, dict) else {}})
    for key in ("sequence",):
        if d.get(key) is not None and not isinstance(d[key], dict):
            raise ValueError(f"'{key}' must be an object")
    return out


def apply_bench(main, d: dict) -> list[str]:
    """Replace the bench with the setup's. Returns problems that didn't stop it."""
    insts = check_bench(d)
    problems = []
    for i in reversed(range(main.tabs.count())):
        main.close_tab(i)
    for e in insts:
        try:
            main.open_instrument(e["resource"], e["model"], e["settings"])
        except Exception as ex:          # noqa: BLE001 - damaged tab settings
            problems.append(f"{e['resource']}: {ex}")
            try:
                main.open_instrument(e["resource"], e["model"])
            except Exception:            # noqa: BLE001
                pass
    if d.get("sequence"):
        try:
            main.sequencer().set_state(d["sequence"])
        except Exception as ex:          # noqa: BLE001
            problems.append(f"sequence: {ex}")
    problems += hub.watchdog.load(d.get("watchdog", []))
    hub.watchdog.armed = bool(d.get("watchdog_armed", True))
    hub.math.channels = Channel.load_list(d.get("math", []))
    for attr in ("wd_win", "math_win"):
        w = getattr(main, attr, None)
        if w is not None:            # rebuilt with the new rules / channels next time
            w.close()
            w.deleteLater()
            setattr(main, attr, None)
    if hub.math.channels:
        from .math_window import MathWindow
        main.math_win = MathWindow(main)     # records math into sessions while hidden
    main.save_bench_state()
    return problems


class SetupsDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Setups")
        self.resize(620, 440)
        v = QVBoxLayout(self)
        hint = QLabel("A setup is the whole bench under one name: which instruments are open "
                      "and how each tab is set up, the sequence, the watchdog rules and the "
                      "math channels. Recall closes the open tabs and opens the setup's. "
                      "Supply outputs are never switched on by a recall.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C_MUTED};")
        v.addWidget(hint)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda *_: self.recall())
        v.addWidget(self.list, 1)
        row = QHBoxLayout()
        save = QPushButton("Save current as…")
        set_role(save, "primary")
        save.clicked.connect(self.save)
        row.addWidget(save)
        for text, fn in (("Recall", self.recall), ("Delete", self.delete),
                         ("Export…", self.export), ("Import…", self.import_)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        v.addLayout(row)
        self._fill()

    def _fill(self):
        self.list.clear()
        for name, mtime, desc in setups.list_setups():
            it = QListWidgetItem(f"{name}    ·  {time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))}"
                                 + (f"\n    {desc}" if desc else ""))
            it.setData(Qt.ItemDataRole.UserRole, name)
            self.list.addItem(it)

    def _current(self) -> str | None:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def save(self):
        name, ok = QInputDialog.getText(self, "Save setup", "Name:", text=self._current() or "")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in [n for n, _, _ in setups.list_setups()] and QMessageBox.question(
                self, "Save setup", f"Replace '{name}'?") != QMessageBox.StandardButton.Yes:
            return
        desc, _ = QInputDialog.getText(self, "Save setup", "Description (optional):")
        try:
            setups.save_setup(name, capture_bench(self.main), desc.strip())
        except OSError as e:
            QMessageBox.warning(self, "Save setup", f"Can't save '{name}':\n{e}")
            return
        self._fill()
        self.main.status.showMessage(f"Saved setup '{name}'", 5000)

    def recall(self):
        name = self._current()
        if not name:
            return
        try:
            d = setups.load_setup(name)
            check_bench(d)
        except ValueError as e:
            QMessageBox.warning(self, "Recall setup", f"Can't recall '{name}': {e}")
            return
        if self.main.panels() and QMessageBox.question(
                self, "Recall setup", f"Close the open instruments and open '{name}'?"
        ) != QMessageBox.StandardButton.Yes:
            return
        problems = apply_bench(self.main, d)
        self.main.status.showMessage(f"Recalled setup '{name}'", 5000)
        if problems:
            QMessageBox.warning(self, "Recall setup", f"'{name}' was recalled, but some parts "
                                "were damaged and skipped:\n\n" + "\n".join(problems))
        self.accept()

    def delete(self):
        name = self._current()
        if name and QMessageBox.question(self, "Delete setup", f"Delete '{name}'?") == \
                QMessageBox.StandardButton.Yes:
            setups.delete_setup(name)
            self._fill()

    def export(self):
        name = self._current()
        if not name:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export setup", f"{name}.json", "Setup (*.json)")
        if path:
            try:
                setups.export_setup(name, path)
            except OSError as e:
                QMessageBox.warning(self, "Export setup", f"Can't write {path}:\n{e}")

    def import_(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import setup", "", "Setup (*.json)")
        if path:
            try:
                setups.import_setup(path)
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, "Import setup", f"Can't read {path}:\n{e}")
            self._fill()
