#!/usr/bin/env bash
# Build the GPIB packages:
#
#   otterbench-gpib-dkms_*_all.deb    linux-gpib's kernel driver as source. DKMS
#                                     compiles it when installed and for every
#                                     new kernel.
#   otterbench-libgpib_*_<cpu>.deb    libgpib and gpib_config, for distributions
#                                     without them (Ubuntu 26.04 has its own:
#                                     libgpib0, gpib-user-tools)
#
#   scripts/build-gpib-deb.sh [output folder]        (default: dist)
#   LIB_IMAGE=debian:12 scripts/build-gpib-deb.sh    library compiled in a Docker
#                                     image, so that it runs on older systems too
#   LIB=no …                          only the driver package
#
# Needs git and dpkg-deb; for the library also gcc, make, autoconf, automake,
# libtool, flex and bison (or Docker).
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mkdir -p "${1:-$HERE/dist}" && cd "${1:-$HERE/dist}" && pwd)"
# The linux-gpib that is packaged: fixed, so that two builds give the same.
COMMIT=e077ed19e2714678edf87c2662041dc9ea741ea5
VERSION="4.3.7+git20260915"
URL=https://git.code.sf.net/p/linux-gpib/git

NAME=otterbench-gpib                    # for DKMS
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
umask 022

GIT="$HERE/vendor/linux-gpib"
if ! git -C "$GIT" cat-file -e "$COMMIT^{commit}" 2>/dev/null; then
  GIT="$WORK/git"
  git clone -q "$URL" "$GIT"
fi
git -C "$GIT" archive "$COMMIT" linux-gpib-kernel linux-gpib-user | tar -x -C "$WORK"
K="$WORK/linux-gpib-kernel"

copyright() {
  cat <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: linux-gpib
Source: $URL
 commit $COMMIT

Files: *
Copyright: Frank Mori Hess and the other linux-gpib authors (see AUTHORS)
License: GPL-2.0-or-later
 On Debian systems the full text is in /usr/share/common-licenses/GPL-2.
EOF
}

pack() {          # root
  (cd "$1" && find usr etc -type f -print0 2>/dev/null | sort -z | xargs -0 md5sum > DEBIAN/md5sums)
  chmod 755 "$1"
  dpkg-deb --root-owner-group --build "$1" "$OUT" >/dev/null
}

# ================================================================ driver
ROOT="$WORK/dkms"
SRC="$ROOT/usr/src/$NAME-$VERSION"
LIBEXEC="$ROOT/usr/lib/$NAME"
DOC="$ROOT/usr/share/doc/otterbench-gpib-dkms"
install -d "$SRC" "$LIBEXEC" "$DOC" "$ROOT/DEBIAN" "$ROOT/usr/lib/udev/rules.d" \
           "$ROOT/etc/kernel/postinst.d" "$ROOT/etc/kernel/header_postinst.d"
cp -r "$K/." "$SRC/"

# USB adapters and the usual PCI cards. The ISA, PCMCIA and embedded ones are
# left out: each more is one more that can stop compiling with a new kernel.
DIRS="common ni_usb agilent_82357a lpvo_usb_gpib xyphro nec7210 tnt4882 tms9914 agilent_82350b"
MODULES="common/gpib_common ni_usb/ni_usb_gpib agilent_82357a/agilent_82357a
         lpvo_usb_gpib/lpvo_usb_gpib xyphro/xyphro_ugc nec7210/nec7210
         tnt4882/tnt4882 tms9914/tms9914 agilent_82350b/agilent_82350b"
{
  sed '/^obj-/d; /CONFIG_GPIB_ISA/,/^endif/d' "$K/drivers/gpib/Makefile"
  for d in $DIRS; do echo "obj-y += $d/"; done
} > "$SRC/drivers/gpib/Makefile"

