#!/usr/bin/env bash
# One-shot setup of OtterBench on a fresh Ubuntu machine (22.04 or later,
# desktop). Run from the repository as your normal user; it calls sudo where
# needed. Safe to re-run (e.g. after pulling a new version).
#
#   scripts/setup-ubuntu.sh               packages, Python venv, sigrok, GPIB
#                                         driver, application-menu entry
#   scripts/setup-ubuntu.sh --no-gpib     skip building the linux-gpib driver
#   scripts/setup-ubuntu.sh --no-sigrok   skip sigrok (cheap USB logic analyzers)
#   scripts/setup-ubuntu.sh --no-saleae   skip the Saleae Logic 2 client library
#   scripts/setup-ubuntu.sh --no-pdf      skip WeasyPrint (PDF test reports; HTML still works)
#   scripts/setup-ubuntu.sh --docs        also install the manual's build tools
#   scripts/setup-ubuntu.sh --dry-run     only print what it would do
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
GPIB=1 SIGROK=1 SALEAE=1 PDF=1 DOCS=0 DRY=0
for a in "$@"; do
  case "$a" in
    --no-gpib) GPIB=0 ;;
    --no-sigrok) SIGROK=0 ;;
    --no-saleae) SALEAE=0 ;;
    --no-pdf) PDF=0 ;;
    --docs) DOCS=1 PDF=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
run() {                       # run a command, or just show it with --dry-run
  if (( DRY )); then printf '   $ %s\n' "$*"; else "$@"; fi
}
in_group() { id -nG "$USER" | grep -qw "$1"; }

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user (it uses sudo itself), not as root." >&2
  exit 1
fi
. /etc/os-release 2>/dev/null || true
if [[ "${ID:-}" != ubuntu && "${ID_LIKE:-}" != *ubuntu* ]]; then
  echo "Note: this is ${PRETTY_NAME:-not Ubuntu}; continuing anyway (any Debian-based"
  echo "      distribution with Qt 6 should work)."
fi
ARCH="$(uname -m)"
if [[ "$ARCH" != x86_64 ]] && (( SALEAE )); then
  echo "Note: Saleae's Logic 2 only runs on x86-64, so its client library is skipped here."
  SALEAE=0
fi

# ---------------------------------------------------------------- packages
step "Installing system packages (sudo)"
PKGS=(
  git
  python3-venv python3-pip
  python3-pyqt6 python3-numpy python3-serial  # the GUI toolkit and numerics, from apt
  pyqt6-dev-tools                             # PyQt6.uic, which pyqtgraph imports
  qt6-wayland libxcb-cursor0                  # Qt on Wayland and on X11 desktops
  fonts-dejavu-core
)
(( SIGROK )) && PKGS+=(
  sigrok-cli                # drives sigrok-supported logic analyzers
  sigrok-firmware-fx2lafw   # firmware for the common 24 MHz "Saleae clone" analyzers
)
(( PDF )) && PKGS+=(libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0)  # WeasyPrint
# GPIO through libgpiod: the Pi header, or USB GPIO adapters / SBCs running Ubuntu
PKGS+=(python3-libgpiod)
run sudo apt-get update
run sudo apt-get install -y "${PKGS[@]}"

step "USB permissions"
# Serial instruments (Nordic PPK2, RS-232 adapters) need 'dialout'; libsigrok's
# udev rules give USB logic analyzers to the desktop user and to 'plugdev'.
CHANGED=0
for g in dialout plugdev gpio; do
  if getent group "$g" >/dev/null && ! in_group "$g"; then
    run sudo usermod -aG "$g" "$USER"
    echo "Added $USER to '$g'."
    CHANGED=1
  fi
