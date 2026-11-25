#!/usr/bin/env bash
# Build a .deb for Ubuntu 24.04 or later, Debian 12 or later and Raspberry Pi OS
# (bookworm or later). One package for every CPU type: it is all Python, and Qt
# and the rest come from the distribution.
#
#   scripts/build-deb.sh [output folder]        (default: dist)
#   sudo apt install ./dist/otterbench_0.9.0_all.deb
#
# The GPIB driver is a package of its own: scripts/build-gpib-deb.sh.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mkdir -p "${1:-$HERE/dist}" && cd "${1:-$HERE/dist}" && pwd)"
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$HERE/otterbench/about.py")"
[[ -n "$VERSION" ]] || { echo "no VERSION in otterbench/about.py" >&2; exit 1; }
ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
chmod 755 "$ROOT"
umask 022

# ---- the program
APP="$ROOT/usr/share/otterbench"
install -d "$APP" "$ROOT/usr/bin" "$ROOT/DEBIAN" "$ROOT/usr/share/doc/otterbench"
cp -r "$HERE/otterbench" "$APP/"
find "$APP" -name __pycache__ -prune -exec rm -rf {} +
cp -r "$HERE/docs/pdf/OtterBench-Manual.pdf" "$ROOT/usr/share/doc/otterbench/" 2>/dev/null || true

# the same launcher as run.sh (--restart-on-crash included), with the system's Python
sed -e 's|^cd "$(dirname "$0")" .*|export PYTHONPATH="/usr/share/otterbench${PYTHONPATH:+:$PYTHONPATH}" PYTHONDONTWRITEBYTECODE=1|' \
    -e 's|\.venv/bin/python|/usr/bin/python3|g' -e 's|\./run\.sh|otterbench|g' \
    -e 's|run\.sh|otterbench|g' "$HERE/run.sh" > "$ROOT/usr/bin/otterbench"
grep -q '^export PYTHONPATH' "$ROOT/usr/bin/otterbench"
if grep -q '\.venv' "$ROOT/usr/bin/otterbench"; then echo "launcher not adapted" >&2; exit 1; fi
for tool in check_instruments replay compare; do
  [[ -f "$HERE/otterbench/$tool.py" ]] || continue
  name="otterbench-${tool//_/-}"
  printf '#!/bin/sh\nexport PYTHONPATH="/usr/share/otterbench" PYTHONDONTWRITEBYTECODE=1\nexec /usr/bin/python3 -m otterbench.%s "$@"\n' \
      "$tool" > "$ROOT/usr/bin/$name"
done
chmod 755 "$ROOT"/usr/bin/*

# ---- menu entry and icons
install -d "$ROOT/usr/share/applications" "$ROOT/usr/share/icons/hicolor/scalable/apps" \
           "$ROOT/usr/share/pixmaps"
sed 's/^Exec=.*/Exec=otterbench/; /^#/d' "$HERE/otterbench.desktop" \
    > "$ROOT/usr/share/applications/otterbench.desktop"
install -m 644 "$HERE/otterbench/gui/branding/logo.svg" \
    "$ROOT/usr/share/icons/hicolor/scalable/apps/otterbench.svg"
install -m 644 "$HERE/otterbench/gui/branding/logo.png" "$ROOT/usr/share/pixmaps/otterbench.png"

# ---- USB permissions: whoever sits at the screen (uaccess) and the groups
# that already stand for this (plugdev, dialout)
install -d "$ROOT/usr/lib/udev/rules.d"
cat > "$ROOT/usr/lib/udev/rules.d/60-otterbench.rules" <<'EOF'
# USB-TMC instruments (meters, supplies, loads, scopes, generators)
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ENV{ID_USB_INTERFACES}=="*:fe03*", MODE="0660", GROUP="plugdev", TAG+="uaccess"
KERNEL=="usbtmc*", MODE="0660", GROUP="plugdev", TAG+="uaccess"
# Serial ports: Nordic PPK2, FNIRSI DPS-150, and the USB-serial chips in other
# instruments (CH340, CP210x, FTDI, Prolific)
SUBSYSTEM=="tty", ATTRS{idVendor}=="1915", ATTRS{idProduct}=="c00a", TAG+="uaccess"
SUBSYSTEM=="tty", ATTRS{idVendor}=="2e3c", ATTRS{idProduct}=="5740", TAG+="uaccess"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", TAG+="uaccess"
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", TAG+="uaccess"
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", TAG+="uaccess"
SUBSYSTEM=="tty", ATTRS{idVendor}=="067b", TAG+="uaccess"
EOF

# ---- package description
cat > "$ROOT/usr/share/doc/otterbench/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: OtterBench
Source: https://github.com/dcm271/otterbench

Files: *
Copyright: 2026 Daniel McCall
License: GPL-3.0-only
 On Debian systems the full text is in /usr/share/common-licenses/GPL-3.
EOF
SIZE="$(du -sk "$ROOT/usr" | cut -f1)"
cat > "$ROOT/DEBIAN/control" <<EOF
Package: otterbench
Version: $VERSION
Section: electronics
Priority: optional
Architecture: all
Installed-Size: $SIZE
Maintainer: OtterBench <https://github.com/dcm271/otterbench>
Homepage: https://github.com/dcm271/otterbench
Depends: python3 (>= 3.11), python3-pyqt6, python3-pyqt6.qtsvg, python3-numpy, python3-pyqtgraph, python3-pyvisa, python3-pyvisa-py, python3-serial, python3-usb
Recommends: fonts-dejavu-core, qt6-wayland, libxcb-cursor0, python3-libgpiod, sigrok-cli, sigrok-firmware-fx2lafw, weasyprint
Suggests: otterbench-gpib-dkms, qt6-svg-plugins
Description: control, log and automate a bench of test instruments
 Power supplies, multimeters, electronic loads, oscilloscopes, function
 generators, logic analyzers, the Nordic PPK2 and the Raspberry Pi's GPIO in
 one window, with a sequencer, limits, reports, sessions and a watchdog.
 .
 Every instrument has a simulator. GPIB needs otterbench-gpib-dkms as well.
EOF
cat > "$ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules || true
    udevadm trigger --subsystem-match=usb --subsystem-match=tty || true
fi
EOF
cat > "$ROOT/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules || true
fi
EOF
chmod 755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/postrm"
(cd "$ROOT" && find usr -type f -print0 | sort -z | xargs -0 md5sum > DEBIAN/md5sums)

DEB="$OUT/otterbench_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$ROOT" "$DEB"
echo "==> $DEB"
