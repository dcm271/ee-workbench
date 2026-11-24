"""Shared widgets, formatting helpers and the dark theme."""
from __future__ import annotations

import csv
import math
import time

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import (QApplication, QDoubleSpinBox, QFileDialog, QLabel, QMessageBox,
                             QVBoxLayout, QWidget)

C_VOLT = "#ffb347"
C_CURR = "#4fc3f7"
C_DVM = "#c39bff"
C_DMM = "#7ee787"
C_POW = "#9e9e9e"
C_ON = "#2e7d32"
C_WARN = "#ef5350"
C_GOOD = "#7ee787"
C_BAD = "#ef5350"
C_MUTED = "#8a93a0"
MONO = "DejaVu Sans Mono"


def fmt_current(a: float | None) -> str:
    if a is None or math.isnan(a):
        return "—"
    if abs(a) < 0.1:
        return f"{a * 1000:+.3f} mA"
    return f"{a:+.4f} A"


_PREFIXES = ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"))


def fmt_si(x: float | None, unit: str, digits: int = 4, signed: bool = False) -> str:
    """SI prefix with a fixed `digits - 1` decimals, trailing zeros kept, so a
    changing value doesn't grow and shrink (1.200 mA, 12.345 mA, 123.400 mA).
    `signed` always shows the sign, so the digits don't shift when it flips."""
    if x is None or math.isnan(x):
        return "—"
    dec = max(0, digits - 1)
    ax = abs(x)
    i = 3 if ax == 0 else next((k for k, (scale, _) in enumerate(_PREFIXES) if ax >= scale),
                               len(_PREFIXES) - 1)
    # 999.9996 m would round up to "1000.000 m": show it as 1.000 instead.
    if i > 0 and round(ax / _PREFIXES[i][0], dec) >= 1000:
        i -= 1
    scale, prefix = _PREFIXES[i]
    return f"{x / scale:{'+' if signed else ''}.{dec}f} {prefix}{unit}"


def _eng_exp(ax: float, unit: str) -> int:
    exp = 0 if ax == 0 else int(math.floor(math.log10(ax) / 3) * 3)
    if unit == "V":
        exp = min(max(exp, -3), 0)
    if unit in ("°C", "°F", "°", "%"):
        exp = 0                          # no m°C
    return max(min(exp, 9), -12 if unit == "F" else -9)


def fmt_fixed(x: float, unit: str, sig: int, full_scale: float | None = None) -> str:
    """Meter-style engineering notation with `sig` significant digits, keeping
    trailing zeros.  With a `full_scale` (a fixed meter range) the prefix and
    the decimals come from the range, as on the meter's own display, so they
    stay put as the reading changes; without one they follow the value."""
    if x is None or math.isnan(x):
        return "—"
    exp = _eng_exp(abs(full_scale if full_scale else x), unit)
    prefix = {9: "G", 6: "M", 3: "k", 0: "", -3: "m", -6: "µ", -9: "n", -12: "p"}[exp]
    m = x / 10 ** exp
    ref = abs(full_scale / 10 ** exp) if full_scale else abs(m)
    int_digits = 1 if ref < 1 else int(math.log10(ref) + 1e-9) + 1
    dec = max(0, sig - int_digits)
    return f"{m:+.{dec}f} {prefix}{unit}"


