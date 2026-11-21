---
title: Installation & Setup
subtitle: From a fresh Linux machine to a working GPIB bus
---

# Installation & Setup

These steps take a fresh Ubuntu machine to EE Workbench talking to your instruments. It takes about 15 minutes, mostly compile time.

> **Raspberry Pi?** `scripts/setup-pi.sh` does all of this in one step. See *Raspberry Pi & Touch Screen*.

## 1. Get the project

The project lives in `~/Dev/ee-workbench`:

```
ee-workbench/
├── run.sh                       launch the GUI
├── ee-workbench.desktop         launcher for the menu, desktop or autostart
├── ee_workbench/                application source
├── scripts/
│   ├── install-gpib-driver.sh   builds and installs linux-gpib
│   ├── setup-pi.sh              complete Raspberry Pi setup
│   ├── build-docs.sh            renders these docs to PDF
│   └── make_screenshots.py      regenerates the user-guide images
├── vendor/linux-gpib/           linux-gpib source (fetched from SourceForge)
├── docs/                        this documentation (Markdown and PDF)
└── tests/                       automated tests (run against the simulators)
```

## 2. Python environment

PyQt6 comes from the distribution; everything else goes into a project virtualenv.

```bash
sudo apt install python3-pyqt6 python3-venv python3-serial
cd ~/Dev/ee-workbench
python3 -m venv --system-site-packages .venv
.venv/bin/pip install pyqtgraph numpy pyvisa pyvisa-py gpib-ctypes pytest
```

| Package | Why |
|---|---|
| PyQt6 | GUI toolkit |
| pyqtgraph, numpy | fast real-time plots and data buffers |
| pyvisa, pyvisa-py | instrument I/O (pure-Python VISA) |
| gpib-ctypes | lets pyvisa-py reach linux-gpib's `libgpib.so` |
| pyserial | only needed for instruments on RS-232 |

Check that it works without any hardware:

```bash
./run.sh SIM::66321D
.venv/bin/python -m pytest tests      # should report all tests passing
```

## 3. GPIB driver (linux-gpib)

Ubuntu's kernel is built with **`CONFIG_GPIB` off**, so the NI GPIB-USB-HS has no driver until you build one. The script does it all:

```bash
cd ~/Dev/ee-workbench
./scripts/install-gpib-driver.sh
```

What it does, step by step:

1. Installs the build dependencies and the headers for your running kernel (asks for sudo).
2. Builds the kernel modules from `vendor/linux-gpib/linux-gpib-kernel` and installs them (`gpib_common`, `ni_usb_gpib`, and others).
3. Builds and installs the userspace library `libgpib`, `gpib_config`, `ibtest` and the udev rules (into `/usr/local`).
4. Writes `/etc/gpib.conf` for one NI GPIB-USB-HS as board `gpib0` (the previous file is backed up).
5. Creates the `gpib` group and adds you to it.
6. Loads the driver and configures the board.

Afterwards, **log out and back in** so the new group membership applies (or run `newgrp gpib` in a terminal).

> **Re-run the script after every kernel upgrade.** The module is built for one kernel version. If GPIB stops working after an update, this is almost always why.

### Verify the bus

```bash
lsmod | grep ni_usb_gpib       # driver loaded
ls -l /dev/gpib0               # crw-rw---- root gpib ...
```

Then start the app and press **⌕ Scan GPIB**. Every powered instrument should appear as `GPIB0::<address>::INSTR`.

For a low-level test, `ibtest` lets you open a device and send `*IDN?` (66321D), `ID?` (6632A) or `F1 T3` (3456A), then read the reply.

### The resulting `/etc/gpib.conf`

```
interface {
    minor       = 0
    board_type  = "ni_usb_b"
    name        = "gpib0"
    pad         = 0
    timeout     = T3s
    eos         = 0x0a
    master      = yes
}
```

You only need to edit it to use a different adapter (`board_type`). You don't need `device { }` entries: the app addresses instruments directly.

## Nordic PPK2 (optional)

No driver is needed: the PPK2 is a USB serial device, and the app uses `python3-serial` (installed above). Your user must be in the `dialout` group:

```bash
groups | grep -q dialout || sudo usermod -aG dialout $USER   # then log out and back in
```

Plug it in and it appears in the resource list as `PPK2::/dev/serial/by-id/…`.

## Saleae logic analyzer (optional)

