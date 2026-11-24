"""Bench-wide services every instrument tab feeds: the safety watchdog and the
live math channels. PanelBase hands each reading to `hub.feed`; the main
window carries out the watchdog's actions (it owns the tabs)."""
from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from ..mathchannels import LiveMath
from ..signals import signals_of
from ..watchdog import Watchdog


class Hub(QObject):
    tripped = pyqtSignal(object, str)      # Rule, instrument name

    def __init__(self):
        super().__init__()
        self.watchdog = Watchdog()
        self.math = LiveMath()

    def feed(self, panel, r):
        want_wd = self.watchdog.armed and any(x.enabled for x in self.watchdog.rules)
        want_math = bool(self.math.channels)
        if not (want_wd or want_math):
            return
        sig = signals_of(panel.KIND, r)
        if not sig:
            return
        name = panel.display_name()
        if want_wd:
            for rule in self.watchdog.feed(name, sig):
                self.tripped.emit(rule, name)
        if want_math:
            try:
                self.math.feed(name, sig)
            except Exception:       # noqa: BLE001 - math is a convenience; never let it
                from .. import crash   # get in the way of the readings or the watchdog
                import sys
                crash.report(*sys.exc_info(), where="math channels")


hub = Hub()
