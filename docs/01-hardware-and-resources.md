---
title: Hardware & Resources
subtitle: What you need, how to cable it, and where the reference manuals are
---

# Hardware & Resources

This guide lists the hardware EE Workbench supports, what each instrument can do from software, how to set GPIB addresses and cables, and where to find the original manuals.

## At a glance

| Item | Role | Interface | Status in EE Workbench |
|---|---|---|---|
| **Agilent 66321D** Mobile Communications DC Source | Main supply with DVM input | GPIB (and RS-232) | **Fully supported**. Tested on real hardware (fw A.03.01) |
| Agilent 66311B / 66321B | Same family without DVM | GPIB / RS-232 | Supported by the same driver; DVM hidden if absent |
| **HP 6632A** System DC Power Supply (also 6633A, 6634A) | Second supply, 20 V / 5 A | GPIB only | Supported. Written from the manual and tested on the simulator |
| **HP 3456A** 6½-digit DMM | Precision voltage / resistance | GPIB only | Supported. Written from the manual and tested on the simulator |
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
| Over-voltage protection (OVP) | up to 22 V |
| Output resistance (battery emulation) | −40 mΩ to +1 Ω |
| Current measurement ranges | 3 A (HIGH) and 20 mA (LOW) |
| Digitizer | up to 4096 points at 15.6 µs (64 kS/s) |
| DVM input (D models only) | rear-panel DVM terminals |
| Factory GPIB address | 5 |

What the app uses: setpoints, output on/off, OVP, over-current protection (OCP), output resistance, current range, ACDC/DC detector, digitizer settings, pulse statistics (max, min, high, low, RMS), DVM readings and waveform capture.

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
| linux-gpib project | [linux-gpib.sourceforge.io](https://linux-gpib.sourceforge.io/) |
