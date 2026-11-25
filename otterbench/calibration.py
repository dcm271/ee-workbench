"""The calibration log: when each instrument was calibrated, when it is due,
and checks of one instrument against a better one.

Kept in calibration.json in the config directory. An instrument is recognised by
its serial number when its *IDN? answer has one, else by its resource. Reports
say which instruments were in calibration on the day.

This is a record of what you enter: it calibrates nothing and proves nothing.
"""
from __future__ import annotations

import calendar
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import date

from .config import config_dir

SOON_DAYS = 30
OK, SOON, OVERDUE, NONE, NOT_NEEDED = "in calibration", "due soon", "overdue", "no record", \
    "not required"


def serial_of(idn: str) -> str:
    """The serial number in 'maker,model,serial,firmware' ("" if none)."""
    p = [x.strip() for x in idn.split(",")]
    if len(p) >= 3 and p[2] and not re.fullmatch(r"[0xX\-]*", p[2]):
        return p[2]
    m = re.search(r"\bserial\s+([A-Za-z0-9\-]{3,})", idn)
    return m.group(1) if m else ""


def key_of(idn: str, resource: str) -> str:
    """Follows a serial number from port to port; without one, the resource."""
    s = serial_of(idn)
    if s:
        p = [x.strip() for x in idn.split(",")]
        return f"{p[1] if len(p) >= 3 else ''} {s}".strip()
    return resource


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat(str(text).strip())
    except ValueError:
        return None


@dataclass
class Entry:
    key: str = ""
    name: str = ""                # what you call it
    idn: str = ""
    resource: str = ""            # where it was last seen
    calibrated: str = ""          # yyyy-mm-dd
    months: int = 12              # interval; 0 = calibration not required
    by: str = ""                  # laboratory / person
    certificate: str = ""
    notes: str = ""
    history: list = field(default_factory=list)      # earlier calibrations
    checks: list = field(default_factory=list)       # compared against a reference
    accuracy: dict = field(default_factory=dict)     # function or reading -> spec (accuracy.py)

    @classmethod
    def from_dict(cls, d: dict) -> "Entry":
        names = {f.name for f in fields(cls)}
        e = cls(**{k: v for k, v in d.items() if k in names})
        e.key, e.months = str(e.key), int(e.months)
        e.history = [x for x in e.history if isinstance(x, dict)]
        e.checks = [x for x in e.checks if isinstance(x, dict)]
        e.accuracy = {str(k): str(v) for k, v in e.accuracy.items()} \
            if isinstance(e.accuracy, dict) else {}
        return e

    def due(self) -> date | None:
        d = parse_date(self.calibrated)
        return add_months(d, self.months) if d and self.months > 0 else None

    def status(self, today: date | None = None) -> str:
        if self.months <= 0:
            return NOT_NEEDED
        due = self.due()
        if due is None:
            return NONE
        left = (due - (today or date.today())).days
        return OVERDUE if left < 0 else SOON if left <= SOON_DAYS else OK

    def describe(self, today: date | None = None) -> str:
        s = self.status(today)
        if s in (NONE, NOT_NEEDED):
            return {NONE: "no calibration record", NOT_NEEDED: "calibration not required"}[s]
        text = {OK: "in calibration", SOON: "in calibration, due soon",
                OVERDUE: "CALIBRATION OVERDUE"}[s]
        out = f"{text} (calibrated {self.calibrated}, due {self.due().isoformat()}"
        return out + (f", {self.certificate})" if self.certificate else ")")

    def record(self, day: str, by: str = "", certificate: str = "", notes: str = ""):
        """A new calibration; the one before it goes into the history."""
        if parse_date(day) is None:
            raise ValueError(f"“{day}” isn't a date (yyyy-mm-dd)")
        if self.calibrated:
            self.history.append({"calibrated": self.calibrated, "by": self.by,
                                 "certificate": self.certificate, "notes": self.notes})
        self.calibrated, self.by, self.certificate, self.notes = day, by, certificate, notes


