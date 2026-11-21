# EE Workbench

A Linux desktop app (PyQt6) for controlling and logging GPIB bench instruments:

| Instrument | Support |
|---|---|
| Agilent 66321D / 66321B / 66311B DC source | Setpoints, protection, output resistance, DVM, pulse statistics; triggered waveform capture with cursors and automatic measurements; battery-life, battery-emulator, sequencer and I-V tests; rear port, display, saved setups, self-test |
| HP 6632A / 6633A / 6634A DC supply | Setpoints, OVP/OCP, readback, status |
| HP 3456A 6½-digit DMM | Function, range, integration time, autozero, filter, null, statistics |
| Nordic Power Profiler Kit II (USB) | Source or ampere meter at 100 kS/s; DUT power and voltage; live full-rate view with logic inputs D0–D7; trend with region average, charge and energy |
| Saleae Logic 8 / Pro 8 / Pro 16 (via Logic 2) | Timed, triggered and manual captures of digital and analog channels; frequency, duty, pulse-width and voltage measurements over the view or a region; Logic 2's protocol decoders (UART, I²C, SPI, any other) in a searchable table; save as .sal or export CSV |

Each instrument opens in its own tab with live readouts, trend plots on a shared time base, and CSV recording. **Esc turns every supply's output off** (and a PPK2's DUT power). Simulators (`SIM::66321D`, `SIM::6632A`, `SIM::3456A`, `SIM::PPK2`, `SIM::SALEAE`) let you try everything without hardware.

![66321D panel](docs/images/supply-66321d.png)

## Quick start

```bash
./scripts/install-gpib-driver.sh           # once: builds linux-gpib for the NI GPIB-USB-HS
./run.sh GPIB0::5::INSTR 3456A@GPIB0::22::INSTR
./run.sh PPK2@/dev/ttyACM0                 # Nordic PPK2 (also listed as PPK2::… when plugged in)
./run.sh SALEAE@                           # Saleae, through a running Logic 2 (automation server on)
./run.sh SIM::66321D                       # no hardware needed
```

On a Raspberry Pi with a touch screen, `./scripts/setup-pi.sh` installs everything and sets the app to start full screen at login. `ee-workbench.desktop` is a ready-made launcher. See `docs/05-raspberry-pi.md`.

## Documentation

The Markdown sources are in [`docs/`](docs/), and the rendered PDFs in `docs/pdf/` (build them with `scripts/build-docs.sh`):

1. [Hardware & Resources](docs/01-hardware-and-resources.md): supported instruments, GPIB addresses, cabling, manuals
2. [Installation & Setup](docs/02-installation.md): Python environment, GPIB driver, troubleshooting
3. [User's Guide](docs/03-users-guide.md): every panel and control, and measurement workflows
4. [Technical Reference](docs/04-technical-reference.md): command sets, timings, architecture, adding instruments
5. [Raspberry Pi & Touch Screen](docs/05-raspberry-pi.md): one-step Pi setup, full-screen launcher, touch layout

## Tests

```bash
.venv/bin/python -m pytest tests
```
