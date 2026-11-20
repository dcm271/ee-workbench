#!/usr/bin/env bash
# Build and install linux-gpib for the NI GPIB-USB-HS on this machine
# (Ubuntu or Raspberry Pi OS).
#
# Ubuntu's kernel is built with CONFIG_GPIB disabled, so the ni_usb_gpib driver
# has to be compiled out-of-tree from vendor/linux-gpib (SourceForge git).
# Run as your normal user; it calls sudo only for the install steps.
#
# Re-run after every kernel upgrade (the module is tied to `uname -r`).
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/vendor/linux-gpib"
KVER="$(uname -r)"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

[[ -d "$SRC/linux-gpib-kernel" ]] || {
  step "Fetching linux-gpib source"
  git clone --depth 1 https://git.code.sf.net/p/linux-gpib/git "$SRC"
}

# A build left over from another machine (e.g. the folder was copied from an
# x86 PC to a Pi) looks "up to date" to make, which would install binaries
# that fail with "Exec format error". Start clean whenever the CPU or kernel
# differs from the last build here.
STAMP="$SRC/.built-for"
WANT="$(uname -m) $KVER"
if [[ "$(cat "$STAMP" 2>/dev/null)" != "$WANT" ]]; then
  step "Cleaning previous build output (built for: $(cat "$STAMP" 2>/dev/null || echo unknown))"
  if [[ -d "$SRC/.git" ]]; then
    git -C "$SRC" clean -xdfq
  else
    make -C "$SRC/linux-gpib-kernel" clean >/dev/null 2>&1 || true
    (cd "$SRC/linux-gpib-user" && { make distclean >/dev/null 2>&1 || true; })
  fi
fi

step "Installing build dependencies (sudo)"
sudo apt-get install -y build-essential autoconf automake libtool flex bison pkg-config
# Ubuntu and Raspberry Pi OS Bookworm both ship linux-headers-<uname -r>;
# older Pi OS releases used raspberrypi-kernel-headers instead.
if ! sudo apt-get install -y "linux-headers-$KVER"; then
  sudo apt-get install -y raspberrypi-kernel-headers
fi

# Newer kernels can carry the GPIB drivers themselves; only build when the
# running kernel has no ni_usb_gpib module (rebuild with FORCE_KERNEL_BUILD=1).
if modinfo ni_usb_gpib >/dev/null 2>&1 && [[ -z "${FORCE_KERNEL_BUILD:-}" ]]; then
  step "Kernel already provides ni_usb_gpib ($(modinfo -n ni_usb_gpib)); skipping module build"
else
  step "Building kernel modules for $KVER"
  make -C "$SRC/linux-gpib-kernel" -j"$(nproc)"

  step "Installing kernel modules (sudo)"
  sudo make -C "$SRC/linux-gpib-kernel" install
fi

step "Building libgpib + gpib_config + udev rules"
cd "$SRC/linux-gpib-user"
[[ -x configure ]] || ./bootstrap
./configure --prefix=/usr/local --sysconfdir=/etc \
  --disable-all-bindings --disable-documentation --disable-html-docs --disable-manpages
make -j"$(nproc)"

step "Installing userspace (sudo)"
sudo make install
sudo ldconfig

echo "$WANT" > "$STAMP"

step "Writing /etc/gpib.conf for the NI GPIB-USB-HS"
if [[ -f /etc/gpib.conf ]]; then
  sudo cp /etc/gpib.conf "/etc/gpib.conf.bak.$(date +%Y%m%d%H%M%S)"
fi
sudo tee /etc/gpib.conf >/dev/null <<'EOF'
/* Minimal config: one NI GPIB-USB-HS as system controller.
   Instruments are addressed by the application (e.g. GPIB0::5::INSTR). */
interface {
    minor       = 0
    board_type  = "ni_usb_b"
    name        = "gpib0"
    pad         = 0
    sad         = 0
    timeout     = T3s
    eos         = 0x0a
    set-reos    = no
    set-bin     = no
    master      = yes
}
EOF

step "Granting $USER access to /dev/gpib* (group 'gpib')"
getent group gpib >/dev/null || sudo groupadd --system gpib
sudo usermod -aG gpib "$USER"

step "Loading driver and configuring board"
sudo udevadm control --reload-rules
sudo modprobe ni_usb_gpib
sleep 2
# udev runs gpib_config on hot-plug; do it explicitly too in case the
# adapter was already plugged in before the rules existed.
sudo /usr/local/sbin/gpib_config --minor 0 || true

step "Done"
ls -l /dev/gpib0 2>/dev/null || echo "!! /dev/gpib0 missing — unplug/replug the adapter, then check: dmesg | tail -30"
cat <<EOF

Next:
  * Log out and back in (or run 'newgrp gpib') so the new group applies.
  * Quick bus test:   ibtest   (choose d, address 5, then write '*IDN?' and read)
  * Launch the GUI:   $HERE/run.sh
EOF
