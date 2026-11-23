"""Runs a measurement (Bode sweep, regulator or battery test, meter check) on a
thread of its own, through the sequencer's Bench: instruments are driven on
their own I/O threads, and Stop / Esc / a watchdog's "stop sequence" end it."""
from __future__ import annotations

import sys
import threading
import traceback

from PyQt6.QtCore import QObject, pyqtSignal

from ..clock import now
from ..sequencer import Bench, SequenceError, Stopped
from .panel_base import open_panels

RUNNERS: list = []              # the ones running now


def stop_all():
    for r in list(RUNNERS):
        r.stop()


def busy() -> str:
    """What is using the bench now ("" = nothing)."""
    return RUNNERS[0].title if RUNNERS else ""


class Runner(QObject):
    """start(fn): fn(bench) on a thread. Anything fn emits through `event`
    arrives on the GUI thread; `done` carries "Finished", "Stopped" or why not."""
    log = pyqtSignal(str)
    event = pyqtSignal(object)
    done = pyqtSignal(str)

    def __init__(self, title: str):
        super().__init__()
        from .sequencer_window import Invoker
        self.title = title
        self.invoke = Invoker()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def insts(self) -> list:
        from .sequencer_window import PanelInst, kind_of
        return [PanelInst(p, self.invoke, self.stop_event) for p in open_panels() if kind_of(p)]

    def start(self, fn) -> str:
        """Returns why it can't start, or ""."""
        if self.running():
            return "It is running already"
        if busy():
            return f"{busy()} is running: stop it first"
        self.stop_event = threading.Event()
        stop = self.stop_event
        bench = Bench(self.insts(), now, log=self.log.emit, stop=stop)
        self.bench = bench

        def body():
            outcome = "Finished"
            try:
                fn(bench)
            except Stopped:
                outcome = "Stopped"
            except SequenceError as e:
                outcome = f"Failed: {e}"
            except Exception as e:      # noqa: BLE001 - say so, don't die silently
                tb = traceback.format_exception(*sys.exc_info())
                self.log.emit("".join(tb[-4:]).rstrip())
                outcome = f"Failed: {type(e).__name__}: {e}"
            finally:
                if self in RUNNERS:
                    RUNNERS.remove(self)
            try:
                self.done.emit(outcome)
            except RuntimeError:        # the window went while quitting
                pass
        RUNNERS.append(self)
        self.thread = threading.Thread(target=body, daemon=True, name=self.title)
        self.thread.start()
        return ""

    def stop(self):
        self.stop_event.set()

