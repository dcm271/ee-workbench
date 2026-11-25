"""Play tapes of hardware checks back (tape.py, hwcheck.py).

    python -m otterbench.replay file.tape …

Each check runs again with the driver talking to the tape. Any step that comes
out differently from how it did on the hardware is listed. Exit status: 0 if
all are the same, 1 otherwise.
"""
from __future__ import annotations

import sys

from . import hwcheck, tape
from .about import safe_output


def main(argv=None) -> int:
    safe_output()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 2
    ok = True
    for path in args:
        try:
            tp = tape.load(path)
            r = hwcheck.replay(path, tp)
        except Exception as e:      # noqa: BLE001 - one bad tape doesn't hide the others
            print(f"FAIL {path}\n     {e}")
            ok = False
            continue
        print(f"{'FAIL' if r.problems else 'ok  '} {path}")
        print(f"     {tp.idn or tp.model} on {tp.link}, {tp.date}: {hwcheck.counts(r.rows)}")
        for p in r.problems:
            print(f"     changed: {p}")
        for u in dict.fromkeys(r.unknown):
            print(f"     not on the tape: {u}")
        ok &= not r.problems
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
