"""Voltage (or current) profiles: make a supply or a PPK2 act as a slow
function generator by stepping its setpoint in software.

A profile is written as one line, used the same way in the Profile window,
in a sequencer step and from scripts:

    sine 3.0..3.6 period=2 for=20            a sine between 3.0 and 3.6, 2 s period, for 20 s
    ramp 0..5 for=10                         0 to 5 over 10 s (then stays at 5)
    triangle 1.8..3.6 period=4 cycles=3
    square 3.0..3.3 period=1 duty=25 for=10
    steps 1.8..3.6 n=7 dwell=2               7 equal steps, 2 s each
    logsteps 10..100000 n=41 dwell=0.5       log-spaced steps (frequency sweeps)
    list 3.0 3.3 3.6 3.3 dwell=1             the values given, 1 s each
    csv /path/profile.csv                    rows of "time_s, value"
    ... rate=10                              setpoint updates per second (default 10)

Points are sent at `rate` per second (default 10; GPIB supplies manage
~20-50 commands/s, so the shape is approximate above a few Hz). Each point is
sent at its scheduled time, so a slow command delays only that point.
"""
from __future__ import annotations

import csv
import math
import re
import time
from dataclasses import dataclass

import numpy as np

SHAPES = ("sine", "ramp", "triangle", "square", "steps", "logsteps", "list", "csv")
MAX_POINTS = 1_000_000


class ProfileError(ValueError):
    pass


@dataclass
class Profile:
    shape: str
    lo: float = 0.0
    hi: float = 0.0
    period: float = 1.0
    duration: float = 0.0
    rate: float = 10.0
    duty: float = 50.0
    n: int = 2
    dwell: float = 1.0
    values: tuple = ()
    path: str = ""
    text: str = ""

    def points(self) -> tuple[np.ndarray, np.ndarray]:
        """(times from the start, setpoints)."""
        s = self.shape
        if s == "list":
            if not self.values:
                raise ProfileError("list needs values, e.g. 'list 3.0 3.3 3.6 dwell=1'")
            t = np.arange(len(self.values)) * self.dwell
            return t, np.array(self.values, float)
        if s in ("steps", "logsteps"):
            if self.n < 2:
                raise ProfileError(f"{s} needs n=2 or more")
            t = np.arange(self.n) * self.dwell
            if s == "logsteps":
                if self.lo <= 0 or self.hi <= 0:
                    raise ProfileError("logsteps needs both ends above 0")
                return t, np.geomspace(self.lo, self.hi, self.n)
            return t, np.linspace(self.lo, self.hi, self.n)
        if s == "csv":
            return read_csv(self.path)
        if self.rate <= 0:
            raise ProfileError("rate must be above 0")
        dur = self.duration
        if dur <= 0:
            raise ProfileError(f"{s} needs a length: for=<seconds> or cycles=<n>")
        n = int(math.floor(dur * self.rate + 1e-9)) + 1
        if n > MAX_POINTS:
            raise ProfileError(f"That is {n} points; lower the rate or the length")
        t = np.arange(n) / self.rate
        lo, hi = self.lo, self.hi
        if s == "ramp":
            return t, lo + (hi - lo) * np.clip(t / dur, 0, 1)
        if self.period <= 0:
            raise ProfileError("period must be above 0")
        ph = np.mod(t / self.period, 1.0)
        if s == "sine":
            v = lo + (hi - lo) * (0.5 - 0.5 * np.cos(2 * np.pi * ph))   # starts at lo
        elif s == "triangle":
            v = lo + (hi - lo) * (1 - np.abs(2 * ph - 1))
        elif s == "square":
            v = np.where(ph < self.duty / 100.0, hi, lo)
        else:
            raise ProfileError(f"Unknown shape '{s}'")
        return t, v

    @property
    def length(self) -> float:
        t, _ = self.points()
        return float(t[-1] + (self.dwell if self.shape in ("list", "steps", "logsteps") else 0.0))