# linux-gpib's Makefile looks for some headers under the running kernel, which
# is the wrong one while a new kernel is being installed.
cat > "$SRC/build-for.sh" <<'EOF'
#!/bin/sh
# build-for.sh KERNEL HEADERS: what DKMS calls
set -e
src="$2"
[ -f "$src/include/linux/device.h" ] || src="/lib/modules/$1/source"
[ -f "$src/include/linux/device.h" ] || src="$(sed -n 's|^include \(/.*\)/Makefile$|\1|p' "$2/Makefile" | head -n1)"
[ -f "$src/include/linux/device.h" ] || { echo "No complete kernel headers in $2" >&2; exit 1; }
exec make -j"$(nproc)" LINUX_SRCDIR="$2" LINUX_SSRCDIR="$src"
EOF
chmod 755 "$SRC/build-for.sh"
{
  echo "PACKAGE_NAME=\"$NAME\""
  echo "PACKAGE_VERSION=\"$VERSION\""
  echo "# Not DKMS's own hook: when a module doesn't compile, that makes the kernel's"
  echo "# installation fail. /etc/kernel/*postinst.d/$NAME does it, and only warns."
  echo 'AUTOINSTALL=""'
  echo '# A kernel with its own GPIB drivers needs none of this.'
  echo 'BUILD_EXCLUSIVE_CONFIG="CONFIG_USB CONFIG_PCI !CONFIG_GPIB"'
  echo 'MAKE="sh ./build-for.sh ${kernelver} ${kernel_source_dir}"'
  echo 'CLEAN="true"'
  i=0
  for m in $MODULES; do
    echo "BUILT_MODULE_NAME[$i]=\"${m#*/}\""
    echo "BUILT_MODULE_LOCATION[$i]=\"drivers/gpib/${m%/*}/\""
    echo "DEST_MODULE_LOCATION[$i]=\"/updates/dkms\""
    i=$((i + 1))
  done
} > "$SRC/dkms.conf"

cat > "$LIBEXEC/build-for" <<EOF
#!/bin/sh
# build-for KERNEL: compile and install the GPIB driver for one kernel. Says so
# when it can't, and never fails: a kernel must be installable without it.
K="\$1"
[ -n "\$K" ] && [ -d "/lib/modules/\$K" ] || exit 0
[ -x /usr/sbin/dkms ] && [ -d "/usr/src/$NAME-$VERSION" ] || exit 0
if [ ! -e "/lib/modules/\$K/build/Makefile" ]; then
    # (its headers may be about to be installed: their hook comes back here)
    [ "\$2" = quiet ] || echo "$NAME: no headers for kernel \$K (sudo apt install linux-headers-\$K)"
    exit 0
fi
case "\$(/usr/sbin/dkms status -m $NAME -v "$VERSION" -k "\$K" 2>/dev/null)" in
    *installed*) exit 0 ;;
esac
if grep -qs '^CONFIG_GPIB=[my]' "/lib/modules/\$K/build/.config" "/boot/config-\$K"; then
    echo "$NAME: kernel \$K has its own GPIB drivers"
    exit 0
fi
for c in USB PCI; do                    # e.g. a kernel for cloud machines
    grep -qs "^CONFIG_\$c=[my]" "/lib/modules/\$K/build/.config" "/boot/config-\$K" && continue
    echo "$NAME: kernel \$K has no \$c; no GPIB driver for it"
    exit 0
done
echo "$NAME: building the GPIB driver for kernel \$K"
LOG="\$(mktemp)"
if /usr/sbin/dkms install -m $NAME -v "$VERSION" -k "\$K" >"\$LOG" 2>&1 </dev/null; then
    rm -f "\$LOG"
    depmod -a "\$K" 2>/dev/null || true      # newer DKMS leaves that out during an installation
    exit 0
fi
tail -n 5 "\$LOG" >&2
rm -f "\$LOG"
echo "$NAME: !! THE GPIB DRIVER COULD NOT BE BUILT FOR KERNEL \$K." >&2
echo "$NAME: !! GPIB won't work with that kernel; everything else will. Details:" >&2
echo "$NAME: !! /var/lib/dkms/$NAME/$VERSION/build/make.log" >&2
exit 0
EOF
for d in postinst.d header_postinst.d; do
  printf '#!/bin/sh\n[ -x /usr/lib/%s/build-for ] || exit 0\nexec /usr/lib/%s/build-for "$1" quiet\n' \
    "$NAME" "$NAME" > "$ROOT/etc/kernel/$d/$NAME"
