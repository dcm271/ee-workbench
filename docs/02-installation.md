---
title: Installation & Setup
subtitle: From a fresh Linux machine to a working GPIB bus
---

# Installation & Setup

Other chapters: [Hardware](01-hardware-and-resources.md) · [User's Guide](03-users-guide.md) · [Technical Reference](04-technical-reference.md) · [Raspberry Pi](05-raspberry-pi.md) · [How-to guides](06-howtos.md) · [README](../README.md)

These steps take a fresh Ubuntu machine to OtterBench talking to your instruments. It takes about 15 minutes, mostly compile time.

> **One step:** `scripts/setup-ubuntu.sh` does everything on this page: apt packages, the Python environment, sigrok, USB permissions, the GPIB driver and a menu entry. It is safe to re-run. Options:
>
> | Option | |
> |---|---|
> | `--no-gpib` | skip building the GPIB driver (no GPIB adapter) |
> | `--no-sigrok` | skip sigrok (cheap logic analyzers) |
> | `--no-saleae` | skip the Saleae automation library |
> | `--no-pdf` | skip WeasyPrint, which makes [PDF reports](#pdf-reports); HTML reports still work |
> | `--docs` | also install what's needed to rebuild these documents as PDFs |
> | `--dry-run` | show what it would do, and do nothing |
>
> It also adds you to the `dialout`, `plugdev` and (if it exists) `gpio` groups, and installs a udev rule for [USB instruments](#scpi-meters-loads-supplies-and-oscilloscopes-optional) (generators, meters, loads, supplies, scopes). Log out and back in afterwards so the groups apply. The sections below explain each part, in case you'd rather do it by hand.
>
> **Raspberry Pi?** `scripts/setup-pi.sh` does the same for the Pi. See *Raspberry Pi & Touch Screen*.

## 1. Get the project

The project lives in `~/Dev/ee-workbench`:

```
ee-workbench/
├── run.sh                       launch the GUI (--restart-on-crash: start again after a crash)
├── ee-workbench.desktop         launcher for the menu, desktop or autostart
├── ee_workbench/                application source
├── scripts/
│   ├── install-gpib-driver.sh   builds and installs linux-gpib
│   ├── prepare-kernel-tree.sh   kernel tree for boards with no headers package (called by the above)
│   ├── setup-ubuntu.sh          complete Ubuntu setup
│   ├── setup-pi.sh              complete Raspberry Pi setup
│   ├── build-docs.sh            renders these docs to PDF (runs build_docs.py)
│   ├── make_screenshots.py      regenerates the user-guide images (--touch: touch layout)
│   └── touch_screenshots.py     the touch-layout part of make_screenshots.py --touch
├── requirements.txt             the Python packages for .venv
├── vendor/linux-gpib/           linux-gpib source (fetched from SourceForge)
├── docs/                        this documentation (Markdown and PDF)
└── tests/                       automated tests (run against the simulators)
```

## 2. Python environment

PyQt6 comes from the distribution; everything else goes into a project virtualenv.

```bash
sudo apt install python3-pyqt6 python3-numpy python3-venv python3-serial libxcb-cursor0
cd ~/Dev/ee-workbench
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

| Package | Why |
|---|---|
| PyQt6 | GUI toolkit |
| pyqtgraph, numpy | fast real-time plots and data buffers |
| pyvisa, pyvisa-py | instrument I/O (pure-Python VISA) |
| gpib-ctypes | lets pyvisa-py reach linux-gpib's `libgpib.so` |
| pyserial | the Nordic PPK2, and instruments on RS-232 |
| pyusb | USB-TMC instruments (function generators) through pyvisa-py |
| pytest | the test suite |

Optional extras, installed by the setup scripts unless you opt out: `weasyprint` ([PDF reports](#pdf-reports)), `logic2-automation` ([Saleae](#saleae-logic-analyzer-optional), x86-64 only) and the apt package `python3-libgpiod` ([GPIO](#raspberry-pi-gpio-optional)).

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

While Logic 2 runs with the server on, each analyzer it sees appears in the resource list as `SALEAE::<device id>`. `SALEAE::` (no ID) means the first one. Logic 2 must run on the same computer, because it writes the captured data to files that OtterBench then reads.

## sigrok logic analyzers (optional)

For the cheap 24 MHz "Saleae Logic clones" (fx2lafw), the original Saleae Logic and Logic16, DreamSourceLab DSLogic and the other analyzers [sigrok supports](https://sigrok.org/wiki/Supported_hardware):

```bash
sudo apt install sigrok-cli sigrok-firmware-fx2lafw
```

The package's udev rules give USB analyzers to the desktop user, so unplug the analyzer and plug it back in after installing. Check with `sigrok-cli --scan`. Each analyzer then appears in the resource list as `SIGROK::<driver>:conn=<bus>.<address>`. `SIGROK::demo` is sigrok's built-in demo device, which is handy for a first try. This works on the Raspberry Pi too.

## Function generators (optional)

Nothing extra to install: generators use the same VISA library as GPIB.

- **LAN** (recommended): give the generator a fixed IP address on its front panel (Utility → I/O → LAN), check you can `ping` it, and connect to `TCPIP::192.168.1.50::INSTR` (VXI-11; Keysight, Rigol and Siglent all answer it). If VXI-11 is blocked, `TCPIP::192.168.1.50::5025::SOCKET` (Keysight, Siglent port 5025; Rigol 5555) works too. Type the resource in the toolbar, with model **Function generator** or **Auto-detect**.
- **USB**: plug it in and it appears in the resource list as `USB0::0x1AB1::0x0642::DG1ZA2xxxxxxx::INSTR`. pyvisa-py drives USB-TMC through `pyusb` (in `requirements.txt`), and needs permission on the device: the setup scripts install `/etc/udev/rules.d/60-ee-workbench-usbtmc.rules` for Keysight/Agilent (`0957`, `2a8d`), Rigol (`1ab1`), Siglent (`f4ec`, `f4ed`), Tektronix (`0699`) and Keithley (`05e6`), giving them to the `plugdev` group. Another brand: add a line with its USB vendor ID (`lsusb`), then `sudo udevadm control --reload-rules` and replug.
- **GPIB**: as any other GPIB instrument.

Auto-detect recognises the Keysight 33xxx, Rigol DG and Siglent SDG families by their `*IDN?` answer; otherwise choose **Function generator** by hand. Test with the simulators first: `./run.sh SIM::FGEN` (SCPI) or `SIM::SDG` (Siglent).

## SCPI meters, loads, supplies and oscilloscopes (optional)

Nothing extra to install: these use the same VISA library (pyvisa-py) as the generators. The [Compatibility](01-hardware-and-resources.md#compatibility) table lists the models with a built-in definition.

1. **Connect it.**
   - **LAN** (recommended for meters, loads and scopes): set a fixed IP address on the instrument (its Utility / I/O / LAN menu), check you can `ping` it, and use `TCPIP::<ip>::INSTR`. If VXI-11 is blocked or not offered, the raw socket form works: `TCPIP::<ip>::5025::SOCKET` (Keysight, Siglent, Keithley), `::5555::SOCKET` (Rigol), `::4000::SOCKET` (Tektronix).
   - **USB**: the instrument appears in the resource list as `USB0::0x1AB1::…::INSTR` once the udev rule above is installed (replug it after installing). Another brand: add a line with its vendor ID from `lsusb`, `sudo udevadm control --reload-rules`, replug.
   - **GPIB**: as any other GPIB instrument. **RS-232**: `ASRL/dev/ttyUSB0::INSTR` with the baud rate set in the toolbar.
2. **Open it** with *Model: Auto-detect*: it reads `*IDN?` and opens a meter / load / supply tab or a scope tab. If it isn't recognised, choose it by hand: **Model** list in the toolbar, or **＋ Add instrument → Meter / load / scope / SCPI…**. On the command line: `./run.sh SCPI:34461A@TCPIP::192.168.1.60::INSTR`, `./run.sh SCOPE:RIGOL@TCPIP::192.168.1.70::INSTR`.
3. **Try the simulator first** if you like: `./run.sh SIM::34461A SIM::DL3021 SIM::DP832 SIM::SCOPE`.

A few instruments need a setting on their front panel first:

| Instrument | Set |
|---|---|
| Keithley DMM6500 / DMM7510 / DAQ6510 | **MENU → System → Settings → Command Set: SCPI** (it may ship in TSP mode), then power-cycle |
| Keysight E36300, 34460A/34461A | LAN or USB is on by default; for GPIB set its address in **Utility → I/O** |
| Siglent SPD3303X | Over LAN, VXI-11 (`TCPIP::<ip>::INSTR`) or port 5025. Keep the poll delay at 200 ms or more; it drops commands sent faster |
| Rigol DS1000Z | Over USB, set **Utility → IO Setting → USB Device: Computer**. Over LAN, turn on DHCP or set a static address |
| Tektronix TBS1000C/2000 | Over USB, set **Utility → Config → USB Device Port: Connect to Computer** |

**Your own instruments.** An instrument without a built-in definition can be added with a small TOML file in `~/.config/ee-workbench/instruments/`. No programming is needed: see [Add your own SCPI instrument](06-howtos.md#add-your-own-scpi-instrument). Check a file with:

```bash
.venv/bin/python -m ee_workbench.check_instruments ~/.config/ee-workbench/instruments/my-meter.toml
.venv/bin/python -m ee_workbench.check_instruments          # every definition in use
```

## Raspberry Pi GPIO (optional)

On a Pi, `scripts/setup-pi.sh` does this. By hand:

```bash
sudo apt install python3-libgpiod          # libgpiod's Python module (v1 or v2 both work)
groups | grep -q gpio || sudo usermod -aG gpio $USER    # then log out and back in
```

The `.venv` must see the system's packages (`python3 -m venv --system-site-packages .venv`, as the setup scripts make it). Check with `.venv/bin/python -c "import gpiod; print(gpiod.__version__)"` and `gpioinfo | head`. The header then appears in the resource list as `GPIO::/dev/gpiochip0` (`gpiochip4` on a Pi 5 with older kernels: the app finds the right chip by its label). `SIM::GPIO` simulates one on any computer. See [Hardware](01-hardware-and-resources.md#raspberry-pi-gpio-header) before wiring anything.

## PDF reports

[Reports](03-users-guide.md#reports) are written as HTML with nothing extra. For PDF, the app uses **WeasyPrint**, which the setup scripts install unless you pass `--no-pdf`. By hand:

```bash
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0
.venv/bin/pip install weasyprint
```

Without it, choosing PDF says "PDF needs WeasyPrint" and HTML still works; a browser's **Print → Save as PDF** on the HTML report gives a PDF too.

## Remote API (optional)

Nothing to install. Switch it on with **Tools ▾ → Remote API**, or start it with the app:

```toml
# ~/.config/ee-workbench/config.toml
[startup]
api = true
api_port = 8765
```

(or `./run.sh --api`). It listens on `127.0.0.1` only. A notebook or script on the same computer uses `from ee_workbench.client import connect`; that file needs only Python's standard library. From another computer, tunnel it over SSH (`ssh -L 8765:127.0.0.1:8765 user@bench`) and copy `~/.config/ee-workbench/api.json` (it holds the access token). See the [User's Guide](03-users-guide.md#remote-api-python-and-jupyter).

## 4. Launching

```bash
./run.sh                                   # empty window; connect from the toolbar
./run.sh GPIB0::5::INSTR                   # open the 66321D (auto-detected)
./run.sh GPIB0::5::INSTR 6632A@GPIB0::6::INSTR 3456A@GPIB0::22::INSTR
./run.sh PPK2@/dev/ttyACM0                 # Nordic PPK2
./run.sh SALEAE@                           # Saleae: the first analyzer Logic 2 sees
./run.sh SALEAE@F4241                      # ... or a particular one, by its device ID
./run.sh SIGROK@fx2lafw                    # a sigrok analyzer
./run.sh FGEN@TCPIP::192.168.1.50::INSTR   # a LAN function generator
./run.sh GPIO@                             # the Raspberry Pi's GPIO header
./run.sh SCPI:DL3021@TCPIP::192.168.1.61::INSTR    # a meter / load / supply from a definition
./run.sh SCOPE:KEYSIGHT@USB0::0x2A8D::0x1797::CN12345678::INSTR   # an oscilloscope
./run.sh PPK2@/dev/ttyACM0 PPK2@/dev/ttyACM2     # two PPK2s: two tabs
./run.sh --api SIM::66321D SIM::PPK2       # simulators, with the Remote API on
```

`MODEL@RESOURCE` forces a driver. Models are `663xx` (or `66321D`), `6632A`, `3456A`, `BK393`, `BK393B`, `XDM`, `PPK2`, `SALEAE`, `SIGROK`, `FGEN`, `GPIO`, `SCPI:<id>` for an [instrument definition](04-technical-reference.md#instrument-definition-files-toml) (`SCPI:34461A`, `SCPI:DP832`, …; plain `34461A` works too) and `SCOPE:RIGOL`, `SCOPE:KEYSIGHT`, `SCOPE:TEK`, `SCOPE:SIGLENT` (a model name such as `DS1054Z` or `TBS2000` works too). **The 3456A always has to be named**, because it has no ID query. The same resource can't be opened twice, but any number of different instruments of one model can.

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
| `--api` | Start the [Remote API](#remote-api-optional) |
| `--no-splash` | Start without the splash screen |
| `--version` | Print the version and quit |
| `--restart-on-crash` | **`run.sh` only.** If the app ends with an error or crashes, start it again after 3 s. A normal quit ends it; five crashes within two minutes give up. Each restart is noted in `~/.config/ee-workbench/crash.log`. Meant for unattended screens: the Pi's autostart uses it. |

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
| sigrok: "sigrok-cli isn't installed" | `sudo apt install sigrok-cli sigrok-firmware-fx2lafw` |
| sigrok: "sigrok finds no fx2lafw device" | Replug the analyzer after installing (udev rules); check `sigrok-cli --scan`; the 24 MHz clones need `sigrok-firmware-fx2lafw` |
| Saleae: "Logic 2 doesn't see analyzer …" | Unplugged, or another analyzer is connected. Check Logic 2's device list; `SALEAE::` picks whichever is there. |
| Saleae: "invalid digital sample rate" (or similar) at Capture | That rate isn't possible with this many channels on. Pick a lower rate or turn channels off. |
| Saleae: a decoder finds nothing | Wrong channel or bit rate, or the analyzer setting names don't match Logic 2's (for **Other…** decoders they must match exactly). |
| "Couldn't identify the instrument" | Its `*IDN?` answer isn't in any definition. Pick the model by hand (the 3456A always needs this), or [write a definition](06-howtos.md#add-your-own-scpi-instrument) with an `idn` entry for it. |
| Status bar: "⚠ N instrument definition file(s) not loaded" | A file in `~/.config/ee-workbench/instruments/` has a mistake. `python -m ee_workbench.check_instruments` says which and why; the full list is in `crash.log`. |
| SCPI tab: a reading shows **—** and the console shows `-113` / `Undefined header` | The instrument doesn't know that command (another model of the family, or another firmware). Type the right one in the **Console** to find it, then change it in a copy of the definition. |
| SCPI tab: a reading shows **OVLD** | The instrument answered 9.9E+37: overload or no reading at the present range. |
| Keithley DMM6500: nothing but errors | It's in TSP mode: switch its command set to SCPI (see above). |
| Scope: "Single → capture" waits forever | The trigger never comes: check the trigger source and level, or use **Grab screen**. **■ Stop** ends the wait. |
| Scope: waveform is flat or clipped | The channel's V/div or offset puts the signal off screen: the screen record is all that is fetched. |
| Production test: the banner says **SERIAL?** | The serial-format pattern is a regular expression matched against the whole serial: `SB\d{6}` means SB and six digits. Clear the box to accept anything. |
| Readings stop and "Polling paused" appears; the tab shows ⚠ and **⟳ Reconnect** | A bus error occurred, or the instrument was switched off or unplugged. Fix the cause, then press **Resume polling**, or **⟳ Reconnect** to reopen the tab with its settings. |
| A window "OtterBench: internal error" | A bug was caught; the app keeps running and the instruments are untouched. Details are in `~/.config/ee-workbench/crash.log`; include it when reporting the problem. |
| The app vanished (no window, no message) | A crash inside Qt or a driver library. The stack is in `~/.config/ee-workbench/fault.log`. Restart it; it reopens the same instruments. On an unattended screen launch it with `./run.sh --restart-on-crash`. |
| Status bar: "Skipped damaged watchdog rules", or "bench.json is damaged" | A hand edit or disk problem. The rest was loaded; fix or delete the file in `~/.config/ee-workbench/`. |
| Status bar: "Output OFF … ⚠ FAILED for …" after Esc | That instrument didn't take the command (disconnected?). Every other output was still switched off; check the named one by hand. |
| 66321D waveform capture is slow | Normal: the instrument sends arrays at about 3.7 KB/s (about 1.2 s per 1024 points in binary). |
| Function generator: "Could not open TCPIP::…" | Wrong IP, or the generator's LAN is off. `ping` it; check its I/O menu. Try the `::5025::SOCKET` form. |
| Function generator over USB: "No backend available" | `pyusb` or libusb is missing: `.venv/bin/pip install pyusb` and `sudo apt install libusb-1.0-0`. |
| Function generator over USB: "Access denied" / not listed | No udev rule for its vendor: see [Function generators](#function-generators-optional). Replug after adding one. |
| Function generator: setting changes, but output level is double | The generator's load is 50 Ω and its output goes into a high impedance. Set its output load to High Z. |
| GPIO: "The libgpiod Python module is missing" | `sudo apt install python3-libgpiod`; the `.venv` must use `--system-site-packages` (re-run the setup script). |
| GPIO: "Permission denied" on `/dev/gpiochip…` | Not in the `gpio` group yet: `sudo usermod -aG gpio $USER`, log out and back in. |
| GPIO: "Device or resource busy" on a pin | Something else holds it: I²C/SPI/serial console enabled on those pins, a 1-Wire overlay, or another program. `gpioinfo` shows the owner. |
| PDF report: "PDF needs WeasyPrint" | `.venv/bin/pip install weasyprint` (and the pango packages above), or save HTML. |
| Remote API: `connect()` says "remote API isn't on" or "Can't reach OtterBench" | Switch it on in **Tools ▾**, or run with `--api`. A stale `api.json` from a crashed run is replaced when the API next starts. |
| Remote API: "Can't listen on port 8765: … Address already in use" | Another copy of the app is running its API. Change `api_port` in config.toml. |
| Module build fails after a kernel update | Run `git -C vendor/linux-gpib pull`, then run the script again. |

## Building these documents

```bash
.venv/bin/pip install markdown weasyprint     # once
./scripts/build-docs.sh                       # writes docs/pdf/*.pdf
```
