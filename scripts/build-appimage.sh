#!/usr/bin/env bash
# Build an AppImage: one file with Python, Qt and OtterBench in it.
#
#   scripts/build-appimage.sh [output folder]        (default: dist)
#
# Downloads a portable Python (python-appimage) and the packages from PyPI, so
# it needs the network. It builds for the machine it runs on (x86_64 or
# aarch64, e.g. a 64-bit Raspberry Pi).
#
# Not inside, because they belong to the system: the linux-gpib kernel driver
# and libgpib, sigrok-cli, Saleae's Logic 2, python3-libgpiod, and the groups
# and udev rules for USB (scripts/setup-ubuntu.sh sets all of that up).
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mkdir -p "${1:-$HERE/dist}" && cd "${1:-$HERE/dist}" && pwd)"
PYVER="${PYVER:-3.12}"
ARCH="$(uname -m)"
case "$ARCH" in x86_64|aarch64) ;; *) echo "no AppImage for $ARCH" >&2; exit 1 ;; esac
TAG="${LINUX_TAG:-manylinux_2_28_$ARCH}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" -q install build "python-appimage==1.4.7"

echo "==> wheel"
mkdir "$WORK/src"
cp -r "$HERE/otterbench" "$HERE/pyproject.toml" "$HERE/README.md" "$HERE/LICENSE" "$WORK/src/"
(cd "$WORK/src" && "$WORK/venv/bin/python" -m build -q --wheel -o "$WORK/wheel")
WHEEL="$(ls "$WORK"/wheel/*.whl)"
VERSION="$(basename "$WHEEL" | cut -d- -f2)"

echo "==> application folder"
APP="$WORK/otterbench"
mkdir "$APP"
cp "$HERE/otterbench/gui/branding/logo.png" "$APP/otterbench.png"
sed 's/^Exec=.*/Exec=otterbench/; /^#/d' "$HERE/otterbench.desktop" > "$APP/otterbench.desktop"
# Saleae's client library exists for x86_64 only
EXTRA=""; [[ "$ARCH" == x86_64 ]] && EXTRA="[saleae]"
echo "$WHEEL$EXTRA" > "$APP/requirements.txt"
cat > "$APP/entrypoint.sh" <<'EOF'
#! /bin/bash
# `OtterBench.AppImage replay file.tape` / `check-instruments [file.toml]` run the tools
case "${1:-}" in
  replay) shift; set -- -m otterbench.replay "$@" ;;
  check-instruments) shift; set -- -m otterbench.check_instruments "$@" ;;
  compare) shift; set -- -m otterbench.compare "$@" ;;
  *) set -- -m otterbench "$@" ;;
esac
exec {{ python-executable }} -s "$@"
EOF

echo "==> AppImage (Python $PYVER, $TAG)"
cd "$WORK"
"$WORK/venv/bin/python" -m python_appimage build app -l "$TAG" -p "$PYVER" -n OtterBench "$APP"
IMG="$(ls "$WORK"/OtterBench*.AppImage)"
DEST="$OUT/OtterBench-$VERSION-$ARCH.AppImage"
mv "$IMG" "$DEST"
chmod +x "$DEST"
echo "==> $DEST ($(du -h "$DEST" | cut -f1))"
