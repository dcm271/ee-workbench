#!/usr/bin/env bash
# One-shot setup of EE Workbench on a Raspberry Pi (Raspberry Pi OS Bookworm or
# later, 64-bit, desktop edition). Run from the repository as your normal user;
# it calls sudo where needed. Safe to re-run.
#
#   scripts/setup-pi.sh                 everything: packages, venv, GPIB driver,
#                                       menu entry and full-screen autostart
#   scripts/setup-pi.sh --no-gpib       skip the linux-gpib driver build
#   scripts/setup-pi.sh --no-autostart  don't launch at login
#   scripts/setup-pi.sh --docs          also install the PDF tool-chain
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
GPIB=1 AUTOSTART=1 DOCS=0
for a in "$@"; do
  case "$a" in
    --no-gpib) GPIB=0 ;;
    --no-autostart) AUTOSTART=0 ;;
    --docs) DOCS=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

if ! grep -qi 'raspberry' /proc/device-tree/model 2>/dev/null; then
  echo "Note: this does not look like a Raspberry Pi; continuing anyway."
fi

step "Installing system packages (sudo)"
sudo apt-get update
# PyQt6 and numpy come from apt: building them with pip on a Pi takes hours.
# qt6-wayland lets Qt run natively on the Bookworm Wayland desktop.
sudo apt-get install -y git python3-venv python3-pip python3-pyqt6 python3-numpy \
  python3-serial qt6-wayland fonts-dejavu-core
if (( DOCS )); then
  sudo apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0
fi

step "Creating the Python environment (.venv)"
cd "$HERE"
[[ -x .venv/bin/python ]] || python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
# pyvisa is pulled in by pyvisa-py.
.venv/bin/pip install pyqtgraph pyvisa-py gpib-ctypes pytest
if (( DOCS )); then
  .venv/bin/pip install markdown weasyprint
fi

step "Self-test (simulated instruments, no hardware needed)"
.venv/bin/python -m pytest -q tests

if (( GPIB )); then
  step "Building the GPIB driver"
  "$HERE/scripts/install-gpib-driver.sh"
fi

step "Adding EE Workbench to the application menu"
mkdir -p ~/.local/share/applications
DESK=~/.local/share/applications/ee-workbench.desktop
# Same as the ee-workbench.desktop in the repo, with this checkout's real path.
sed "s|^Exec=.*|Exec=\"$HERE/run.sh\" --fullscreen|" "$HERE/ee-workbench.desktop" > "$DESK"
chmod +x "$HERE/run.sh"

if (( AUTOSTART )); then
  step "Starting full screen at login"
  mkdir -p ~/.config/autostart
  cp "$DESK" ~/.config/autostart/ee-workbench.desktop
  echo "Remove ~/.config/autostart/ee-workbench.desktop to stop this."
fi

step "Done"
cat <<EOF

Next:
  * Reboot (or log out and in) so the 'gpib' group membership applies.
  * EE Workbench then starts full screen. F11 leaves full screen; the ✕ button
    at the top right quits. Esc turns every supply output off.
  * To run it by hand:   $HERE/run.sh            (windowed)
                         $HERE/run.sh --fullscreen
  * Optional: disable screen blanking in  sudo raspi-config  →  Display Options.
EOF