def range_full_scale(label: str | None) -> float | None:
    """'100 mV' → 0.1, '10 kΩ' → 1e4; None for AUTO or anything unparsed."""
    try:
        num, unit = (label or "").split()
        mult = {"m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9}.get(unit[0], 1) if len(unit) > 1 else 1
        return float(num) * mult
    except ValueError:
        return None


class SafeSpin(QDoubleSpinBox):
    """Spin box that ignores the mouse wheel unless focused, and highlights
    values that differ from what the instrument has (not yet applied)."""

    def __init__(self, decimals=3, step=0.01, suffix="", track=True):
        super().__init__()
        self.track = track  # False: a plain input, not mirroring an instrument value
        self.setDecimals(decimals)
        self.setSingleStep(step)
        self.setSuffix(suffix)
        self.setKeyboardTracking(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.setMinimumWidth(110)
        self.instrument_value: float | None = None
        self.valueChanged.connect(self._mark)

    def wheelEvent(self, e):
        if self.hasFocus():
            super().wheelEvent(e)
        else:
            e.ignore()

    def set_from_instrument(self, v: float | None):
        """None = instrument value unknown: leave the box, show it as unknown."""
        if v is not None:
            self.blockSignals(True)
            self.setValue(v)
            self.blockSignals(False)
        self.instrument_value = v
        self._mark()

    def _mark(self, *_):
        if not self.track:
            return
        if self.instrument_value is None:
            pending = True
            tip = "Instrument value unknown. Press Enter or Set to program it."
        else:
            pending = abs(self.value() - self.instrument_value) > 10 ** -self.decimals() / 2
            tip = "Not yet applied. Press Enter or Set." if pending else ""
        self.setStyleSheet("QDoubleSpinBox { background: #4a3b12; }" if pending else "")
        self.setToolTip(tip)


class Readout(QWidget):
    def __init__(self, label: str, color: str, size: int = 30):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.caption = QLabel(label.upper())
        self.caption.setStyleSheet(f"color:{C_MUTED}; font-size:10px; letter-spacing:1px;")
        self.value = QLabel("—")
        f = QFont(MONO, size)
        f.setBold(True)
        self.value.setFont(f)
        self.value.setStyleSheet(f"color:{color};")
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.caption)
        lay.addWidget(self.value)

    def set(self, text: str):
        self.value.setText(text)


class TraceBuffer:
    """Append-only numpy buffer of float rows; drops the oldest half when full."""

    def __init__(self, ncols: int, max_rows: int = 2_000_000):
        self.ncols, self.max_rows = ncols, max_rows
        self.clear()

    def clear(self):
        self.data = np.full((4096, self.ncols), np.nan)
        self.n = 0

    def append(self, row):
        if self.n == len(self.data):
            if len(self.data) >= self.max_rows:
                keep = self.n // 2
                self.data[:keep] = self.data[self.n - keep:self.n]
                self.n = keep
            else:
                self.data = np.concatenate([self.data, np.full_like(self.data, np.nan)])
        self.data[self.n] = [np.nan if v is None else v for v in row]
        self.n += 1

    def extend(self, rows: np.ndarray):
        """Append many rows at once (an (n, ncols) array)."""
        rows = np.asarray(rows, dtype=float)
        if rows.size == 0:
            return
        if len(rows) >= self.max_rows:
            rows = rows[-(self.max_rows // 2):]
        while self.n + len(rows) > len(self.data):
            if len(self.data) >= self.max_rows:
                keep = max(0, min(self.n, self.max_rows // 2) - len(rows))
                self.data[:keep] = self.data[self.n - keep:self.n]
                self.n = keep
            else:
                grow = min(len(self.data), self.max_rows - len(self.data))
                self.data = np.concatenate([self.data, np.full((grow, self.ncols), np.nan)])
        self.data[self.n:self.n + len(rows)] = rows
        self.n += len(rows)

    def view(self) -> np.ndarray:
        return self.data[: self.n]


def io_errors(title: str):
    """Decorator for a widget's save / export method (taking only self): a
    file that can't be written (permissions, full disk, a removed USB stick)
    becomes a warning instead of an internal error."""
    def deco(fn):
        def wrapper(self):
            try:
                return fn(self)
            except (OSError, UnicodeError) as e:
                where = f"\n{e.filename}" if getattr(e, "filename", None) else ""
                QMessageBox.warning(self, title, f"Couldn't write the file:{where}\n\n"
                                    f"{getattr(e, 'strerror', None) or e}")
        wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
        return wrapper
    return deco


def hint_label(text: str, width: int = 500, size: int = 11) -> QLabel:
    """A muted, word-wrapped explanation of a fixed width. Its height is set
    from the wrapped text: inside group boxes Qt doesn't pass height-for-width
    on, so a plain word-wrapped label gets squashed and its lines overlap."""
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet(f"color:{C_MUTED}; font-size:{size}px;")
    f = lab.font()
    f.setPixelSize(size)
    lab.setFont(f)
    lab.setFixedWidth(width)
    lab.setMinimumHeight(lab.heightForWidth(width) + 2)
    return lab


class CsvRecorder:
    def __init__(self):
        self.fh = None
        self.w = None
        self.path = ""
        self.error = ""          # why the last row() failed (disk full, unplugged USB stick)

    @property
    def active(self) -> bool:
        return self.w is not None

    def start(self, parent, default_name: str, header: list[str]) -> bool:
        path, _ = QFileDialog.getSaveFileName(
            parent, "Record to CSV", time.strftime(default_name), "CSV (*.csv)")
        if not path:
            return False
        try:
            fh = open(path, "w", newline="")
            w = csv.writer(fh)
            w.writerow(header)
        except OSError as e:
            QMessageBox.warning(parent, "Record to CSV", f"Can't write {path}:\n{e}")
            return False
        self.fh, self.w, self.path, self.error = fh, w, path, ""
        return True

    def row(self, values) -> bool:
        """Write one row. False (with `error` set) if the file can't be written;
        recording then stops instead of failing again on every reading."""
        if not self.w:
            return False
        try:
            self.w.writerow(["" if v is None else v for v in values])
            self.fh.flush()
            return True
        except (OSError, ValueError) as e:
            self.error = str(e)
            self.stop()
            return False

    def rows(self, rows) -> bool:
        """Many rows at once (the PPK2's binned samples); like row()."""
        if not self.w:
            return False
        try:
            self.w.writerows(rows)
            self.fh.flush()
            return True
        except (OSError, ValueError) as e:
            self.error = str(e)
            self.stop()
            return False

    def stop(self):
        if self.fh:
            try:
                self.fh.close()
            except OSError:
                pass
        self.fh = self.w = None


def dark_palette(app: QApplication):
    """The app's look (kept under this name for the scripts that call it)."""
    from . import theme
    theme.apply(app)

class SteadyY:
    """Y autoscale for a scrolling strip chart that doesn't twitch.

    pyqtgraph's autorange refits on every new point, so the axis jumps each
    time a peak enters or scrolls out of the window.  This grows the range at
    once (with some headroom) when data would go off-scale, but only shrinks
    once the data has used under half of it, and then eases in over about a
    second.  Zooming Y by hand turns it off; the plot's "A" button (or
    "View all") turns it back on."""

    HEADROOM = 0.10     # extra room added when growing, as a fraction of the data range
    PAD = 0.05          # margin kept around the data
    SHRINK_AT = 0.5     # start shrinking when the data fills less than this ...
    SHRINK_TO = 0.85    # ... and keep easing in until it fills this much
    EASE = 0.15         # fraction of the remaining shrink done per update

    def __init__(self, plot):
        self.vb = plot.getViewBox()
        self.auto = True
        self.range: list[float] | None = None
        self._shrinking = False
        self.vb.disableAutoRange(axis=self.vb.YAxis)
        self.vb.sigRangeChangedManually.connect(self._manual)

    def _manual(self, mask):
        if mask[1]:
            self.auto = False

    def reset(self):
        """Forget the current range (after Clear or a log/linear switch)."""
        self.range = None
        self._shrinking = False

    def update(self):
        if self.vb.autoRangeEnabled()[1]:      # "A" button / View all: take over again
            self.vb.disableAutoRange(axis=self.vb.YAxis)
            self.auto = True
            self.reset()
        if not self.auto or not self.vb.isVisible():
            return
        xr = self.vb.viewRange()[0]
        b = self.vb.childrenBounds(orthoRange=(None, xr))[1]
        if b is None or b[0] is None or not (math.isfinite(b[0]) and math.isfinite(b[1])):
            return
        lo, hi = b
        size = (hi - lo) or max(abs(hi), 1e-12) * 1e-3
        want = [lo - size * self.PAD, hi + size * self.PAD]
        r = self.range
        if r is None:
            r = want
        elif want[0] < r[0] or want[1] > r[1]:
            # Data off-scale: grow straight away, only on the side that needs it.
            r = [min(r[0], want[0] - size * self.HEADROOM), max(r[1], want[1] + size * self.HEADROOM)]
            self._shrinking = False
        else:
            fill = (want[1] - want[0]) / (r[1] - r[0])
            if fill < self.SHRINK_AT:
                self._shrinking = True
            elif fill > self.SHRINK_TO:
                self._shrinking = False
            if self._shrinking:
                # Moving each end toward `want` never cuts off data inside it.
                r = [r[0] + (want[0] - r[0]) * self.EASE, r[1] + (want[1] - r[1]) * self.EASE]
        self.range = r
        self.vb.setYRange(r[0], r[1], padding=0)
