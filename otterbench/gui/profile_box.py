"""'Profile' group: vary a setpoint over time (a software function generator).

Used by the supply, PPK2 and function-generator tabs. The points are sent
from a thread of their own through the tab's I/O queue, one at a time (the
next waits for the last to be done), on schedule: if the instrument is
slower than the profile's rate, points are skipped rather than falling
behind. Esc / ALL OUTPUTS OFF stops it."""
from __future__ import annotations

import threading
import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import (QComboBox, QGroupBox, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout)

from .. import profile as prof
from . import touch
from .theme import set_role
from .widgets import C_MUTED, C_WARN

EXAMPLES = ["sine 3.0..3.6 period=2 for=20", "ramp 0..5 for=10", "steps 1.8..3.6 n=7 dwell=2",
            "triangle 1.8..3.6 period=4 cycles=3", "square 3.0..3.3 period=1 duty=25 for=10",
            "list 3.0 3.3 3.6 3.3 dwell=1", "csv /path/to/profile.csv"]
HELP = ("Shapes: sine / triangle / square lo..hi period= (for= or cycles=), ramp lo..hi "
        "for=, steps lo..hi n= dwell=, list v1 v2 … dwell=, csv file (rows of time_s, value). "
        "rate= points per second (default 10).")


class _Sig(QObject):
    point = pyqtSignal(int, float, float)
    done = pyqtSignal(str)


class ProfileBox(QGroupBox):
    """setters: {"voltage": fn(driver, value), ...}; panel: the tab (worker, connected)."""

    def __init__(self, panel, setters: dict, units: dict | None = None,
                 title: str = "Profile  (vary a setpoint over time)"):
        super().__init__(title)
        self.panel = panel
        self.setters = setters
        self.units = units or {}
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.sig = _Sig()
        self.sig.point.connect(self._on_point)
        self.sig.done.connect(self._on_done)
        v = QVBoxLayout(self)
        row = QHBoxLayout()
        self.what = QComboBox()
        self.what.addItems(list(setters))
        row.addWidget(self.what)
        self.text = QComboBox()
        self.text.setEditable(True)
        self.text.addItems(EXAMPLES)
        self.text.setCurrentText(EXAMPLES[0])
        self.text.lineEdit().setPlaceholderText("e.g. sine 3.0..3.6 period=2 for=20")
        self.text.setToolTip(HELP)
        self.text.currentTextChanged.connect(self._preview)
        row.addWidget(self.text, 1)
        v.addLayout(row)
        self.plot = pg.PlotWidget()
        self.plot.setFixedHeight(90 if not touch.TOUCH else 110)
        self.plot.hideButtons()
        self.plot.setMouseEnabled(False, False)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.curve = self.plot.plot(pen=pg.mkPen("#4fc3f7", width=1.5))
        self.marker = self.plot.plot(pen=None, symbol="o", symbolSize=7, symbolBrush="#ffb347")
        v.addWidget(self.plot)
        row = QHBoxLayout()
        self.info = QLabel("")
        self.info.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        self.info.setWordWrap(True)
        row.addWidget(self.info, 1)
        self.run_btn = QPushButton("▶ Run profile")
        set_role(self.run_btn, "primary")
        self.run_btn.clicked.connect(self.start)
        row.addWidget(self.run_btn)
        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        row.addWidget(self.stop_btn)
        v.addLayout(row)
        self.what.currentTextChanged.connect(self._preview)
        self._preview()

    # ---------------------------------------------------------------- preview
    def _parse(self):
        p = prof.parse(self.text.currentText())
        t, y = p.points()
        return p, t, y

    def _preview(self, *_):
        try:
            p, t, y = self._parse()
        except prof.ProfileError as e:
            self.info.setText(f"⚠ {e}")
            self.info.setStyleSheet(f"color:{C_WARN}; font-size:11px;")
            self.curve.setData([], [])
            return
        u = self.units.get(self.what.currentText(), "")
        if p.shape in ("list", "steps", "logsteps", "csv"):
            x = np.repeat(t, 2)[1:]
            x = np.append(x, p.length)
            yy = np.repeat(y, 2)
        else:
            x, yy = t, y
        log = p.shape == "logsteps"
        self.plot.setLogMode(y=log)
        self.curve.setData(x, yy)
        self.marker.setData([], [])
        self.info.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        self.info.setText(f"{len(t)} points over {p.length:g} s, {y.min():.6g} to "
                          f"{y.max():.6g} {u}".rstrip())

    # ---------------------------------------------------------------- running
    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.running or not self.panel.connected:
            return
        try:
            p, t, y = self._parse()
        except prof.ProfileError as e:
            self.info.setText(f"⚠ {e}")
            return
        fn = self.setters[self.what.currentText()]
        worker = self.panel.worker
        stop = self.stop_event = threading.Event()
        last = [0.0]

        def setter(v):
            done = threading.Event()

            def job(drv):
                try:
                    fn(drv, v)
                finally:
                    done.set()
            # read the setpoints back about once a second so the tab follows along
            m = time.monotonic()
            refresh = m - last[0] > 1.0
            if refresh:
                last[0] = m
            worker.submit(job, refresh=refresh)
            while not done.wait(0.05):
                if stop.is_set():
                    raise prof.ProfileStopped()

        def body():
            msg = "done"
            try:
                prof.run(p, setter, stop, on_point=lambda k, tt, vv: self.sig.point.emit(k, tt, vv))
            except prof.ProfileStopped:
                msg = "stopped"
            except Exception as e:      # noqa: BLE001
                msg = f"failed: {e}"
            worker.submit(lambda d: None, refresh=True)      # show the final setpoint
            self.sig.done.emit(msg)
        self.thread = threading.Thread(target=body, daemon=True, name="profile")
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.text.setEnabled(False)
        self.what.setEnabled(False)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _on_point(self, k, t, v):
        self.marker.setData([t], [v])   # (pyqtgraph applies the log axis itself)
        u = self.units.get(self.what.currentText(), "")
        self.info.setText(f"Running: point {k + 1}, t = {t:.2f} s, {v:.6g} {u}".rstrip())

    def _on_done(self, msg):
        self.run_btn.setEnabled(self.panel.connected)
        self.stop_btn.setEnabled(False)
        self.text.setEnabled(True)
        self.what.setEnabled(True)
        self.info.setText(f"Profile {msg}.")
        self.panel.message.emit(f"{self.panel.display_name()}: profile {msg}")

    def set_enabled(self, on: bool):
        """Called from the tab's set_enabled (which enables every button)."""
        if self.running:
            self.run_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
        else:
            self.run_btn.setEnabled(on)
            self.stop_btn.setEnabled(False)
