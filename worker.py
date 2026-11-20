"""Per-instrument I/O thread. The GUI never touches a transport directly."""
from __future__ import annotations

import copy
import queue
import time
import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from .instruments import make_driver
from .transport import open_transport

# One time base for every instrument so their traces line up.
APP_T0 = time.monotonic()


def now() -> float:
    return time.monotonic() - APP_T0


class InstrumentWorker(QThread):
    connected = pyqtSignal(object, object, object)   # driver family, caps, state
    connect_failed = pyqtSignal(str)
    reading = pyqtSignal(object)
    state = pyqtSignal(object)                       # Setpoints / DmmConfig
    waveform = pyqtSignal(str, float, object)        # what, dt, list[float]
    reply = pyqtSignal(str, str)                     # cmd, response
    log = pyqtSignal(str)
    error = pyqtSignal(str)
    poll_paused = pyqtSignal()

    def __init__(self, resource: str, model: str, baud: int = 9600):
        super().__init__()
        self.resource, self.model, self.baud = resource, model, baud
        self._q: queue.Queue = queue.Queue()
        self._running = True
        self.drv = None
        self.poll_cfg = None
        self.poll_enabled = True
        self.interval_s = 0.0
        self._trace = False
        self._last_output = None

    # ---- called from GUI thread ---------------------------------------
    def submit(self, fn, *args, refresh=False):
        """Queue `fn(drv, *args)` to run on the I/O thread."""
        self._q.put((fn, args, refresh))

    def set_trace(self, on: bool):
        self._trace = on
        if self.drv:
            self.drv.log = self.log.emit if on else None

    def stop(self):
        self._running = False
        self._q.put(None)
        self.wait(10000)

    # ---- thread body ----------------------------------------------------
    def run(self):
        if not self._connect():
            return
        next_poll = 0.0
        while self._running:
            timeout = max(0.0, next_poll - time.monotonic()) if self.poll_enabled else 0.5
            try:
                item = self._q.get(timeout=timeout)
            except queue.Empty:
                item = None
            if item is not None:
                self._handle(*item)
                continue
            if not self._running:
                break
            if self.poll_enabled:
                self._do_poll()
                next_poll = time.monotonic() + self.interval_s
        try:
            self.drv.t.close()
        except Exception:
            pass

    def _connect(self) -> bool:
        try:
            self.log.emit(f"Opening {self.resource} ...")
            t = open_transport(self.resource, self.baud)
            drv = make_driver(self.model, t, self.resource)
            caps = drv.identify()
            self.drv = drv
            drv.log = self.log.emit if self._trace else None
            st = self._read_state()
            self.log.emit(f"Connected: {caps.idn}")
            self.connected.emit(drv.family, caps, st)
            return True
        except Exception as e:
            self.log.emit(traceback.format_exc().rstrip())
            self.connect_failed.emit(str(e))
            return False

    def _read_state(self):
        if self.drv.kind == "supply":
            sp = self.drv.read_setpoints()
            self._last_output = sp.output
            return sp
        return self.drv.cfg

    def _handle(self, fn, args, refresh):
        try:
            fn(self.drv, *args)
            for e in self.drv.errors():
                self.error.emit(f"Instrument error: {e}")
            if refresh:
                self.state.emit(self._read_state())
        except Exception as e:
            self._fail(e)

    def _fail(self, e: Exception):
        self.error.emit(f"{type(e).__name__}: {e}")
        self.log.emit(traceback.format_exc().rstrip())

    def _do_poll(self):
        try:
            # Snapshot: the GUI thread may change the settings mid-poll.
            r = self.drv.poll(now(), copy.copy(self.poll_cfg))
            for e in getattr(r, "errors", []):
                self.error.emit(f"Instrument error: {e}")
            out = getattr(r, "output", None)
            if self.drv.kind == "supply" and out is not None and out != self._last_output:
                # changed by protection trip or the front panel
                self.state.emit(self._read_state())
            self.reading.emit(r)
        except Exception as e:
            self._fail(e)
            self.poll_enabled = False
            self.poll_paused.emit()

    # ---- higher-level helpers -------------------------------------------
    def request_capture(self, what: str, points: int, tint: float):
        def job(drv):
            data = drv.capture(what, points, tint)
            self.waveform.emit(what, tint, data)
        self.submit(job)

    def request_raw(self, cmd: str, read: bool):
        def job(drv):
            if read:
                drv.t.write(cmd)
                self.reply.emit(cmd, drv.t.read())
            else:
                drv.t.write(cmd)
                self.reply.emit(cmd, "(sent)")
        # Commands may change settings; plain queries don't need a refresh.
        self.submit(job, refresh=not read)