def read_csv(path: str) -> tuple[np.ndarray, np.ndarray]:
    t, v = [], []
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            for row in csv.reader(fh):
                if len(row) < 2 or row[0].strip().startswith("#"):
                    continue
                try:
                    t.append(float(row[0]))
                    v.append(float(row[1]))
                except ValueError:
                    continue          # a header line
    except OSError as e:
        raise ProfileError(f"Can't read {path}: {e}") from None
    if not t:
        raise ProfileError(f"{path} has no 'time_s, value' rows")
    t, v = np.array(t), np.array(v)
    if np.any(np.diff(t) < 0):
        raise ProfileError(f"{path}: the times must not go backwards")
    return t - t[0], v


def parse(text: str) -> Profile:
    """'sine 3.0..3.6 period=2 for=20 rate=10' -> Profile."""
    words = text.split()
    if not words:
        raise ProfileError("Empty profile")
    shape = words[0].lower()
    if shape not in SHAPES:
        raise ProfileError(f"Unknown shape '{words[0]}'; use one of: " + ", ".join(SHAPES))
    p = Profile(shape, text=text.strip())
    if shape == "csv":
        if len(words) < 2:
            raise ProfileError("csv needs a file: 'csv /path/to/profile.csv'")
        p.path = " ".join(w for w in words[1:] if "=" not in w)
        words = [w for w in words[1:] if "=" in w]
    else:
        words = words[1:]
    cycles = None
    for w in words:
        m = re.fullmatch(r"(-?[\d.eE+-]+)\.\.(-?[\d.eE+-]+)", w)
        if m:
            p.lo, p.hi = _num(m.group(1)), _num(m.group(2))
            continue
        if "=" in w:
            k, v = w.split("=", 1)
            k = k.lower()
            if k in ("for", "duration"):
                p.duration = _num(v)
            elif k == "period":
                p.period = _num(v)
            elif k == "rate":
                p.rate = _num(v)
            elif k == "duty":
                p.duty = _num(v)
            elif k in ("n", "steps"):
                p.n = int(_num(v))
            elif k == "dwell":
                p.dwell = _num(v)
            elif k == "cycles":
                cycles = _num(v)
            else:
                raise ProfileError(f"Unknown setting '{k}'")
            continue
        if shape == "list":
            p.values += (_num(w),)
            continue
        raise ProfileError(f"Don't understand '{w}' (ranges are written lo..hi)")
    if cycles is not None:
        p.duration = cycles * p.period
    if shape in ("sine", "triangle", "square", "ramp", "steps", "logsteps") and p.lo == p.hi == 0.0:
        raise ProfileError("Give the range as lo..hi, e.g. 3.0..3.6")
    return p


def _num(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        raise ProfileError(f"'{s}' isn't a number") from None


class ProfileStopped(Exception):
    pass


def run(p: Profile, setter, stop, clock=time.monotonic, on_point=None) -> int:
    """Send each point at its time: setter(value) (it may block while the
    instrument works). Late points are skipped so the profile keeps to time;
    the last point is always sent. `stop` is a threading.Event. For list /
    steps / csv the run lasts until the last point's dwell is over."""
    t, v = p.points()
    end = p.length
    t_start = clock()
    sent = 0
    k = 0
    n = len(t)
    while k < n:
        due = t_start + t[k]
        wait = due - clock()
        if wait > 0 and stop.wait(wait):
            raise ProfileStopped()
        if stop.is_set():
            raise ProfileStopped()
        el = clock() - t_start
        # the newest point that is due (skip any we are too late for)
        j = int(np.searchsorted(t, el, side="right")) - 1
        k = max(k, min(j, n - 1))
        setter(float(v[k]))
        sent += 1
        if on_point:
            on_point(k, float(t[k]), float(v[k]))
        k += 1
    rest = t_start + end - clock()
    if rest > 0 and stop.wait(rest):
        raise ProfileStopped()
    return sent
