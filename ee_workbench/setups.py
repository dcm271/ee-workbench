"""Named bench setups: the open instruments with their tab settings, the
sequence, the watchdog rules and the math channels, saved under a name
("nRF52 sleep test") and recalled in one go.

Files: ~/.config/ee-workbench/setups/<name>.json. Supply setpoints are
saved too but applied only with the output off; recalling never switches
an output on (a sequence's Set steps do that).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .config import config_dir

VERSION = 1


def setups_dir() -> Path:
    return config_dir() / "setups"


def _file(name: str) -> Path:
    safe = re.sub(r"[^\w .,()+-]", "_", name).strip() or "setup"
    return setups_dir() / f"{safe}.json"


def list_setups() -> list[tuple[str, float, str]]:
    """(name, modified time, description) newest first."""
    out = []
    for p in setups_dir().glob("*.json"):
        try:
            d = json.loads(p.read_text())
            out.append((d.get("name", p.stem), p.stat().st_mtime, d.get("description", "")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda x: -x[1])


def save_setup(name: str, data: dict, description: str = "") -> Path:
    d = {"version": VERSION, "name": name, "description": description,
         "saved": time.strftime("%Y-%m-%d %H:%M:%S"), **data}
    p = _file(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    os.replace(tmp, p)
    return p


def load_setup(name: str) -> dict:
    p = _file(name)
    try:
        d = json.loads(p.read_text())
    except OSError as e:
        raise ValueError(f"No setup called '{name}'") from e
    except ValueError as e:
        raise ValueError(f"{p} is damaged: {e}") from e
    d.setdefault("instruments", [])
    return d


def delete_setup(name: str):
    try:
        _file(name).unlink()
    except FileNotFoundError:
        pass


def export_setup(name: str, path: str):
    Path(path).write_text(_file(name).read_text())


def import_setup(path: str) -> str:
    d = json.loads(Path(path).read_text())
    name = d.get("name") or Path(path).stem
    save_setup(name, {k: v for k, v in d.items() if k not in ("version", "name", "saved",
                                                              "description")},
               d.get("description", ""))
    return name
