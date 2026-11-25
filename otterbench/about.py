"""The program's name, version and what it is made of (Qt-free).

The visible name lives here only. Before it was called OtterBench the program
was EE Workbench (ee-workbench): config.migrate_old_dir() brings its settings
over, and session files saved as .eewb still open.
"""
from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

APP_NAME = "OtterBench"
TAGLINE = "Control, log and automate a bench of test instruments"
VERSION = "0.9.0"
AUTHOR = "Daniel McCall"
COPYRIGHT = f"© 2026 {AUTHOR}"
LICENSE = "GNU General Public License, version 3"
LICENSE_URL = "https://www.gnu.org/licenses/gpl-3.0.html"
URL = "https://github.com/dcm271/otterbench"

ROOT = Path(__file__).resolve().parent.parent
BRANDING = Path(__file__).resolve().parent / "gui" / "branding"


def revision() -> str:
    """The git commit this copy is at, "+" when it has changes ("" unknown)."""
    if not (ROOT / ".git").exists():
        return ""                # pip-installed: not some other repository's commit
    try:
        def git(*a):
            return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True,
                                  timeout=2, check=True).stdout.strip()
        return git("rev-parse", "--short", "HEAD") + ("+" if git("status", "--porcelain") else "")
    except (OSError, subprocess.SubprocessError):
        return ""


def version_text() -> str:
    r = revision()
    return f"{VERSION} ({r})" if r else VERSION


def components() -> list[tuple[str, str]]:
    """(name, version) of what it is built on and runs on."""
    out = [("Python", platform.python_version())]
    try:
        from PyQt6.QtCore import PYQT_VERSION_STR, qVersion
        out += [("Qt", qVersion() or "?"), ("PyQt6", PYQT_VERSION_STR)]
    except ImportError:
        pass
    for label, mod in (("NumPy", "numpy"), ("pyqtgraph", "pyqtgraph"), ("PyVISA", "pyvisa"),
                       ("PyVISA-py", "pyvisa_py"), ("pySerial", "serial")):
        m = sys.modules.get(mod)
        if m is None:
            try:
                m = __import__(mod)
            except Exception:         # noqa: BLE001 - not installed, or broken
                continue
        out.append((label, str(getattr(m, "__version__", getattr(m, "VERSION", "?")))))
    out.append(("System", f"{platform.system()} {platform.release()} {platform.machine()}"))
    return out


def details() -> str:
    """Plain text to paste into a bug report. No personal details."""
    from .config import config_dir
    from .crash import log_path
    out = [f"{APP_NAME} {version_text()}"] + [f"{n} {v}" for n, v in components()]
    home = str(Path.home())
    out += [f"Config {config_dir()}".replace(home, "~"),
            f"Crash log {log_path()}".replace(home, "~")]
    return "\n".join(out)
