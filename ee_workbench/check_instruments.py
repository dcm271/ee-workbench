"""Check instrument definition files (generic SCPI meters, loads, supplies).

    python -m ee_workbench.check_instruments              every definition in use
    python -m ee_workbench.check_instruments my.toml …    just these files

Each file is parsed (the same checks as at start-up), then run against its own
simulator: every setting is set and read back and every reading polled. This
shows mistakes in the file (a `reply` regex that doesn't match its own `set`,
a reading that names a missing setting); it can't show a command the real
instrument doesn't understand: try that in the tab's Console.
Exit status: 0 if all are fine, 1 otherwise.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from .instruments.scpi_generic import (PROBLEMS, DefinitionError, definitions,
                                       describe_definition, parse_definition, self_test,
                                       user_dir)


def _report(d, where: str) -> bool:
    probs = self_test(d)
    print(f"{'ok  ' if not probs else 'FAIL'} {d.model_key:<18} {where}")
    print(f"     {describe_definition(d)}")
    if not d.idn:
        print("     note: no `idn` list, so Auto-detect won't pick it: choose it by hand")
    if not d.has_outputs and d.type in ("supply", "load", "source"):
        print("     warning: no `off` commands and no `output = true` setting, so "
              "All outputs OFF (Esc) can't switch it off")
    for p in probs:
        print(f"     problem: {p}")
    return not probs


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    ok = True
    if not args:
        print(f"Your definitions folder: {user_dir()}")
        for d in sorted(definitions(reload=True).values(), key=lambda d: d.id):
            ok &= _report(d, "built-in" if d.builtin else d.path)
        for p in PROBLEMS:
            print(f"FAIL {p}")
            ok = False
        return 0 if ok else 1
    for a in args:
        try:
            d = parse_definition(tomllib.loads(Path(a).read_text()), a, False)
        except (OSError, tomllib.TOMLDecodeError, DefinitionError, TypeError, ValueError) as e:
            print(f"FAIL {a}\n     {e}")
            ok = False
            continue
        ok &= _report(d, a)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