class Log:
    def __init__(self, path=None):
        self._path = path
        self.entries: list[Entry] = []
        self.error = ""
        self.load()

    @property
    def path(self):
        return self._path or config_dir() / "calibration.json"

    def load(self):
        self.entries, self.error = [], ""
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            for x in d["instruments"]:
                try:
                    self.entries.append(Entry.from_dict(x))
                except (TypeError, ValueError, AttributeError) as e:
                    self.error = f"{self.path}: an entry is damaged ({e})"
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.error = f"{self.path} is damaged: {e}"

    def save(self):
        if self.error:
            # never write over records that couldn't be read
            bak = self.path.with_name(self.path.name + ".damaged")
            if self.path.exists() and not bak.exists():
                bak.write_bytes(self.path.read_bytes())
            self.error = ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"instruments": [asdict(e) for e in self.entries]}, indent=1,
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
        from .accuracy import forget
        forget()

    def find(self, idn: str, resource: str) -> Entry | None:
        k = key_of(idn, resource)
        hit = next((e for e in self.entries if e.key == k), None)
        if hit is None and not serial_of(idn):
            return None
        return hit

    def get(self, idn: str, resource: str, name: str = "") -> Entry:
        """Its entry, made if it has none."""
        e = self.find(idn, resource)
        if e is None:
            e = Entry(key=key_of(idn, resource), name=name)
            self.entries.append(e)
        e.idn, e.resource = idn, resource
        return e

    def describe(self, idn: str, resource: str) -> str:
        e = self.find(idn, resource)
        return e.describe() if e is not None else "no calibration record"


def simulated(resource: str) -> bool:
    return resource.upper().startswith("SIM")


def note_for(idn: str, resource: str) -> str:
    """For reports ("" when the log isn't used at all, or it is a simulator)."""
    if simulated(resource):
        return "simulated"
    log = Log()
    return log.describe(idn, resource) if log.entries else ""


# ---------------------------------------------------------------- checks
@dataclass
class CheckSettings:
    dut: str = ""                 # "tab: reading"
    ref: str = ""
    source: str = ""              # port text (roles.py), "" = set the levels by hand
    levels: str = "0, 1, 2, 5, 10"
    percent: float = 0.1          # ± % of the reference reading
    floor: float = 0.001          # ± this much (in the reading's unit)
    settle: float = 2.0
    average: float = 2.0
    current_limit: float = 0.1

    def problem(self) -> str:
        from .regulator import parse_list
        if not self.dut or not self.ref:
            return "Choose the instrument to check and the reference"
        if self.dut == self.ref:
            return "The instrument can't be its own reference"
        if self.percent < 0 or self.floor < 0 or self.percent == 0 and self.floor == 0:
            return "Give a tolerance"
        if self.source:
            try:
                if min(parse_list(self.levels)) < 0:
                    return "The levels can't be negative"
            except ValueError as e:
                return f"Levels: {e}"
        return ""


def judge(ref: float, dut: float, s: CheckSettings) -> dict:
    err = dut - ref
    lim = abs(ref) * s.percent / 100 + s.floor
    ok = math.isfinite(err) and abs(err) <= lim
    return {"reference": ref, "reading": dut, "error": err, "limit": lim,
            "percent": 100 * err / ref if ref else math.nan, "result": "PASS" if ok else "FAIL"}


@dataclass
class Check:
    s: CheckSettings
    rows: list = field(default_factory=list)

    def run(self, bench, on_row=None):
        """Each level from the source (or one point, at what is there now)."""
        from . import roles
        from .regulator import parse_list
        from .sequencer import SequenceError, Unstopped
        s = self.s
        if s.problem():
            raise SequenceError(s.problem())
        names = [t.partition(": ") for t in (s.dut, s.ref)]
        for n, _, _ in names:
            bench.inst(n)
        src = roles.find(bench, s.source, "source") if s.source else None
        try:
            levels = parse_list(s.levels) if src else [None]
            if src:
                src.off()
                src.amps(s.current_limit)
                src.volts(levels[0])
                src.on()
            for x in levels:
                if src:
                    src.volts(x)
                    bench.wait(s.settle)
                # reference, instrument, reference: a slow drift cancels
                a = bench.measure(names[1][0], names[1][2], s.average / 2)
                u = bench.uncertainty(names[1][0], names[1][2], a)
                if u is not None and 4 * u > abs(a) * s.percent / 100 + s.floor:
                    bench.log(f"the reference is only good to ±{u:.3g} here: less than four "
                              "times better than the tolerance")
                d = bench.measure(names[0][0], names[0][2], s.average)
                b = bench.measure(names[1][0], names[1][2], s.average / 2)
                row = {"level": x if x is not None else (a + b) / 2, **judge((a + b) / 2, d, s)}
                self.rows.append(row)
                if on_row:
                    on_row(row)
        finally:
            if src:
                with Unstopped(bench):
                    try:
                        src.off()
                    except Exception as e:      # noqa: BLE001
                        bench.log(f"source off failed: {e}")
        return self.rows


def verdict(rows: list[dict]) -> str:
    if not rows:
        return ""
    return "PASS" if all(r["result"] == "PASS" for r in rows) else "FAIL"
