---
title: Technical Reference
subtitle: Command sets, measured timings, architecture and extending the app
---

# Technical Reference

## Architecture

```
MainWindow
 ├─ SupplyPanel (66321D) ── worker thread ── Agilent663xx ──┐
 ├─ SupplyPanel (6632A)  ── worker thread ── HP6632A ───────┼─ VisaTransport
 └─ DmmPanel    (3456A)  ── worker thread ── HP3456A ───────┘       │
                                                       pyvisa-py ─ gpib-ctypes
                                                                    │
                                              libgpib ─ ni_usb_gpib (kernel)

 └─ Ppk2Panel   (PPK2)   ── worker thread ── PPK2 ── SerialStream (pyserial + reader thread)
```

- **One I/O thread per instrument** (`worker.py`). The GUI never blocks on the bus. Commands from the GUI go into a queue and run before the next poll.
- After each command the worker drains the instrument's error queue and re-reads the instrument's state, so the GUI always shows what the instrument actually has.
- If a poll fails, polling pauses (no flood of errors) until you press **Resume polling**.
- Each poll uses a **snapshot** of the poll settings, so changing a checkbox mid-poll can't make the query and its parsing disagree.
- **Shared clock:** every reading is stamped with seconds since the app started (`worker.now()`).
- **Transports** (`transport.py`) all provide `write / read / query / read_stb / clear / temp_timeout`. A real session (`VisaTransport`) and the simulators (`sim/`) are interchangeable.