done
chmod 755 "$LIBEXEC/build-for" "$ROOT"/etc/kernel/*/"$NAME"
printf '/etc/kernel/postinst.d/%s\n/etc/kernel/header_postinst.d/%s\n' "$NAME" "$NAME" > "$ROOT/DEBIAN/conffiles"

cat > "$ROOT/usr/lib/udev/rules.d/60-$NAME.rules" <<'EOF'
# GPIB boards: whoever sits at the screen, and the group gpib
KERNEL=="gpib[0-9]*", MODE="0660", GROUP="gpib", TAG+="uaccess"
EOF

copyright > "$DOC/copyright"
cat > "$ROOT/DEBIAN/control" <<EOF
Package: otterbench-gpib-dkms
Version: $VERSION
Section: kernel
Priority: optional
Architecture: all
Installed-Size: $(du -sk "$ROOT/usr" | cut -f1)
Maintainer: OtterBench <https://github.com/dcm271/otterbench>
Homepage: https://linux-gpib.sourceforge.io
Depends: dkms, gcc, make, kmod, gpib-user-tools (>= 4.3.6) | otterbench-libgpib
Recommends: linux-headers-rpi-v8 | linux-headers-rpi-2712 | linux-headers-generic | linux-headers-amd64 | linux-headers-arm64 | linux-headers
Enhances: otterbench
Conflicts: linux-gpib-dkms, gpib-dkms
Description: GPIB (IEEE 488) kernel driver for OtterBench, through DKMS
 linux-gpib's driver for the NI GPIB-USB-HS, Agilent 82357 and other adapters,
 as source. It is compiled when installed and again for each new kernel. When
 that fails, it says so and the kernel is installed all the same.
EOF

cat > "$ROOT/DEBIAN/postinst" <<EOF
#!/bin/sh
set -e
[ "\$1" = configure ] || exit 0
getent group gpib >/dev/null 2>&1 || groupadd --system gpib || true
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules || true
fi
dkms status -m $NAME -v "$VERSION" 2>/dev/null | grep -q . || dkms add -m $NAME -v "$VERSION" >/dev/null || true

# the running kernel and the newest: what will be booted next
RUNNING="\$(uname -r)"
NEWEST="\$(ls -d /lib/modules/*/build 2>/dev/null | sed 's|/lib/modules/||; s|/build||' | sort -V | tail -n1)"
/usr/lib/$NAME/build-for "\$RUNNING"
[ -z "\$NEWEST" ] || [ "\$NEWEST" = "\$RUNNING" ] || /usr/lib/$NAME/build-for "\$NEWEST"

