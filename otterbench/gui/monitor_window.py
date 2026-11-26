"""Monitor: everything that is on, from every open instrument, on one page.

The table lists each signal (a supply's voltage, current and power while its
output is on; a meter's reading; a PPK2's current; ...) with its value now and
its average, minimum and maximum over the time the chart shows. Type a
nickname into the first column ("DUT 3V3 rail") and it names that signal
here, in the chart's legend and View menu, and in saved setups.

The chart has an axis for each unit, so volts from a supply and a meter share
one, and amps from a supply and a PPK2 another.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel,
                             QMainWindow, QPushButton, QSplitter, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from ..clock import now
from .hub import hub
from .panel_base import SPANS
from .theme import set_role
from .trend_view import ONE, Axis, Trace, TrendView
from . import touch
from .widgets import C_MUTED, MONO, SteadyY, fmt_si

COLS = ["Nickname", "Signal", "", "Now", "Average", "Min", "Max"]
NICK, SOURCE, STATE, NOW, AVG, MIN, MAX = range(len(COLS))
PALETTE = ["#4fc3f7", "#ffb347", "#7ee787", "#c39bff", "#ef9a9a", "#fff176",
           "#80cbc4", "#f48fb1", "#bcaaa4", "#9fa8da", "#ffcc80", "#a5d6a7"]
AXIS_NAMES = {"V": "Voltage", "A": "Current", "W": "Power", "Ω": "Resistance",
              "Hz": "Frequency", "°C": "Temperature", "": "Value"}
AXIS_COLORS = {"V": "#ffb347", "A": "#4fc3f7", "W": "#c39bff"}
ON_ONLY, EVERYTHING = "What is on", "Everything open"


class MonitorWindow(QMainWindow):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Monitor")
        self.resize(1300, 800)
        self._colors: dict[str, str] = {}
        self._keys: list[str] = []
        self._filling = False
        self._live = True
        self._steady: list[SteadyY] = []

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(6, 6, 6, 6)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Show"))
        self.which = QComboBox()
        self.which.addItems([ON_ONLY, EVERYTHING])
        self.which.setToolTip("A supply's readings count as on while its output is; "
                              "other instruments while their readings keep coming")
        self.which.currentIndexChanged.connect(lambda *_: self.refresh())
        bar.addWidget(self.which)
        bar.addWidget(QLabel("Span"))
        self.span_combo = QComboBox()
        self.span_combo.addItems(list(SPANS))
        self.span_combo.setCurrentText("2 min")
        self.span_combo.currentIndexChanged.connect(lambda *_: self.go_live())
        bar.addWidget(self.span_combo)
        self.view = TrendView([], [])
        self.view.legend = False           # the table, in the traces' colours, is the key
        self.view.mode = ONE
        self.view.rebuilt.connect(self._rebuilt)
        self.view.browsed.connect(self._browsed)
        bar.addWidget(self.view.menu_button)
        bar.addStretch(1)
        for text, tip, fn in (("T−", "Show more time", lambda: self.zoom_time(False)),
                              ("T+", "Show less time", lambda: self.zoom_time(True)),
                              ("Y−", "Zoom every axis out", lambda: self.zoom_y(2.0)),
                              ("Y+", "Zoom every axis in", lambda: self.zoom_y(0.5))):
            b = QPushButton(text)
            b.setToolTip(tip + ".\nWith a mouse: the wheel over the chart zooms time, "
                               "over an axis that axis; drag to move.")
            b.setFixedWidth(52 if touch.TOUCH else 40)
            b.clicked.connect(fn)
            bar.addWidget(b)
        self.fit_btn = QPushButton("Fit")
        self.fit_btn.setToolTip("Follow the newest readings and scale every axis to its data")
        self.fit_btn.clicked.connect(self.go_live)
        bar.addWidget(self.fit_btn)
        v.addLayout(bar)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.SelectedClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed
                                   | QAbstractItemView.EditTrigger.AnyKeyPressed)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(SOURCE, QHeaderView.ResizeMode.Stretch)
        for c, w in ((NICK, 200), (STATE, 44), (NOW, 130), (AVG, 130), (MIN, 130), (MAX, 130)):
            self.table.setColumnWidth(c, w)
        self.table.itemChanged.connect(self._edited)
        self.table.cellDoubleClicked.connect(self._double_clicked)
        self.table.setToolTip("Double-click (or tap twice) a nickname to change it; "
                              "double-click a signal to go to its instrument's tab")
        split.addWidget(self.table)
        split.addWidget(self.view)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([300, 450])
        v.addWidget(split, 1)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color:{C_MUTED};")
        v.addWidget(self.status)
        self.setCentralWidget(central)
        saved = (self.main.prefs.get("monitor") or {}).get("view")
        if saved:
            self.view.restore(saved)
        else:
            self.view.set_mode(ONE)
        self.view.changed.connect(self._save_view)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)
        self.refresh()

    # ---- what to show ------------------------------------------------------------
    def keys(self) -> list[str]:
        """The signals to list: open instruments only; live ones, unless
        everything is asked for."""
        mon = hub.monitor
        mon.keep_only([p.display_name() for p in self.main.panels()])
        at = now()
        every = self.which.currentText() == EVERYTHING
        return [k for k, s in mon.signals.items() if every or s.live(at)]

    def color(self, key: str) -> str:
        if key not in self._colors:
            self._colors[key] = PALETTE[len(self._colors) % len(PALETTE)]
        return self._colors[key]

    def _layout(self, keys):
        mon = hub.monitor
        axes, traces = {}, []
        for k in keys:
            s = mon.signals[k]
            ak = "u:" + s.unit
            if ak not in axes:
                axes[ak] = Axis(ak, AXIS_NAMES.get(s.unit, s.unit), s.unit,
                                AXIS_COLORS.get(s.unit, "#c9ccd1"))
            traces.append(Trace(k, mon.label(k), ak, self.color(k)))
        order = {"u:V": 0, "u:A": 1, "u:W": 2}
        return sorted(axes.values(), key=lambda a: order.get(a.key, 3)), traces

    # ---- refresh -------------------------------------------------------------------
    def refresh(self):
        if not self.isVisible():
            return
        keys = self.keys()
        editing = self.table.state() == QAbstractItemView.State.EditingState
        if keys != self._keys and not editing:
            self._keys = keys
            self._fill_table()
        self.view.set_traces(*self._layout(self._keys))
        mon = hub.monitor
        self.view.set_data(None, {k: mon.signals[k].data() for k in self._keys
                                  if k in mon.signals})
        self._scroll()
        self._update_values()

    def _fill_table(self):
        mon = hub.monitor
        self._filling = True
        try:
            self.table.setRowCount(len(self._keys))
            for row, k in enumerate(self._keys):
                s = mon.signals[k]
                nick = QTableWidgetItem(mon.nickname(k))
                nick.setData(Qt.ItemDataRole.UserRole, k)
                nick.setForeground(QBrush(QColor(self.color(k))))
                nick.setToolTip(f"{k}  (the name math channels and scripts use)")
                self.table.setItem(row, NICK, nick)
                src = QTableWidgetItem("■  " + s.source)
                src.setFlags(src.flags() & ~Qt.ItemFlag.ItemIsEditable)
                src.setForeground(QBrush(QColor(self.color(k))))
                src.setToolTip(f"{s.instrument}: {s.quantity}. Double-click to go to its tab")
                self.table.setItem(row, SOURCE, src)
                for c in (STATE, NOW, AVG, MIN, MAX):
                    it = QTableWidgetItem("")
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if c != STATE:
                        it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                            | Qt.AlignmentFlag.AlignVCenter)
                        it.setFont(QFont(MONO, 11 if touch.TOUCH else 10))
                    self.table.setItem(row, c, it)
        finally:
            self._filling = False
        if not self._keys:
            self.status.setText("Nothing is on. Switch an output on, or show everything "
                                "open, to see it here." if self.main.panels() else
                                "No instruments are open.")

    def _update_values(self):
        mon = hub.monitor
        w = self.view.window()
        at = now()
        for row, k in enumerate(self._keys):
            s = mon.signals.get(k)
            if s is None or self.table.item(row, NOW) is None:
                continue
            live = s.live(at)
            st = self.table.item(row, STATE)
            st.setText("●" if live else "off" if s.on is False else "—")
            st.setForeground(QBrush(QColor("#7ee787" if live else C_MUTED)))
            st.setToolTip("On" if live else "Output off" if s.on is False else "No readings")
            self.table.item(row, NOW).setText(fmt_si(s.last(), s.unit, 5, signed=True))
            y = s.window(*w) if w is not None else []
            for c, f in ((AVG, "mean"), (MIN, "min"), (MAX, "max")):
                self.table.item(row, c).setText(
                    fmt_si(float(getattr(y, f)()), s.unit, 5, signed=True) if len(y) else "—")
        if self._keys:
            span = f"{w[1] - w[0]:.0f} s" if w is not None else ""
            self.status.setText(f"{len(self._keys)} signals. Average, min and max are over "
                                f"the {span} the chart shows."
                                + ("" if self._live else "  Looking back: Live to follow again."))

    # ---- chart ---------------------------------------------------------------------
    def _rebuilt(self):
        self._steady = [SteadyY(vb) for vb in self.view.boxes.values()]

    def _scroll(self):
        vb = self.view.x_box()
        if vb is None:
            return
        if self._live:
            edge = now()
            span = SPANS[self.span_combo.currentText()]
            if span is None:
                starts = [s.t[0] for k in self._keys if (s := hub.monitor.signals.get(k))
                          and s.n]
                start = min(starts) if starts else edge - 10
            else:
                start = edge - span
            vb.setXRange(start, edge, padding=0)
        for s in self._steady:
            s.update()

    def _browsed(self):
        self._live = False
        self.fit_btn.setText("▶ Live")
        set_role(self.fit_btn, "primary")

    def go_live(self):
        self._live = True
        for s in self._steady:
            s.auto = True
            s.reset()
        self.fit_btn.setText("Fit")
        set_role(self.fit_btn, "")
        self.refresh()

    def zoom_time(self, zoom_in: bool):
        if self._live:
            names = list(SPANS)
            i = names.index(self.span_combo.currentText())
            i = max(0, i - 1) if zoom_in else min(len(names) - 1, i + 1)
            self.span_combo.setCurrentIndex(i)
        elif self.view.x_box() is not None:
            self.view.x_box().scaleBy(x=0.5 if zoom_in else 2.0)

    def zoom_y(self, factor: float):
        for s in self._steady:
            s.auto = False
        self.view.zoom_y(factor)

    # ---- nicknames -------------------------------------------------------------------
    def _edited(self, item: QTableWidgetItem):
        if self._filling or item.column() != NICK:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        hub.monitor.set_nickname(key, item.text())
        self.main.save_bench_state()
        self.view._sig = None              # relabel the legend and the View menu
        self.refresh()

    def _double_clicked(self, row: int, col: int):
        if col == NICK or row >= len(self._keys):
            return
        s = hub.monitor.signals.get(self._keys[row])
        if s is None:
            return
        for p in self.main.panels():
            if p.display_name() == s.instrument:
                self.main.tabs.setCurrentWidget(p)
                self.main.raise_()
                self.main.activateWindow()
                return

    def _save_view(self):
        self.main.prefs["monitor"] = {"view": self.view.state()}
        self.main.save_bench_state()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.refresh)