done
# USB-TMC instruments (generators, meters, loads, supplies, scopes) for
# pyvisa-py: Keysight/Agilent, Rigol, Siglent, Tektronix, Keithley. Members of 'plugdev' may open them.
TMC_RULES=/etc/udev/rules.d/60-otterbench-usbtmc.rules
TMC_TEXT='SUBSYSTEM=="usb", ATTR{idVendor}=="0957", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="2a8d", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="1ab1", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="f4ec", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="f4ed", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="0699", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="05e6", MODE="0660", GROUP="plugdev"'
# From before the program was renamed
if [[ -e /etc/udev/rules.d/60-ee-workbench-usbtmc.rules ]]; then
  run sudo rm -f /etc/udev/rules.d/60-ee-workbench-usbtmc.rules
fi
if (( DRY )); then
  echo "  (would write $TMC_RULES)"
else
  echo "$TMC_TEXT" | sudo tee "$TMC_RULES" >/dev/null
fi
run sudo udevadm control --reload-rules
run sudo udevadm trigger --subsystem-match=usb

# ---------------------------------------------------------------- python
step "Creating the Python environment (.venv)"
cd "$HERE"
# A .venv copied from another computer has the wrong CPU type and paths
# ("Exec format error" / "bad interpreter"): rebuild it if it wasn't made here.
WANT="$ARCH $(python3 -V 2>&1) $HERE"
if [[ -d .venv ]]; then
  STAMP="$(cat .venv/.built-for 2>/dev/null || true)"
  if ! .venv/bin/python -c 'import sys' >/dev/null 2>&1 || [[ -n "$STAMP" && "$STAMP" != "$WANT" ]]; then
    echo "Existing .venv was made on another machine or Python; recreating it."
    run rm -rf .venv
  fi
fi
if [[ ! -x .venv/bin/python ]]; then
  run python3 -m venv --system-site-packages .venv
fi
(( DRY )) || echo "$WANT" > .venv/.built-for
run .venv/bin/pip install --upgrade pip
run .venv/bin/pip install -r requirements.txt
(( SALEAE )) && run .venv/bin/pip install "logic2-automation>=1.0"
(( PDF )) && run .venv/bin/pip install weasyprint
(( DOCS )) && run .venv/bin/pip install markdown

step "Self-test (simulated instruments, no hardware needed)"
run .venv/bin/python -m pytest -q tests

# ---------------------------------------------------------------- GPIB
if (( GPIB )); then
  step "Building the GPIB driver (for the NI GPIB-USB-HS)"
  run "$HERE/scripts/install-gpib-driver.sh"
  CHANGED=1
fi

# ---------------------------------------------------------------- menu
step "Adding OtterBench to the application menu"
DESK=~/.local/share/applications/otterbench.desktop
run mkdir -p ~/.local/share/icons/hicolor/512x512/apps
run cp "$HERE/otterbench/gui/branding/logo.png" ~/.local/share/icons/hicolor/512x512/apps/otterbench.png
run mkdir -p ~/.local/share/applications
# The otterbench.desktop in the repo, with this checkout's path; windowed.
if (( DRY )); then
  printf '   $ write %s\n' "$DESK"
else
  sed "s|^Exec=.*|Exec=\"$HERE/run.sh\"|" "$HERE/otterbench.desktop" > "$DESK"
fi
run chmod +x "$HERE/run.sh"
# From before the program was renamed
run rm -f ~/.local/share/applications/ee-workbench.desktop \
  ~/.local/share/icons/hicolor/512x512/apps/ee-workbench.png

step "Done"
cat <<EOF

Next:
EOF
(( CHANGED )) && echo "  * Log out and back in (or reboot) so the new group memberships apply."
cat <<EOF
  * Start it from the application menu, or:   $HERE/run.sh
    Try it without hardware:                   $HERE/run.sh SIM::66321D SIM::PPK2
  * Plug USB logic analyzers in after this (or replug them) so the new
    permissions apply. sigrok ones appear as SIGROK::… in the resource list.
EOF
if (( SALEAE )); then
  cat <<EOF
  * Saleae Logic 8 / Pro 8 / Pro 16: install Logic 2 from saleae.com/downloads
    (an AppImage; keep it outside this folder), then start it with its
    automation server on:   ./Logic-2.*.AppImage --automation
    The analyzer then appears as SALEAE::… in OtterBench.
EOF
fi
