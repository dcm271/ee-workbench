"""Programs run by a sequence step: a flasher, a build, a test script."""
from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

MAX_OUTPUT = 1_000_000           # characters kept
LOG_LINES = 400                  # lines copied to the log


@dataclass
class Result:
    code: int | None = None
    output: str = ""
    problem: str = ""            # why it failed ("" = exit status 0)
    stopped: bool = False


def split(line: str, extra: dict) -> list[str]:
    """Words as a shell would split them. {serial} and the like are filled in
    afterwards, so a scanned text is always one word and never a command."""
    try:
        words = shlex.split(line)
    except ValueError as e:
        raise ValueError(f"{line!r}: {e}") from None
    if not words:
        raise ValueError("give a program to run")
    out = []
    for w in words:
        w = re.sub(r"\{(\w+)\}", lambda m: str(extra[m.group(1)]) if m.group(1) in extra
                   else m.group(0), w)
        out.append(os.path.expanduser(w) if w.startswith("~") else w)
    return out


def shown(args: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)


def _kill(p: subprocess.Popen):
    """The program and whatever it started."""
    for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.wait(wait)
        except subprocess.TimeoutExpired:
            continue


def run(args: list[str], timeout: float, stop: threading.Event, log) -> Result:
    res = Result()
    try:
        p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        res.problem = f"can't start {args[0]}: {e.strerror or e}"
        return res
    parts: list[str] = []
    size = [0]

    def reader():
        n = 0
        for raw in iter(p.stdout.readline, b""):
            text = raw.decode("utf-8", "replace")
            if size[0] < MAX_OUTPUT:
                parts.append(text)
                size[0] += len(text)
            n += 1
            if n <= LOG_LINES:
                # a progress bar redraws one line: its last state is enough
                log("  " + text.rstrip().rsplit("\r", 1)[-1])
            elif n == LOG_LINES + 1:
                log("  … (more output)")
    th = threading.Thread(target=reader, daemon=True, name="command output")
    th.start()
    end = time.monotonic() + timeout
    while True:
        try:
            res.code = p.wait(0.05)
            break
        except subprocess.TimeoutExpired:
            pass
        if stop.is_set():
            res.stopped = True
            _kill(p)
            break
        if time.monotonic() > end:
            res.problem = f"not finished after {timeout:g} s"
            _kill(p)
            break
    th.join(2.0)                 # a child left behind may hold the pipe open
    res.output = "".join(parts)
    if not res.stopped and not res.problem and res.code:
        res.problem = f"exit status {res.code}" if res.code > 0 else f"killed by signal {-res.code}"
    return res


def find(regex: str, text: str):
    """(found, value): the number in the first group, else its text."""
    try:
        m = re.search(regex, text)
    except re.error as e:
        from .sequencer import SequenceError
        raise SequenceError(f"Command: bad pattern '{regex}': {e}") from None
    if m is None:
        return False, None
    if not m.groups():
        return True, None
    g = next((x for x in m.groups() if x is not None), None)
    try:
        return True, float(g)
    except (TypeError, ValueError):
        return True, g
