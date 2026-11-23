"""Regulator (DC-DC / LDO) characterization: every input voltage with every
load current. Efficiency, losses, load and line regulation, dropout.

Safe by order: the load is off whenever the input changes, an output above the
allowed maximum ends the test at once, a collapsing output ends that input
voltage's row, and whatever happens the load goes off first, then the input.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from . import roles
from .sequencer import Bench, SequenceError, Unstopped

COLUMNS = ["vin_set", "iload_set", "vin", "iin", "vout", "iout", "pin", "pout", "efficiency",
           "loss", "note"]
UNITS = {"vin_set": "V", "iload_set": "A", "vin": "V", "iin": "A", "vout": "V", "iout": "A",
         "pin": "W", "pout": "W", "efficiency": "%", "loss": "W"}


def parse_list(text: str) -> list[float]:
    """'5, 9, 12' or '4.5 … 16 step 0.5' ('4.5-16/0.5' also) or '0..2 x 11'."""
    import re
    t = text.replace("…", "..").replace("–", "-").strip()
    num = r"([\d.]+(?:e[-+]?\d+)?)"
    m = re.match(rf"^{num}\s*(?:\.\.|to|-)\s*{num}\s*(?:step|/)\s*{num}$", t, re.I)
    if m:
        a, b, s = (float(x) for x in m.groups())
        if s <= 0:
            raise ValueError("the step must be above 0")
        n = int(math.floor(abs(b - a) / s + 1e-9)) + 1
        out = [a + (s if b >= a else -s) * k for k in range(min(n, 1001))]
    else:
        m = re.match(rf"^{num}\s*(?:\.\.|to|-)\s*{num}\s*(?:x|\*|points)\s*(\d+)$", t, re.I)
        if m:
            a, b, n = float(m.group(1)), float(m.group(2)), int(m.group(3))
            out = [float(x) for x in np.linspace(a, b, max(1, min(n, 1001)))]
        else:
            out = [float(x) for x in re.split(r"[,;\s]+", t) if x]
    if not out:
        raise ValueError("give at least one value")
    if len(out) > 1000 or any(not math.isfinite(x) for x in out):
        raise ValueError("too many values")
    return [round(x, 9) for x in out]


@dataclass
class RegSettings:
    source: str = ""             # port texts (roles.py)
    load: str = ""
    vout_meter: str = ""         # optional: a meter at the output / input terminals
    vin_meter: str = ""
    vin: str = "5, 9, 12"
    iload: str = "0, 0.1, 0.25, 0.5, 1"
    iin_limit: float = 2.0
    vout_nominal: float = 3.3
    vout_max: float = 3.6        # above this the test ends at once
    settle: float = 0.5
    average: float = 0.5
    # limits for PASS / FAIL (0: not judged)
    min_efficiency: float = 0.0  # % at the highest load
    max_load_reg: float = 0.0    # % of nominal over the load range
    max_line_reg: float = 0.0    # % of nominal over the input range
    tolerance: float = 0.0       # % of nominal at every point

    def problem(self) -> str:
        try:
            vin, il = parse_list(self.vin), parse_list(self.iload)
        except ValueError as e:
            return f"Input voltages / load currents: {e}"
        if min(vin) < 0 or min(il) < 0:
            return "Voltages and currents can't be negative"
        if not self.source:
            return "Choose the power supply that feeds the regulator"
        if not self.load:
            return "Choose the electronic load"
        if self.vout_nominal <= 0:
            return "Give the output voltage it should have"
        if self.vout_max <= self.vout_nominal:
            return "The highest allowed output must be above the nominal output"
        if self.iin_limit <= 0:
            return "The input current limit must be above 0"
        if len(vin) * len(il) > 5000:
            return f"That is {len(vin) * len(il)} points; use fewer"
        return ""

    def points(self) -> int:
        return len(parse_list(self.vin)) * len(parse_list(self.iload))


@dataclass
class RegTest:
    s: RegSettings
    rows: list = field(default_factory=list)

    def run(self, bench: Bench, on_row=None):
        s = self.s
        if s.problem():
            raise SequenceError(s.problem())
        src = roles.find(bench, s.source, "source")
        load = roles.find(bench, s.load, "load")
        if src.name == load.name:
            raise SequenceError("The supply and the load must be different instruments")
        for m in (s.vin_meter, s.vout_meter):
            if m:
                bench.inst(m)               # say so now if it isn't open
        try:
            load.prepare()
            src.off()
            src.amps(s.iin_limit)
            for k, vin in enumerate(parse_list(s.vin)):
                load.off()
                src.volts(vin)
                if k == 0:
                    src.on()
                bench.wait(s.settle)
                for il in parse_list(s.iload):
                    if il > 0:
                        load.amps(il)
                        load.on()
                    else:
                        load.off()
                    bench.wait(s.settle)
                    row = self._row(bench, src, load, vin, il)
                    self.rows.append(row)
                    if on_row:
                        on_row(row)
                    if row["vout"] > s.vout_max:
                        raise SequenceError(f"Output {row['vout']:.3f} V is above "
                                            f"{s.vout_max:g} V: stopped")
                    if il > 0 and row["vout"] < 0.5 * s.vout_nominal:
                        bench.log(f"Output collapsed at {vin:g} V in, {il:g} A: "
                                  "higher loads skipped")
                        break
        finally:
            with Unstopped(bench):
                for what, fn in (("load", load.off), ("supply", src.off)):
                    try:
                        fn()
                    except Exception as e:      # noqa: BLE001 - try the other one all the same
                        bench.log(f"{what} off failed: {e}")
        return self.rows

    def _row(self, bench, src, load, vin_set, il_set) -> dict:
        s = self.s
        vin, iin = src.read(s.average)
        vout, iout = load.read(s.average)
        if s.vin_meter:
            vin = bench.measure(s.vin_meter, _volts(bench, s.vin_meter), s.average)
        if s.vout_meter:
            vout = bench.measure(s.vout_meter, _volts(bench, s.vout_meter), s.average)
        if il_set == 0:
            iout = 0.0                   # the load is off: what it shows is its zero error
        pin, pout = vin * iin, vout * iout
        notes = []
        if vin < 0.97 * vin_set and iin > 0.9 * s.iin_limit:
            notes.append("input current limit")
        if il_set > 0 and abs(iout - il_set) > max(0.05 * il_set, 0.005):
            notes.append("load not at its current")
        if pout > 1.02 * pin and pout > 0.01:
            notes.append("more power out than in: check the wiring and the meters")
        if vout < 0.9 * s.vout_nominal:
            notes.append("output low")
        return {"vin_set": vin_set, "iload_set": il_set, "vin": vin, "iin": iin, "vout": vout,
                "iout": iout, "pin": pin, "pout": pout,
                "efficiency": 100 * pout / pin if pin > 1e-9 and pout > 0 else math.nan,
                "loss": pin - pout, "note": ", ".join(notes)}


def _volts(bench, name: str) -> str:
    inst = bench.inst(name)
    can = inst.measure_names if inst.measure_names is not None else []
    return "voltage" if "voltage" in can else ""


# ---------------------------------------------------------------- analysis
def analyse(rows: list[dict], s: RegSettings) -> dict:
    """Load regulation per input voltage, line regulation per load current
    (points with a note other than "" are left out), the best efficiency, and
    the verdict against the limits."""
    good = [r for r in rows if not r["note"]]
    out: dict = {"load": [], "line": [], "checks": []}
    for vin in sorted({r["vin_set"] for r in good}):
        g = sorted((r for r in good if r["vin_set"] == vin), key=lambda r: r["iload_set"])
        if len(g) >= 2:
            dv = g[0]["vout"] - g[-1]["vout"]
            di = g[-1]["iout"] - g[0]["iout"]
            out["load"].append({"vin": vin, "from": g[0]["iload_set"], "to": g[-1]["iload_set"],
                                "dv": dv, "percent": 100 * dv / s.vout_nominal,
                                "ohms": dv / di if di > 0 else math.nan})
    for il in sorted({r["iload_set"] for r in good}):
        g = sorted((r for r in good if r["iload_set"] == il), key=lambda r: r["vin_set"])
        if len(g) >= 2:
            dv = g[-1]["vout"] - g[0]["vout"]
            dvin = g[-1]["vin"] - g[0]["vin"]
            out["line"].append({"iload": il, "from": g[0]["vin_set"], "to": g[-1]["vin_set"],
                                "dv": dv, "percent": 100 * dv / s.vout_nominal,
                                "mv_per_v": 1e3 * dv / dvin if dvin > 0 else math.nan})
    eff = [r for r in good if math.isfinite(r["efficiency"])]
    if eff:
        out["best"] = max(eff, key=lambda r: r["efficiency"])
    c = out["checks"]
    # a point that couldn't be reached must not quietly pass
    for r in rows:
        if r["note"]:
            c.append((f"{r['vin_set']:g} V in, {r['iload_set']:g} A: {r['note']}", False))
    want = len(parse_list(s.vin)) * len(parse_list(s.iload))
    if rows and len(rows) < want:
        c.append((f"{len(rows)} of {want} points measured", False))
    if s.tolerance > 0 and rows:
        worst = max(rows, key=lambda r: abs(r["vout"] - s.vout_nominal))
        pc = 100 * abs(worst["vout"] - s.vout_nominal) / s.vout_nominal
        c.append((f"Output within ±{s.tolerance:g} % (worst {pc:.2f} %)", pc <= s.tolerance))
    if s.min_efficiency > 0 and eff:
        top = max(r["iload_set"] for r in eff)
        worst_e = min(r["efficiency"] for r in eff if r["iload_set"] == top)
        c.append((f"Efficiency at {top:g} A ≥ {s.min_efficiency:g} % (worst {worst_e:.1f} %)",
                  worst_e >= s.min_efficiency))
    for key, limit, label in (("load", s.max_load_reg, "Load"), ("line", s.max_line_reg, "Line")):
        if limit > 0:
            if out[key]:
                w = max(abs(x["percent"]) for x in out[key])
                c.append((f"{label} regulation ≤ {limit:g} % (worst {w:.3f} %)", w <= limit))
            else:
                c.append((f"{label} regulation: not enough points", False))
    judged = any(x > 0 for x in (s.tolerance, s.min_efficiency, s.max_load_reg, s.max_line_reg))
    out["verdict"] = "" if not (judged and rows) else \
        "PASS" if all(ok for _, ok in c) else "FAIL"
    return out
