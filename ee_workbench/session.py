"""Session files (.eewb): everything the bench recorded, in one SQLite file.

While a session is being recorded, every open instrument's readings, every
logic-analyzer capture, the sequencer's results and the notes typed in go
into one file, all on the app's own clock (seconds since EE Workbench
started), so they can be laid over each other afterwards in the session
viewer, or read with any SQLite tool:

    meta(key, value)           created, wall_t0 (Unix time of app t = 0), ...
    streams(id, name, model, resource, columns, tbl)
    s_<id>(t, <columns>...)    one table per stream, t = app time in s
    notes(t, text)
    captures(id, t_app, t0, aligned, name, cfg, data, table_header, table_rows, note)
        t0 = app time of the capture's own t = 0 (to µs when aligned to a
        PPK2, to a few ms otherwise); data = compressed .npz of the traces

    sqlite3 bench.eewb "select t, voltage_V from s_1"

Writes are buffered and committed about once a second from the GUI thread.

"Only around events" (a flight recorder): start(path, gate=(pre, post)) keeps
the last `pre` seconds of every stream in memory and writes nothing until
trigger(t) is called (a watchdog rule tripping, a capture, Mark event); then
it writes from t - pre and keeps writing until t + post. Notes and captures
are always written. Each trigger is kept in the `events` table.
"""
from __future__ import annotations

import io
import json
from collections import deque
import sqlite3
import time
from dataclasses import asdict, dataclass, field

import numpy as np

from .clock import APP_T0, now
from .instruments.logic import AnalogTrace, DigitalTrace, LogicCapture, LogicConfig