1. Install **Logic 2** from [saleae.com/downloads](https://www.saleae.com/downloads/) (an AppImage on Linux) and its udev rules, as Saleae's instructions describe. Check that Logic 2 sees the analyzer.
2. Install the automation library into the app's environment:

   ```bash
   .venv/bin/pip install logic2-automation
   ```

3. Turn on Logic 2's automation server: **Preferences ▸ Automation ▸ Enable automation server** (port 10430), or start it with `./Logic-2.x.AppImage --automation`.

While Logic 2 runs with the server on, each analyzer it sees appears in the resource list as `SALEAE::<device id>`. `SALEAE::` (no ID) means the first one. Logic 2 must run on the same computer, because it writes the captured data to files that EE Workbench then reads.

## 4. Launching

```bash
./run.sh                                   # empty window; connect from the toolbar
./run.sh GPIB0::5::INSTR                   # open the 66321D (auto-detected)
./run.sh GPIB0::5::INSTR 6632A@GPIB0::6::INSTR 3456A@GPIB0::22::INSTR
./run.sh PPK2@/dev/ttyACM0                 # Nordic PPK2
./run.sh SALEAE@                           # Saleae: the first analyzer Logic 2 sees
./run.sh SALEAE@F4241                      # ... or a particular one, by its device ID
```

`MODEL@RESOURCE` forces a driver. Models are `663xx` (or `66321D`), `6632A`, `3456A`, `PPK2` and `SALEAE`. **The 3456A always has to be named**, because it has no ID query.

### Desktop launcher (optional)

The repository includes `ee-workbench.desktop`. It assumes the project is in `~/ee-workbench` and starts full screen, so edit its `Exec=` line to suit, for example:

```
Exec=/home/ghost/Dev/ee-workbench/run.sh
```

Then copy it to `~/.local/share/applications/` (menu), `~/Desktop/`, or `~/.config/autostart/` (start at login).

### Command-line options

| Option | Effect |
|---|---|
| `--fullscreen` | Start full screen. **F11** toggles, and a ✕ quit button appears in the top bar. |
| `--touch` / `--no-touch` | Force the touch or desktop layout (normally chosen automatically) |
| `--hide-cursor` | Hide the mouse pointer (touch-only setups) |
| `--open last\|list\|scan\|none` | What to open at start-up, overriding `config.toml` (see the User's Guide) |
| `--config FILE` | Use a different config file |

## Alternatives

| Situation | What to do |
|---|---|
| 66321D on RS-232 instead of GPIB | Use resource `ASRL/dev/ttyUSB0::INSTR` and pick the baud rate that matches the supply's I/O menu (factory 9600). Hidden `ttyS` ports can be typed in by hand. |
| NI-VISA or Keysight IO Libraries installed | `EEWB_VISA_LIB=@ivi ./run.sh` uses the vendor VISA instead of pyvisa-py |
| A different GPIB adapter | Set `board_type` in `/etc/gpib.conf` (e.g. `agilent_82357a`) and run `sudo gpib_config` |

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| No GPIB entries, only simulators | Driver not loaded: run `lsmod \| grep gpib`. Re-run the install script (kernel upgraded?). |
| `/dev/gpib0` missing | Unplug and replug the adapter. Check `dmesg \| tail -30` for `ni_usb_gpib` messages. |
| "Permission denied" opening GPIB0 | You're not in the `gpib` group yet: log out and back in. |
| Scan finds nothing, but the driver is loaded | Instruments off, cable loose, or the 3456A in TALK ONLY mode. |
| Connect times out on one instrument | Wrong address, or two instruments sharing an address. Use **⌕ Scan GPIB** to see the real addresses. |
| "GPIB address 0" warning | Address 0 is the adapter itself. The 3456A defaults to 22, the 66321D to 5. |
| PPK2: "Permission denied" | Not in the `dialout` group yet: `sudo usermod -aG dialout $USER`, then log out and back in. |
| PPK2: "No metadata from the PPK2" | You opened its second serial port, or nRF Connect Power Profiler has it open. Use the `PPK2::…-if01` entry and close the other program. |
| PPK2: "Samples lost" climbs | The USB link or the computer can't keep up (400 kB/s). Use another USB port and avoid hubs; on a Pi, close other heavy programs. |
| Saleae: "Logic 2 isn't answering on 127.0.0.1:10430" | Logic 2 isn't running, or its automation server is off: **Preferences ▸ Automation**. |
| Saleae: "logic2-automation package is missing" | `.venv/bin/pip install logic2-automation` |
| Saleae: "Logic 2 doesn't see analyzer …" | Unplugged, or another analyzer is connected. Check Logic 2's device list; `SALEAE::` picks whichever is there. |
| Saleae: "invalid digital sample rate" (or similar) at Capture | That rate isn't possible with this many channels on. Pick a lower rate or turn channels off. |
| Saleae: a decoder finds nothing | Wrong channel or bit rate, or the analyzer setting names don't match Logic 2's (for **Other…** decoders they must match exactly). |
| "Couldn't identify the instrument" | Auto-detect only knows the supplies. Pick the model by hand (the 3456A always needs this). |
| Readings stop and "Polling paused" appears | A bus error occurred. Fix the cause, then press **Resume polling**. |
| 66321D waveform capture is slow | Normal: the instrument sends arrays at about 3.7 KB/s (about 1.2 s per 1024 points in binary). |
| Module build fails after a kernel update | Run `git -C vendor/linux-gpib pull`, then run the script again. |

## Building these documents

```bash
.venv/bin/pip install markdown weasyprint     # once
./scripts/build-docs.sh                       # writes docs/pdf/*.pdf
```
