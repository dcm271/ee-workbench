"""Start-up configuration and the remembered last session.

Two files live in ~/.config/ee-workbench/ (or $XDG_CONFIG_HOME/ee-workbench):

config.toml        written once with commented defaults, then edited by you.
                   Decides what opens at start-up.
last-session.json  written by the app whenever instruments are opened or
                   closed (and on quit). Remembers what was open, plus the
                   model and panel settings of every instrument ever used.
"""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

OPEN_MODES = ("last", "list", "scan", "none")
MODEL_KEYS = ("auto", "663xx", "6632A", "3456A")
MODEL_ALIASES = {"66321D": "663xx", "66321B": "663xx", "66311B": "663xx",
                 "6633A": "6632A", "6634A": "6632A"}


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "ee-workbench"


TEMPLATE = '''\
# EE Workbench start-up configuration.
# Resources given on the command line (./run.sh GPIB0::5::INSTR ...) override this.

[startup]
# What to open when EE Workbench starts:
#   "last"  reopen the instruments that were open when it last closed (default)
#   "list"  always open the [[instrument]] entries below
#   "scan"  scan the GPIB bus and open everything that answers; addresses use
#           the model from the list below, or the one last used there
#   "none"  start with no instruments open
open = "last"

# At boot the GPIB adapter can take a few seconds to appear. Wait up to this
# many seconds for it before opening GPIB instruments.
wait_for_gpib = 30

# Instruments for open = "list" (also tells "scan" which model is where).
# model: "auto" (supplies identify themselves), "663xx", "6632A" or "3456A".
# The 3456A cannot identify itself, so always give its model.
# Optional for a 3456A: name, function, range, nplc, autozero, filter.
#
# [[instrument]]
# resource = "GPIB0::5::INSTR"
# model = "auto"
#
# [[instrument]]
# resource = "GPIB0::22::INSTR"
# model = "3456A"
# name = "E3610A output"
# function = "DCV"
# range = "AUTO"
# nplc = 1
'''


@dataclass
class Entry:
    resource: str
    model: str = "auto"
    settings: dict = field(default_factory=dict)


@dataclass
class Config:
    open: str = "last"
    wait_for_gpib: float = 30.0
    instruments: list[Entry] = field(default_factory=list)
    path: Path | None = None
    warning: str = ""          # problem found while reading, shown in the status bar


def norm_model(m: str) -> str:
    m = MODEL_ALIASES.get(str(m), str(m))
    return m if m in MODEL_KEYS else "auto"


def load_config(path: Path | None = None) -> Config:
    path = path or config_dir() / "config.toml"
    cfg = Config(path=path)
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(TEMPLATE)
        except OSError:
            pass
        return cfg
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        cfg.warning = f"{path}: {e}. Using defaults."
        return cfg
    st = data.get("startup", {})
    mode = str(st.get("open", "last")).lower()
    if mode in OPEN_MODES:
        cfg.open = mode
    else:
        cfg.warning = f'{path}: open = "{mode}" is not one of {", ".join(OPEN_MODES)}.'
    try:
        cfg.wait_for_gpib = max(0.0, float(st.get("wait_for_gpib", 30)))
    except (TypeError, ValueError):
        pass
    for item in data.get("instrument", []):
        if not isinstance(item, dict) or not item.get("resource"):
            continue
        rest = {k: v for k, v in item.items() if k not in ("resource", "model")}
        cfg.instruments.append(Entry(str(item["resource"]).strip(),
                                     norm_model(item.get("model", "auto")), rest))
    return cfg


# ---------------------------------------------------------------- session
class Session:
    """last-session.json: {"open": [entries], "known": {resource: entry}}."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "last-session.json"
        self.open: list[Entry] = []
        self.known: dict[str, Entry] = {}
        try:
            data = json.loads(self.path.read_text())
            self.open = [self._entry(d) for d in data.get("open", [])]
            self.known = {k: self._entry(d) for k, d in data.get("known", {}).items()}
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    @staticmethod
    def _entry(d: dict) -> Entry:
        return Entry(str(d["resource"]), norm_model(d.get("model", "auto")),
                     dict(d.get("settings", {})))

    def save(self, entries: list[Entry]):
        self.open = entries
        for e in entries:
            self.known[e.resource] = e
        data = {"open": [e.__dict__ for e in self.open],
                "known": {k: e.__dict__ for k, e in self.known.items()}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self.path)       # atomic: survives a power cut mid-write
        except OSError:
            pass


def startup_entries(cfg: Config, session: Session, scanned: list[str] | None = None,
                    mode: str | None = None) -> list[Entry]:
    """What to open, given the mode. `scanned` is the GPIB scan result for "scan"."""
    mode = mode or cfg.open
    listed = {e.resource: e for e in cfg.instruments}

    def merged(res: str, model: str | None = None, settings: dict | None = None) -> Entry:
        # Settings from config.toml win over the remembered ones.
        known = session.known.get(res)
        out = Entry(res, "auto", dict(known.settings) if known else {})
        if known:
            out.model = known.model
        if res in listed:
            if listed[res].model != "auto" or not known:
                out.model = listed[res].model
            out.settings.update(listed[res].settings)
        if model:
            out.model = model
        if settings:
            out.settings.update(settings)
        return out

    if mode == "last":
        return [merged(e.resource, e.model, e.settings) for e in session.open]
    if mode == "list":
        return [merged(e.resource) for e in cfg.instruments]
    if mode == "scan":
        return [merged(r) for r in (scanned or [])]
    return []
