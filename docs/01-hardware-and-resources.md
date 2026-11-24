---
title: Hardware & Resources
subtitle: What you need, how to cable it, and where the reference manuals are
---

# Hardware & Resources

Other chapters: [Installation](02-installation.md) · [User's Guide](03-users-guide.md) · [Technical Reference](04-technical-reference.md) · [Raspberry Pi](05-raspberry-pi.md) · [How-to guides](06-howtos.md) · [README](../README.md)

This guide lists the hardware OtterBench supports, what each instrument can do from software, how to set GPIB addresses and cables, and where to find the original manuals.

## At a glance

| Item | Role | Interface | Status in OtterBench |
|---|---|---|---|
| **Agilent 66321D** Mobile Communications DC Source | Main supply with DVM input | GPIB (and RS-232) | **Fully supported**. Tested on real hardware (fw A.03.01) |
| Agilent 66311B / 66321B | Same family without DVM | GPIB / RS-232 | Supported by the same driver; DVM hidden if absent |
| **HP 6632A** System DC Power Supply (also 6633A, 6634A) | Second supply, 20 V / 5 A | GPIB only | Supported. Written from the manual and tested on the simulator |
| **HP 3456A** 6½-digit DMM | Precision voltage / resistance | GPIB only | Supported. Written from the manual and tested on the simulator |
| **FNIRSI DPS-150** USB-C bench supply | 30 V / 5.1 A / 150 W programmable supply | USB (ID `2e3c:5740`, serial) | Supported. Written from the community-documented protocol and tested on the simulator; not yet tried on real hardware |
| **B&K Precision 393** and **393B** handheld DMMs | 60,000- and 100,000-count meters, read-only | USB cable (serial) | Supported. 393 written from B&K's PC software and a community driver that agree; 393B from the APPA 500-series protocol. Tested on simulators; not yet tried on real hardware |
| **OWON XDM1041 / XDM1241 / XDM2041** bench DMMs | 55,000 counts | USB serial (CH340) or RS232, 115200 baud | Supported. Written from OWON's programming manual, a tested XDM1041 command list and logs of real meters. Tested on a simulator; not yet tried on real hardware |
| **Nordic Power Profiler Kit II** (PPK2) | 100 kS/s current profiler and 0.8–5 V source | USB (ID `1915:c00a`, serial) | Supported. Tested on real hardware (HW 8682) |
| **Saleae Logic 8 / Logic Pro 8 / Logic Pro 16** | Logic analyzer with analog inputs | USB, through the **Logic 2** software (automation API) | Supported. Written from Saleae's API and file-format docs and tested on the simulator; not yet tried on real hardware |
| sigrok-supported logic analyzers (fx2lafw clones, DSLogic, …) | Logic analyzer | USB, through `sigrok-cli` | Supported, including on the Raspberry Pi. Tested on the simulator |
| **Function generators**: Keysight 33500B/33600A/33210A/33220A, Rigol DG800/DG900/DG1000Z/DG4000, Siglent SDG800/1000X/2000X/6000X | Signal source, 1–2 channels | LAN (VXI-11 / socket), USB-TMC or GPIB | Supported (waveform, frequency, amplitude, offset, duty, output; profiles). Written from the programming guides and tested on simulators of both command sets |
| **Bench multimeters (SCPI)**: Keysight 34401A/344xxA, Keithley 2000-series and DMM6500/7510, Rigol DM3058/3068, Siglent SDM3000X, any SCPI meter | Measurements | LAN, USB-TMC, GPIB, RS-232 | Supported through [instrument definitions](#generic-scpi-meters-loads-and-supplies). Written from the programming guides and tested on simulators; not yet tried on real hardware |
| **Electronic loads (SCPI)**: Rigol DL3021/3031, Siglent SDL1020X/1030X, Keithley 2380 / B&K 8600 | CC / CV / CR / CP load | LAN, USB-TMC, GPIB | As above |
| **Programmable supplies (SCPI)**: Rigol DP832/831/821, Keysight E36311A–E36313A, Siglent SPD3303X, any single-output SCPI supply | Extra supplies, 1–3 outputs | LAN, USB-TMC, GPIB | As above |
| **Oscilloscopes**: Rigol DS1000Z/DS2000A/MSO5000/DHO800-900, Keysight InfiniiVision 1000X–6000X, Tektronix TBS1000C/2000/MDO3/MSO2-6/DPO2000-3000, Siglent SDS1000X-E/2000X-E | Waveform capture and measurements | LAN, USB-TMC, GPIB | [Supported](#oscilloscopes) (channels, timebase, trigger, run/stop/single, waveforms, measurements). Written from the programming guides and tested on simulators of all four command sets; not yet tried on real hardware |
| **Any other SCPI instrument** | Whatever you describe | as above | Write a [definition file](04-technical-reference.md#instrument-definition-files-toml): no programming needed |
| **Raspberry Pi 40-pin GPIO header** | Relays, buttons, resets, "done" inputs | libgpiod (`/dev/gpiochip*`) | Supported on Pi 3/4/5/Zero 2 W. Tested on the simulator |
| **Agilent E3610A** bench supply | Manual bench supply | **None** | Cannot be controlled. Monitor its output with the 3456A instead (see below) |
| **NI GPIB-USB-HS** | USB-to-GPIB controller | USB 2.0 (ID `3923:709b`) | Supported through linux-gpib |
| Linux PC | Runs the application | — | Ubuntu with kernel 7.0 tested |

## Compatibility

Every instrument below has a simulator, so its tab, the sequencer, the watchdog and sessions can all be tried without the hardware (`SIM::<model>`, see the last column). "Status" says how far each has been checked:

- **Tested**: used with the real instrument (model and firmware noted).
- **Simulator**: written from the maker's programming manual and checked against a simulator that follows that manual. It should work; if it doesn't, the tab's **Console** shows each command and its answer, and fixing it is usually a one-line change to a definition file. Please report what you find, with the instrument's `*IDN?` answer.

**Auto-detect** means *Model: Auto-detect* in the toolbar recognises it from its `*IDN?` answer and opens the right tab. Otherwise choose the model by hand (the **Meter / load / scope / SCPI…** entry in the **＋ Add instrument** dialog, or the Model list).

### Supplies, meters, profiler, logic, generators (built-in drivers)

| Instrument | Model key | Auto-detect | Status | Simulator |
|---|---|---|---|---|
| Agilent 66321D, 66321B, 66311B | `66321D` | yes | Tested (66321D, fw A.03.01) | `SIM::66321D` |
| HP 6632A, 6633A, 6634A | `6632A` | yes (`ID?`) | Simulator | `SIM::6632A` |
| HP 3456A | `3456A` | **no**: choose by hand (it doesn't answer `*IDN?`) | Simulator | `SIM::3456A` |
| FNIRSI DPS-150 | `DPS150` | yes (USB ID) | Simulator | `SIM::DPS150` |
| B&K Precision 393 | `BK393` (or `393`) | **no**: choose by hand (generic FTDI cable) | Simulator | `SIM::BK393` |
| B&K Precision 393B | `BK393B` (or `393B`) | **no**: choose by hand (generic CP210x cable) | Simulator | `SIM::BK393B` |
| OWON XDM1041 / XDM1241 / XDM2041 | `XDM` (or the model name) | **no**: choose by hand (generic CH340 chip) | Simulator | `SIM::XDM1041`, `SIM::XDM2041` |
| Nordic PPK2 | `PPK2` | yes (USB ID) | Tested (HW 8682) | `SIM::PPK2` |
| Saleae Logic 8 / Pro 8 / Pro 16 | `SALEAE` | through Logic 2 | Simulator | `SIM::SALEAE` |
| sigrok analyzers | `SIGROK` | through `sigrok-cli --scan` | Simulator | `SIM::SIGROK` |
| Keysight 33500B, 33600A, 33210A, 33220A; Rigol DG800–DG4000 | `FGEN` | yes | Simulator | `SIM::FGEN` |
| Siglent SDG800, SDG1000X, SDG2000X, SDG6000X | `FGEN` | yes | Simulator | `SIM::SDG` |
| Raspberry Pi GPIO header | `GPIO` | listed when present | Simulator | `SIM::GPIO` |
| A DUT's serial console | `UART` | **no**: choose by hand, or **Identify port…** | Simulator | `SIM::UART` |

### Meters, loads and supplies (instrument definitions)

These are driven by the generic SCPI driver from the [definition files](04-technical-reference.md#instrument-definition-files-toml) in `otterbench/instruments/scpi_defs/`. Each one lists the model families its commands cover; close relatives usually work with the same file.

| Instrument | Model key | Auto-detect | Readings | Settings | Simulator |
|---|---|---|---|---|---|
| Keysight 34461A, 34465A, 34470A (also 34401A, 34410A, 34411A, 34460A) | `SCPI:34461A` | yes | value (unit follows the function) | function (DC/AC V and I, 2W/4W Ω, frequency, capacitance…), NPLC, autozero | `SIM::34461A` |
| Keithley 2000, 2001, 2002, 2010, 2100 | `SCPI:K2000` | yes | value | function, NPLC | `SIM::K2000` |
| Keithley DMM6500, DMM7510, DAQ6510 (**SCPI** command set, not TSP) | `SCPI:DMM6500` | yes | value | function, NPLC | `SIM::DMM6500` |
| Rigol DM3058, DM3058E, DM3068 | `SCPI:DM3058` | yes | value | function | `SIM::DM3058` |
| Siglent SDM3045X, SDM3055, SDM3065X | `SCPI:SDM3055` | yes | value | function | `SIM::SDM3055` |
| Any SCPI meter that answers `READ?` | `SCPI:SCPI-DMM` | no | value | (set the function on the meter) | `SIM::SCPI-DMM` |
| Rigol DL3021, DL3021A, DL3031, DL3031A load | `SCPI:DL3021` | yes | voltage, current, power | function (CC/CV/CR/CP), cc, cv, cr, cp levels, input on/off | `SIM::DL3021` |
| Siglent SDL1020X(-E), SDL1030X(-E) load | `SCPI:SDL1020` | yes | voltage, current, power | as above | `SIM::SDL1020` |
| Keithley 2380, B&K Precision 8600 series load | `SCPI:K2380` | yes | voltage, current, power | as above | `SIM::K2380` |
| Rigol DP832, DP832A, DP831, DP821 (3 outputs) | `SCPI:DP832` | yes | voltage1–3, current1–3 | vset, iset, ovp, ovp_on, ocp, ocp_on, out (per output) | `SIM::DP832` |
| Keysight E36311A, E36312A, E36313A (3 outputs) | `SCPI:E36312A` | yes | voltage1–3, current1–3 | vset, iset, ovp, ocp_on, out (per output) | `SIM::E36312A` |
| Siglent SPD3303X, SPD3303X-E (2 programmable outputs) | `SCPI:SPD3303X` | yes | voltage1–2, current1–2 | vset, iset, out (per output); no OVP over SCPI | `SIM::SPD3303X` |
| Any single-output SCPI supply (`VOLT`, `CURR`, `OUTP`, `MEAS:VOLT?`): Keysight E36100/E3630/E3640 series, Rigol DP711/712, R&S NGE/HMP (output 1), TTi SCPI models… | `SCPI:SCPI-SUPPLY` | no | voltage, current | vset, iset, ovp, out | `SIM::SCPI-SUPPLY` |

The following don't speak SCPI over VISA; an adapter (`instruments/adapters.py`) translates, so they get the same tab. Give the port in the resource: `KA3005P::/dev/ttyUSB0`, with `@baud` behind it if it isn't the usual speed.

| Instrument | Resource | Readings | Settings | Simulator |
|---|---|---|---|---|
| Riden RD6006, RD6012, RD6018, RD6024, RK6006 (USB, 115200 baud) | `RD60XX::/dev/ttyUSB0` | voltage, current, power, vin, ah, wh, temp, cc, prot | vset, iset, ovp, ocp, out | `SIM::RD60XX` |
| Korad KA3005P, KD3005P, Tenma 72-2540 / 72-2535 / 72-2710, RND 320, Velleman PS3005D | `KA3005P::/dev/ttyACM0` | voltage, current, cv | vset, iset, ovp_on, ocp_on, out | `SIM::KA3005P` |
| Korad KA6003P, KD6005P, Tenma 72-2550 (60 V) | `KA6003P::/dev/ttyACM0` | as above | as above | `SIM::KA6003P` |
| Kiprim DC310Pro (supply + multimeter), OWON SPM3103, Multicomp Pro MP711134 (USB, 115200 baud) | `DC310PRO::/dev/ttyUSB0` | voltage, current, power, dmm, prot, mode | vset, iset, ovp, ocp, function (multimeter), out | `SIM::DC310PRO` |
| Kiprim DC605Pro (supply + multimeter), OWON SPM6053 | `DC605PRO::/dev/ttyUSB0` | as above | as above | `SIM::DC605PRO` |
| Atorch PX-100, and DL24 units whose firmware answers its protocol (not all do) | `PX100::/dev/ttyUSB0` | voltage, current, power, ah, wh, temp | cc, cutoff, input | `SIM::PX100` |
| Center 309, Voltcraft K204 (4 thermocouples) | `CENTER309::/dev/ttyUSB0` | t1–t4 | | `SIM::CENTER309` |
| DS18B20 (Pi 1-Wire, or any Linux 1-Wire bus) | `W1::` | one per sensor | | `SIM::W1` |
| MAX31855 thermocouple board (SPI) | `MAX31855::/dev/spidev0.0` | temperature, cold junction | | `SIM::MAX31855` |
| MAX31865 RTD board (SPI) | `MAX31865::/dev/spidev0.0` | temperature, resistance | | `SIM::MAX31865` |
| East Tester ET44, ET45 LCR (SCPI over USB serial) | `ET44::/dev/ttyUSB0` | both parameters | function, frequency, level… | `SIM::ET44` |
| Keysight E4980A / E4980AL LCR | `SCPI:E4980A` (auto-detected) | both parameters | function, frequency, level… | `SIM::E4980A` |

All of these are at the **Simulator** level. Any instrument that is missing can be added with a file, see [Add your own instrument](06-howtos.md#add-your-own-scpi-instrument).

### Oscilloscope compatibility

| Family | Model key | Command set used | Auto-detect | Simulator |
|---|---|---|---|---|
| Rigol DS1000Z (DS1054Z, DS1104Z, MSO1000Z), DS2000A, MSO5000, DHO800/900 | `SCOPE:RIGOL` | `:WAV:DATA?` bytes with `:WAV:PRE?` scaling | yes | `SIM::SCOPE` or `SIM::DS1054Z` |
| Keysight / Agilent InfiniiVision 1000X–6000X (DSOX, MSOX, EDUX) | `SCOPE:KEYSIGHT` | `:WAV:DATA?` bytes, `:WAV:PRE?` | yes | `SIM::DSOX` |
| Tektronix TBS1000C, TBS2000, MDO3, MSO2/4/5/6, DPO2000/3000 | `SCOPE:TEK` | `CURVE?` signed bytes, `WFMOUTPRE?` | yes | `SIM::TBS` |
| Siglent SDS1000X-E, SDS2000X-E, SDS1000X | `SCOPE:SIGLENT` | the classic `C1:WF? DAT2` set | yes | `SIM::SDS` |

All at the **Simulator** level. Newer Siglent models (SDS800X HD, SDS2000X HD, SDS3000X HD) use a different, SCPI-style command set and are not supported yet; nor are the analyzers' digital channels on MSO models (use a [logic-analyzer tab](03-users-guide.md#logic-analyzer-panel-saleae-and-sigrok) for those).

### Several of the same instrument

Any number of instruments of any kind can be open at once, including several of the same model (two PPK2s, three DP832s). Each gets its own tab; give each a name (the box under the instrument's ID) and the sequencer, watchdog, math and Remote API tell them apart by it. Two PPK2s can be lined up in time through a shared logic input: see [Several PPK2s](03-users-guide.md#several-ppk2s-in-step).

### Not supported

- **Prologix-style** USB/LAN-to-GPIB adapters: the app expects a linux-gpib board.
- **Keithley TSP mode** (DMM6500/7510 default on some units, 2450/2460 SMUs): switch the instrument to its SCPI command set.
- **Instruments without a remote interface** (E3610A and similar): see [below](#agilent-e3610a-not-remotely-controllable).
- **Oscilloscope protocol decoding and digital channels**: use a logic-analyzer tab.

## The computer

- **OS:** tested on Ubuntu 26.04.1 LTS (kernel `7.0.0-43-generic`). Any Linux works if you can build kernel modules for it.
- **Python:** 3.12 or newer, with PyQt6 installed from the distribution (`python3-pyqt6`).
- **Build tools** for the GPIB driver: `build-essential`, `autoconf`, `automake`, `libtool`, `flex`, `bison`, and the kernel headers that match your running kernel. The installation script installs all of these.
- **Secure Boot:** if it is enabled, the out-of-tree GPIB module has to be signed (MOK enrolment) before it will load. On the test machine Secure Boot is off.
- **A USB 2.0 port** for the GPIB adapter. Avoid unpowered hubs.

## GPIB interface: NI GPIB-USB-HS

The GPIB-USB-HS is the system controller for the bus. On Linux it is driven by the open-source **linux-gpib** project (kernel module `ni_usb_gpib`).

Ubuntu builds its kernel with `CONFIG_GPIB` turned off, so the module has to be built from source. `scripts/install-gpib-driver.sh` does this for you; see the **Installation** guide.

Other controllers supported by linux-gpib also work, such as the Agilent 82357A/B or a PCI-GPIB card. Edit `board_type` in `/etc/gpib.conf` to match yours. Prologix-style serial adapters are *not* supported, because the app expects a real GPIB board.

## Instruments in detail

### Agilent 66321D (and 66311B / 66321B)

| Parameter | Value (read from the test unit) |
|---|---|
| Output voltage | 0 to 15.356 V |
| Current limit | 0 to 3.071 A |
| Voltage limit (`VOLT:PROT`) | 0 to 22 V; plus an over-voltage trip ~2 V above the setting |
| Output resistance (battery emulation) | −40 mΩ to +1 Ω |
| Current measurement ranges | 3 A, 1 A and 20 mA |
| Digitizer | up to 4096 points at 15.6 µs (64 kS/s) |
| DVM input (D models only) | rear-panel DVM terminals |
| Factory GPIB address | 5 |

What the app uses: essentially everything. Setpoints, output on/off, voltage limit, over-voltage trip, over-current protection (OCP) and its delay, open-sense detection, output resistance, current range, measurement window, ACDC/DC detector, digitizer settings, pulse statistics (max, min, high, low, RMS), DVM readings, triggered and pre-triggered waveform capture (level, external and output-step triggers, multiple segments), output transients, output compensation mode, the rear control port (remote inhibit, fault output, digital I/O, external trigger), the front-panel display, saved setups, power-on state, self-test and instrument information.

### HP 6632A / 6633A / 6634A

| Model | Voltage | Current | OVP |
|---|---|---|---|
| 6632A | 0 to 20.475 V | 0.02 to 5.119 A | 0 to 22 V |
| 6633A | 0 to 51.188 V | 0.008 to 2.048 A | 0 to 55 V |
| 6634A | 0 to 102.38 V | 0.004 to 1.024 A | 0 to 110 V |

These supplies use HP's command language from before SCPI (`VSET`, `ISET`, `VOUT?`, `STS?` …). **They cannot report their setpoints back**, so the app shows the values it last sent and treats them as unknown until you set them. The supply reports its model with `ID?`, so the app's auto-detect works.

### FNIRSI DPS-150

| | |
|---|---|
| Output | 0 to 30 V (never above the input voltage minus about 1 V), 0 to 5.1 A, 150 W |
| Protection | OVP, OCP, OPP, OTP and LVP thresholds; a trip turns the output off |
| Readback | Setpoints, output V / I / P, CC/CV mode, output state and protection state |

The DPS-150 is a USB serial device with FNIRSI's own binary protocol (not SCPI). The app lists it as `DPS150::/dev/serial/by-id/…` when it is plugged in; from the command line or `config.toml` use `DPS150@/dev/ttyACM0` or `resource = "DPS150::/dev/ttyACM0"`. You need to be in the `dialout` group, and FNIRSI's PC software must be closed. While the app is connected, the supply is in remote mode; closing the tab hands control back to the front panel.

- **OCP** works differently from the 663xx. The DPS-150 has an OCP *threshold*. Ticking **OCP** sets that threshold to the current limit (and keeps it there as you change the limit), so the output trips off instead of regulating in CC. Unticking it moves the threshold above 5.1 A.
- **Clear trip** turns the output off. The supply has no clear command: fix the cause, then turn the output on again.
- **Safe state** (the reset button) turns the output off, sets 0 V and opens the protection thresholds up. There is no `*RST`.
- The **Console** tab takes hex frames: `START CMD [DATA…]`, and the app adds the length and checksum. For example `a1 de 00` asks for the model name and `b1 d6 0c` sets the brightness to 12. A line starting with `f1` is sent exactly as typed.
- FNIRSI doesn't publish the protocol. The driver follows [cho45/fnirsi-dps-150](https://github.com/cho45/fnirsi-dps-150) and [Philipp-IoT/fnirsi_protocol](https://philipp-iot.github.io/fnirsi_protocol/).

### B&K Precision 393 and 393B

| | 393 | 393B |
|---|---|---|
| Display | 60,000 counts | 100,000 counts, two lines |
| USB cable | FTDI serial (opto-isolated, USB390A) | CP210x serial, optical |
| Protocol | One way: the meter streams 10-byte frames, 9600 8N1 | Request / reply, 9600 8N1 (APPA 500 series) |
| From the app | Reading, function | Reading, function, auto/manual range, HOLD/MIN/MAX/REL…, second line |

Both are **read-only**: function and range are set with the meter's knob and buttons, and the tab follows. Their USB cables use generic serial chips, so the app can't recognise them: pick the `/dev/ttyUSB…` port and the **B&K 393** or **B&K 393B** model. From the command line or `config.toml` use `393@/dev/ttyUSB0`, `393B@/dev/ttyUSB0`, or `resource = "BK393B::/dev/serial/by-id/…"` (that path stays the same when port numbers change). You need to be in the `dialout` group.

- **393:** press the **RS232** key (6000/RS232) to turn the computer interface on. To stop the meter switching itself off after 30 minutes, hold **MAX/MIN** while turning it on.
- **393B:** nothing to switch on; set auto power off to *never* in the meter's setup menu for long runs. It measures 3 times a second, so the tab asks every 300 ms.
- **Overload** shows as OL and leaves a gap in the trend and CSV. A range code that isn't in the 393's table shows as "unknown range" instead of a guess; please report it with the frame from the **Console** tab.
- The driver only ever sends the 393B's read commands. The same protocol can write calibration and firmware, so the Console refuses everything else.
- B&K publishes no protocol for either. The 393 follows B&K's AK393 software (serial settings, frame, scale table, status bits) and [dstahlke's Linux driver](https://gist.github.com/dstahlke/d0621a061eb8839e2f5f). The 393B follows libsigrok's [appa-dmm](https://github.com/Cymaphore/libsigrok) driver; B&K's own software for it is APPA's WinDMM500, whose function, unit and status tables match. Older meters that use APPA's earlier frames are detected automatically.
- There is no 393A in B&K's catalogue; `393A` is accepted as another name for the 393.

### Kiprim DC310Pro and DC605Pro

A supply and a multimeter in one, over USB (a serial port, 115200 baud). Choose *DC310PRO* or *DC605PRO* as the model, or use **Identify…**. `voltage` and `current` are what the supply's output delivers; `dmm` is what the multimeter's jacks see, so the two can be measured at once (for example the rail at the board with `dmm`, and the supply's own current).

- No programming manual for these two was found. The commands are those of OWON's SPM series, whose ratings match (DC310Pro / SPM3103, DC605Pro / SPM6053), as used by [multicomp-mcp](https://github.com/cameroncoats/multicomp-mcp), [OpenBench](https://github.com/VlasovLabTech/OpenBench-lab-control) and [owon-psu](https://github.com/robbederks/owon-psu-control) on real SPM units. **Not yet confirmed on a Kiprim**: try it first with nothing connected to the output.
- Switching off is repeated until the supply answers that its output is off.
- Changing the multimeter's function takes about a second. When it is changed at the front panel, the app follows with the next reading.
- Not supported: the multimeter's ranges (it auto-ranges), hold and relative; the supply's list mode, which has no remote commands.
- `*RST` in the Console is a factory reset.

### OWON XDM1041, XDM1241 and XDM2041

Bench meters that share one command set: the XDM1041 (mains), the XDM1241 (the same with a battery) and the XDM2041 (adds 4-wire ohms). Plug in USB, pick the `/dev/ttyUSB…` port and the **OWON XDM1041** model, or use `XDM1041@/dev/ttyUSB0` or `resource = "XDM::/dev/serial/by-id/…"`. The meter's baud rate must be 115200, its default. You need to be in the `dialout` group.

- **Function, range, rate and the second display** can be set in the tab, and it follows the meter's own buttons, which stay unlocked. A session remembers only the name and poll delay; `function`, `range` and `rate` in `config.toml` are applied when connecting.
- **AUTO** on these meters stays within a group of ranges. **AUTO (mV)** and **AUTO (mA)** start in the low ranges. The (mA) sequence is inferred from the mV one.
- **Current:** the 10 A and mA inputs are different jacks. Owners report a reading of 0 when the 10 A jack is used with a mA range selected, *although current is flowing*.
- **Poll delay** is 250 ms (the slow rate measures 4 times a second). Lower it for the Medium and Fast rates. Owners have seen only 2–3 new readings a second over USB.
- **K type:** the meter stops answering for about 3 s every 10 minutes while it measures its cold junction. The driver waits.
- **The Console accepts documented measuring commands only.** An owner reports an undocumented command (`INT:START`) that starts a factory calibration.
- The XDM3041 and XDM3051 use a different command set. The driver refuses XDM3… meters; try the generic *Any SCPI meter* definition.
- Sources: OWON's XDM2041 programming manual, [TheHWcave's XDM1041 command list](https://github.com/TheHWcave/OWON-XDM1041), [ElDuderino's session log](https://github.com/ElDuderino/XDM1041Python) (V4.0.0), [an XDM1241 reference](https://kenosinc.github.io/sigrok-mcp-server/devices/owon-xdm1241) (V4.3.0) and libsigrok's scpi-dmm driver (overload limit).

### HP 3456A digital multimeter

| Parameter | Value |
|---|---|
| Functions | DCV, ACV, ACV+DCV, 2-wire Ω, 4-wire Ω, offset-compensated Ω, DCV/DCV ratio |
| Voltage ranges | 100 mV, 1 V, 10 V, 100 V, 1000 V, plus auto |
| Resistance ranges | 100 Ω to 1 GΩ, plus auto |
| Integration time | 0.01, 0.1, 1, 10 or 100 power-line cycles (PLC) |
| Factory GPIB address | **22** (set on the rear-panel switches) |

The 3456A has **no identification query**, so it can't be auto-detected. You choose it by model when you connect. The app checks that it's there by taking a reading. Make sure the rear-panel **TALK ONLY** switch is off, otherwise it won't accept commands.

### Nordic Power Profiler Kit II (PPK2)

| Parameter | Value |
|---|---|
| Sample rate | 100 kS/s, streamed continuously |
| Current | 200 nA to 1 A, five shunt ranges switched automatically by the PPK2 |
| Source meter | Supplies the DUT from **VOUT**, 800 to 5000 mV |
| Ampere meter | The DUT keeps its own supply; current flows **VIN → VOUT** through the PPK2 |
| Logic inputs | D0–D7, sampled with every current sample |
| Interface | USB (the **USB DATA/POWER** port). No GPIB or linux-gpib needed |

The PPK2 shows up as **two** serial ports (`/dev/ttyACM0` and `/dev/ttyACM1`, say). Only the first carries data. The app lists just that one, as `PPK2::/dev/serial/by-id/usb-Nordic_Semiconductor_PPK2_<serial>-if01`. The by-id name keeps working when the `ttyACM` numbers change after replugging. You need to be in the `dialout` group to open it.

Each unit stores its own calibration, which the app reads at connect. The PPK2 can't report whether DUT power is on, so the button shows **?** until you set it. Close nRF Connect Power Profiler first: only one program can read the stream.

### Saleae Logic 8 / Logic Pro 8 / Logic Pro 16

| | |
|---|---|
| Channels | 8 or 16 digital; each can also be an analog input (Logic 8: 8; Pro 16: 16) |
| Digital rate | Up to 100 MS/s (Logic 8) or 500 MS/s (Pro), less with many channels on |
| Analog rate | Up to 10 MS/s (Logic 8) or 50 MS/s (Pro) |
| Threshold | Pro: 1.2, 1.8 or 3.3 V; Logic 8: fixed |

Saleae doesn't publish the USB protocol of these analyzers. The only supported way to control them is **Logic 2's automation server**, so OtterBench talks to a running copy of Logic 2 (on this computer, port 10430), and Logic 2 talks to the analyzer. Logic 2 runs on x86-64 Linux, Windows and macOS, **not on the Raspberry Pi**, so on the Pi only the simulator (`SIM::SALEAE`) is available.

Older Saleae models (the original 8-channel Logic and the original Logic16) aren't supported by Logic 2. Use them through sigrok instead (below).

### sigrok-supported logic analyzers

| | |
|---|---|
| Examples | 24 MHz 8-channel "Saleae Logic clones" (fx2lafw, a few dollars), original Saleae Logic and Logic16, DreamSourceLab DSLogic, Hantek 4032L, Kingst LA2016, … ([full list](https://sigrok.org/wiki/Supported_hardware)) |
| Channels | Digital only, as many as the analyzer has |
| Rates | As the analyzer allows (fx2lafw: 20 kHz to 24 MHz; at 24 MHz with many channels, USB 2 may not keep up) |
| Triggers | Edges and levels, in software |

These run through `sigrok-cli`, so they also work on the Raspberry Pi. A cheap fx2lafw clone is a good second analyzer just for the PPK2 shared signal.

### Function generators

| | |
|---|---|
| Keysight / Agilent | 33210A, 33220A, 33250A, 33500B series (33509B–33522B), 33600A series: **SCPI** (`SOUR1:FREQ 1000`, `OUTP1 ON`) |
| Rigol | DG800, DG900, DG1000Z, DG4000/5000: the same **SCPI** commands |
| Siglent | SDG800, SDG1000X, SDG2000X, SDG6000X (and their Owon / Teledyne LeCroy rebadges): **Siglent's** `C1:BSWV …` commands |
| Others | Any SCPI generator that takes `SOUR{n}:FUNC / FREQ / VOLT / VOLT:OFFS` and `OUTP{n}` should work: choose model **Function generator** by hand. Tektronix AFG models use a different syntax and are not yet supported. |
| Controlled | Waveform (sine, square, ramp, pulse, noise, DC), frequency, amplitude (Vpp), offset, square duty, output on/off, per channel; plus [profiles](03-users-guide.md#profiles-varying-a-setpoint-over-time) (frequency sweeps, amplitude steps) |
| Not controlled | Arbitrary waveforms, modulation, sweep/burst modes on the generator itself (use the front panel or the Console tab), the output-load setting |

Connect them by **LAN** (simplest: `TCPIP::192.168.1.50::INSTR`, nothing to install), **USB** (`USB0::0x1AB1::0x0642::DG1ZA…::INSTR`; needs a udev rule, see [Installation](02-installation.md#function-generators-optional)) or **GPIB** if the model has it. Give the generator a fixed IP address on its own front panel (Utility → I/O → LAN), or a DHCP reservation in your router.

> **Amplitude is into the generator's load setting.** Most generators assume a 50 Ω load. Into a high-impedance input (a DUT's pin, a scope at 1 MΩ) the voltage is up to twice what the app shows. Set the generator's output load to **High Z** on its front panel if that's how you use it. Watch the offset too: a 3.3 V logic input driven with 5 Vpp around 0 V gets −2.5 V.

### Raspberry Pi GPIO header

On a Raspberry Pi (3, 4, 5, Zero 2 W; the 40-pin header of every Pi since the B+) the app can drive and read the header's GPIO pins: switch relays, press a DUT's buttons, pulse its reset, wait for a "done" pin. The 26 usable pins are:

| GPIO | Pin | | GPIO | Pin | |
|---|---|---|---|---|---|
| 2 | 3 | I²C SDA (1.8 kΩ pull-up on the board) | 14 | 8 | serial console TX |
| 3 | 5 | I²C SCL (1.8 kΩ pull-up on the board) | 15 | 10 | serial console RX |
| 4 | 7 | | 18 | 12 | |
| 17 | 11 | | 23 | 16 | |
| 27 | 13 | | 24 | 18 | |
| 22 | 15 | | 25 | 22 | |
| 10 | 19 | SPI MOSI | 8 | 24 | SPI CE0 |
| 9 | 21 | SPI MISO | 7 | 26 | SPI CE1 |
| 11 | 23 | SPI SCLK | 12 | 32 | |
| 5 | 29 | | 16 | 36 | |
| 6 | 31 | | 20 | 38 | |
| 13 | 33 | | 21 | 40 | |
| 19 | 35 | | | | |
| 26 | 37 | | | | |

Ground is on pins 6, 9, 14, 20, 25, 30, 34 and 39; 3.3 V on 1 and 17; 5 V on 2 and 4. Pick pins with nothing in the third column first: they're free unless you've enabled I²C, SPI or the serial console (with those on, the app can't claim them and says so).

**Electrical limits.** The pins are **3.3 V logic and not 5 V tolerant**: a 5 V signal on an input damages the Pi. An output drives about 3.3 V at a few mA (keep each under 8–16 mA, and the total small). So:

- **A relay:** use a relay *board* with a driver transistor and flyback diode (the common 1/2/4/8-channel "5 V relay modules" with opto-isolators), powered from the Pi's 5 V or better its own supply, with IN wired to a GPIO. Most switch on when IN is *low*: tick **Active low** for that pin, so "on" means on and **Esc** (all outputs off) releases the relay. Never drive a bare relay coil from a pin.
- **A DUT's button:** wire an NPN transistor or small N-MOSFET (2N7000, BSS138) across the button (drain to the button's signal side, source to DUT ground, gate to the GPIO through 100 Ω), with the Pi's and DUT's grounds joined. **Pulse** then "presses" it. An optocoupler (PC817) does the same with no shared ground.
- **A DUT's reset or another logic input at 3.3 V:** connect directly through a 330 Ω–1 kΩ series resistor (it limits the current if both sides drive at once), grounds joined.
- **Reading a DUT's 3.3 V output** ("done", "error"): directly, through 1 kΩ. From a **5 V or higher** signal: a resistor divider (10 kΩ over 20 kΩ to ground gives 3.3 V from 5 V) or a level shifter.
- **A switch or e-stop button:** between the GPIO and ground, with **Pull = up** in the GPIO tab. It reads low when pressed (or tick Active low so pressed = 1). The [watchdog](03-users-guide.md#watchdog) can then stop a sequence on it.

Off the Pi, a USB GPIO board isn't supported yet. On a PC, use the Pi as a [remote bench](03-users-guide.md#remote-api-python-and-jupyter) or a sigrok analyzer's inputs to read signals.

### Generic SCPI meters, loads and supplies

These instruments are described by [definition files](04-technical-reference.md#instrument-definition-files-toml) instead of code: which commands set each setting and which queries give each reading. The tab built from a definition shows:

- **Readings** as large readouts (grouped by output on a multi-output supply), polled one after another as fast as the instrument answers (or with a set delay).
- **Output / Input** buttons for every switch the definition marks as an output. **All outputs OFF (Esc)** sends the definition's `off` commands.
- **Settings** forms: a list for choices (meter function, load mode), a tick box for on/off, a number box with the instrument's limits.
- A **Trend** plot per reading (one curve per output), CSV recording and a **Console** for typing raw commands.

**Connecting.** Most of these instruments have LAN, USB-TMC and sometimes GPIB or RS-232. LAN is the easiest: give the instrument a fixed address on its front panel and use `TCPIP::<ip>::INSTR` (VXI-11). USB instruments appear in the resource list as `USB0::…::INSTR` once the udev rules from the setup script are installed (see [Installation](02-installation.md#scpi-meters-loads-supplies-and-oscilloscopes-optional)).

**Meters** set their function from the app (DC V, AC V, DC I, Ω 2-wire / 4-wire, frequency, …) and read with `READ?` at auto range. The unit on the readout follows the function. A reading of `9.9E+37` (overload) shows **OVLD**.

**Electronic loads** have a **Mode** (CC, CV, CR, CP) and a level for each mode, plus an **Input** switch. Set the level for the mode before switching the input on. The load's readings are voltage, current and power.

**Multi-output supplies** have their settings per output (`vset1`, `iset2`, `out3`, …). On the Rigol DP832 and Keysight E36312A, the watchdog can push its limits into the supply's own OVP / OCP: see [Watchdog → In HW](03-users-guide.md#watchdog).

### Oscilloscopes

A scope tab controls the scope and fetches what it captures:

- **Run**, **Stop**, **Single → capture** (arms, waits for the trigger, fetches every channel shown) and **Grab screen** (fetches what is on screen now, without re-arming).
- Per channel: on/off, V/div, offset. Timebase (s/div, position), trigger source, level, slope and sweep (auto, normal, single).
- The fetched **Waveforms** plotted in volts against time from the trigger, with a table of Vpp, max, min, mean, RMS, frequency, period, duty, rise and fall time per channel, worked out from the samples (the scope's own measurement unit isn't used, so every brand gives the same numbers).
- Captures go into the [session](03-users-guide.md#sessions-everything-in-one-file) being recorded and can be triggered by the sequencer and the watchdog.

Waveforms are fetched as bytes (8 bits per sample). Rigol and Keysight scopes send their screen record (about 1200–2400 points per channel); Tektronix sends the record length set on the scope (up to 1 M points: lower it for faster transfers); Siglent sends its current acquisition. Deep-memory ("RAW" mode) transfers of Rigol and Keysight scopes are not used: they are slow over USB and LAN, and the screen record is what you see.

### Agilent E3610A: not remotely controllable

The E3610A is a manual bench supply (front-panel knobs, analog meters) with **no GPIB, RS-232 or USB port**, so no software can control or read it directly. Two practical options:

1. **Monitor it with the 3456A.** Connect the 3456A's HI/LO inputs across the E3610A's output, then open the 3456A in OtterBench and type a channel name such as "E3610A output". You get a logged, plotted, 6½-digit record of its voltage.
2. Measure its current by putting a precision shunt in series and reading the shunt voltage with the 3456A (DCV, 100 mV range).

## GPIB addresses

Every instrument on the bus needs its own address (0 to 30). The controller uses address 0.

| Instrument | Factory default | Suggested | How to change it |
|---|---|---|---|
| 66321D | 5 | **5** | Front panel: **Address** menu |
| 6632A | 5 on many units (check yours) | **6** | Front panel: **LCL/ADDR** key, see manual §5 |
| 3456A | 22 | **22** | Rear-panel address switches; power-cycle after changing |

> **Watch for address conflicts.** The 66321D and the 6632A may both ship at address 5. Change one of them *before* connecting both to the bus. Two devices on the same address corrupt every transfer.

Once everything is cabled, press **⌕ Scan GPIB** in the app to see which addresses respond.

## GPIB cabling rules (IEEE-488)

- At most **15 devices** on one bus, including the controller.
- Total cable length at most **20 m**, and no more than **2 m per device** on the bus.
- Daisy-chain or star; stack the connectors no more than about three deep.
- Tighten the connector thumbscrews. A loose GPIB connector causes intermittent timeouts.
- At least two-thirds of the devices should be powered on while the bus is in use.

A typical bench needs one GPIB-USB-HS and two or three 1 m or 2 m GPIB cables.

## Bench wiring tips

- **PPK2 + logic analyzer:** to line their timing up, connect one DUT signal to both: an analyzer channel and a PPK2 logic input (the 10-pin header's D0–D7, with its GND and its VCC tied to the DUT's I/O voltage). Use a signal that changes at irregular times, such as UART TX or a debug pin.

- **Function generator into a DUT:** check the amplitude and offset with the output **off** first, and set the generator's load to High Z for a high-impedance input. A [watchdog](03-users-guide.md#watchdog) rule "*supply voltage < 2.9 → turn off* the generator" stops it driving a DUT pin while the DUT is unpowered (back-powering through its ESD diodes).
- **GPIO and the DUT:** always join the grounds (unless through an optocoupler or relay), and see [the limits above](#raspberry-pi-gpio-header).

- **Two PPK2s on one DUT** (two rails, or the DUT and a second board): join their grounds, then wire one DUT signal that changes at irregular times to the **same logic input** (e.g. D7) on both. That signal is how the app lines their time axes up to within a few tens of µs.
- **Heartbeat to an external watchdog:** for unattended runs, a GPIO output toggled by the app can drive a retriggerable timer (a 74HC123 monostable, a 555 in missing-pulse mode, or a small microcontroller) whose output holds a relay or a load switch in the DUT's supply. If the app hangs or the computer stops, the toggling stops, the timer runs out and the DUT's power is cut. Set it up in **Tools ▾ → Safety…** ([details](03-users-guide.md#safety-and-unattended-runs)).
- **Electronic load on a DUT's output:** use short, thick leads, and the load's remote-sense inputs for accurate CV/CR. Set the load's level *before* switching its input on, and keep the source's current limit above the load's CC level.

- **Remote sense:** for accurate voltage at the device under test, use the 66321D's sense terminals (+S/−S) and set the output compensation to match your leads. With local sensing, the voltage reading is taken at the rear terminals.
- **Pulsed loads (e.g. phone transmit bursts):** keep the leads short and twisted, set the current detector to ACDC, and use the pulse statistics.
- **3456A ohms:** use 4-wire Ω for anything below about 100 Ω. Press **Null** with the leads shorted to remove lead resistance in 2-wire mode.

## Reference manuals

| Document | Where |
|---|---|
| HP 6632A/6633A/6634A Operating Manual (5957-6360). Section VI covers remote programming | [Keysight](https://www.keysight.com/us/en/assets/9018-05839/user-manuals/9018-05839.pdf) · [Internet Archive](https://archive.org/details/hp-6632-a-33-a-34-a-operating) |
| HP 3456A Operating & Service Manual | [ManualsLib](https://www.manualslib.com/manual/1303474/Hp-3456a.html) |
| HP 3456A quick-reference card (M. Hepperle) | [mh-aerotools.de](https://mh-aerotools.de/hp/documents/HP%203456A%20-%20Quick-Reference.pdf) |
| Agilent 66311B/66321B/D User's Guide | Keysight website, search "66321D user's guide" |
| Saleae Logic 2 automation API | [saleae.github.io/logic2-automation](https://saleae.github.io/logic2-automation/) |
| Saleae binary export format | [support.saleae.com](https://support.saleae.com/faq/technical-faq/binary-export-format-logic-2) |
| Keysight 33500B / 33600A programming (SCPI) | Keysight website, search "33500B series user's guide" (programming chapter) |
| Rigol DG1000Z programming guide | Rigol website, search "DG1000Z programming guide" |
| Siglent SDG programming guide (`C1:BSWV`) | Siglent website, search "SDG programming guide" |
| Keysight 34461A / 34465A operating and service guide (SCPI chapter) | Keysight website, search "Truevolt DMM operating and service guide" |
| Keithley 2000 user's manual, DMM6500 reference manual (SCPI) | Tektronix/Keithley website |
| Rigol DM3058, DL3000, DP800 programming guides | Rigol website, search the model and "programming guide" |
| Siglent SDM3000X, SDL1000X, SPD3303X programming guides | Siglent website |
| Keysight E36300 series programming guide | Keysight website, search "E36300 programming guide" |
| Keithley 2380 / B&K 8600 programming manual | Tektronix/Keithley or B&K Precision website |
| Rigol DS1000Z programming guide | Rigol website, search "MSO1000Z DS1000Z programming guide" |
| Keysight InfiniiVision 2000X/3000X programmer's guide | Keysight website |
| Tektronix TBS1000C / 2 Series MSO programmer manual | Tektronix website |
| Siglent "Digital Oscilloscope Programming Guide" (SDS1000X-E series) | Siglent website |
| Raspberry Pi GPIO and header | [raspberrypi.com/documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#gpio) · [pinout.xyz](https://pinout.xyz/) |
| libgpiod | [git.kernel.org libgpiod](https://git.kernel.org/pub/scm/libs/libgpiod/libgpiod.git/about/) |
| linux-gpib project | [linux-gpib.sourceforge.io](https://linux-gpib.sourceforge.io/) |
