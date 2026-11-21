#!/usr/bin/env bash
# One-shot setup of EE Workbench on a Raspberry Pi (Raspberry Pi OS Bookworm or
# later, 64-bit, desktop edition). Run from the repository as your normal user;
# it calls sudo where needed. Safe to re-run.
#
#   scripts/setup-pi.sh                 everything: packages, venv, GPIB driver,
#                                       menu entry and full-screen autostart
#   scripts/setup-pi.sh --no-gpib       skip the linux-gpib driver build
#   scripts/setup-pi.sh --no-autostart  don't launch at login
#   scripts/setup-pi.sh --no-pdf        skip WeasyPrint (PDF test reports; HTML still works)
#   scripts/setup-pi.sh --docs          also install the manual's build tools
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
GPIB=1 AUTOSTART=1 DOCS=0 PDF=1
for a in "$@"; do
  case "$a" in
    --no-gpib) GPIB=0 ;;
    --no-autostart) AUTOSTART=0 ;;
    --no-pdf) PDF=0 ;;
    --docs) DOCS=1 PDF=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
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
# sigrok-cli drives USB logic analyzers (Saleae's Logic 2 doesn't run on ARM).
# python3-libgpiod drives the GPIO header (relays, buttons, "done" pins).
sudo apt-get install -y git python3-venv python3-pip python3-pyqt6 python3-numpy \
  python3-serial qt6-wayland fonts-dejavu-core sigrok-cli sigrok-firmware-fx2lafw \
  python3-libgpiod
if (( PDF )); then
  # WeasyPrint (PDF reports) draws text with Pango
  sudo apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0
fi
# USB serial instruments (Nordic PPK2, RS-232 adapters) need the dialout group;
# libsigrok's udev rules give USB logic analyzers to plugdev; /dev/gpiochip*
# belongs to gpio.
for g in dialout plugdev gpio; do
  if getent group "$g" >/dev/null && ! id -nG "$USER" | grep -qw "$g"; then
    sudo usermod -aG "$g" "$USER"
    echo "Added $USER to '$g': log out and back in."
  fi
done
# USB-TMC instruments (function generators) for pyvisa-py: Keysight/Agilent,
# Rigol, Siglent, Tektronix. Members of 'plugdev' may open them.
TMC_RULES=/etc/udev/rules.d/60-ee-workbench-usbtmc.rules
TMC_TEXT='SUBSYSTEM=="usb", ATTR{idVendor}=="0957", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="2a8d", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="1ab1", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="f4ec", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="f4ed", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="0699", MODE="0660", GROUP="plugdev"'
echo "$TMC_TEXT" | sudo tee "$TMC_RULES" >/dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb

step "Creating the Python environment (.venv)"
cd "$HERE"
# A .venv copied from another computer has the wrong CPU type and paths
# ("Exec format error" / "bad interpreter"): rebuild it if it wasn't made here.
WANT="$(uname -m) $(python3 -V 2>&1) $HERE"
if [[ -d .venv ]]; then
  STAMP="$(cat .venv/.built-for 2>/dev/null || true)"
  # rebuild only a broken venv or one stamped for another machine (an
  # unstamped one that works is kept)
  if ! .venv/bin/python -c 'import sys' >/dev/null 2>&1 || [[ -n "$STAMP" && "$STAMP" != "$WANT" ]]; then
    echo "Existing .venv was made on another machine or Python; recreating it."
    rm -rf .venv
  fi
fi
[[ -x .venv/bin/python ]] || python3 -m venv --system-site-packages .venv
echo "$WANT" > .venv/.built-for
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
(( PDF )) && .venv/bin/pip install weasyprint
(( DOCS )) && .venv/bin/pip install markdown

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
  # At login it also restarts itself after a crash (see run.sh), so an
  # unattended bench screen doesn't stay on an empty desktop.
  sed "s|^Exec=.*|Exec=\"$HERE/run.sh\" --restart-on-crash --fullscreen|" "$HERE/ee-workbench.desktop" \
    > ~/.config/autostart/ee-workbench.desktop
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
