"""Unattended-run safety: what to do on quit, after a crash, and while running.

Settings (Tools ▾ → Safety…, kept in bench.json under "safety"):

    off_on_quit        switch every output off when the app is closed
    off_after_crash    after a crash, switch off the outputs of every
                       instrument that was open (they are reopened if needed)
    heartbeat_tab      a GPIO tab, and
    heartbeat_pin      an output pin on it, toggled every
    heartbeat_period   seconds while the app runs: an external watchdog
                       (a relay timer, a microcontroller) can cut the DUT's
                       power if the toggling stops
    auto_reconnect     reopen an instrument that stopped answering (USB
                       unplugged and replugged, instrument power-cycled),
                       retrying with a growing delay
    resume_sequence    after a crash, carry on a steps-mode sequence from
                       the pass it had reached

A crash is noticed with a marker file, running.json, written at start-up and
removed on a normal quit. Finding it at start-up (and its process gone) means
the last run ended without quitting: a crash, a power cut, a kill -9.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, fields

from .config import config_dir


@dataclass
class Safety:
    off_on_quit: bool = False
    off_after_crash: bool = False
    heartbeat_tab: str = ""
    heartbeat_pin: str = ""
    heartbeat_period: float = 1.0
    auto_reconnect: bool = False
    resume_sequence: bool = False

    @classmethod
    def from_dict(cls, d) -> "Safety":
        s = cls()
        if not isinstance(d, dict):
            return s
        for f in fields(cls):
            if f.name not in d:
                continue
            v = d[f.name]
            try:
                if f.type in ("bool", bool):
                    v = bool(v)
                elif f.type in ("float", float):
                    v = max(0.05, float(v))
                else:
                    v = str(v)
            except (TypeError, ValueError):
                continue
            setattr(s, f.name, v)
        return s

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def heartbeat(self) -> bool:
        return bool(self.heartbeat_tab.strip() and self.heartbeat_pin.strip())


def marker_path():
    return config_dir() / "running.json"


def _alive(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def previous_run() -> dict | None:
    """The marker of a run that ended without quitting, else None. A marker
    whose process is still alive (a second copy running) isn't a crash."""
    p = marker_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"pid": 0, "started": "", "open": [], "damaged": True}
    if not isinstance(d, dict):
        return {"pid": 0, "started": "", "open": [], "damaged": True}
    try:
        pid = int(d.get("pid", 0))
    except (TypeError, ValueError):
        pid = 0
    if pid == os.getpid() or _alive(pid):
        return None
    d["open"] = [x for x in d.get("open", []) if isinstance(x, dict) and x.get("resource")]
    return d


def other_instance_running() -> bool:
    try:
        d = json.loads(marker_path().read_text(encoding="utf-8"))
        return _alive(int(d.get("pid", 0)))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def write_marker(open_tabs: list[dict], sequence: bool = False):
    """open_tabs: [{"resource", "model", "settings"}] (what to switch off
    after a crash). Never raises."""
    p = marker_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "started": _STARTED,
                                   "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "open": open_tabs, "sequence": bool(sequence)},
                                  default=str), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def clear_marker():
    """A normal quit."""
    try:
        d = json.loads(marker_path().read_text(encoding="utf-8"))
        pid = int(d.get("pid", 0))
        if pid != os.getpid() and _alive(pid):
            return                    # another copy that is still running
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        marker_path().unlink()
    except OSError:
        pass


_STARTED = time.strftime("%Y-%m-%dT%H:%M:%S")


class Backoff:
    """Reconnect delays: 3, 6, 12, 24, 48, then every 60 s."""

    def __init__(self, first: float = 3.0, cap: float = 60.0):
        self.first, self.cap = first, cap
        self.tries: dict = {}

    def next_delay(self, key) -> float:
        n = self.tries.get(key, 0)
        self.tries[key] = n + 1
        return min(self.cap, self.first * (2 ** n))

    def reset(self, key):
        self.tries.pop(key, None)


def device_path(resource: str) -> str:
    """The /dev path behind a resource, if any (to wait for a replug)."""
    import re
    m = re.search(r"(/dev/[^\s:]+)", resource)
    return m.group(1) if m else ""