| Module | Contents |
|---|---|
| `instruments/base.py` | Data types: `SupplyCaps`, `Setpoints`, `SupplyReading`, `DmmConfig`, `DmmReading` |
| `instruments/agilent663xx.py` | 66311B/66321B/66321D SCPI driver |
| `instruments/hp6632a.py` | 6632A/33A/34A driver (HP's pre-SCPI command language) |
| `instruments/hp3456a.py` | 3456A driver (program codes) |
| `instruments/ppk2.py` | Nordic PPK2: commands, calibration, vectorised spike filter, sample time base |
| `instruments/__init__.py` | Model list, auto-detect, driver factory |
| `analysis.py` | Waveform measurements (numpy only): levels, pulse timing, rise/fall, charge, cursors, histogram, spectrum |
| `sim/` | One simulator per instrument |
| `gui/` | Main window, panels, shared widgets |
| `gui/capture_tab.py`, `tests_tab.py`, `system_tab.py` | The 663xx Capture, Tests and System tabs |
| `gui/ppk2_panel.py` | PPK2 tab: binning, ring buffer, min/max envelope plots, region statistics |

## Agilent 66321D (SCPI)

### Measured performance (66321D fw A.03.01, NI GPIB-USB-HS, pyvisa-py)

| Operation | Time |
|---|---|
| Simple query round trip (`*IDN?`, `STAT:OPER:COND?`) | 10–12 ms |
| `MEAS:VOLT?` or `MEAS:CURR?`, 2048 points | ~50 ms |
| `MEAS:CURR?`, 256 points | ~20 ms |
| One combined poll (`MEAS:VOLT?;:MEAS:CURR?;:STAT…;:OUTP?`), 2048 points | 107 ms |
| Same, 256 points | 47 ms |
| Current-only poll, 256 points | 31 ms |
| Array transfer | ~3.7 KB/s (same with raw `ibrd`, so the instrument is the limit) |
| 1024-point array, ASCII vs binary (`FORM REAL`) | 2.86 s vs 1.15 s |
| `identify()` with all capability probes | 0.32 s |
| `read_setpoints()` (two compound queries + display text) | 0.115 s |
| `ABOR` until the WTG bit clears | ~70 ms |

Voltage and current are **digitized separately**: `FETC:VOLT?` after `MEAS:CURR?` returns error +603. That's why a V+I reading costs two acquisitions.

### Commands used

| Purpose | Command |
|---|---|
| Setpoints | `VOLT`, `CURR`, `OUTP ON/OFF`, `VOLT:PROT` (a limit on the setting, not a trip), `RES` |
| Protection | `CURR:PROT:STAT`, `VOLT:PROT:STAT` (tracking OV trip), `OUTP:PROT:DEL`, `SENS:PROT:STAT` (open sense), `OUTP:PROT:CLE` |
| Measurement setup | `SENS:SWE:POIN`, `SENS:SWE:TINT`, `SENS:CURR:RANG MAX/1/MIN`, `SENS:WIND HANN/RECT`, `SENS:CURR:DET ACDC/DC` |
| Poll (one message) | `MEAS:VOLT?;:MEAS:CURR?;[:FETC:CURR:MAX?;…MIN?;…HIGH?;…LOW?;…ACDC?];[:MEAS:DVM?];:STAT:OPER:COND?;:STAT:QUES:COND?;:STAT:QUES?;:OUTP?` |
| Capture | `ABOR`, `SENS:FUNC`, `SENS:SWE:OFFS:POIN` (negative = pre-trigger), `TRIG:ACQ:SOUR BUS/INT/EXT`, `TRIG:ACQ:LEV/HYST/SLOP/COUN:<CURR\|VOLT>`, `INIT:NAME ACQ`, `TRIG:ACQ`, `FETC:ARR:<CURR\|VOLT>?` (binary), `FETC:<f>?`, `…:ACDC?`, `…:MAX?` etc. |
| Output transients | `VOLT:TRIG`, `CURR:TRIG`, `RES:TRIG`, `INIT:NAME TRAN`, `TRIG:TRAN`; `*TRG` fires both systems for a step capture |
| Output response | `OUTP:COMP:MODE LLOC/HLOC/LREM/HREM` (`OUTP:TYPE LOW/HIGH` on older units) |
| Rear port | `DIG:FUNC RIDF/DIG/TRIG` (non-volatile), `DIG:DATA`, `OUTP:RI:MODE`, `OUTP:DFI`, `OUTP:DFI:SOUR`, `OUTP:PON:STAT` (non-volatile) |
| Display | `DISP`, `DISP:MODE NORM/TEXT`, `DISP:TEXT` |
| Saved setups, housekeeping | `*SAV`, `*RCL`, `*TST?`, `*RST`, `*CLS`, `SYST:ERR?`, `*OPT?`, `SYST:VERS?`, `CAL:DATE?` |
| Data format | `FORM REAL,32;:FORM:BORD SWAP` after connect, `*RST` and `*RCL` (arrays arrive as little-endian floats; scalar replies stay ASCII) |
| Capability probes at connect | `VOLT? MAX`, `CURR? MAX`, `VOLT:PROT? MAX`, `MEAS:DVM?`, `RES?`, `SENS:CURR:RANG:AUTO?`, plus a query for each optional feature (an "undefined header" reply hides it) |

The pulse-statistics `FETC` queries have to come before `MEAS:DVM?`, because the DVM acquisition replaces the current data (error +603 otherwise).

Not available on fw A.03.01, so not offered: long integration, DVM arrays (`FETC:ARR:DVM?`), and `SENS:LEAD:STAT?`.

### Triggered capture

`Agilent663xx.arm(spec)` configures the digitizer and initiates it, `fire()` sends the software trigger, `capture_done()` checks the WTG bit (operation bit 5), and `fetch()` reads the binary array plus the instrument's own DC/RMS/min/max/high/low, puts back any stepped levels, and restores the reading settings (`SENS:SWE:POIN/TINT`, offset 0, count 1, `TRIG:ACQ:SOUR INT`).

- **Immediate and output-step** captures use `TRIG:ACQ:SOUR BUS`. With a pre-trigger, the instrument ignores triggers until the pre-trigger buffer has filled, so `arm()` returns how long to wait before firing.
- **Level** captures use `INT` (the instrument watches the signal); **external** captures use `EXT` (rear port pin 3 with `DIG:FUNC TRIG`). Both wait indefinitely; the worker polls WTG every 100 ms and `ABOR` cancels.
- **Output step:** the trigger levels are set, `INIT:NAME TRAN` arms the transient system, and `*TRG` triggers both the transient and the acquisition at once. The original levels are read first and written back after the fetch (unless *put the levels back* is off).
- While a capture is armed the worker does no polling (one digitizer), but its command queue stays live, so **ALL OFF** and setpoint changes still go through. `points × segments ≤ 4096`.

### Status bits decoded

| Register | Bit | Shown as |
|---|---|---|
| `STAT:OPER:COND?` | 8 | CV |
| | 10 | CC |
| | 11 | CC- |
| `STAT:QUES:COND?` | 0 | OV |
| | 1 | OCP |
| | 3 | FP-LOCAL (front panel in local) |
| | 4 | OT |
| | 5 | SENSE-OPEN |
| | 9 | RI |
| | 10 | UNREG |
| | 14 | OVLD |
| `STAT:QUES?` (event register, cleared on read) | same bits | Protection events log; each event is reported once |

## HP 6632A / 6633A / 6634A

Source: Operating Manual 5957-6360, Section VI (Tables 6-1, 6-2, 6-4, 6-6). The commands are terminated with LF.

| Purpose | Command | Notes |
|---|---|---|
| Identify | `ID?` | returns `HP6632A` (or 6633A/6634A) |
| Voltage / current | `VSET <v>`, `ISET <a>` | no matching queries exist |
| Output | `OUT 1` / `OUT 0` | power-on default is ON |
| OVP | `OVSET <v>` | trips the SCR crowbar |
| OCP | `OCP 1` / `OCP 0` | trips only while in CC |
| Reset a trip | `RST` | clear the cause first |
| Power-on defaults | `CLR` | 0 V, minimum current, output ON |
| Readback | `VOUT?`, `IOUT?` | plain numbers |
| Status | `STS?` | decimal, bits below |
| Error | `ERR?` | reading it clears the ERR bit |

`STS?` bits (Table 6-4): CV=1, +CC=2, UNR=4, OV=8, OT=16, OC=64, ERR=128, INH=256, −CC=512, FAST=1024, NORM=2048.

Error codes (Table 6-6), main ones: 10 header expected, 11 unrecognized header, 20 number expected, 21 number syntax, 41 parameter out of range, 42 voltage, 43 current, 44 overvoltage programming error.

Auto-detect sends `*IDN?` first. The 6632A answers that with ERR 10 and no reply; the app then sends `ID?` and reads `ERR?` to clear the error.

## HP 3456A

Source: 3456A Operating & Service Manual §III and the Hepperle quick-reference card.

| Purpose | Codes |
|---|---|
| Function | `S0F1` DCV, `S0F2` ACV, `S0F3` ACV+DCV, `S0F4` 2W Ω, `S0F5` 4W Ω, `S1F4`/`S1F5` offset-compensated Ω, `S1F1` DCV/DCV ratio |
| Range | `R1` auto, `R2` 100 mV / 100 Ω, `R3` 1 V / 1 kΩ, `R4` 10 V / 10 kΩ, `R5` 100 V / 100 kΩ, `R6` 1000 V / 1 MΩ, `R7` 10 MΩ, `R8` 100 MΩ, `R9` 1 GΩ |
| Trigger | `T1` internal, `T2` external, `T3` single, `T4` hold |
| Integration | `<n>STI`, n = 0.01 … 100 PLC |
| Autozero / filter | `Z0`/`Z1`, `FL0`/`FL1` |
| Math | `M0` off, `M3` null |
| Reset | `H` (home), or a device clear |

**Read cycle:** the app sends `T3`, waits the expected conversion time *without holding the bus* (so other instruments keep polling), then reads. The reply is ASCII `+RDDDDDDD+D` (sign, overrange digit, digits with a floating decimal point, signed exponent) followed by CR LF. The parser also accepts an `E`-exponent form.

**Status** comes from a serial poll (octal masks): 004 data ready, 020 illegal state or syntax error, 040 program memory error.

**Confirmed on the real 3456A** (address 22, 2026-11-20): readings arrive in `E` notation, e.g. `-1.177060E+0` or `-01.39843E+0` with the overrange digit, followed by CR LF. An open-circuit ohms reading returns about `+2E+15`, which the app shows as OVLD. A single T3 reading at 1 PLC with autozero takes about 56 ms, and the app polls at about 8–11 readings/s.

**Still to verify:** the R2 ohms range (the reference card is ambiguous) and whether ohms readings arrive in Ω or kΩ. Measure a known resistor with **Send + read** in the Console tab to check.

## Nordic PPK2

The PPK2 is a USB CDC-ACM device (`1915:c00a`). The protocol and calibration follow Nordic's open-source nRF Connect Power Profiler (`pc-nrfconnect-ppk`) and were checked against a real unit (HW 8682).

### Commands used

| Bytes | Meaning |
|---|---|
| `06` / `07` | Start / stop the sample stream |
| `19` | Metadata: calibration as text, `R0: …` … `IA: …`, then `END` |
| `11 01` / `11 02` | Ampere meter / source meter |
| `0D hi lo` | Source voltage in mV, big-endian (800–5000) |
| `0C 01` / `0C 00` | DUT power on / off |

### Sample format

Little-endian 32-bit words at 100 kS/s: bits 0–13 ADC, 14–16 range (shunt 0–4), 18–23 a counter that increases by 1 per sample (mod 64), 24–31 logic D0–D7.

Current per sample, with the unit's metadata for range *r*:

```
x = (4·adc − O[r]) · (1.8 / 163840) / R[r]
I = UG[r] · ( x · (GS[r]·x + GI[r]) + S[r]·VDD/1000 + I[r] )
```

`VDD` is the source voltage (in ampere mode, the DUT voltage typed in).

### Details that matter

- **Continuous reading.** `SerialStream` reads the port on its own thread. Reading only when the worker polls (every 50 ms) let the PPK2's USB buffer overflow. It then dropped whole blocks, about 80% of the data, in multiples of 64 samples, which the counter can't detect.
- **Connect.** The PPK2 may still be streaming from an earlier session. The driver sends stop, waits for 150 ms of silence, then asks for metadata. Connecting changes nothing on the PPK2's outputs.
- **Time base.** Samples are timed from the PPK2's own clock, anchored to app time at the start of a run, and re-anchored only if the two drift more than 0.25 s apart. Measured: 100.4 kS/s against the host clock over 10 s, no re-anchoring.
- **Lost samples:** counter gaps are counted and shown; affected blocks are not re-timed.
- **Spike filter:** after each range switch, 3 samples are replaced by an exponential average (α = 0.18; 0.06 in range 4, which also skips its first 2 samples). The reference runs the averages sample by sample. Here they're evaluated only where needed, over a 400-sample window (truncation ≈ 2·10⁻¹¹). The tests check it against a direct port of the reference, and with an 800-sample window it matches to rounding error.
- **Display.** The worker emits each block of raw samples (~5000). The panel bins them (1 ms and 100 ms min/mean/max), keeps 10 s raw in a ring buffer, and draws at most ~3000 min/max pairs per curve.

## Simulators

| Resource | Model |
|---|---|
| `SIM::66321D` | 47 Ω load with 4.7 Ω, 577 µs bursts every 4.615 ms. The output is modelled over time (40 µs step response from a history of setpoint changes), so level triggers, pre-trigger data, segments, output steps, external triggers (fires 0.4 s after arming) and `ABOR` behave like the real unit. Binary and ASCII arrays, the WTG bit, latched status events, every System-tab setting with `*RST` defaults, `*SAV/*RCL`, `*TST?`, a DVM at 3.7 V ± 0.15 V, the full SCPI error queue (including +603), and OCP trips |
| `SIM::6632A` | 10 Ω load; ERR codes; OCP/OVP trips; 30 ms readback delay |
| `SIM::3456A` | DCV 5 V with slow drift, Ω ~1 kΩ, ACV 120 mV; noise scales with integration time; null; syntax-error status |
| `SIM::PPK2` | The real byte stream: a BLE-style DUT encoded through its own calibration (with range switching and a spike on each switch), metadata, mode, voltage and DUT power commands, counter, logic D0/D1, and a hook to drop samples. Its clock can be injected, so tests run without waiting |

## Tests

```bash
.venv/bin/python -m pytest tests
```

The tests run each driver against its simulator: identification, CV/CC, protection trips and events, error decoding, every 663xx setting, saved setups, immediate/level/segmented/output-step captures with pre-trigger, abort, capture validation, the waveform measurements in `analysis.py`, the config files, and the 3456A reading formats. PPK2 tests cover metadata captured from a real unit, word decoding, the calibration formula, the spike filter against a sample-by-sample reference (across chunk boundaries), and the driver against the simulator: sample count, timing, levels, logic, lost samples, modes, and stop/start.

## Adding an instrument

1. **Driver:** write a class in `instruments/` with `kind = "supply"` or `"dmm"`, plus `family`, `identify()`, `poll(t, cfg)`, `errors()`, and the setters the panel calls. Return the shared data types from `base.py`. A streaming device brings its own panel and data types instead; see the PPK2 (`kind = "ppk2"`, a `cfg` property for the worker to re-read, and `close()` to stop the stream).
2. **Capabilities:** fill in `SupplyCaps`. The supply panel hides what's unsupported (DVM, digitizer, waveform, resistance) and handles `setpoint_readback=False`.
3. **Register it:** add it to `MODELS` and `_CLASSES` in `instruments/__init__.py`, and teach `detect()` its ID string if it has one.
4. **Simulator:** subclass `sim.base.SimBase`, implement `write()`, and add it to `sim.open_sim` and `SIM_RESOURCES`.
5. **Tests:** add `tests/test_<model>.py`.

## Licences and third-party code

- **linux-gpib** (GPL): built from `vendor/linux-gpib` (not committed; fetched by the install script).
- **pyvisa, pyvisa-py** (MIT), **pyqtgraph** (MIT), **PyQt6** (GPL v3), **gpib-ctypes** (GPL v2), **pyserial** (BSD).
- **PPK2 protocol and calibration:** our own implementation, following the behaviour of Nordic's open-source nRF Connect Power Profiler (`pc-nrfconnect-ppk`); no code is copied from it.