# An adapter that is plugged in already: load its driver (udev then sets the board up)
for f in /sys/bus/usb/devices/*/idVendor; do
    [ -e "\$f" ] || continue
    d="\${f%idVendor}"
    case "\$(cat "\$f"):\$(cat "\${d}idProduct")" in
        3923:709b|3923:7618|3923:702a|3923:702b|3923:725c|3923:725d) modprobe ni_usb_gpib 2>/dev/null || true ;;
        0957:0107|0957:0718) modprobe agilent_82357a 2>/dev/null || true ;;
    esac
done

if command -v mokutil >/dev/null 2>&1 && mokutil --sb-state 2>/dev/null | grep -q enabled; then
    echo "$NAME: Secure Boot is on. Unless DKMS's key is enrolled, the driver won't load:"
    echo "$NAME:   sudo mokutil --import /var/lib/shim-signed/mok/MOK.der   (Debian: /var/lib/dkms/mok.pub)"
    echo "$NAME: then reboot and choose \"Enroll MOK\"."
fi
if [ -e /usr/local/lib/libgpib.so.0 ]; then
    echo "$NAME: there is also a linux-gpib compiled by hand in /usr/local, which is used first."
    echo "$NAME: OtterBench's scripts/remove-local-gpib.sh takes it out."
fi
exit 0
EOF
cat > "$ROOT/DEBIAN/prerm" <<EOF
#!/bin/sh
set -e
case "\$1" in
    remove|upgrade|deconfigure)
        dkms remove -m $NAME -v "$VERSION" --all >/dev/null 2>&1 || true ;;
esac
exit 0
EOF
cat > "$ROOT/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules || true
fi
EOF
chmod 755 "$ROOT"/DEBIAN/{postinst,prerm,postrm}
pack "$ROOT"
echo "==> $OUT/otterbench-gpib-dkms_${VERSION}_all.deb"

[[ "${LIB:-yes}" != no ]] || exit 0

# ================================================================ library
U="$WORK/linux-gpib-user"
git -C "$GIT" archive "$COMMIT" linux-gpib-user | gzip -n > "$WORK/user-src.tar.gz"
cat > "$WORK/build-lib.sh" <<'EOF'
set -e
cd "$1"
{ ./bootstrap
  ./configure --prefix=/usr --sysconfdir=/etc --libdir=/usr/lib/lib-here \
      --disable-all-bindings --disable-documentation --disable-html-docs --disable-manpages
  make -j"$(nproc)"
  make install DESTDIR="$2"; } > "$1/build.log" 2>&1 || { tail -n 30 "$1/build.log" >&2; exit 1; }
strip "$2"/usr/lib/lib-here/libgpib.so.0.* "$2"/usr/sbin/gpib_config
dpkg --print-architecture > "$1/arch"
dpkg-architecture -qDEB_HOST_MULTIARCH > "$1/multiarch"
# the newest glibc symbol that is used
objdump -T "$2"/usr/lib/lib-here/libgpib.so.0.* "$2"/usr/sbin/gpib_config |
    grep -o 'GLIBC_[0-9.]*' | sed 's/GLIBC_//' | sort -V | tail -n1 > "$1/glibc"
EOF
if [[ -n "${LIB_IMAGE:-}" ]]; then
  docker run --rm -v "$WORK:$WORK" "$LIB_IMAGE" bash -c "
    set -e
    apt-get update -qq >/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends build-essential \
      autoconf automake libtool flex bison pkg-config >/dev/null 2>&1
    bash '$WORK/build-lib.sh' '$U' '$WORK/inst'
    chown -R $(id -u):$(id -g) '$U' '$WORK/inst'"
else
  bash "$WORK/build-lib.sh" "$U" "$WORK/inst"
fi
ARCH="$(cat "$U/arch")"; TRIPLET="$(cat "$U/multiarch")"; GLIBC="$(cat "$U/glibc")"
I="$WORK/inst"
ROOT="$WORK/lib"
DOC="$ROOT/usr/share/doc/otterbench-libgpib"
install -d "$ROOT/DEBIAN" "$ROOT/usr/lib/$TRIPLET" "$ROOT/usr/sbin" \
           "$ROOT/usr/lib/udev/rules.d" "$ROOT/etc" "$DOC"
cp -P "$I"/usr/lib/lib-here/libgpib.so.0* "$ROOT/usr/lib/$TRIPLET/"
chmod 644 "$ROOT/usr/lib/$TRIPLET"/libgpib.so.0.*
install -m 755 "$I/usr/sbin/gpib_config" "$ROOT/usr/sbin/"
for h in gpib_udev_config gpib_udev_fxloader gpib_udevadm_wrapper; do
  f="$(find "$I" -name "$h" | head -n1)"
  [[ -n "$f" ]] || { echo "$h wasn't installed" >&2; exit 1; }
  install -m 755 "$f" "$ROOT/usr/lib/udev/"
done
n=0
while IFS= read -r f; do
  sed -E 's#"[^"]*/(gpib_udev_config|gpib_udev_fxloader|gpib_udevadm_wrapper)#"/usr/lib/udev/\1#' "$f" \
    > "$ROOT/usr/lib/udev/rules.d/$(basename "$f")"
  n=$((n + 1))
done < <(find "$I" -path '*rules.d*' -name '*.rules')
((n)) || { echo "no udev rules were installed" >&2; exit 1; }
cat > "$ROOT/etc/gpib.conf" <<'EOF'
/* One USB adapter (NI GPIB-USB-HS) as system controller. udev sets the board
   type of other USB adapters by itself. Instruments are addressed by the
   application (e.g. GPIB0::5::INSTR). */
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
echo /etc/gpib.conf > "$ROOT/DEBIAN/conffiles"
copyright > "$DOC/copyright"
install -m 644 "$WORK/user-src.tar.gz" "$DOC/linux-gpib-user-src.tar.gz"     # the GPL's source
cat > "$ROOT/DEBIAN/control" <<EOF
Package: otterbench-libgpib
Version: $VERSION
Section: libs
Priority: optional
Architecture: $ARCH
Installed-Size: $(du -sk "$ROOT/usr" | cut -f1)
Maintainer: OtterBench <https://github.com/dcm271/otterbench>
Homepage: https://linux-gpib.sourceforge.io
Depends: libc6 (>= $GLIBC)
Conflicts: libgpib0, libgpib-bin, gpib-user-tools
Description: GPIB (IEEE 488) library for OtterBench
 linux-gpib's libgpib, gpib_config and udev rules, for distributions that don't
 have them. Where libgpib0 and gpib-user-tools exist, use those.
EOF
echo "activate-noawait ldconfig" > "$ROOT/DEBIAN/triggers"
pack "$ROOT"
echo "==> $OUT/otterbench-libgpib_${VERSION}_$ARCH.deb   (glibc $GLIBC or later)"
