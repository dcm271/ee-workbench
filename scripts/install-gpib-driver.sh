#!/usr/bin/env bash
# Build and install linux-gpib for the NI GPIB-USB-HS on this machine.
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

step "Installing build dependencies (sudo)"
sudo apt-get install -y build-essential autoconf automake libtool flex bison \
  pkg-config "linux-headers-$KVER"

step "Building kernel modules for $KVER"
make -C "$SRC/linux-gpib-kernel" -j"$(nproc)"

step "Installing kernel modules (sudo)"
sudo make -C "$SRC/linux-gpib-kernel" install

step "Building libgpib + gpib_config + udev rules"
cd "$SRC/linux-gpib-user"
[[ -x configure ]] || ./bootstrap
./configure --prefix=/usr/local --sysconfdir=/etc \
  --disable-all-bindings --disable-documentation --disable-html-docs --disable-manpages
make -j"$(nproc)"

step "Installing userspace (sudo)"
sudo make install
sudo ldconfig

step "Writing /etc/gpib.conf for the NI GPIB-USB-HS"
if [[ -f /etc/gpib.conf ]]; then
  sudo cp /etc/gpib.conf "/etc/gpib.conf.bak.$(date +%Y%m%d%H%M%S)"
fi
sudo tee /etc/gpib.conf >/dev/null <<'EOF'
/* Minimal config: one NI GPIB-USB-HS as system controller.
   The 66321B is addressed by the application (default GPIB address 5). */
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
  * Launch the GUI:   $HERE/run.sh GPIB0::5::INSTR
EOF
