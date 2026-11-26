"""Tab for a DUT's serial console (UART::/dev/ttyUSB0@115200): the text with
time stamps, a send line, and patterns that turn printed numbers into signals
(trend, watchdog, math, Measure). See instruments/uart.py."""
from __future__ import annotations

import time

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout,
                             QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
                             QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                             QVBoxLayout, QWidget)

from ..instruments.uart import (BAUDS, EOLS, SET_NAMES, Pattern, UartReading, UartState,
                                parse_resource)
from . import touch
from .panel_base import PanelBase
from .widgets import C_MUTED, TraceBuffer, fmt_fixed

COLORS = ["#4fc3f7", "#ffb74d", "#81c784", "#e57373", "#ba68c8", "#fff176"]
RX, TX, INFO = QColor("#d7dae0"), QColor("#7ee787"), QColor("#8a8f98")


class UartPanel(PanelBase):
    KIND = "uart"

    def __init__(self, resource: str, model: str, baud: int):
        super().__init__(resource, model, baud)
        self.worker.interval_s = 0.02
        self.patterns: list[Pattern] = []
        self.bufs: dict = {}                # pattern name -> TraceBuffer
        self.latest: dict = {}
        self._pending: dict | None = None
        self._history: list[str] = []
        self._hist_at = 0
        self._stamp: list[tuple[float, int]] = []

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        console = self._build_console()
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        self.idn_label = QLabel(f"Opening {self.resource}…")
        self.idn_label.setWordWrap(True)
        self.idn_label.setStyleSheet(f"color:{C_MUTED};")
        lay.addWidget(self.idn_label)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Name, e.g. “DUT console”")
        self.name_edit.textChanged.connect(self._name_changed)
        lay.addWidget(self.name_edit)
        lay.addWidget(self._build_port())
        lay.addWidget(self._build_patterns())
        lay.addStretch(1)
        sa = touch.scroll(inner)
        sa.setMinimumWidth(420 if touch.TOUCH else 380)
        split.addWidget(sa)
        self.tabs = QTabWidget()
        self.tabs.addTab(console, "Console")
        self.tabs.addTab(self._build_trend(), "Trend")
        split.addWidget(self.tabs)
        split.setStretchFactor(1, 1)
        split.setSizes([440, 840] if touch.TOUCH else [420, 1000])
        root.addWidget(split)
        self.set_enabled(False)
        self.start()

    # ---- building ------------------------------------------------------------
    def _build_console(self):
        w = self.build_console_tab("Text to send (Enter); ↑ for the last one")
        self.raw_line.installEventFilter(self)
        self.trace_chk.setVisible(False)
        row = QHBoxLayout()
        self.stamp_chk = QCheckBox("Times")
        self.stamp_chk.setChecked(True)
        row.addWidget(self.stamp_chk)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Show only lines containing…")
        self.filter_edit.setClearButtonEnabled(True)
        row.addWidget(self.filter_edit, 1)
        clr = QPushButton("Clear")
        clr.clicked.connect(lambda: self.log_view.clear())
        row.addWidget(clr)
        save = QPushButton("Save text…")
        save.clicked.connect(self._save_text)
        row.addWidget(save)
        w.layout().insertLayout(0, row)
        return w

    def _build_port(self):
        g = QGroupBox("Port")
        form = QFormLayout(g)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.baud_box = QComboBox()
        self.baud_box.setEditable(True)
        self.baud_box.addItems([str(b) for b in BAUDS])
        self.baud_box.setCurrentText(str(parse_resource(self.resource)[1]))
        self.baud_box.activated.connect(lambda *_: self._set_baud())
        self.baud_box.lineEdit().editingFinished.connect(self._set_baud)
        form.addRow("Baud", self.baud_box)
        self.eol_box = QComboBox()
        self.eol_box.addItems(list(EOLS))
        self.eol_box.setToolTip("What is sent after each line")
        self.eol_box.activated.connect(
            lambda *_: self.cmd(lambda d, e: d.set_eol(e), self.eol_box.currentText()))
        form.addRow("Line end", self.eol_box)
        row = QHBoxLayout()
        self.dtr_chk, self.rts_chk = QCheckBox("DTR"), QCheckBox("RTS")
        for chk, n in ((self.dtr_chk, "dtr"), (self.rts_chk, "rts")):
            chk.setChecked(True)
            chk.clicked.connect(lambda on, n=n: self.cmd(lambda d, v: d.set(n, v), float(on)))
            row.addWidget(chk)
        self.reset_btn = QPushButton("Pulse DTR")
        self.reset_btn.setToolTip("DTR off for 0.1 s: resets an Arduino and many other boards")
        self.reset_btn.clicked.connect(lambda: self.cmd(lambda d: d.pulse("dtr")))
        row.addWidget(self.reset_btn)
        form.addRow("Lines", row)
        return g

    def _build_patterns(self):
        g = QGroupBox("Values in the text")
        v = QVBoxLayout(g)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Pattern (regex)", "Unit", "Now"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c, width in ((0, 80), (2, 46), (3, 96)):
            self.table.setColumnWidth(c, width)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setMinimumHeight(170)
        self.table.itemChanged.connect(lambda *_: self._mark_edited())
        v.addWidget(self.table)
        row = QHBoxLayout()
        for text, fn in (("+ Value", lambda: self._add_row()), ("Remove", self._remove_row)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.clicked.connect(self.apply_patterns)
        row.addWidget(self.apply_btn)
        v.addLayout(row)
        hint = QLabel("The first (group) is the value:  vbat=([\\d.]+).  Without a group the "
                      "value is 1 when the line matches (PANIC), for the watchdog.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        v.addWidget(hint)
        return g

    def _build_trend(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addLayout(self.build_trend_bar())
        self.glw = pg.GraphicsLayoutWidget()
        self.plots: dict = {}
        self.curves: dict = {}
        v.addWidget(self.glw, 1)
        return w

    # ---- patterns ------------------------------------------------------------
    def _add_row(self, p: Pattern | None = None):
        self.table.blockSignals(True)
        r = self.table.rowCount()
        self.table.insertRow(r)
        p = p or Pattern("", "")
        for c, text in enumerate((p.name, p.regex, p.unit)):
            self.table.setItem(r, c, QTableWidgetItem(text))
        it = QTableWidgetItem("")
        it.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.table.setItem(r, 3, it)
        self.table.blockSignals(False)

    def _remove_row(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)
            self.apply_patterns()

    def _mark_edited(self):
        self.apply_btn.setText("Apply *")

    def _rows(self) -> list[Pattern]:
        def cell(r, c):
            it = self.table.item(r, c)
            return it.text().strip() if it else ""
        rows = [Pattern(cell(r, 0), cell(r, 1), cell(r, 2))
                for r in range(self.table.rowCount())]
        return [p for p in rows if p.name or p.regex]

    def apply_patterns(self):
        pats = self._rows()
        names = [p.name for p in pats]
        bad = next((p.problem() for p in pats if p.problem()), "")
        if not bad and len(set(names)) < len(names):
            bad = "Two values have the same name"
        if bad:
            QMessageBox.warning(self, "Values in the text", bad)
            return
        if self.recorder.active and names != [p.name for p in self.patterns]:
            self.rec_btn.setChecked(False)         # the CSV's columns would no longer fit
            self.console("CSV recording stopped: the values changed")
        self._use_patterns(pats)
        self.cmd(lambda d, p: d.set_patterns(p), pats)
        self.settings_changed.emit()

    def _use_patterns(self, pats):
        self.patterns = list(pats)
        self.apply_btn.setText("Apply")
        self.bufs = {p.name: self.bufs.get(p.name) or TraceBuffer(2) for p in pats}
        self.glw.clear()
        self.plots, self.curves = {}, {}
        first = None
        for k, p in enumerate(pats):
            pl = self.glw.addPlot(row=k, col=0)
            pl.showGrid(x=True, y=True, alpha=0.15)
            pl.setLabel("left", p.name, units=p.unit or None)
            pl.setClipToView(True)
            pl.setDownsampling(auto=True, mode="peak")
            if first is None:
                first = pl
            else:
                pl.setXLink(first)
            self.plots[p.name] = pl
            self.curves[p.name] = pl.plot(pen=pg.mkPen(COLORS[k % len(COLORS)], width=1.5))
        if first is not None:
            touch.plots(*self.plots.values())
            self.scrolling_trend(first, *self.plots.values())
        else:
            self._x_plot, self._steady = None, []

    # ---- names the sequencer / watchdog / math see --------------------------------
    def quantities(self):
        return [p.name for p in self.patterns]

    def measure_names(self):
        return [p.name for p in self.patterns]

    def set_names(self):
        return list(SET_NAMES)

    def unit_of(self, name: str) -> str:
        return next((p.unit for p in self.patterns if p.name == name), "")

    # ---- sending -------------------------------------------------------------
    def _send_raw(self, _read):
        if not self.connected:
            return
        text = self.raw_line.text()
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._hist_at = len(self._history)
        self.worker.submit(lambda d: d.send(text))
        self.raw_line.clear()

    def eventFilter(self, obj, e):
        if obj is self.raw_line and e.type() == e.Type.KeyPress \
                and e.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down) and self._history:
            step = -1 if e.key() == Qt.Key.Key_Up else 1
            self._hist_at = max(0, min(len(self._history), self._hist_at + step))
            self.raw_line.setText(self._history[self._hist_at]
                                  if self._hist_at < len(self._history) else "")
            return True
        return super().eventFilter(obj, e)

    def _set_baud(self):
        try:
            b = int(self.baud_box.currentText())
        except ValueError:
            return
        st = getattr(self, "_state", None)
        if self.connected and (st is None or st.baud != b):
            self.cmd(lambda d, v: d.set("baud", v), b)

    # ---- console text --------------------------------------------------------
    def _append(self, lines):
        """Coloured lines, without moving the view of someone reading back."""
        view = self.log_view
        bar = view.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        keep = bar.value()
        cur = QTextCursor(view.document())
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.beginEditBlock()
        fmt = QTextCharFormat()
        for text, color in lines:
            fmt.setForeground(color)
            if not view.document().isEmpty():
                cur.insertBlock()
            cur.insertText(text, fmt)
        cur.endEditBlock()
        bar.setValue(bar.maximum() if at_end else keep)

    def console(self, text: str):
        self._append([(time.strftime("%H:%M:%S  ") + text, INFO)])

    def _save_text(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save console", time.strftime("console_%Y%m%d_%H%M%S.txt"), "Text (*.txt)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(self.log_view.toPlainText() + "\n")
            except OSError as e:
                QMessageBox.warning(self, "Save console", f"Can't write {path}:\n{e}")

    # ---- names and settings --------------------------------------------------
    def _title(self) -> str:
        t = self.name_edit.text().strip()
        return f"UART · {t}" if t else "UART"

    def _name_changed(self, _):
        self.title_changed.emit(self._title())
        self.settings_changed.emit()

    def session_settings(self) -> dict:
        if self._pending is not None and not self.connected:
            return dict(self._pending)
        return {"name": self.name_edit.text(), "baud": self.baud_box.currentText(),
                "eol": self.eol_box.currentText(), "dtr": self.dtr_chk.isChecked(),
                "rts": self.rts_chk.isChecked(), "times": self.stamp_chk.isChecked(),
                "patterns": [[p.name, p.regex, p.unit] for p in self.patterns]}

    def restore_settings(self, s: dict):
        self._pending = dict(s)
        self.name_edit.setText(str(s.get("name", "")))
        self.stamp_chk.setChecked(bool(s.get("times", True)))
        pats = []
        for x in s.get("patterns") or []:
            try:
                p = Pattern(*[str(v) for v in x])
            except TypeError:
                continue
            if not p.problem() and p.name not in [q.name for q in pats]:
                pats.append(p)
        self.table.setRowCount(0)
        for p in pats:
            self._add_row(p)
        self._use_patterns(pats)

    # ---- worker callbacks ----------------------------------------------------
    def on_connected(self, family, caps, state):
        self.idn_label.setText(caps.idn)
        self.idn_label.setStyleSheet("color:#c9ccd1;")
        self.title_changed.emit(self._title())
        s, self._pending = self._pending or {}, None
        if s.get("eol") in EOLS:
            self.cmd(lambda d, e: d.set_eol(e), s["eol"])
        # a speed in the resource wins over the remembered one
        if str(s.get("baud", "")).isdigit() and "@" not in self.resource:
            self.cmd(lambda d, b: d.set("baud", b), int(s["baud"]))
        for n in ("dtr", "rts"):
            if s.get(n) is False:
                self.cmd(lambda d, n=n: d.set(n, 0))
        if self.patterns:
            self.cmd(lambda d, p: d.set_patterns(p), list(self.patterns))

    def on_state(self, st: UartState):
        if getattr(self, "_state", None) == st:
            return
        self._state = st
        for w in (self.baud_box, self.eol_box):
            w.blockSignals(True)
        if not self.baud_box.lineEdit().hasFocus():
            self.baud_box.setCurrentText(str(st.baud))
        self.eol_box.setCurrentText(st.eol)
        for w in (self.baud_box, self.eol_box):
            w.blockSignals(False)
        self.dtr_chk.setChecked(st.dtr)
        self.rts_chk.setChecked(st.rts)
        self.settings_changed.emit()

    def on_reading(self, r: UartReading):
        if not r.lines:
            return
        m = time.monotonic()
        self._stamp.append((m, len(r.lines)))
        while self._stamp and m - self._stamp[0][0] > 5:
            self._stamp.pop(0)
        want, stamps = self.filter_edit.text().lower(), self.stamp_chk.isChecked()
        shown = []
        changed = False
        for ln in r.lines:
            if not want or want in ln.text.lower():
                head = f"{ln.t:10.3f}  " if stamps else ""
                shown.append((head + ("» " if ln.dir == "tx" else "") + ln.text,
                              TX if ln.dir == "tx" else RX))
            for n, v in ln.values.items():
                if n in self.bufs:
                    self.bufs[n].append([ln.t, v])
                    self.latest[n] = v
                    changed = True
            if self.recording:
                self.record([time.strftime("%Y-%m-%dT%H:%M:%S"), f"{ln.t:.3f}", ln.dir, ln.text]
                            + [ln.values.get(p.name) for p in self.patterns])
        if shown:
            self._append(shown)
        if changed:
            self.table.blockSignals(True)
            for row in range(self.table.rowCount()):
                it = self.table.item(row, 0)
                v = self.latest.get(it.text().strip()) if it else None
                if v is not None and self.table.item(row, 3):
                    u = self.unit_of(it.text().strip())
                    self.table.item(row, 3).setText(
                        fmt_fixed(v, u, 5).lstrip("+") if u else f"{v:.6g}")
            self.table.blockSignals(False)

    def readings_per_s(self) -> float:
        return sum(n for _, n in self._stamp) / 5.0

    def _maybe_redraw(self):
        super()._maybe_redraw()
        self.rate_label.setText(f"{self.readings_per_s():.1f} lines/s" if self.connected else "")

    def _view_edge(self):
        return self._t_last            # polled 50 times a second: no smoothing needed

    def csv_header(self):
        return ["timestamp", "t_s", "dir", "text"] + [
            f"{p.name}_{p.unit}".rstrip("_").replace("Ω", "ohm") for p in self.patterns]

    def csv_name(self):
        return "uart_%Y%m%d_%H%M%S.csv"

    def clear_trend(self):
        for b in self.bufs.values():
            b.clear()
        super().clear_trend()

    def redraw(self):
        for n, c in self.curves.items():
            d = self.bufs[n].view()
            c.setData(d[:, 0], d[:, 1])

    def set_enabled(self, on: bool):
        for w in (self.raw_line, self.baud_box, self.eol_box, self.dtr_chk, self.rts_chk,
                  self.reset_btn):
            w.setEnabled(on)
