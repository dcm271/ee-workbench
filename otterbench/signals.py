"""What each instrument's readings contain, as named signals: shared by the
watchdog (rules) and math channels.

signals_of(kind, reading) -> {quantity: (t array, value array)}

  supply   voltage, current, power, dvm (when the model has one)
  ppk2     current (every 10 µs sample)
  dmm      value
  gpio     gpio17 … (each configured pin: 0 / 1)
  scpi     each reading its definition names (value; voltage1, current1, …)
  uart     each value its patterns find in the text

Identifiers for expressions: `slug("66321D · DUT rail")` = "_66321d_dut_rail",
so a math channel can say  _66321d_dut_rail.power / ppk2.current .
"""
from __future__ import annotations

import re

import numpy as np

QUANTITIES = {
    "supply": ["voltage", "current", "power", "dvm"],
    "ppk2": ["current"],
    "dmm": ["value"],
    "gpio": [],
    "fgen": [],
    "logic": [],
}
UNITS = {"voltage": "V", "current": "A", "power": "W", "dvm": "V", "value": ""}


def slug(name: str) -> str:
    """A Python identifier for an instrument's display name. An unnamed tab's
    "(resource)" is left out: "PPK2 (PPK2::/dev/ttyACM0)" → ppk2."""
    name = re.sub(r"\s*\([^()]*\)\s*$", "", name)
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    if not s:
        return "_"
    return "_" + s if s[0].isdigit() else s


def name_matches(query: str, name: str) -> bool:
    """Does `query` (from a watchdog rule, a sequence step, a script) mean the
    instrument called `name`? Exact or same identifier, else a part of the
    name. With several of one model open, the second is "PPK2 #2", and so on:
    a query without "#" then means the first, never "#2" and up."""
    q, n = query.strip().lower(), name.strip().lower()
    if not q:
        return False
    if q == n or slug(q) == slug(n):
        return True
    if re.search(r" #\d+\b", n) and "#" not in q:
        return False
    return q in n


def signals_of(kind: str, r) -> dict:
    if r is None:
        return {}
    if kind == "ppk2":
        n = int(r.current.size)
        if not n:
            return {}
        from .instruments.ppk2 import DT
        t = r.t0 + np.arange(n) * DT
        return {"current": (t, np.asarray(r.current, float))}
    t = np.array([float(r.t)])
    if kind == "supply":
        out = {}
        v = getattr(r, "voltage", None)
        i = getattr(r, "current", None)
        if v is not None:
            out["voltage"] = (t, np.array([float(v)]))
        if i is not None:
            out["current"] = (t, np.array([float(i)]))
        p = getattr(r, "power", None)
        if p is None and v is not None and i is not None:
            p = v * i
        if p is not None:
            out["power"] = (t, np.array([float(p)]))
        d = getattr(r, "dvm", None)
        if d is not None:
            out["dvm"] = (t, np.array([float(d)]))
        return out
    if kind == "dmm":
        if getattr(r, "overload", False) or r.value is None:
            return {"value": (t, np.array([np.nan]))}
        return {"value": (t, np.array([float(r.value)]))}
    if kind == "gpio":
        return {f"gpio{g}": (t, np.array([float(v)])) for g, v in r.levels.items()}
    if kind == "uart":
        out: dict = {}
        for ln in r.lines:
            for k, v in ln.values.items():
                out.setdefault(k, ([], []))
                out[k][0].append(ln.t)
                out[k][1].append(v)
        return {k: (np.array(a, float), np.array(b, float)) for k, (a, b) in out.items()}
    if kind == "scpi":
        return {k: (t, np.array([np.nan if v is None else float(v)]))
                for k, v in r.values.items()}
    return {}
