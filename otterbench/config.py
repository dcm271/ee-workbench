"""Start-up configuration and the remembered last session.

Two files live in ~/.config/otterbench/ (or $XDG_CONFIG_HOME/otterbench):

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
MODEL_KEYS = ("auto", "663xx", "6632A", "DPS150", "3456A", "BK393", "BK393B", "XDM", "PPK2",
              "SALEAE", "SIGROK", "FGEN", "GPIO", "UART")
MODEL_ALIASES = {"66321D": "663xx", "66321B": "663xx", "66311B": "663xx",
                 "6633A": "6632A", "6634A": "6632A", "ppk2": "PPK2",
                 "saleae": "SALEAE", "Saleae": "SALEAE",
                 "sigrok": "SIGROK", "fgen": "FGEN", "gpio": "GPIO", "SDG": "FGEN",
                 "uart": "UART",
                 "33500": "FGEN", "DG1000Z": "FGEN",
                 "DPS-150": "DPS150", "dps150": "DPS150", "dps-150": "DPS150",
                 "393": "BK393", "393A": "BK393", "BK393A": "BK393", "393B": "BK393B",
                 "XDM1041": "XDM", "XDM1141": "XDM", "XDM1241": "XDM", "XDM1000": "XDM",
                 "XDM2041": "XDM", "xdm": "XDM"}


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "otterbench"


def migrate_old_dir() -> str:
    """Move everything from ~/.config/ee-workbench (the program's old name) to
    config_dir(). Does nothing once config_dir() has a config.toml, so it is
    over after the first start. Files already in config_dir() are kept.
    Returns a warning for the user, or ""."""
    new = config_dir()
    old = new.parent / "ee-workbench"
    if not old.is_dir() or (new / "config.toml").exists():
        return ""
    try:
        new.mkdir(parents=True, exist_ok=True)
        # config.toml last: an interrupted move is picked up again next time
        for p in sorted(old.iterdir(), key=lambda p: p.name == "config.toml"):
            if not (new / p.name).exists():
                p.rename(new / p.name)
        if not any(old.iterdir()):
            old.rmdir()
    except OSError as e:
        return f"could not move the settings in {old} to {new}: {e}"
    return ""


TEMPLATE = '''\
# OtterBench start-up configuration.
# Resources given on the command line (./run.sh GPIB0::5::INSTR ...) override this.

[startup]
# What to open when OtterBench starts:
#   "last"  reopen the instruments that were open when it last closed (default)
#   "list"  always open the [[instrument]] entries below
#   "scan"  scan the GPIB bus and open everything that answers; addresses use
#           the model from the list below, or the one last used there
#   "none"  start with no instruments open
open = "last"

# At boot the GPIB adapter can take a few seconds to appear. Wait up to this
# many seconds for it before opening GPIB instruments.
wait_for_gpib = 30

# Start the remote API (drive the bench from Python / Jupyter: see
# docs/03-users-guide.md) automatically. It only listens on 127.0.0.1.
api = false
api_port = 8765

# Instruments for open = "list" (also tells "scan" which model is where).
# model: "auto" (supplies and function generators identify themselves), "663xx",
# "6632A", "DPS150", "3456A", "BK393", "BK393B", "XDM", "PPK2", "SALEAE", "SIGROK",
# "FGEN", "GPIO" or "UART" (or any definition's id: "34461A", "DP832", "ET44", …).
# The 3456A cannot identify itself, so always give its model.
# A Nordic PPK2 is a serial port: resource = "PPK2::/dev/ttyACM0".
# So is a FNIRSI DPS-150 supply: resource = "DPS150::/dev/ttyACM0", and a B&K 393 /
# 393B handheld meter: "BK393::/dev/ttyUSB0" / "BK393B::/dev/ttyUSB0", and an OWON
# XDM1041 / XDM1241 / XDM2041 bench meter: "XDM::/dev/ttyUSB0" (a
# /dev/serial/by-id/... path keeps working when the port number changes).
# A Saleae logic analyzer goes through a running Logic 2 (automation server on):
# resource = "SALEAE::<device id>" (or "SALEAE::" for the first one it sees).
# Anything sigrok supports: resource = "SIGROK::fx2lafw" (driver[:conn=bus.addr]).
# A LAN function generator: resource = "TCPIP::192.168.1.50::INSTR", model = "FGEN".
# A DUT's serial console: resource = "UART::/dev/ttyUSB0@115200".
# Riden, Korad / Tenma and Atorch units, thermometers and Pi sensor boards:
# "RD60XX::/dev/ttyUSB0", "KA3005P::/dev/ttyACM0", "PX100::/dev/ttyUSB0",
# "CENTER309::/dev/ttyUSB0", "W1::" (DS18B20), "MAX31855::/dev/spidev0.0".
# The Raspberry Pi's GPIO header: resource = "GPIO::" (or "GPIO::/dev/gpiochip0").
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
    api: bool = False
    api_port: int = 8765


def norm_model(m: str) -> str:
    m = MODEL_ALIASES.get(str(m), str(m))
    if m in MODEL_KEYS:
        return m
    # generic SCPI definitions ("SCPI:34461A", or just "34461A") and scopes
    try:
        from .instruments import resolve_model
        r = resolve_model(m)
    except Exception:       # noqa: BLE001 - a broken definition must not stop start-up
        return "auto"
    return r if r.startswith(("SCPI:", "SCOPE:")) else "auto"


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
    if not isinstance(st, dict):
        cfg.warning = f"{path}: [startup] should be a table. Using defaults."
        st = {}
    mode = str(st.get("open", "last")).lower()
    if mode in OPEN_MODES:
        cfg.open = mode
    else:
        cfg.warning = f'{path}: open = "{mode}" is not one of {", ".join(OPEN_MODES)}.'
    try:
        cfg.wait_for_gpib = max(0.0, float(st.get("wait_for_gpib", 30)))
    except (TypeError, ValueError):
        pass
    cfg.api = bool(st.get("api", False))
    try:
        cfg.api_port = int(st.get("api_port", 8765))
    except (TypeError, ValueError):
        pass
    items = data.get("instrument", [])
    for item in items if isinstance(items, list) else []:
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
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        # one damaged entry costs only that entry, never the start-up
        opened = data.get("open", [])
        for d in opened if isinstance(opened, list) else []:
            e = self._entry(d)
            if e:
                self.open.append(e)
        known = data.get("known", {})
        for k, d in (known.items() if isinstance(known, dict) else []):
            e = self._entry(d)
            if e:
                self.known[str(k)] = e

    @staticmethod
    def _entry(d) -> Entry | None:
        try:
            res = str(d["resource"]).strip()
            settings = d.get("settings") or {}
            if not res:
                return None
            return Entry(res, norm_model(d.get("model", "auto")),
                         dict(settings) if isinstance(settings, dict) else {})
        except (KeyError, TypeError, ValueError, AttributeError):
            return None

    def save(self, entries: list[Entry]):
        self.open = entries
        for e in entries:
            self.known[e.resource] = e
        data = {"open": [e.__dict__ for e in self.open],
                "known": {k: e.__dict__ for k, e in self.known.items()}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=json_default))
            os.replace(tmp, self.path)       # atomic: survives a power cut mid-write
        except (OSError, TypeError, ValueError):
            pass


def json_default(o):
    """For json.dumps: numpy numbers and anything else odd in a tab's settings."""
    if hasattr(o, "item"):
        try:
            return o.item()
        except (TypeError, ValueError):
            pass
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


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
