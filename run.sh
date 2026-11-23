#!/usr/bin/env bash
# Launch OtterBench.  Examples:
#   ./run.sh                       # pick a resource in the GUI
#   ./run.sh GPIB0::5::INSTR       # connect straight to GPIB address 5
#   ./run.sh SIM::66321D SIM::PPK2 # built-in simulators, no hardware needed
#   ./run.sh --restart-on-crash --fullscreen   # kiosk: start again if it ever dies
#
# --restart-on-crash (handled here, not by the app): if the app ends with an
# error or is killed by a crash signal, start it again after 3 s. A normal quit
# (✕, window close, logout) ends it. Five crashes within two minutes give up,
# so a problem that happens at every start doesn't loop for ever.
cd "$(dirname "$0")" || exit 1

restart=0
args=()
for a in "$@"; do
  if [[ "$a" == --restart-on-crash ]]; then restart=1; else args+=("$a"); fi
done

if (( ! restart )); then
  exec .venv/bin/python -m ee_workbench "${args[@]}"
fi

crashes=()
while true; do
  .venv/bin/python -m ee_workbench "${args[@]}"
  rc=$?
  # 0: quit normally; 2: bad command-line options (restarting won't help)
  if (( rc == 0 || rc == 2 )); then exit "$rc"; fi
  now=$(date +%s)
  crashes+=("$now")
  recent=()
  for t in "${crashes[@]}"; do (( now - t < 120 )) && recent+=("$t"); done
  crashes=("${recent[@]}")
  log="${XDG_CONFIG_HOME:-$HOME/.config}/ee-workbench/crash.log"
  mkdir -p "$(dirname "$log")"
  echo "---- $(date '+%Y-%m-%d %H:%M:%S')  run.sh: OtterBench exited with status $rc" >> "$log"
  if (( ${#crashes[@]} >= 5 )); then
    echo "---- run.sh: 5 crashes in 2 minutes; not restarting again" >> "$log"
    exit "$rc"
  fi
  sleep 3
done
