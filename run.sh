#!/usr/bin/env bash
# Launch the 663xx control panel.  Examples:
#   ./run.sh                       # pick a resource in the GUI
#   ./run.sh GPIB0::5::INSTR       # connect straight to GPIB address 5
#   ./run.sh SIM::66321B           # built-in simulator, no hardware needed
cd "$(dirname "$0")" && exec .venv/bin/python -m ee_workbench "$@"
