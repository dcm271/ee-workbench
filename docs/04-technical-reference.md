---
title: Technical Reference
subtitle: Command sets, measured timings, architecture and extending the app
---

# Technical Reference

Other chapters: [Hardware](01-hardware-and-resources.md) · [Installation](02-installation.md) · [User's Guide](03-users-guide.md) · [Raspberry Pi](05-raspberry-pi.md) · [How-to guides](06-howtos.md) · [README](../README.md)

**Contents:** [Architecture](#architecture) · [66321D](#agilent-66321d-scpi) · [6632A](#hp-6632a--6633a--6634a) · [3456A](#hp-3456a) · [PPK2](#nordic-ppk2) · [Saleae](#saleae-logic-2-automation) · [sigrok](#sigrok-sigrok-cli) · [Alignment](#lining-up-a-logic-capture-with-a-ppk2-alignpy) · [Sessions](#session-files-sessionpy) · [Sequencer](#sequencer-sequencerpy) · [Function generators](#function-generators-instrumentsfgenpy) · [GPIO](#gpio-instrumentsgpiopy) · [Profiles](#profiles-profilepy) · [Watchdog, events, math](#signals-watchdog-events-math) · [Reports](#reports-reportpy) · [Setups](#setups-and-bench-state-setupspy) · [Remote API](#remote-api-apipy-clientpy) · [Errors and recovery](#errors-and-recovery-crashpy) · [Files](#files-the-app-writes) · [Environment](#environment-variables) · [Simulators](#simulators) · [Tests](#tests) · [Adding an instrument](#adding-an-instrument)

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
 └─ LogicPanel  (Saleae) ── worker thread ── SaleaeLogic ── Logic2Link ── gRPC ── Logic 2 ── USB
 └─ LogicPanel  (sigrok) ── worker thread ── SigrokLogic ── sigrok-cli subprocess ── USB
 └─ FgenPanel   (FGEN)   ── worker thread ── FunctionGenerator ── VisaTransport (LAN / USB-TMC / GPIB)
 └─ GpioPanel   (GPIO)   ── worker thread ── GpioDriver ── GpioLink ── libgpiod ── /dev/gpiochipN
 └─ ScpiPanel   (SCPI:…) ── worker thread ── ScpiDriver(Definition) ── VisaTransport
 └─ ScopePanel  (SCOPE:…)── worker thread ── ScopeDriver(dialect) ──── VisaTransport (query_raw for bytes)

 every panel's readings ─► hub ─► Watchdog (rules ─► trip ─► MainWindow actions)
                              └─► LiveMath (channels ─► MathWindow, session "Math" stream)
 SequencerWindow / ApiServer ─► Bench ─► PanelInst ─► each panel's worker queue
```

- **One I/O thread per instrument** (`worker.py`). The GUI never blocks on the bus. Commands from the GUI go into a queue and run before the next poll.
- After each command the worker drains the instrument's error queue and re-reads the instrument's state, so the GUI always shows what the instrument actually has.
- If a poll fails, polling pauses (no flood of errors) until you press **Resume polling**. The panel emits `link_lost`, and the main window puts **⟳ Reconnect** on the tab; the next reading emits `link_ok` and removes it. Tabs without a Resume button (GPIO) re-enable polling themselves every 5 s.
- The worker's loop is wrapped so an unexpected exception is reported (and the tab offered a Retry) rather than ending the thread silently, and the driver and transport are always closed on the way out, each on its own, so a failing `close()` can't keep a port open.
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
| `instruments/saleae.py` | Saleae through Logic 2: `Logic2Link`, binary-export parser, edge measurements, lane envelope |
| `gui/logic_panel.py` | Logic-analyzer tab: capture modes, lanes, measurements, decoders and their table, PPK2 alignment |
| `instruments/sigrok.py` | sigrok analyzers through `sigrok-cli`: scan, capture, `.sr` reading, decoder output parsing |
| `instruments/fgen.py` | Function generators: SCPI and Siglent dialects |
| `instruments/gpio.py` | Raspberry Pi header through libgpiod v1/v2 |
| `align.py` | Matching a logic capture's edges to the PPK2's logic inputs |
| `session.py` | Session files: recorder and reader |
| `sequencer.py` | Steps, plans, limits, `Bench`, `run_plan`, `run_script` (no Qt) |
| `profile.py` | Setpoint profiles: parsing and timed running |
| `signals.py` | Readings as named signals; identifiers for expressions |
| `watchdog.py`, `events.py`, `mathchannels.py` | Watchdog rules, PPK2 event finding, math expressions (no Qt) |
| `report.py` | HTML / PDF reports with SVG plots |
| `setups.py` | Named bench setups (JSON) |
| `api.py`, `client.py` | Remote API server, and its stdlib-only client |
| `crash.py` | Exception hooks, crash / fault logs, SIGINT / SIGTERM handling |
| `config.py` | `config.toml`, `last-session.json`, start-up plan |
| `gui/hub.py` | Where readings meet the watchdog and live math |
| `gui/sequencer_window.py`, `session_viewer.py` | Sequencer window; session viewer (cursors, math, export) |
| `gui/fgen_panel.py`, `gpio_panel.py`, `profile_box.py`, `events_tab.py` | Generator and GPIO tabs; the Profile and PPK2 Events tabs |
| `gui/watchdog_window.py`, `math_window.py`, `setups_dialog.py`, `report_dialog.py` | Tools-menu windows |
| `instruments/scpi_generic.py`, `instruments/scpi_defs/*.toml` | Generic SCPI driver and the [definition files](#instrument-definition-files-toml) it reads; `self_test()` |
| `instruments/scopes.py` | Oscilloscopes: four command dialects, waveform decoding, measurements |
| `check_instruments.py` | `python -m ee_workbench.check_instruments`: validate and self-test definition files |
| `sim/sscpi.py`, `sim/sscope.py` | Simulator for any definition; four-dialect scope simulator |
| `gui/scpi_panel.py`, `gui/scope_panel.py` | The meter / load / supply tab built from a definition; the scope tab |
| `production.py`, `gui/production_window.py` | Production-test results database and yield; the station window |
| `safety.py`, `gui/safety_dialog.py` | Unattended-run settings, crash marker, reconnect back-off |
| `gui/record_dialog.py` | Record everything or only around events |

## Instrument definition files (TOML)

Meters, loads, supplies and other SCPI instruments that only need "set this, read that" are described by a [TOML](https://toml.io) file instead of a Python driver. The built-in ones are in `ee_workbench/instruments/scpi_defs/`; your own go in `~/.config/ee-workbench/instruments/*.toml` and are read at start-up. A file of yours with the same `id` as a built-in replaces it, so the easiest start is to copy the closest built-in and edit it. Check a file with `python -m ee_workbench.check_instruments FILE` (it also runs the file against its simulator), and see the [how-to](06-howtos.md#add-your-own-scpi-instrument) for a worked example.

A complete small example:

```toml
id = "MY-LOAD"                       # model key SCPI:MY-LOAD, simulator SIM::MY-LOAD
name = "Acme EL-150 electronic load"
type = "load"                        # meter | load | supply | source | other
idn = ["EL-150"]                     # *IDN? containing this → Auto-detect picks this file
init = ["*CLS"]                      # sent once after connecting
error_query = "SYST:ERR?"            # read until it answers 0 ("" if there is none)
off = ["INP OFF"]                    # what All outputs OFF (Esc) sends
poll_ms = 100                        # default poll delay (the tab can change it)
notes = "Set the level before switching the input on."

[[reading]]
name = "voltage"
label = "Voltage"
query = "MEAS:VOLT?"
unit = "V"
sim = "12.0 - 0.1 * cc * input"      # simulator only

[[reading]]
name = "current"
query = "MEAS:CURR?"
unit = "A"
sim = "cc * input + noise(1e-4)"

[[setting]]
name = "cc"
label = "CC current"
kind = "number"
unit = "A"
min = 0
max = 30
set = "CURR {value}"
query = "CURR?"
sim = "1.0"                          # simulator: starting value

[[setting]]
name = "input"
label = "Input"
kind = "bool"
set = "INP {on_off}"
query = "INP?"
output = true                        # an output switch: a red/green button, and Esc turns it off
```

### Top-level keys

| Key | Required | Meaning |
|---|---|---|
| `id` | yes | Short identifier: letters, digits, `.` `_` `+` `-`. The model key is `SCPI:<id>`, the simulator `SIM::<id>`. |
| `name` | | Shown in the model lists. Defaults to `id`. |
| `type` | | `meter`, `load`, `supply`, `source` or `other` (default). Decides the readout colours and how the tab names its output buttons (**Output** / **Input**). |
| `maker` | | Free text. |
| `idn` | | List of strings; if the `*IDN?` answer contains one of them (any case), *Auto-detect* uses this definition. Without it the model has to be chosen by hand. |
| `channels` | | 1 to 16 (default 1). With more than 1, every reading and setting whose text contains `{ch}` is repeated per channel (see below). |
| `init` | | Commands sent once after connecting and after `*IDN?` (e.g. `*CLS`, switching continuous triggering off). |
| `error_query` | | The error-queue query (default `SYST:ERR?`). It is read after every command until it answers a code of 0; `""` turns error checking off (for instruments without one). |
| `off` | | Commands **All outputs OFF (Esc)** sends; `{ch}` repeats them per channel. If missing, Esc turns off every setting marked `output = true` instead. A supply or load should always have one of the two. |
| `poll_ms` | | Default delay between polls in ms (default 0: as fast as the instrument answers). |
| `notes` | | Shown under the instrument's ID on the tab: anything the user should know. |
| `tested` | | The hardware it has been checked on, e.g. `"DL3021A fw 00.01.04"`. Left empty, the tab says the definition hasn't been checked on this model yet. |

### `[[reading]]`: polled values

One table per value. Each poll sends every reading's query, in file order, and parses the first number in the answer. `9.9E37` or more (SCPI's overload) is shown as **OVLD**.

| Key | Meaning |
|---|---|
| `name` | Identifier (letters, digits, `_`): the name in the sequencer, watchdog, math, CSV and sessions. Unique among readings *and* settings. |
| `label` | Shown on the readout (default: `name`). |
| `query` | The query to send. |
| `unit` | `V`, `A`, `W`, `Ω`, `Hz`, `F`, … (SI prefixes are added for display). |
| `unit_from` | Name of a `choice` setting: the unit is that setting's current option's `unit` (a meter whose unit follows its function). |
| `query_from` | Name of a `choice` setting: the query is that setting's current option's `read` (meters that read with `MEAS:VOLT:DC?`, `MEAS:CURR:AC?`, … instead of `READ?`). `query` is then the fallback. |
| `sim` | Simulator only: a Python expression for the value (see below). |

### `[[setting]]`: things the panel can set

| Key | Meaning |
|---|---|
| `name`, `label`, `unit` | As for readings. |
| `kind` | `number` (default), `choice` or `bool`. |
| `set` | The command template. `{value}` is the number (`number`), `{on_off}` gives `ON`/`OFF` and `{1_0}` gives `1`/`0` (`bool`). Not used by `choice` (each option has its own). |
| `query` | Reads the setting back. Optional, but without it the panel can only show what was last sent. A `number` answer is parsed as a number, a `bool` answer accepts `1`/`0`/`ON`/`OFF`, and a `choice` answer is matched against the options' `reply`. |
| `min`, `max` | Limits for a number: the panel's box and the sequencer refuse values outside them. |
| `values` | A list of numbers offered as a list instead of a free number box (NPLC: `[0.02, 0.2, 1, 10, 100]`). |
| `options` | For `choice`: a list of option tables (below). |
| `output` | `true` for the instrument's output / input switch: shown as a button, turned off by **Esc** (if `off` is missing), and used by the watchdog's **turn off**. |
| `sim` | Simulator only: the starting value (a number, `0`/`1`, or an option label). |

**Options** (`options = [{…}, {…}]`):

| Key | Meaning |
|---|---|
| `label` | Shown in the list, and the value used in the sequencer (`function` = `DC V`). |
| `set` | The command that selects it. |
| `reply` | A regular expression ([Python syntax](https://docs.python.org/3/library/re.html#regular-expression-syntax), case-insensitive) searched in the setting's `query` answer; the first option that matches is the current one. Without it, the answer must equal the label. Put the most specific options first, or anchor them (`'^"?VOLT '` vs `'^"?VOLT:AC'`). |
| `unit` | The unit for readings with `unit_from` this setting. |
| `read` | The reading query for readings with `query_from` this setting. |
| `sim` | Simulator only: what the simulator answers to the query while this option is selected (default: built from `reply`). |

### Channels

With `channels = 3`, a reading or setting whose text contains `{ch}` becomes three, named with the channel number appended (`vset` → `vset1`, `vset2`, `vset3`) and labelled `Voltage CH1`, … The `{ch}` is replaced in every command:

```toml
channels = 3
off = ["OUTP OFF,CH{ch}"]        # sent three times
[[setting]]
name = "vset"
set = ":SOUR{ch}:VOLT {value}"   # vset2 = 5 sends :SOUR2:VOLT 5
query = ":SOUR{ch}:VOLT?"
```

Readings and settings without `{ch}` stay single. The panel groups readouts and settings per channel. For the [watchdog's In HW](03-users-guide.md#limits-in-the-instrument-in-hw) limits a supply's settings must be called `vset`, `iset`, `ovp`, `ovp_on`, `ocp`, `ocp_on` (any of the last four may be missing), and its readings `voltage` and `current`.

### Simulator expressions

Every definition runs on its own simulator (`SIM::<id>`), which turns each `set` template back into a pattern (so the sim accepts exactly the commands the file sends), stores the value, and answers `query` with it. Unknown commands put `-113,"Undefined header"` in the error queue, as a real instrument would. A reading's `sim` is a Python expression evaluated with:

- every setting by name (numbers as floats, bools as `True`/`False`, choices as the option label), with `{ch}` replaced by the channel (`"vset{ch} if out{ch} else 0"`),
- `t` (seconds since the simulator started), `noise(sd)` (Gaussian noise), `min`, `max`, `abs` and `math`.

The simulator only shows that the file is consistent with itself. Whether the commands are right for the real instrument is only shown by the real instrument: open it and watch the tab's **Console**.

### How the driver uses a definition

`ScpiDriver.identify()` sends `*IDN?`, then `init`, empties the error queue, and reads every setting. `poll()` sends every reading's query (per channel), parses the numbers and fills in units. `set(name, value)` checks the value against the setting (kind, option, `min`/`max`), sends the filled-in template, reads the setting back (if it has a `query`), and the worker then drains the error queue, so an instrument error appears in the tab's log at once. `set_output(False)` sends `off`, or turns off every `output` setting. Answers are parsed leniently: the first number in the text, so `+1.23456E+00`, `1.23456 V` and `VOLT 1.2` all work.

## Oscilloscopes (`instruments/scopes.py`)

One driver, four command dialects. Each fetches a channel's waveform as bytes (`query_raw`, an IEEE 488.2 `#N<len>` block) and scales it with the scope's own preamble:

**Control commands:**

- **`rigol`**: channel `:CHAN<n>:DISP`, `:CHAN<n>:SCAL`, `:CHAN<n>:OFFS`; timebase `:TIM:SCAL`, `:TIM:OFFS`; trigger `:TRIG:EDG:SOUR`, `:TRIG:EDG:LEV`, `:TRIG:EDG:SLOP`, `:TRIG:SWE`; `:RUN`, `:STOP`, `:SING`; status `:TRIG:STAT?` (TD, WAIT, RUN, AUTO, STOP).
- **`keysight`**: channel `:CHAN<n>:DISP`, `:CHAN<n>:SCAL`, `:CHAN<n>:OFFS`; timebase `:TIM:SCAL`, `:TIM:POS`; trigger `:TRIG:EDGE:SOUR`, `:TRIG:EDGE:LEV`, `:TRIG:EDGE:SLOP`, `:TRIG:SWE`; `:RUN`, `:STOP`, `:SING`; status `:OPER:COND?` bit 3 (running).
- **`tek`**: channel `SEL:CH<n>`, `CH<n>:SCA`, `CH<n>:OFFS`; timebase `HOR:SCA`, `HOR:DEL:TIM`; trigger `TRIG:A:EDGE:SOU`, `TRIG:A:EDGE:SLO`, `TRIG:A:LEV:CH<n>`, `TRIG:A:MOD`; run `ACQ:STOPA RUNST;:ACQ:STATE RUN`, stop `ACQ:STATE STOP`, single `ACQ:STOPA SEQ;:ACQ:STATE RUN`; status `ACQ:STATE?`.
- **`siglent`**: channel `C<n>:TRA`, `C<n>:VDIV`, `C<n>:OFST`; timebase `TDIV`, `TRDL`; trigger `TRSE EDGE,SR,C<n>,HT,OFF`, `C<n>:TRLV`, `C<n>:TRSL`, `TRMD`; run `TRMD AUTO`, `STOP`, single `TRMD SINGLE;ARM`; status `SAST?`.

**Waveforms:**

| Dialect | Waveform | Scaling |
|---|---|---|
| `rigol` | `:WAV:SOUR CHAN<n>; :WAV:MODE NORM; :WAV:FORM BYTE`, `:WAV:PRE?`, `:WAV:DATA?` (unsigned bytes, the screen record) | v = (byte − yorigin − yreference) × yincrement; t₀ = −xreference × xincrement + xorigin |
| `keysight` | `:WAV:SOUR CHAN<n>; :WAV:POIN:MODE NORM; :WAV:UNS 1; :WAV:FORM BYTE`, `:WAV:PRE?`, `:WAV:DATA?` | v = (byte − yreference) × yincrement + yorigin |
| `tek` | `DATA:SOU CH<n>; DATA:ENC RIB; DATA:WID 1; DATA:STAR 1; DATA:STOP <HOR:RECO?>`, `WFMO:XIN?/XZE?/YMU?/YOF?/YZE?`, `CURV?` (signed bytes, the whole record, at most 1 M points) | v = (code − YOFF) × YMULT + YZERO; t₀ = XZERO |
| `siglent` | `C<n>:VDIV?`, `C<n>:OFST?`, `TDIV?`, `SARA?`, `TRDL?`, `C<n>:WF? DAT2` (signed bytes) | v = code × VDIV / 25 − OFST; t₀ = −7 × TDIV − TRDL; Δt = 1 / SARA |

**Capture protocol** (the same as a logic analyzer's, so the sequencer, watchdog and sessions treat both alike): `arm()` starts a single acquisition; `capture_done()` polls `status()` until the scope has stopped; `fetch()` reads every displayed channel into a `LogicCapture` with one `AnalogTrace` per channel; `abort()` stops it. `grab()` fetches without arming. `measure("vpp1")` fetches one channel and runs `trace_stats()`: Vpp, max, min, mean, RMS, frequency, period and duty from crossings of the mid level, and 10–90 % rise and fall times.

`detect_idn()` recognises the families from `*IDN?` (maker and model prefix); `resolve()` accepts `SCOPE:RIGOL`, `DS1054Z`, `TBS2000`, `scope:tek` and similar.

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

## Saleae (Logic 2 automation)

What both logic-analyzer drivers share lives in `instruments/logic.py`: `LogicConfig`, `LogicCaps` (which also says which trigger types and decoders an analyzer has, its channel names, and its file type), `LogicCapture`, `DigitalTrace` / `AnalogTrace`, and the measurement and drawing helpers. The panel reads everything that differs from the caps.


Saleae's current analyzers have an undocumented USB protocol, so the driver uses Logic 2's gRPC automation API (`logic2-automation` package, API 1.x, port 10430). `Logic2Link` wraps it in a few blocking calls (`start`, `wait`, `stop`, `close_capture`, `export_binary`, `add_analyzer`, `export_table`, `save`), which `SimLogic2` also implements.

### A capture

1. `start_capture` with the channels, rates and threshold, and a `TimedCaptureMode`, `DigitalTriggerCaptureMode` (`after_trigger_seconds`, and `trim_data_seconds = before + after`, which keeps that much from the end) or `ManualCaptureMode`.
2. Timed and trigger: a helper thread calls `wait()`, and the worker's capture loop polls it every 100 ms, so the I/O queue stays responsive. Manual: **Stop** calls `stop()`. The API forbids calling both on one capture. **Abort** therefore uses `close()`, which also ends a pending `wait()`.
3. `export_raw_data_binary` into a temporary folder, then parse `digital_<ch>.bin` / `analog_<ch>.bin`. Analog is downsampled by Logic 2 to at most ~2 M points per channel, estimated from the capture's elapsed time.
4. If decoders are set: `add_analyzer` for each, `export_data_table` (hex radix) to one CSV, then `remove_analyzer`.

The capture stays open in Logic 2 (for **Decode**, **Save .sal**, **Export CSV**) until the next capture or the tab closes.

### Binary export format

Little-endian. Header: `<SALEAE>`, int32 version (0 or 1), int32 type (0 digital, 1 analog). Both versions are read.

| | Version 0 | Version 1 |
|---|---|---|
| Digital | u32 initial state, f64 begin, f64 end, u64 n, f64[n] transition times | u64 chunks; per chunk: u32 initial state, f64 sample rate, f64 begin, f64 end, u64 n, f64[n] |
| Analog | f64 begin, u64 rate, u64 downsample, u64 n, f32[n] | u64 waveforms; per waveform: f64 begin, f64 trigger time, f64 rate, i64 downsample, u64 n, f32[n] |

A digital channel becomes edge times plus levels (they alternate from the initial state). Chunk boundaries count as edges only where the level changes. Gaps between analog waveforms are filled with NaN, so one uniform time base remains.

### Drawing and measuring

- **Lanes:** with no more edges in view than the plot is wide, the exact steps; otherwise one column per pixel, full height where that column has any edge. Recomputed (throttled to 20/s) when the view changes.
- **Frequency** is from the first to the last rising edge in the window; **duty** is time high ÷ window; pulse widths count only pulses with both edges inside the window.

## sigrok (sigrok-cli)

`SigrokLogic` runs `sigrok-cli` (0.7 / libsigrok 0.5) as a subprocess instead of binding libsigrok: the bindings aren't packaged for Python 3 on Ubuntu, and a separate process keeps a misbehaving USB driver away from the GUI. `SigrokLink` has two calls, `run(args)` (wait for it, return code and output) and `spawn(args)` (a capture in the background). `SimSigrokCli` answers the same two calls by reading the argument list.

- **Finding devices:** `sigrok-cli -d <driver> --scan` for each USB driver in `SCAN_DRIVERS` (fx2lafw, saleae-logic16, dreamsourcelab-dslogic, hantek-4032l, …), side by side. Serial-port drivers are deliberately not scanned, because probing would send bytes to whatever sits on `/dev/ttyACM*`, such as a PPK2. `--show` gives the sample rates (a list or a range), the trigger types (`0 1 r f e`) and whether `captureratio` (pre-trigger %) is supported.
- **A capture** is one run: `--config samplerate=N[:captureratio=P] -d <spec> --channels D0,D3 -o capture.sr` plus `--samples N [--triggers D3=f]`, or `--continuous` for Manual. That run is stopped with SIGINT. stdin must stay an open pipe, because a continuous capture ends as soon as its stdin reaches end-of-file.
- **The .sr file** is a zip: `metadata` (INI: `samplerate`, `probeN = name`, `unitsize`) and raw chunks `logic-1-1`, `logic-1-2`, … Channel `probeN` is bit N−1 of each sample word even when other channels are off. `read_sr` joins the chunks into one numpy array, and `traces_from_samples` turns each bit into edge times.
- **Trigger position:** the file doesn't record it. libsigrok's software trigger fires on the first matching sample, so t = 0 is put at the first sample in the data that meets the condition (`trigger_index`).
- **Decoding** is one more run per decoder on the saved file: `-i capture.sr -P uart:rx=D0:baudrate=115200 -A uart=rx-data:… --protocol-decoder-samplenum`, whose lines `509-579 uart-1: 48` become table rows. A wrong channel name only prints `No channel with name …` to stderr and still exits 0, so stderr is checked.

## Lining up a logic capture with a PPK2 (`align.py`)

The PPK2's samples are timed on the app clock (anchored at the start of a run, then counted at exactly 10 µs). A logic capture is timed on the analyzer's clock, from its own start or trigger. The host only knows roughly when the capture started (`t_armed`, within a few ms, worse for a triggered capture). Both see one shared signal.

`align()` takes the analyzer's edges on that channel (dropping any closer than 20 µs to a neighbour, which the PPK2 can't resolve) and the PPK2's edges on its input. Each PPK2 edge of the same direction within the search window (the PPK2's record from 2 s before arming to 0.5 s after the capture arrived) is tried as the match for the first analyzer edge. Each candidate offset is scored by how many analyzer edges then land within 16 µs of a same-direction PPK2 edge. The best candidate is refined with the median of its matched differences. The result is rejected if fewer than half the edges match, and flagged as ambiguous if a candidate more than four tolerances away scores ≥ 90 % as well, which is what a periodic signal does. Scoring uses at most 400 edges and 6000 candidates (~20 ms).

The result is `LogicCapture.offset`, the app time of the capture's t = 0. Its accuracy is set by the PPK2's 10 µs sampling (±5–10 µs). The PPK2's samples for the capture's span are stored in `LogicCapture.extra["ppk2"]` on the capture's own time axis.

## Session files (`session.py`)

A session is one SQLite file (WAL mode) written from the GUI thread. Rows are buffered and committed once a second.

| Table | Contents |
|---|---|
| `meta(key, value)` | `format` (1), `created`, `wall_t0` (Unix time of app t = 0); `gate` (`{"pre_s", "post_s"}`) for a recording made only around events |
| `streams(id, name, model, resource, columns, tbl)` | one per instrument (or sequencer layout); `columns` is JSON |
| `s_<id>(t, …)` | that stream's rows: `t` = app time (s), then its columns, the same as its CSV without `timestamp` and `t_s` |
| `notes(t, text)` | |
| `events(t, t_from, t_to, why)` | only-around-events recordings: each event's time and the window kept around it |
| `captures(id, t_app, t0, aligned, name, cfg, data, table_header, table_rows, note)` | `t0` = app time of the capture's t = 0 (PPK2-aligned if `aligned`, else the host's arming time); `cfg` = the LogicConfig as JSON; `data` = a compressed `.npz` of the traces (`d<ch>_t`, `d<ch>_l`, `d<ch>_end`, `a<ch>_v`, `a<ch>_td`, `x_ppk2_t`, `x_ppk2_i`) |

Panels feed it through `PanelBase.record(row)`, the same row as their CSV, so a new instrument gets session recording by laying out its CSV row. Recording into an existing file shifts the new run's times by the difference in `wall_t0`, so one file keeps one time base. `SessionReader` loads it back for the viewer.

**Only around events** (`recorder.start(path, gate=(pre, post))`): rows go into an in-memory ring per stream, trimmed to the last `pre` seconds. `recorder.trigger(t, why)` writes the ring from `t − pre`, records an `events` row, and keeps writing directly until `t + post`; overlapping windows merge. Notes, captures, sequence rows and math are written as usual. Callers: watchdog trips (every action), `capture()` (logic analyzer and scope), **✎ Note**.

## Sequencer (`sequencer.py`)

The engine doesn't import Qt. `Bench` holds one `Inst` per instrument tab and offers `set`, `measure`, `capture`, `wait`, `note`, `record`. Every wait goes through the stop event. `run_plan` runs a `Plan` (steps + once/repeat/sweep), and `run_script` runs Python with `bench` in scope. The window (`gui/sequencer_window.py`) runs either on its own thread and supplies `PanelInst`:

- driver calls are queued on that tab's `InstrumentWorker` (`submit(job, refresh=True)`), and the thread waits on an event, so a sequence never talks to an instrument at the same time as its polling;
- readings come from the tab's `last_reading`, and a measurement waits for one timed after it was asked for;
- PPK2 averages come from its sample ring, and captures from the logic tab's own Capture, both through a queued call onto the GUI thread (`Invoker`).
- A tab's names for **Set** / **Measure** / **Profile** come from the panel (`set_names()`, `measure_names()`, `setting_kinds()`, `profile_names()`), so SCPI and scope tabs offer exactly what their definition or channel count gives.

`run_plan(plan, bench, start=0, extra=None, on_pass=None)`: `start` skips the passes before it (resuming); `extra` is a dict of names for the steps (production test: `serial`, `product`, `operator`; numbers can be used in values, any value replaces `{name}` in a Note, and `serial` becomes a result column); `on_pass(index)` is called after each finished pass (the window writes its checkpoint there). `run_script(code, bench, extra)` puts `extra` in the script's namespace.

## Function generators (`instruments/fgen.py`)

`FunctionGenerator` (kind `"fgen"`) speaks one of two dialects, chosen from `*IDN?`: Siglent (`SDG` in the model) or SCPI (everything else). Settings are read back after every change, so the panel shows what the generator really took (it may round frequency or clamp amplitude to its limits).

| | SCPI (Keysight 33xxx, Rigol DG) | Siglent SDG |
|---|---|---|
| Waveform | `SOUR1:FUNC SIN` (`SQU RAMP PULS NOIS DC`) | `C1:BSWV WVTP,SINE` (`SQUARE RAMP PULSE NOISE DC`) |
| Frequency | `SOUR1:FREQ 1000` | `C1:BSWV FRQ,1000` |
| Amplitude (Vpp) | `SOUR1:VOLT 1.0` | `C1:BSWV AMP,1.0` |
| Offset | `SOUR1:VOLT:OFFS 0` | `C1:BSWV OFST,0` |
| Duty | `SOUR1:FUNC:SQU:DCYC 50` | `C1:BSWV DUTY,50` |
| Output | `OUTP1 ON` / `OUTP1?` | `C1:OUTP ON` / `C1:OUTP?` |
| Read back | each query: `SOUR1:FUNC?` … | one `C1:BSWV?`, parsed as `key,value` pairs |
| Channels | 2 if `SOUR2:FUNC?` answers without error | from the model number (`SDG…2…X` = 2) |

`set_output(False)` (**Esc**) turns every channel off. `poll_enabled` is false: a generator has nothing to measure, so the worker only reads state after commands. Model numbers in `KNOWN` make auto-detect pick this driver; `looks_like_fgen(idn)` is the test.

## GPIO (`instruments/gpio.py`)

`GpioDriver` (kind `"gpio"`) uses **libgpiod** (`/dev/gpiochip*`, the kernel's character-device API; not the deprecated sysfs interface or RPi.GPIO), through `GpioLink`, which supports both the v1 (`gpiod.Chip.get_line`) and v2 (`gpiod.request_lines`) Python bindings. Each pin in use is one requested line, with direction, bias (pull) and active-low set by the kernel, so "on" is always the logical level and the hardware inversion happens below. Pins in mode *off* aren't requested at all.

- `find_chip()` picks the chip whose label is one of `pinctrl-rp1` (Pi 5), `pinctrl-bcm2712`, `pinctrl-bcm2711` (Pi 4), `pinctrl-bcm2835` (Pi 3 and earlier); `HEADER` maps the 26 header GPIOs to physical pins.
- `configure(pins)` re-requests the lines; outputs start inactive. `set_pin`, `pulse(gpio, seconds)` (invert, sleep on the worker thread, restore), `get_pin`.
- `poll()` reads every in/out line at once (20 Hz) into `GpioReading.levels` (logical 0/1), which the trend, CSV, session, watchdog (`gpio17`) and math (`jig.gpio17`) use.
- `set_output(False)` makes every output inactive; `close()` releases the lines.

## Profiles (`profile.py`)

`parse(text) → Profile` with `times`, `values` and the update `rate`; `Profile.points()` lists `(t, value)` pairs (periodic shapes are sampled at `rate`, default 10/s; step shapes give one point per step). `run(profile, setter, stop, clock, on_point)` sends each point at `t0 + t`: it sleeps until the point is due (waking on `stop`), calls `setter(value)`, and if the setter took so long that later points are already due, skips to the latest due one. So timing never drifts, and a slow instrument sees a coarser shape rather than a slower one. `stop.set()` raises `ProfileStopped`. The GUI's `ProfileBox` runs it on a thread with the setter going through the panel's worker (`submit(…).wait()`); the sequencer's **Profile** step and `bench.profile()` call the same `run`.

## Signals, watchdog, events, math

**`signals.py`** turns any panel's reading into named arrays: `signals_of(kind, reading) → {quantity: (t, values)}`; for a PPK2 that's every 10 µs sample of the block (`t0 + k·10 µs`). `slug(name)` makes the identifiers used in expressions. `gui/hub.py` is the one place readings arrive: `PanelBase._on_reading` calls `hub.feed(panel, reading)`, which feeds the watchdog and the live math.

**`watchdog.py`**: `Rule(source, quantity, op, limit, hold_ms, action, target, enabled, hw, rearm_s)`. `ACTIONS`: `all outputs off`, `turn off`, `stop sequence`, `capture`, `mark session`, `note only` (the first three show a message box: `SAFETY_ACTIONS`). `Watchdog.auto_rearm(now)` re-arms rules whose `rearm_s` has passed since they tripped (the main window calls it every second). `hw_plan(rule, kind, settings)` returns `(job, text)`, where `job(driver)` sets the instrument's own limit on its I/O thread, or `(None, why)`: for a built-in supply `set_ovp(L)` or `set_current(min(current, L))` + `set_ocp(True)`; for a SCPI definition the settings `ovp`/`ovp_on` or `iset`/`ocp`/`ocp_on` (with the channel number when the quantity has one). `MainWindow.apply_hw_limits()` runs the jobs once per rule and connection. `Watchdog.feed(name, kind, reading)` checks each enabled, un-tripped rule on that instrument's signals, vectorised over the block: it finds the runs of samples past the limit (carrying a run that's still going across blocks), and trips when a run has lasted `hold_ms` (for one-reading-per-block instruments, from the first past-limit reading's time to the current one's). A trip latches (`rule.tripped`, `rule.trip_t`, `rule.trip_value`) and is returned; the main window carries out the action through the same paths as Esc. Rules serialise with `to_list()` / `from_list()`.

**`events.py`**: `find_events(t, i, threshold, min_gap, min_len, hysteresis)`: a Schmitt trigger (rise above `threshold`, fall below `threshold × (1 − hysteresis)`), done with numpy over the whole buffer; events closer than `min_gap` are merged, shorter than `min_len` dropped. Each `Event` has `t0, t1, duration, peak, mean, charge` (trapezoidal integral). `summary(events, t, i)` gives the counts, rate and the mean current outside events. `attach_bus(events, rows, offset, pad)` fills each event's `bus` with the decoded rows (from an aligned capture, times shifted by its `offset`) within `pad` of it. `histogram(events, field, bins)`.

**`mathchannels.py`**: `evaluate(expr, series, grid)`: each `inst.quantity` series is first put on the evaluation `grid` by `resample()`: bin-averaged when the series has several samples per grid step (a PPK2), else linearly interpolated, so a 50 µs burst counts in proportion to its charge rather than depending on whether a grid point happened to land on it. The expression is evaluated with numpy functions only (`__builtins__` empty) and `inst.quantity` attribute access via `_Inst` objects, which name the missing signal in the error if one doesn't exist. `avg(x, s)` is a rolling mean over `s` seconds of grid. `integrate(t, y)` is the cumulative trapezoid. `LiveMath` keeps 60 s of each signal (a PPK2 reduced to 1 ms means as it arrives) and `tick(now)` evaluates every channel on a 50 ms grid; integrated channels keep their running total across ticks.

## Production results database

`production.py`: one SQLite file, `~/.config/ee-workbench/results.db` by default (`"production": {"db": "…"}` in `bench.json` moves it):

```sql
CREATE TABLE units(
  id INTEGER PRIMARY KEY, serial TEXT, product TEXT, operator TEXT,
  station TEXT,        -- the computer's host name
  started TEXT, finished TEXT,   -- ISO 8601 local time
  seconds REAL, result TEXT,     -- PASS | FAIL | ERROR
  failures TEXT,       -- "; "-joined failure texts of the FAIL rows (or why it was an ERROR)
  outcome TEXT,        -- how the sequence ended: "Finished", "Stopped", "Failed: …", …
  data TEXT,           -- the sequence's result rows, JSON
  report TEXT);        -- path of the unit's HTML report, if one was saved
CREATE INDEX units_serial ON units(product, serial);
```

`verdict(outcome, passed, failed)`: FAIL if any pass failed a limit, ERROR if the sequence didn't finish (other than *Stopped at the first fail*), else PASS. `ResultsDB.yield_stats(product, since)` counts runs (`tested`, `passed`, `failed`, `errors`) and units (`units`, `first_pass`: first run passed, `final_pass`: latest run passed). Examples:

```bash
sqlite3 ~/.config/ee-workbench/results.db \
  "select serial, result, finished, failures from units where product='Sensor board rev C' order by id desc limit 20"
sqlite3 -csv ~/.config/ee-workbench/results.db "select * from units" > all-units.csv
```

## Safety and crash recovery (`safety.py`)

**`running.json`** (the crash marker), written at start-up and whenever the open instruments change, removed by a normal quit:

```json
{"pid": 12345, "started": "2026-11-21T08:00:03", "updated": "2026-11-21T08:14:52",
 "open": [{"resource": "TCPIP::192.168.1.61::INSTR", "model": "SCPI:DL3021", "settings": {…}}],
 "sequence": false}
```

At start-up, a marker whose `pid` is no longer running means the last run didn't quit normally (`previous_run()`); one whose `pid` is still running means another copy of the app is open, and it is left alone. A damaged marker counts as a crash with nothing known to be open.

**`sequence-run.json`** (the checkpoint), written after each pass of a steps-mode run when *resume* is on, removed when the run ends:

```json
{"plan": {…the sequencer's state…}, "next_pass": 37, "total": 100, "saved": "2026-11-21T03:12:09"}
```

**Reconnect back-off** (`Backoff`): 3, 6, 12, 24, 48, then 60 s per resource, reset when it answers again. `device_path(resource)` gives the `/dev/…` path of a serial resource (`PPK2::/dev/ttyACM0`, `ASRL/dev/ttyUSB0::INSTR`), which must exist again before a reconnect is tried.

## Reports (`report.py`)

`sequence_report(title, rows, columns, plan, log, instruments, …)` and `session_report(path, title, t_range, columns)` return one self-contained HTML string: inline CSS, plots as inline SVG made by `svg_plot()` (each series cut into ≤ 800 x-buckets drawn as a min/max envelope, so spikes survive), tables, the sequence. `save(html, path)` writes HTML, or PDF through WeasyPrint (`HTML(string=…).write_pdf`) when the path ends in `.pdf`. Nothing loads from the network, so a report renders the same offline, years later.

## Setups and bench state (`setups.py`)

A setup is a JSON file in `~/.config/ee-workbench/setups/<name>.json`:

```json
{"version": 1, "name": "nRF52 sleep current", "description": "…", "saved": "2026-11-20 14:02:11",
 "instruments": [{"resource": "PPK2::/dev/serial/by-id/…", "model": "PPK2", "settings": {…}}, …],
 "sequence": {"steps": [{"action": "Measure", "target": "PPK2", "what": "current", "value": "2",
                         "label": "I_sleep", "lo": "", "hi": "10e-6"}, …],
              "loop": "once", "count": 1, "start": 0.0, "stop": 1.0, "step": 0.1,
              "off_at_end": true, "stop_on_fail": true, "script": "…", "use_script": false},
 "watchdog": [{"source": "PPK2", "quantity": "current", "op": ">", "limit": 0.05, "hold_ms": 10,
               "action": "all outputs off", "target": "", "enabled": true}],
 "watchdog_armed": true,
 "math": [{"name": "P", "expr": "…", "unit": "W", "integrate": false, "enabled": true}]}
```

`settings` is each panel's `session_settings()` (what `last-session.json` keeps too). Recall (`gui/setups_dialog.apply_bench`) closes the tabs, opens the listed instruments through the normal connect path (which reads each instrument's state and never writes setpoints or outputs), applies the settings, then the sequence, rules and channels. The current watchdog rules and math channels are also kept in `~/.config/ee-workbench/bench.json`, saved whenever they change. Its keys:

| Key | |
|---|---|
| `watchdog`, `watchdog_armed` | the rules (as in a setup, plus `hw` and `rearm_s`) and whether armed |
| `math` | the math channels |
| `recording` | `{"gated": bool, "pre_s", "post_s"}`: the Record dialog's last choice |
| `safety` | `{"off_on_quit", "off_after_crash", "heartbeat_tab", "heartbeat_pin", "heartbeat_period", "auto_reconnect", "resume_sequence"}` |
| `production` | `{"product", "operator", "pattern", "reports", "report_dir", "db"}` |

## Remote API (`api.py`, `client.py`)

JSON over HTTP on `127.0.0.1:<api_port>` (default 8765), served by a `ThreadingHTTPServer` thread in the app.

| Request | Reply |
|---|---|
| `GET /` | `{"app": "ee-workbench", "methods": [...]}`, no token needed |
| `POST /call` with `Authorization: Bearer <token>` and body `{"method": "measure", "args": ["PPK2", "current", 1.0], "kwargs": {}}` | `200 {"result": 0.000123}` |
| bad token | `401 {"error": …}` |
| unknown method | `404 {"error": "no method …"}` |
| a sequencer error (no such instrument, bad value) | `400 {"error": …, "type": "SequenceError"}` |
| Esc / All outputs OFF during the call | `409 {"error": "stopped …", "type": "Stopped"}` |
| anything else | `500 {"error": …, "type": …, "trace": …}` |

Methods (`METHODS`): `instruments set measure capture wait note record gpio read_pin wait_for profile expect outputs_off log`, the [Script-tab](03-users-guide.md#script-tab) `bench` calls. Each request gets a fresh `Bench` over the open tabs (`PanelInst`, as the sequencer uses), so calls are queued on each instrument's worker thread like any other command; concurrent requests are allowed but queue per instrument. Results go through `to_json()`: a capture becomes `{header, rows, offset, span, channels}`, numpy scalars become floats. `record()` and `expect()` rows go to the sequencer window's results (and the session).

The token is 24 random bytes (`secrets.token_urlsafe(24)`), checked with `hmac.compare_digest`, and written with the URL, port and PID to `~/.config/ee-workbench/api.json`, created with mode 600 and removed when the API stops. `client.connect(url=None, token=None, timeout=600)` reads that file, and the returned `RemoteBench` turns `bench.anything(*a, **k)` into a call; errors raise `BenchError`. A raw call with curl:

```bash
TOKEN=$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.config/ee-workbench/api.json")))["token"])')
curl -s -H "Authorization: Bearer $TOKEN" -d '{"method":"instruments"}' http://127.0.0.1:8765/call
```

## Errors and recovery (`crash.py`)

PyQt6 calls `qFatal()` (abort, core dump) when a Python exception escapes a slot. With instruments connected that would leave outputs on and the bench unattended, so `main()` calls `crash.install()` before creating the `QApplication`:

| Hook | Does |
|---|---|
| `sys.excepthook` | Logs the traceback to `crash.log` and calls the main window's notifier, which shows a non-modal *internal error* box (one at a time) and a status-bar line. Identical errors (same traceback tail and place) are logged and shown at most once a minute, with a count of the repeats, so a failing timer or per-reading slot can't flood the log or the screen. |
| `threading.excepthook` | Logs exceptions escaping plain Python threads. The sequence, profile and API threads catch and report their own. |
| `faulthandler` | Writes every thread's stack to `fault.log` on SIGSEGV / SIGABRT / SIGBUS / SIGFPE. |
| SIGINT / SIGTERM | `crash.install_signal_handlers()` calls `MainWindow.close()` on the GUI thread (a 250 ms timer lets Python see the signal while Qt's loop runs). |

Both logs rotate to `.1` at about 1 MB. `crash.report(exc_type, exc, tb, where=…)` is the entry point for code that catches an exception but wants it recorded.

Other measures, by place:

- **Safety paths.** `all_outputs_off()` and the watchdog's *turn off* call each panel's `output_off()` through `MainWindow._off()`, which never raises; a failure is listed in the status bar. In the hub, the watchdog sees each reading before math channels, and math errors are caught there.
- **Closing.** `closeEvent` runs each step (stop the sequence, close the session, stop the API, save `last-session.json` and `bench.json`, shut each panel down) independently. `PanelBase.shutdown()` also stops the tab's profile runner and Tests-tab run. `InstrumentWorker.stop()` waits 10 s; a thread still inside a driver call is *parked* (kept referenced) instead of being destroyed while running, which would abort. At exit `worker.wait_all()` gives parked threads 5 s more, then the process ends with `os._exit` rather than abort.
- **Threads and Qt.** Widgets are only created on the GUI thread: switching the Remote API on creates the (hidden) Sequencer window first, since `make_bench()` runs on the HTTP server's threads.
- **Start-up files.** `config.Session`, `Watchdog.load`, `Channel.load_list`, `Plan.from_dict` and the setups check types and skip damaged entries; `_finish_restore` opens each remembered instrument on its own (retrying once without its saved settings). JSON state is written atomically (`.tmp` + `os.replace`) with `config.json_default` for numpy values.
- **Setups** are validated by `check_bench()` before any tab is closed; `apply_bench()` returns the parts it had to skip.
- **Writes that can fail.** `CsvRecorder.row()` stops recording on `OSError` and sets `error`; `SessionWriter.stop()` never raises and returns the error; save / export methods carry `@io_errors(title)` (`gui/widgets.py`), which turns `OSError` into a warning box.
- **Math.** Anything an expression raises, including shape and type errors after evaluation, becomes `MathError`; `LiveMath.tick` isolates channels from each other.
- **Remote API.** Bodies that aren't a JSON object, or have non-list `args` / non-object `kwargs`, get 400; a client that disconnects mid-call is ignored; if `api.json` can't be written the server is stopped again.
- **`run.sh --restart-on-crash`** relaunches after a non-zero exit other than 2 (bad options), 3 s later, at most 5 times in 120 s.

## Files the app writes

| File | What |
|---|---|
| `~/.config/ee-workbench/config.toml` | Start-up settings (created with comments on first run) |
| `~/.config/ee-workbench/last-session.json` | The open instruments and their panel settings, for the next start |
| `~/.config/ee-workbench/bench.json` | Watchdog rules (and whether armed), math channels, and the record, safety and production settings ([keys](#setups-and-bench-state-setupspy)) |
| `~/.config/ee-workbench/instruments/*.toml` | Your own [instrument definitions](#instrument-definition-files-toml) (read, never written) |
| `~/.config/ee-workbench/results.db` | [Production-test results](#production-results-database) |
| `~/.config/ee-workbench/running.json` | The [crash marker](#safety-and-crash-recovery-safetypy), only while the app runs |
| `~/.config/ee-workbench/sequence-run.json` | A running sequence's checkpoint, with *resume* on |
| `~/.config/ee-workbench/sequence.json` | The sequencer's steps and script as last run (`sequence.bad`: a damaged one, set aside at start-up) |
| `~/.config/ee-workbench/presets.json` | 663xx System-tab presets |
| `~/.config/ee-workbench/setups/*.json` | Named [setups](#setups-and-bench-state-setupspy) |
| `~/.config/ee-workbench/api.json` | Remote API address and token (mode 600), only while it runs |
| `~/.config/ee-workbench/crash.log` (`.1`) | Caught errors with tracebacks, signals received, `run.sh` restarts ([details](#errors-and-recovery-crashpy)) |
| `~/.config/ee-workbench/fault.log` (`.1`) | A line per start-up; every thread's stack after a hard crash |
| `*.csv`, `*.eewb`, `*.html` / `*.pdf` | Wherever you save recordings, sessions and reports |

`$XDG_CONFIG_HOME` moves `~/.config`.

## Environment variables

| Variable | Effect |
|---|---|
| `XDG_CONFIG_HOME` | Where `ee-workbench/` (all the files above) lives, instead of `~/.config` |
| `EEWB_VISA_LIB` | VISA library for pyvisa: `@py` (default, pyvisa-py) or `@ivi` (NI-VISA / Keysight IO Libraries) |
| `EEWB_GPIB_LIB` | Full path of `libgpib.so.0`, tried before the usual locations |
| `EEWB_SIGROK_CLI` | The `sigrok-cli` program to run, instead of the one on `PATH` |
| `EEWB_TOUCH` | `1` / `0`: force the touch or desktop layout (`--touch` / `--no-touch` win) |
| `QT_QPA_PLATFORM` | Qt's display back end, e.g. `xcb` when Wayland misbehaves, `offscreen` for screenshots and tests |

## Look and feel (`gui/theme.py`)

Colours are tokens in `theme.py`, and one application stylesheet uses them. Buttons get a role instead of a colour (`set_role(button, "primary" | "danger" | "danger-solid" | "recording")`). Check-box and arrow icons are small SVGs in `gui/icons/`. Touch mode adds `theme.touch_extra()` on top of `touch.TOUCH_STYLE`. The instrument colours (volts amber, amps blue, DVM violet, DMM green) stay in `widgets.py`.

## Simulators

| Resource | Model |
|---|---|
| `SIM::66321D` | 47 Ω load with 4.7 Ω, 577 µs bursts every 4.615 ms. The output is modelled over time (40 µs step response from a history of setpoint changes), so level triggers, pre-trigger data, segments, output steps, external triggers (fires 0.4 s after arming) and `ABOR` behave like the real unit. Binary and ASCII arrays, the WTG bit, latched status events, every System-tab setting with `*RST` defaults, `*SAV/*RCL`, `*TST?`, a DVM at 3.7 V ± 0.15 V, the full SCPI error queue (including +603), and OCP trips |
| `SIM::6632A` | 10 Ω load; ERR codes; OCP/OVP trips; 30 ms readback delay |
| `SIM::3456A` | DCV 5 V with slow drift, Ω ~1 kΩ, ACV 120 mV; noise scales with integration time; null; syntax-error status |
| `SIM::SALEAE` | A Logic Pro 16 behind a fake Logic 2: UART text on D0, square waves on D1/D2, clock bursts on D3, analog sine and RC-filtered square. Edges fall on the sample grid; the trigger finds the real first edge and times everything from it. It writes real version 0 or 1 binary files, rejects invalid sample rates like Logic 2, and decodes Async Serial on D0 at 115200. Its `speed` lets tests finish captures faster than real time |
| `SIM::PPK2` | The real byte stream: a BLE-style DUT encoded through its own calibration (with range switching and a spike on each switch), metadata, mode, voltage and DUT power commands, counter, logic D0/D1, and a hook to drop samples. Its clock can be injected, so tests run without waiting |
| `SIM::FGEN` | A two-channel Keysight 33522B: SCPI `SOUR{n}:…` / `OUTP{n}` with read-back and the error queue |
| `SIM::SDG` | A two-channel Siglent SDG2042X: `C{n}:BSWV` / `C{n}:OUTP` |
| `SIM::GPIO` | A Pi header with 26 lines, honouring direction, pull and active-low, wired as a small jig: GPIO21 follows GPIO20, GPIO12 is GPIO16 inverted, GPIO26 a button (low 0.2 s every 2 s); the rest float to their pull |
| `SIM::SIGROK` | A fake `sigrok-cli` with an 8-channel 24 MHz fx2lafw analyzer: answers `--scan` / `--show` like the real program, writes real `.sr` files with the Saleae simulator's signals, triggers in software like libsigrok, ends a continuous capture on SIGINT, and decodes UART |

| `SIM::<id>` of any definition | `sim/sscpi.py`: built from the definition (see [Simulator expressions](#simulator-expressions)) |
| `SIM::SCOPE` / `DS1054Z`, `SIM::DSOX`, `SIM::TBS`, `SIM::SDS` | `sim/sscope.py`: a four-channel scope in the Rigol, Keysight, Tektronix or Siglent dialect (CH1 1 kHz 0–3.3 V square, CH2 2 kHz sine, CH3 triangle, CH4 DC, with noise), answering the dialect's channel, timebase, trigger, run/stop/single and status commands and sending its waveform bytes and preamble in that dialect's format |

`#n` after any simulator name opens another unit of it (`SIM::PPK2#2`); the number is only part of the resource name.

All three logic/power simulators share a **debug pin** (`sim/shared.py`): an irregular signal on app time, toggling about every 1 ms, that appears on the simulated analyzers' D7 and the simulated PPK2's D7, as if one DUT pin were wired to both. Use it to try PPK2 alignment without hardware: D7 ↔ PPK2 D7.

## Tests

```bash
.venv/bin/python -m pytest tests
```

The tests run each driver against its simulator: identification, CV/CC, protection trips and events, error decoding, every 663xx setting, saved setups, immediate/level/segmented/output-step captures with pre-trigger, abort, capture validation, the waveform measurements in `analysis.py`, the config files, and the 3456A reading formats. PPK2 tests cover metadata captured from a real unit, word decoding, the calibration formula, the spike filter against a sample-by-sample reference (across chunk boundaries), and the driver against the simulator: sample count, timing, levels, logic, lost samples, modes, and stop/start. `test_bench_features.py` covers profiles (parsing, timing, skipping), GPIO and both generator dialects against their simulators, limits and stop-on-fail, Wait for, the watchdog's hold logic across blocks, event finding and bus attachment, math evaluation and resampling, reports (HTML, and PDF when WeasyPrint is present), setups, and the Remote API end to end over HTTP. Saleae tests cover both binary-format versions (including multi-chunk files), the edge measurements and lane envelope, resource parsing, and timed, triggered, manual and aborted captures, decoding and save/export through the simulator and the real worker thread. `test_instruments_generic.py` loads every definition, round-trips every setting and option through its simulator, checks multi-channel expansion, units that follow a function, `query_from`, the scope dialects' decoding (Vpp and frequency for all four), the model names and auto-detect, the sequencer with SCPI and scope tabs, two units of one model, and two PPK2s aligning. `test_unattended.py` covers watchdog HW plans and re-arm, only-around-events sessions, the crash marker, safety settings and back-off, the results database and verdicts, resuming and `extra` in `run_plan`, and in an offscreen window: a limit pushed into a supply, outputs off on quit, outputs off after a crash, and a production run with the serial-format check. `test_robustness.py` covers the error handling: crash-log rate limiting and rotation, damaged `last-session.json` / `config.toml` / watchdog / math / sequence / setup data, math expressions that fail after evaluating, a session file whose last write fails, foreign session files, malformed Remote API requests, and, in an offscreen Qt window, an exception in a slot not aborting, a GPIO tab's lost link offering ⟳ Reconnect and clearing it on recovery, and Esc still reaching every instrument when one fails.

## Adding an instrument

**A SCPI instrument that only needs "set this, read that"** (a meter, a load, a supply, a switch box, a temperature chamber): write a [definition file](#instrument-definition-files-toml). No code, and its tab, simulator, sequencer and watchdog support come with it. To have it built in, add the file to `instruments/scpi_defs/`; `tests/test_instruments_generic.py` runs every built-in definition against its simulator.

**Anything else** (streaming data, waveforms, a binary protocol) needs a driver:

1. **Driver:** write a class in `instruments/` with `kind = "supply"` or `"dmm"` (or a new kind with its own panel, like `"fgen"` and `"gpio"`), plus `family`, `identify()`, `poll(t, cfg)`, `errors()`, and the setters the panel calls. Return the shared data types from `base.py`. A streaming device brings its own panel and data types instead; see the PPK2 (`kind = "ppk2"`, a `cfg` property for the worker to re-read, and `close()` to stop the stream).
2. **Capabilities:** fill in `SupplyCaps`. The supply panel hides what's unsupported (DVM, digitizer, waveform, resistance) and handles `setpoint_readback=False`.
3. **Register it:** add it to `MODELS` and `_CLASSES` in `instruments/__init__.py`, and teach `detect()` its ID string if it has one.
4. **Simulator:** subclass `sim.base.SimBase`, implement `write()`, and add it to `sim.open_sim` and `SIM_RESOURCES`.
5. **Panel:** a new kind needs a `PanelBase` subclass with `KIND` set (so Esc, the sequencer and the watchdog know what it is), `output_off()` if it has outputs, `csv_header()` and `self.record(row)` from `on_reading` for CSV and sessions, `session_settings()` / `restore_settings()`, and a mapping in `MainWindow`. To use it from the sequencer, add its settings to `sequencer.SET_WHAT` / `MEASURE_WHAT` (and `PROFILE_WHAT`) and handle them in `PanelInst`; for the watchdog and math, add its quantities to `signals.py`.
6. **Errors:** let driver exceptions propagate: the worker reports them, pauses polling and offers ⟳ Reconnect. Give a `close()` that releases the device even after an error, and keep each driver call bounded in time (a timeout), since a stuck call holds the tab's thread.
7. **Tests:** add `tests/test_<model>.py`.

## Licences and third-party code

- **linux-gpib** (GPL): built from `vendor/linux-gpib` (not committed; fetched by the install script).
- **pyvisa, pyvisa-py** (MIT), **pyqtgraph** (MIT), **PyQt6** (GPL v3), **gpib-ctypes** (GPL v2), **pyserial** (BSD), **pyusb** (BSD), **libgpiod** Python bindings (LGPL), **WeasyPrint** (BSD; optional), **sigrok-cli** (GPL; run as a separate program), **logic2-automation** (Apache 2.0; optional).
- **PPK2 protocol and calibration:** our own implementation, following the behaviour of Nordic's open-source nRF Connect Power Profiler (`pc-nrfconnect-ppk`); no code is copied from it.
