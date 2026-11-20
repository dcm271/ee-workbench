#!/usr/bin/env bash
# Prepare a kernel tree to build out-of-tree modules against, for kernels that
# ship without a headers package (e.g. EDATec's 6.6.31-rpi-2712 on their
# Raspberry Pi 5 panel PCs).
#
#   scripts/prepare-kernel-tree.sh [DEST]          prints the tree's path when done
#   scripts/prepare-kernel-tree.sh --add-symbols DEST SYMBOL...
#                                     compute CRCs for symbols no module uses
#
# How: download the matching kernel source, configure it with the running
# kernel's own /boot/config-$(uname -r), run `make modules_prepare`, and build a
# Module.symvers whose symbol CRCs are read from the modules already installed
# for this kernel (so CONFIG_MODVERSIONS checks pass). Symbols that no installed
# module uses get their CRCs computed from the source; the computation is
# checked against the harvested CRCs first and refused if they disagree.
#
# Source used: KERNEL_SRC_URL if set, else a known Raspberry Pi tag for the
# version, else the kernel.org release of the same version.
set -euo pipefail
export PATH="$PATH:/usr/sbin:/sbin"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*" >&2; }
die() { printf '\n\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == "--add-symbols" ]]; then
  DEST="$2"; shift 2
  cd "$DEST"
  step "Computing CRCs for: $*"
  : > .computed-crcs
  for sym in "$@"; do
    grep -q "	$sym	" Module.symvers && continue
    src="$(grep -rlE --include='*.c' "EXPORT_SYMBOL(_GPL)?(_NS(_GPL)?)?\($sym[,)]" \
             kernel mm lib drivers fs block net arch/arm64 2>/dev/null | head -1)"
    [[ -n "$src" ]] || die "Can't find where $sym is exported."
    obj="${src%.c}.o"
    make -s "$obj" >&2
    grep -h "^#SYMVER " "$(dirname "$obj")/.$(basename "$obj").cmd" >> .computed-crcs
    grep -q "^#SYMVER $sym " .computed-crcs || die "No CRC for $sym in $obj."
  done
  # Trust the computation only if it reproduces every CRC we already know.
  bad=0; known=0
  while read -r _ sym crc; do
    h="$(awk -v s="$sym" '$2==s{print $1}' .harvested-crcs)"
    [[ -z "$h" ]] && continue
    known=$((known + 1))
    [[ "$h" == "$crc" ]] || { echo "  CRC mismatch for $sym" >&2; bad=1; }
  done < .computed-crcs
  ((known > 0)) || die "No known CRCs to check the computation against."
  ((bad == 0)) || die "Computed CRCs disagree with the running kernel's; not using them."
  echo "  computation reproduces all $known known CRCs" >&2
  for sym in "$@"; do
    grep -q "	$sym	" Module.symvers ||
      awk -v s="$sym" '$2==s{printf "%s\t%s\tvmlinux\tEXPORT_SYMBOL\t\n", $3, $2; exit}' \
        .computed-crcs >> Module.symvers
  done
  exit 0
fi

KVER="$(uname -r)"
DEST="${1:-$HOME/kbuild/linux-$KVER}"
CONFIG="/boot/config-$KVER"
MODDIR="/lib/modules/$KVER"
VER="${KVER%%-*}"; VER="${VER%%+*}"          # 6.6.31-rpi-2712 -> 6.6.31

[[ -f "$CONFIG" ]] || die "No $CONFIG: can't tell how this kernel was configured."

# Raspberry Pi kernel tags (github.com/raspberrypi/linux) by kernel version.
# Add more as needed; the version check below catches a wrong one.
declare -A RPI_TAGS=([6.6.31]=stable_20240529)
if [[ -n "${KERNEL_SRC_URL:-}" ]]; then
  URL="$KERNEL_SRC_URL"
elif [[ "$KVER" == *rpi* && -n "${RPI_TAGS[$VER]:-}" ]]; then
  URL="https://github.com/raspberrypi/linux/archive/refs/tags/${RPI_TAGS[$VER]}.tar.gz"
else
  URL="https://cdn.kernel.org/pub/linux/kernel/v${VER%%.*}.x/linux-$VER.tar.xz"
fi

if [[ ! -f "$DEST/.prepared-for" || "$(cat "$DEST/.prepared-for")" != "$KVER" ]]; then
  step "Installing kernel build tools (sudo)"
  sudo apt-get install -y -qq bc flex bison libssl-dev libelf-dev kmod curl >/dev/null

  step "Downloading kernel source $VER ($URL)"
  rm -rf "$DEST"; mkdir -p "$DEST"
  curl -fL --progress-bar "$URL" | tar -x --strip-components=1 -C "$DEST" \
    $([[ "$URL" == *.xz ]] && echo -J || echo -z)
  got="$(make -s -C "$DEST" kernelversion)"
  [[ "$got" == "$VER" ]] || die "Downloaded source is $got, the running kernel is $VER. Set KERNEL_SRC_URL."

  step "Configuring with $CONFIG"
  cp "$CONFIG" "$DEST/.config"
  make -s -C "$DEST" olddefconfig
  make -s -C "$DEST" -j"$(nproc)" modules_prepare
  rel="$(cat "$DEST/include/config/kernel.release")"
  [[ "$rel" == "$KVER" ]] || die "Prepared tree is $rel, the running kernel is $KVER."
fi

if grep -q '^CONFIG_MODVERSIONS=y' "$DEST/.config"; then
  step "Reading symbol CRCs from the installed modules"
  # (not linux-gpib's own modules from an earlier install: their symbols
  # would look like kernel exports and clash with the new build)
  find "$MODDIR" -path "$MODDIR/gpib" -prune -o -name '*.ko*' -print0 |
    xargs -0 -n 50 -P "$(nproc)" sh -c 'for f; do modprobe --dump-modversions "$f" 2>/dev/null; done' _ |
    awk '{print $1, $2}' | sort -u -k2,2 > "$DEST/.harvested-crcs"
  echo "  $(wc -l < "$DEST/.harvested-crcs") symbols" >&2
  awk '{printf "%s\t%s\tvmlinux\tEXPORT_SYMBOL\t\n", $1, $2}' "$DEST/.harvested-crcs" \
    > "$DEST/Module.symvers"
else
  : > "$DEST/Module.symvers"
fi
echo "$KVER" > "$DEST/.prepared-for"
echo "$DEST"
