"""Files are UTF-8 whatever the locale: a Pi set to a Latin-1 one (en_GB without
.UTF-8) garbled the units and couldn't write reports."""
import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from otterbench.config import load_config
from otterbench.profile import read_csv

ROOT = Path(__file__).resolve().parent.parent


def _unspecified(src: str) -> list[int]:
    """Lines that open a text file, or read a program's text, in the locale's encoding."""
    out = []
    for c in ast.walk(ast.parse(src)):
        if not isinstance(c, ast.Call) or any(k.arg == "encoding" for k in c.keywords):
            continue
        f = c.func
        name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
        mode = c.args[1] if len(c.args) > 1 else next(
            (k.value for k in c.keywords if k.arg == "mode"), None)
        binary = isinstance(mode, ast.Constant) and "b" in str(mode.value)
        text = any(k.arg in ("text", "universal_newlines") and getattr(k.value, "value", 0) is True
                   for k in c.keywords)
        if (name in ("read_text", "write_text") or text
                or (not binary and (name == "fdopen" or
                                    (name == "open" and isinstance(f, ast.Name))))):
            out.append(c.lineno)
    return sorted(out)


def test_the_check_itself():
    assert _unspecified('open(p)\nopen(p, "rb")\np.read_text()\nrun(a, text=True)') == [1, 3, 4]
    assert not _unspecified('open(p, encoding="utf-8")\np.write_text(s, encoding="utf-8")')


def test_no_file_depends_on_the_locale():
    bad = [f"{p.relative_to(ROOT)}:{n}" for p in sorted((ROOT / "otterbench").rglob("*.py"))
           for n in _unspecified(p.read_text(encoding="utf-8"))]
    assert not bad, 'encoding="utf-8" is missing: ' + ", ".join(bad)


def test_config_that_is_not_utf8_is_reported(tmp_path):
    p = tmp_path / "config.toml"
    p.write_bytes("[startup]\n# 25 °C\n".encode("latin-1"))
    assert "Using defaults" in load_config(p).warning


def test_profile_with_a_latin1_header(tmp_path):
    p = tmp_path / "p.csv"
    p.write_bytes("time_s,T (°C)\n0,1\n1,2\n".encode("latin-1"))
    assert list(read_csv(str(p))[1]) == pytest.approx([1, 2])


@pytest.mark.parametrize("tool", ["check_instruments", "replay", "compare"])
def test_tools_print_on_a_latin1_terminal(tool):
    p = subprocess.run([sys.executable, "-m", f"otterbench.{tool}", "--help"], cwd=ROOT,
                       env={**os.environ, "PYTHONIOENCODING": "latin-1"}, capture_output=True)
    assert p.returncode == 0, p.stderr.decode("latin-1")