SUFFIX = ".eewb"
FORMAT = 1


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class SessionWriter:
    def __init__(self):
        self.db: sqlite3.Connection | None = None
        self.path = ""
        self._streams: dict[object, tuple[int, str, list[str]]] = {}
        self._pending: dict[int, list] = {}
        self.rows = 0
        self.started = 0.0
        self.gate: tuple[float, float] | None = None
        self._ring: dict[int, deque] = {}       # sid -> chunks of rows (gated mode)
        self._open_until = -1e300               # gated: writing until this time
        self.events = 0

    @property
    def active(self) -> bool:
        return self.db is not None

    def start(self, path: str, gate: tuple[float, float] | None = None):
        """gate=(pre_s, post_s): record only around trigger() calls."""
        self.stop()
        if gate is not None:
            pre, post = float(gate[0]), float(gate[1])
            if pre < 0 or post < 0:
                raise ValueError("pre / post must be 0 or more seconds")
            gate = (pre, post)
        db = sqlite3.connect(path)
        try:
            self._open(db)
        except Exception:
            db.close()
            raise
        self.db, self.path = db, path
        self._streams.clear()
        self._pending.clear()
        self.rows = 0
        self.started = now()
        self.gate = gate
        self._ring.clear()
        self._open_until = -1e300
        self.events = 0
        if gate:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('gate', ?)",
                            (json.dumps({"pre_s": gate[0], "post_s": gate[1]}),))
            self.db.commit()

    def _open(self, db: sqlite3.Connection):
        db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS streams(id INTEGER PRIMARY KEY, name TEXT, model TEXT,
                resource TEXT, columns TEXT, tbl TEXT);
            CREATE TABLE IF NOT EXISTS notes(t REAL, text TEXT);
            CREATE TABLE IF NOT EXISTS events(t REAL, t_from REAL, t_to REAL, why TEXT);
            CREATE TABLE IF NOT EXISTS captures(id INTEGER PRIMARY KEY, t_app REAL, t0 REAL,
                aligned INTEGER, name TEXT, cfg TEXT, data BLOB, table_header TEXT,
                table_rows TEXT, note TEXT);
        """)
        wall_t0 = time.time() - (time.monotonic() - APP_T0)
        meta = {"format": FORMAT, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "wall_t0": f"{wall_t0:.6f}", "app": "EE Workbench"}
        old = dict(db.execute("SELECT key, value FROM meta"))
        if "wall_t0" in old:   # appending to an earlier file: keep its t = 0
            meta.pop("wall_t0")
            meta.pop("created")
            self._shift = wall_t0 - float(old["wall_t0"])
        else:
            self._shift = 0.0
        db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                       [(k, str(v)) for k, v in meta.items()])
        db.commit()

    def stop(self) -> str:
        """Write what is buffered and close the file. Never raises (it runs
        while quitting and after a failed write); returns the error, if any."""
        err = ""
        if self.db is not None:
            try:
                self.flush()
            except Exception as e:      # noqa: BLE001 - full disk, file removed
                err = f"{type(e).__name__}: {e}"
            try:
                self.db.close()
            except Exception:           # noqa: BLE001
                pass
        self.db = None
        self._pending.clear()
        self._ring.clear()
        return err

    # ---- data ------------------------------------------------------------
    def stream(self, key, name: str, model: str, resource: str, columns: list[str]) -> int:
        """The id of key's stream, made the first time it records."""
        known = self._streams.get(key)
        if known and known[2] == columns:
            return known[0]
        cur = self.db.execute("INSERT INTO streams(name, model, resource, columns) "
                              "VALUES (?, ?, ?, ?)", (name, model, resource, json.dumps(columns)))
        sid = cur.lastrowid
        tbl = f"s_{sid}"
        self.db.execute(f"CREATE TABLE {tbl}(t REAL, " +
                        ", ".join(_ident(c) for c in columns) + ")")
        self.db.execute("UPDATE streams SET tbl = ? WHERE id = ?", (tbl, sid))
        self._streams[key] = (sid, tbl, list(columns))
        return sid

    def add(self, key, t: float, values: list):
        sid = self._streams[key][0]
        self._put(sid, [[t + self._shift] + list(values)])

    def add_many(self, key, rows):
        sid = self._streams[key][0]
        self._put(sid, [[float(r[0]) + self._shift] + list(r[1:]) for r in rows])

    def _put(self, sid: int, rows: list):
        if not rows:
            return
        if self.gate is None:
            self._pending.setdefault(sid, []).extend(rows)
            return
        # gated: rows inside an open event window are written, the rest wait
        # in the ring (trimmed to the pre-trigger time)
        live = [r for r in rows if r[0] <= self._open_until]
        if live:
            self._pending.setdefault(sid, []).extend(live)
        rest = [r for r in rows if r[0] > self._open_until] if live else rows
        if not rest:
            return
        ring = self._ring.setdefault(sid, deque())
        ring.append(rest)
        cutoff = rest[-1][0] - self.gate[0]
        while ring and ring[0][-1][0] < cutoff:
            ring.popleft()

    def trigger(self, t: float | None = None, why: str = "") -> bool:
        """An event: in "only around events" mode, keep from t - pre to t + post.
        Returns False when recording everything (nothing to do)."""
        if self.db is None or self.gate is None:
            return False
        t = (now() if t is None else float(t)) + self._shift
        pre, post = self.gate
        start = max(t - pre, self._open_until)
        for sid, ring in self._ring.items():
            keep = [r for chunk in ring for r in chunk if r[0] >= start]
            if keep:
                self._pending.setdefault(sid, []).extend(keep)
            ring.clear()
        self._open_until = max(self._open_until, t + post)
        self.events += 1
        self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (t, t - pre, t + post, why))
        return True

    @property
    def gated_open(self) -> bool:
        return self.gate is not None and now() + self._shift <= self._open_until

    def note(self, text: str, t: float | None = None):
        self.db.execute("INSERT INTO notes VALUES (?, ?)",
                        ((now() if t is None else t) + self._shift, text))
        self.db.commit()

    def capture(self, res: LogicCapture, name: str):
        self.trigger(res.t_app, f"capture: {name}")       # a capture is an event
        aligned = res.offset is not None
        t0 = res.offset if aligned else (res.t_armed if res.t_armed is not None
                                         else res.t_app - res.elapsed)
        self.db.execute(
            "INSERT INTO captures(t_app, t0, aligned, name, cfg, data, table_header, "
            "table_rows, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (res.t_app + self._shift, t0 + self._shift, int(aligned), name,
             json.dumps(asdict(res.cfg)), pack_traces(res), json.dumps(res.table_header),
             json.dumps(res.table_rows), res.offset_note))
        self.db.commit()

    def flush(self):
        if self.db is None:
            return
        for sid, rows in self._pending.items():
            if rows:
                tbl = f"s_{sid}"
                n = len(rows[0])
                self.db.executemany(f"INSERT INTO {tbl} VALUES ({','.join('?' * n)})", rows)
                self.rows += len(rows)
        self._pending.clear()
        self.db.commit()


def pack_traces(res: LogicCapture) -> bytes:
    arrays = {}
    for ch, tr in res.digital.items():
        arrays[f"d{ch}_t"] = tr.t
        arrays[f"d{ch}_l"] = tr.level
        arrays[f"d{ch}_end"] = np.array([tr.end])
    for ch, tr in res.analog.items():
        arrays[f"a{ch}_v"] = tr.v.astype(np.float32)
        arrays[f"a{ch}_td"] = np.array([tr.t0, tr.dt])
    for k, v in res.extra.items():
        if isinstance(v, dict) and "t" in v:
            arrays[f"x_{k}_t"] = np.asarray(v["t"])
            arrays[f"x_{k}_i"] = np.asarray(v["i"], np.float32)
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


