"""Production-test results: one row per unit tested, in an SQLite file
(~/.config/otterbench/results.db by default), with history and yield.

    units(id, serial, product, operator, station, started, finished, seconds,
          result, failures, outcome, data, report)

result is PASS, FAIL or ERROR (the sequence stopped or failed before it
could judge the unit). data holds the sequence's result rows as JSON.

    sqlite3 results.db "select serial, result, finished from units order by id desc"
"""
from __future__ import annotations

import csv
import json
import socket
import sqlite3
import time
from dataclasses import dataclass

from .config import config_dir

RESULTS = ("PASS", "FAIL", "ERROR")


def default_path():
    return config_dir() / "results.db"


@dataclass
class Unit:
    id: int
    serial: str
    product: str
    operator: str
    station: str
    started: str
    finished: str
    seconds: float
    result: str
    failures: str
    outcome: str
    report: str


@dataclass
class Yield:
    tested: int = 0            # test runs
    units: int = 0             # different serial numbers
    passed: int = 0            # runs that passed
    failed: int = 0
    errors: int = 0
    first_pass: int = 0        # units whose first run passed
    final_pass: int = 0        # units whose latest run passed

    @property
    def first_pass_yield(self) -> float | None:
        return self.first_pass / self.units if self.units else None

    @property
    def final_yield(self) -> float | None:
        return self.final_pass / self.units if self.units else None


def verdict(outcome: str, passed: int, failed: int) -> str:
    """The unit's result from how the sequence ended and its limit checks."""
    if failed:
        return "FAIL"
    if outcome != "Finished" and not outcome.startswith("Stopped at the first fail"):
        return "ERROR"
    return "PASS"


class ResultsDB:
    def __init__(self, path=None):
        self.path = str(path or default_path())
        from pathlib import Path
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS units(
                id INTEGER PRIMARY KEY, serial TEXT, product TEXT, operator TEXT,
                station TEXT, started TEXT, finished TEXT, seconds REAL, result TEXT,
                failures TEXT, outcome TEXT, data TEXT, report TEXT);
            CREATE INDEX IF NOT EXISTS units_serial ON units(product, serial);
        """)
        self.db.commit()

    def close(self):
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    def add(self, serial: str, product: str, operator: str, started: float, result: str,
            failures: str = "", outcome: str = "", rows: list | None = None,
            report: str = "", finished: float | None = None) -> int:
        if result not in RESULTS:
            raise ValueError(f"result must be one of {', '.join(RESULTS)}")
        finished = time.time() if finished is None else finished
        fmt = "%Y-%m-%dT%H:%M:%S"
        cur = self.db.execute(
            "INSERT INTO units(serial, product, operator, station, started, finished, seconds,"
            " result, failures, outcome, data, report) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (serial, product, operator, socket.gethostname(),
             time.strftime(fmt, time.localtime(started)),
             time.strftime(fmt, time.localtime(finished)), round(finished - started, 3),
             result, failures, outcome, json.dumps(rows or [], default=str), report))
        self.db.commit()
        return int(cur.lastrowid)

    def set_report(self, unit_id: int, report: str):
        self.db.execute("UPDATE units SET report = ? WHERE id = ?", (report, unit_id))
        self.db.commit()

    def _where(self, product: str | None, since: str | None):
        cond, args = [], []
        if product:
            cond.append("product = ?")
            args.append(product)
        if since:
            cond.append("finished >= ?")
            args.append(since)
        return (" WHERE " + " AND ".join(cond)) if cond else "", args

    def history(self, product: str | None = None, limit: int = 200,
                serial: str | None = None) -> list[Unit]:
        where, args = self._where(product, None)
        if serial:
            where += (" AND" if where else " WHERE") + " serial = ?"
            args.append(serial)
        rows = self.db.execute(
            "SELECT id, serial, product, operator, station, started, finished, seconds, "
            f"result, failures, outcome, report FROM units{where} ORDER BY id DESC LIMIT ?",
            (*args, int(limit))).fetchall()
        return [Unit(*r) for r in rows]

    def previous(self, serial: str, product: str = "") -> list[Unit]:
        return self.history(product or None, 1000, serial)

    def products(self) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT product FROM units WHERE product != '' ORDER BY product")]

    def yield_stats(self, product: str | None = None, since: str | None = None) -> Yield:
        where, args = self._where(product, since)
        y = Yield()
        first: dict = {}
        last: dict = {}
        for serial, result in self.db.execute(
                f"SELECT serial, result FROM units{where} ORDER BY id", args):
            y.tested += 1
            y.passed += result == "PASS"
            y.failed += result == "FAIL"
            y.errors += result == "ERROR"
            first.setdefault(serial, result)
            last[serial] = result
        y.units = len(first)
        y.first_pass = sum(r == "PASS" for r in first.values())
        y.final_pass = sum(r == "PASS" for r in last.values())
        return y

    def export_csv(self, path: str, product: str | None = None) -> int:
        where, args = self._where(product, None)
        cur = self.db.execute(
            "SELECT id, serial, product, operator, station, started, finished, seconds, "
            f"result, failures, outcome, report, data FROM units{where} ORDER BY id", args)
        rows = cur.fetchall()
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([d[0] for d in cur.description])
            w.writerows(rows)
        return len(rows)
