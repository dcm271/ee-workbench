---
title: Hardware & Resources
subtitle: What you need, how to cable it, and where the reference manuals are
---

# Hardware & Resources

Other chapters: [Installation](02-installation.md) · [User's Guide](03-users-guide.md) · [Technical Reference](04-technical-reference.md) · [Raspberry Pi](05-raspberry-pi.md) · [How-to guides](06-howtos.md) · [README](../README.md)

This guide lists the hardware EE Workbench supports, what each instrument can do from software, how to set GPIB addresses and cables, and where to find the original manuals.

## At a glance

| Item | Role | Interface | Status in EE Workbench |
|---|---|---|---|
| **Agilent 66321D** Mobile Communications DC Source | Main supply with DVM input | GPIB (and RS-232) | **Fully supported**. Tested on real hardware (fw A.03.01) |
| Agilent 66311B / 66321B | Same family without DVM | GPIB / RS-232 | Supported by the same driver; DVM hidden if absent |
| **HP 6632A** System DC Power Supply (also 6633A, 6634A) | Second supply, 20 V / 5 A | GPIB only | Supported. Written from the manual and tested on the simulator |
| **HP 3456A** 6½-digit DMM | Precision voltage / resistance | GPIB only | Supported. Written from the manual and tested on the simulator |
| **Nordic Power Profiler Kit II** (PPK2) | 100 kS/s current profiler and 0.8–5 V source | USB (ID `1915:c00a`, serial) | Supported. Tested on real hardware (HW 8682) |
| **Saleae Logic 8 / Logic Pro 8 / Logic Pro 16** | Logic analyzer with analog inputs | USB, through the **Logic 2** software (automation API) | Supported. Written from Saleae's API and file-format docs and tested on the simulator; not yet tried on real hardware |
| sigrok-supported logic analyzers (fx2lafw clones, DSLogic, …) | Logic analyzer | USB, through `sigrok-cli` | Supported, including on the Raspberry Pi. Tested on the simulator |
| **Function generators**: Keysight 33500B/33600A/33210A/33220A, Rigol DG800/DG900/DG1000Z/DG4000, Siglent SDG800/1000X/2000X/6000X | Signal source, 1–2 channels | LAN (VXI-11 / socket), USB-TMC or GPIB | Supported (waveform, frequency, amplitude, offset, duty, output; profiles). Written from the programming guides and tested on simulators of both command sets |
| **Raspberry Pi 40-pin GPIO header** | Relays, buttons, resets, "done" inputs | libgpiod (`/dev/gpiochip*`) | Supported on Pi 3/4/5/Zero 2 W. Tested on the simulator |
| **Agilent E3610A** bench supply | Manual bench supply | **None** | Cannot be controlled. Monitor its output with the 3456A instead (see below) |
| **NI GPIB-USB-HS** | USB-to-GPIB controller | USB 2.0 (ID `3923:709b`) | Supported through linux-gpib |
| Linux PC | Runs the application | — | Ubuntu with kernel 7.0 tested |

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

Saleae doesn't publish the USB protocol of these analyzers. The only supported way to control them is **Logic 2's automation server**, so EE Workbench talks to a running copy of Logic 2 (on this computer, port 10430), and Logic 2 talks to the analyzer. Logic 2 runs on x86-64 Linux, Windows and macOS, **not on the Raspberry Pi**, so on the Pi only the simulator (`SIM::SALEAE`) is available.

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

### Agilent E3610A: not remotely controllable

The E3610A is a manual bench supply (front-panel knobs, analog meters) with **no GPIB, RS-232 or USB port**, so no software can control or read it directly. Two practical options:

1. **Monitor it with the 3456A.** Connect the 3456A's HI/LO inputs across the E3610A's output, then open the 3456A in EE Workbench and type a channel name such as "E3610A output". You get a logged, plotted, 6½-digit record of its voltage.
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
| Raspberry Pi GPIO and header | [raspberrypi.com/documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#gpio) · [pinout.xyz](https://pinout.xyz/) |
| libgpiod | [git.kernel.org libgpiod](https://git.kernel.org/pub/scm/libs/libgpiod/libgpiod.git/about/) |
| linux-gpib project | [linux-gpib.sourceforge.io](https://linux-gpib.sourceforge.io/) |
