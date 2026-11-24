"""Keeping the app alive when something goes wrong, and leaving a record.

PyQt6 aborts the whole process when a Python exception escapes a slot (a
button handler, a timer, a reading arriving), which would leave supplies on
and the bench unattended over one bad CSV path or one odd reading. `install()`
replaces that with: write the traceback to the crash log, tell the user once
(not once per poll), and carry on.

    ~/.config/otterbench/crash.log      Python tracebacks, newest last
                                          (kept under ~1 MB: the older half
                                          is moved to crash.log.1)
    ~/.config/otterbench/fault.log      a native crash (segfault in Qt or
                                          a driver library): every thread's
                                          stack, written by faulthandler

SIGINT (Ctrl+C in the terminal) and SIGTERM (logout, systemd, kill) close the
main window properly, so the session file, last-session.json and bench.json
are saved and every instrument thread is stopped.
"""
from __future__ import annotations

import faulthandler
import os
import signal
import sys
import threading
import time
import traceback

from .config import config_dir

MAX_LOG = 1_000_000          # bytes before crash.log is rotated
_lock = threading.Lock()
_notify = None               # GUI callback(summary, details), set by the main window
_shown: dict[str, tuple[float, int]] = {}
_fault_fh = None
_count = 0                   # exceptions caught since start-up


def log_path():
    return config_dir() / "crash.log"


def fault_path():
    return config_dir() / "fault.log"


def errors_caught() -> int:
    return _count


def write_log(text: str):
    """Append a block to crash.log (and stderr). Never raises."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    block = f"---- {stamp}  pid {os.getpid()}\n{text.rstrip()}\n"
    try:
        sys.stderr.write(block)
    except Exception:       # noqa: BLE001 - no terminal (desktop launcher)
        pass
    with _lock:
        try:
            p = log_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > MAX_LOG:
                os.replace(p, p.with_name(p.name + ".1"))
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(block)
        except OSError:
            pass


def report(exc_type, exc, tb, where: str = ""):
    """Log an exception and, if the GUI is up, tell the user (rate-limited)."""
    global _count
    if exc_type is None or issubclass(exc_type, KeyboardInterrupt):
        return
    _count += 1
    summary = f"{exc_type.__name__}: {exc}".strip()
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    # The same error from a timer or on every reading must not fill the log
    # or pile up dialogs: one of each per distinct error per minute.
    k = f"{where}|{text[-600:]}"
    now = time.monotonic()
    last, repeats = _shown.get(k, (-1e9, 0))
    if now - last < 60:
        _shown[k] = (last, repeats + 1)
        return
    _shown[k] = (now, 0)
    if repeats:
        text += f"(the same error happened {repeats} more times in the minute before)\n"
    write_log((f"[{where}]\n" if where else "") + text)
    cb = _notify
    if cb is None:
        return
    try:
        cb(summary, text)
    except Exception:       # noqa: BLE001 - never let the reporter itself crash
        pass


def _excepthook(exc_type, exc, tb):
    report(exc_type, exc, tb)


def _thread_hook(args):
    if args.exc_type is SystemExit:
        return
    name = getattr(args.thread, "name", "?")
    # Not on the GUI thread: only log. Threads that matter (sequence, profile,
    # instrument I/O) report their own errors to the window.
    global _count
    _count += 1
    write_log(f"[thread {name}]\n" + "".join(
        traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))


def install():
    """Python-level hooks. Safe to call more than once."""
    global _fault_fh
    sys.excepthook = _excepthook
    threading.excepthook = _thread_hook
    if _fault_fh is None:
        try:
            p = fault_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > MAX_LOG:
                os.replace(p, p.with_name(p.name + ".1"))
            _fault_fh = open(p, "a", buffering=1)
            _fault_fh.write(f"---- {time.strftime('%Y-%m-%d %H:%M:%S')}  pid {os.getpid()} "
                            "started\n")
            faulthandler.enable(_fault_fh, all_threads=True)
        except OSError:
            faulthandler.enable(all_threads=True)


def set_notifier(fn):
    """fn(summary, details) on the GUI thread; None to stop notifying."""
    global _notify
    _notify = fn


def install_signal_handlers(app, close):
    """SIGINT / SIGTERM → close() on the GUI thread (saves state, stops the
    instrument threads). Qt's event loop doesn't run Python code between
    events, so a cheap timer lets the interpreter see the signal."""
    from PyQt6.QtCore import QTimer

    def handler(signum, _frame):
        write_log(f"Signal {signal.Signals(signum).name}: closing")
        QTimer.singleShot(0, close)

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, handler)
        except (ValueError, OSError):     # not the main thread / not supported
            pass
    t = QTimer(app)
    t.timeout.connect(lambda: None)
    t.start(250)
    app._otterbench_signal_timer = t           # keep it alive