def unpack_traces(blob: bytes, res: LogicCapture):
    z = np.load(io.BytesIO(blob))
    for k in z.files:
        if k.startswith("d") and k.endswith("_t"):
            ch = int(k[1:-2])
            res.digital[ch] = DigitalTrace(ch, z[k], z[f"d{ch}_l"], float(z[f"d{ch}_end"][0]))
        elif k.startswith("a") and k.endswith("_v"):
            ch = int(k[1:-2])
            t0, dt = z[f"a{ch}_td"]
            res.analog[ch] = AnalogTrace(ch, float(t0), float(dt), z[k].astype(np.float64))
        elif k.startswith("x_") and k.endswith("_t"):
            name = k[2:-2]
            res.extra[name] = {"name": name, "t": z[k], "i": z[f"x_{name}_i"].astype(float)}


# ---------------------------------------------------------------- reading
@dataclass
class Stream:
    id: int
    name: str
    model: str
    resource: str
    columns: list[str]
    t: np.ndarray
    data: dict[str, np.ndarray] = field(default_factory=dict)   # numeric columns only


@dataclass
class SavedCapture:
    id: int
    t_app: float
    t0: float
    aligned: bool
    name: str
    note: str
    _db: object = None
    _capture: LogicCapture | None = None

    def load(self) -> LogicCapture:
        if self._capture is None:
            row = self._db.execute("SELECT cfg, data, table_header, table_rows FROM captures "
                                   "WHERE id = ?", (self.id,)).fetchone()
            cfg = LogicConfig(**json.loads(row[0]))
            res = LogicCapture(cfg, {}, {}, t_app=self.t_app, wall=0.0, elapsed=0.0,
                               offset=self.t0 if self.aligned else None, offset_note=self.note)
            res.table_header = json.loads(row[2] or "[]")
            res.table_rows = json.loads(row[3] or "[]")
            unpack_traces(row[1], res)
            self._capture = res
        return self._capture

    @property
    def span(self) -> tuple[float, float]:
        a, b = self.load().span
        return self.t0 + a, self.t0 + b


class SessionReader:
    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            self._read()
        except Exception:
            self.db.close()      # a damaged or foreign file: don't keep it open
            raise

    def _read(self):
        path = self.path
        self.meta = dict(self.db.execute("SELECT key, value FROM meta"))
        if "wall_t0" not in self.meta:
            raise ValueError(f"{path} isn't an EE Workbench session file")
        self.wall_t0 = float(self.meta["wall_t0"])
        self.streams: list[Stream] = []
        for sid, name, model, res, cols, tbl in self.db.execute(
                "SELECT id, name, model, resource, columns, tbl FROM streams ORDER BY id"):
            cols = json.loads(cols)
            rows = self.db.execute(f"SELECT * FROM {tbl} ORDER BY t").fetchall()
            t = np.array([r[0] for r in rows], float)
            s = Stream(sid, name, model, res, cols, t)
            for k, c in enumerate(cols):
                vals = [r[k + 1] for r in rows]
                if vals and all(isinstance(v, (int, float)) or v is None for v in vals):
                    s.data[c] = np.array([np.nan if v is None else v for v in vals], float)
            self.streams.append(s)
        self.notes = [(float(t), text) for t, text in
                      self.db.execute("SELECT t, text FROM notes ORDER BY t")]
        try:        # "only around events" recordings: the windows that were kept
            self.events = [(float(a), float(b), why or "") for a, b, why in self.db.execute(
                "SELECT t_from, t_to, why FROM events ORDER BY t")]
        except sqlite3.Error:     # files from before events existed
            self.events = []
        self.captures = [SavedCapture(i, ta, t0, bool(al), name or "", note or "", self.db)
                         for i, ta, t0, al, name, note in self.db.execute(
                             "SELECT id, t_app, t0, aligned, name, note FROM captures "
                             "ORDER BY t_app")]

    def span(self) -> tuple[float, float]:
        ts = [s.t[[0, -1]] for s in self.streams if s.t.size]
        ts += [np.array([t, t]) for t, _ in self.notes]
        ts += [np.array([c.t0, c.t_app]) for c in self.captures]
        if not ts:
            return 0.0, 1.0
        allt = np.concatenate(ts)
        return float(allt.min()), float(allt.max())

    def wall(self, t: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.wall_t0 + t))

    def close(self):
        self.db.close()


recorder = SessionWriter()      # the one the app records into
