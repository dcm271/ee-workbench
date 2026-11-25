#!/usr/bin/env bash
# Take out the linux-gpib library and tools that install-gpib-driver.sh put into
# /usr/local, once the packages (build-gpib-deb.sh) are installed: otherwise the
# old copy goes on being used. /etc/gpib.conf and kernel modules stay.
#
#   scripts/remove-local-gpib.sh        shows what it would remove, then asks
set -euo pipefail
shopt -s nullglob

# gpib_config is in /usr/sbin, which may not be on a normal user's PATH
if [[ ! -x /usr/sbin/gpib_config ]]; then
  echo "The GPIB packages aren't installed (no /usr/sbin/gpib_config): nothing removed."
  echo "Without them this copy is the only one."
  exit 1
fi
FILES=(/usr/local/lib/libgpib.* /usr/local/lib/pkgconfig/libgpib.pc
       /usr/local/sbin/gpib_config /usr/local/bin/{ibtest,ibterm,findlisteners}
       /usr/local/lib/udev/gpib_udev_{config,fxloader} /usr/local/lib/udev/gpib_udevadm_wrapper
       /usr/local/include/gpib/{gpib_user,gpib_version,ib}.h)
# rules of these names in /etc replace the packages' and call /usr/local
for f in /etc/udev/rules.d/{98-gpib-generic,99-agilent_82357a,99-lpvo_usb_gpib,99-ni_usb_gpib}.rules; do
  if [[ -e "$f" ]] && { grep -q /usr/local "$f" || [[ "$f" == *98-gpib-generic* ]]; }; then
    if dpkg -S "$f" >/dev/null 2>&1; then continue; fi
    FILES+=("$f")
  fi
done
GONE=()
for f in "${FILES[@]}"; do if [[ -e "$f" || -L "$f" ]]; then GONE+=("$f"); fi; done
if ((${#GONE[@]} == 0)); then echo "Nothing to remove."; exit 0; fi
printf '  %s\n' "${GONE[@]}"
read -r -p "Remove these ${#GONE[@]} files? [y/N] " answer
[[ "$answer" == [yY]* ]] || { echo "Nothing removed."; exit 0; }
sudo rm -f -- "${GONE[@]}"
sudo rmdir --ignore-fail-on-non-empty /usr/local/include/gpib /usr/local/lib/udev 2>/dev/null || true
sudo ldconfig
sudo udevadm control --reload-rules || true
echo "Done. Unplug the adapter and plug it in again."
