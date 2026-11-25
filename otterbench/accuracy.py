"""How far a reading may be off: accuracy specifications, as a datasheet or a
calibration certificate gives them.

    0.0035% + 50u                        of the reading, plus a fixed part
    100ppm + 2m
    0.1: 0.005% + 3.5u; 1: 0.004% + 7u; 10: 0.0035% + 50u
                                         by range: the first whose end is at or
                                         above the reading (a last part without
                                         "end:" is for everything above)

The fixed part is in the reading's own unit (V, A, Ω …) with an optional SI
prefix. "1% of range" has to be worked out: 0.001% of 10 V is 100u.

They come from the instrument's entry in the calibration log (what you typed in,
by function or reading name) or from its definition file. Nothing is assumed: no
specification, no ±. It is the datasheet's limit, not a full uncertainty budget
(probes, leads, temperature and noise aren't in it).
"""
from __future__ import annotations

import math
import os
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache

PREFIX = {"": 1.0, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
          "k": 1e3, "M": 1e6, "G": 1e9}
_NUM = r"(\d+\.?\d*(?:e[-+]?\d+)?|\.\d+(?:e[-+]?\d+)?)"
_TERM = re.compile(rf"^{_NUM}\s*(%|ppm|[pnuµμmkMG]?)\s*(?:of\s+)?(?:rdg|reading)?$")
OFF, MARGINAL, STRICT = "off", "marginal", "strict"
GUARDS = {OFF: "Limits as they are", MARGINAL: "Mark what is within ± of a limit",
          STRICT: "FAIL what is within ± of a limit"}


class AccuracyError(ValueError):
    pass


@dataclass
class Part:
    upto: float                  # |reading| it holds to (inf: any)
    relative: float = 0.0        # of the reading, 0.01 = 1 %
    fixed: float = 0.0


@dataclass
class Spec:
    parts: list = field(default_factory=list)
    text: str = ""

    def of(self, value: float) -> float | None:
        """± this much at `value`; None: no reading, or past the last range."""
        if value is None or not math.isfinite(value):
            return None
        a = abs(value)
        for p in self.parts:
            # a reading a little over the range's nominal end is still in it
            if a <= p.upto * 1.2 if p is self.parts[-1] else a <= p.upto:
                return a * p.relative + p.fixed
        return None


def _terms(text: str, whole: str) -> tuple[float, float]:
    rel = fixed = 0.0
    text = text.strip().removeprefix("±").strip()
    for t in text.split("+"):
        m = _TERM.match(t.strip())
        if not m:
            raise AccuracyError(f"“{t.strip() or whole}” isn't like 0.01%, 50ppm or 3.5u")
        v, u = float(m.group(1)), m.group(2)
        if u == "%":
            rel += v / 100
        elif u == "ppm":
            rel += v * 1e-6
        else:
            fixed += v * PREFIX[u]
    return rel, fixed


@lru_cache(maxsize=256)
def parse(text: str) -> Spec:
    parts = []
    pieces = [p for p in str(text).split(";") if p.strip()]
    if not pieces:
        raise AccuracyError("no specification")
    for k, piece in enumerate(pieces):
        head, colon, rest = piece.partition(":")
        if colon:
            m = re.match(rf"^{_NUM}\s*([pnuµμmkMG]?)$", head.strip())
            if not m:
                raise AccuracyError(f"“{head.strip()}” isn't the end of a range")
            upto = float(m.group(1)) * PREFIX[m.group(2)]
        elif k + 1 < len(pieces):
            raise AccuracyError("only the last part may be without a range")
        else:
            upto, rest = math.inf, piece
        if parts and upto <= parts[-1].upto:
            raise AccuracyError("the ranges must go up")
        parts.append(Part(upto, *_terms(rest, piece.strip())))
    return Spec(parts, str(text).strip())


def parse_lines(text: str) -> dict[str, str]:
    """'DC V = 0.0035% + 50u' a line -> {name: spec}; AccuracyError says which."""
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        name, eq, spec = line.partition("=")
        if not eq or not name.strip():
            raise AccuracyError(f"“{line.strip()}” isn't NAME = SPECIFICATION")
        try:
            parse(spec)
        except AccuracyError as e:
            raise AccuracyError(f"{name.strip()}: {e}") from None
        out[name.strip()] = spec.strip()
    return out


def lines(specs: dict) -> str:
    return "\n".join(f"{k} = {v}" for k, v in specs.items())


def pick(specs: dict, keys: list[str]) -> Spec | None:
    """The first of `keys` that has a usable specification."""
    if not specs:
        return None
    low = {str(k).strip().lower(): v for k, v in specs.items()}
    for k in keys:
        if k and k.lower() in low:
            try:
                return parse(low[k.lower()])
            except AccuracyError:
                return None              # a damaged entry: no ± rather than a wrong one
    return None


CHECK_S = 1.0                    # how often to look whether the log changed
_cache: tuple = (None, None)
_looked = -1e9
_found: dict = {}                # (identity, resource) -> its specifications


def forget():
    """The log was saved: look again."""
    global _looked
    _looked = -1e9


def from_log(idn: str, resource: str) -> dict:
    """The entry's specifications; the log is read again when it changes. Asked
    for with every reading, so it must be quick."""
    global _looked
    t = time.monotonic()
    if t - _looked >= CHECK_S:
        _looked = t
        _found.clear()
    key = (idn, resource)
    if key not in _found:
        _found[key] = _read(idn, resource)
    return _found[key]


def _read(idn: str, resource: str) -> dict:
    global _cache
    from .calibration import Log
    from .config import config_dir
    path = config_dir() / "calibration.json"
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _cache[0] != key:
        _cache = (key, Log())
    e = _cache[1].find(idn, resource)
    return e.accuracy if e is not None else {}


def marginal(v, lo, hi, u: float | None) -> bool:
    """Passes, but within ± of a limit."""
    if u is None or v is None or not math.isfinite(v):
        return False
    return (lo is not None and v - u < lo) or (hi is not None and v + u > hi)
