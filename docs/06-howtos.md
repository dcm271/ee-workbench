---
title: How-to Guides
subtitle: Step-by-step recipes for common bench jobs
---

# How-to Guides

Other chapters: [Hardware](01-hardware-and-resources.md) · [Installation](02-installation.md) · [User's Guide](03-users-guide.md) · [Technical Reference](04-technical-reference.md) · [Raspberry Pi](05-raspberry-pi.md) · [README](../README.md)

Each recipe is a complete job from start to finish. The [User's Guide](03-users-guide.md) explains every control the recipes use.

| Recipe | Uses |
|---|---|
| [Try everything without hardware](#try-everything-without-hardware) | simulators |
| [Measure sleep current and estimate battery life](#measure-sleep-current-and-estimate-battery-life) | PPK2, events |
| [Find out what each current burst is](#find-out-what-each-current-burst-is) | PPK2, logic analyzer, alignment, events |
| [Power-cycle a DUT with a relay and check it boots](#power-cycle-a-dut-with-a-relay-and-check-it-boots) | GPIO, sequencer, Wait for |
| [Run a pass/fail test and get a report](#run-a-passfail-test-and-get-a-report) | sequencer limits, reports, setups |
| [Sweep the supply voltage and plot current](#sweep-the-supply-voltage-and-plot-current) | sequencer sweep |
| [Find a DUT's brown-out voltage](#find-a-duts-brown-out-voltage) | supply profile, session |
| [Measure a frequency response](#measure-a-frequency-response) | function generator, DMM, sequencer |
| [Measure a regulator's efficiency](#measure-a-regulators-efficiency) | two supplies, math channels |
| [Protect a DUT with the watchdog](#protect-a-dut-with-the-watchdog) | watchdog |
| [Switch between bench setups](#switch-between-bench-setups) | setups |
| [Drive the bench from Jupyter](#drive-the-bench-from-jupyter) | Remote API |
| [Run hardware tests from pytest or CI](#run-hardware-tests-from-pytest-or-ci) | Remote API |
| [Analyse a recorded session](#analyse-a-recorded-session) | session viewer, cursors, export |
| [Connect a LAN function generator](#connect-a-lan-function-generator) | function generator |
| [Recover from a pulled cable or a crash](#recover-from-a-pulled-cable-or-a-crash) | ⟳ Reconnect, crash log, restart-on-crash |

---

## Try everything without hardware

Every instrument has a simulator, so you can learn the app (or develop a sequence) at your desk.

```bash
./run.sh SIM::66321D SIM::PPK2 SIM::SIGROK SIM::FGEN SIM::GPIO
```

1. **66321D tab:** set 3.3 V, **Set**, then **Output ON**. The simulated phone load draws bursts; watch the **Trend**.
2. **PPK2 tab:** **DUT POWER** on. Turn on **Log scale** to see sleep µA and radio mA together. In **Events**, press **Find events**.
3. **Logic tab (sigrok):** set **Align with PPK2** to D7 ↔ PPK2 D7, tick D0 and D7, add a **UART** decoder on D0 at 115200, and **▶ Capture**. The PPK2 current appears under the lanes.
4. **GPIO tab:** set GPIO20 to Output and GPIO21 to Input. Switching 20 also switches 21, because the simulated jig wires them together. GPIO26 is a button that is pressed every 2 s.
5. **Tools ▾ → Sequencer**, **Watchdog**, **Math channels**: see the recipes below.

---

## Measure sleep current and estimate battery life

**You need:** a PPK2, and the DUT's battery removed.

1. Wire the PPK2 as a **source meter**: VOUT to the DUT's battery +, GND to battery −. Plug it in and open it (`PPK2::…`).
2. **Source meter**, **Voltage** = the battery's nominal voltage (e.g. 3000 mV for a CR2032), **DUT POWER** on.
3. Let the device settle into its normal cycle (advertising, sensing, sleeping).
4. **Trend** tab: **Span** 2 min, **Log scale** on. The readouts give the **Average** over the span.
5. **Events** tab: **Data** = *Trend span shown*, **Above** = a level clearly over the sleep current (e.g. 0.5 mA), and **Find events**. The summary gives:
   - the number of events and their **rate** (e.g. 1.0/s),
   - the **charge per event** (e.g. 18 µC),
   - the average current **between** events, which is the true sleep current.
6. Battery life ≈ capacity ÷ average current. For example, 220 mAh ÷ 21 µA ≈ 10,500 h ≈ 14 months. Then see which term dominates: sleep current × time, or events × charge. The **Histogram of Charge** shows whether some events cost much more than others.

To keep a record, press **⏺ Record** before step 4. The session holds the trend, and the viewer's [cursors](03-users-guide.md#looking-through-a-session) can measure the charge over any stretch later.

---

## Find out what each current burst is

**You need:** a PPK2 and a logic analyzer (a Saleae, or any sigrok one; a few-dollar fx2lafw clone is enough).

1. Power the DUT from the PPK2 as above.
2. Connect the logic analyzer to the buses you care about (UART TX, I²C SDA/SCL, SPI) **and** connect one irregular signal to both the analyzer and a PPK2 logic input. UART TX or a firmware debug pin works well. Wire the PPK2's logic-header GND to the DUT, and its VCC to the DUT's I/O voltage.
3. **Logic tab:** add the bus decoders. In **Align with PPK2**, choose the PPK2 tab and the shared signal (e.g. analyzer D0 ↔ PPK2 D0). Set **Duration** to cover several events (e.g. 2 s).
4. **▶ Capture.** The note should say "✓ … edges match, ±5 µs".
5. **PPK2 → Events → Live buffer → Find events.** The **Bus during the event** column lists the decoded traffic inside each burst, e.g. `i2c write 0x48 …`, `uart "TX OK"`. Sort by **Charge** to see the most expensive transactions first.
6. Select an event to see its waveform. On the logic tab, click a decoded row to jump to it, with the current drawn underneath.

---

## Power-cycle a DUT with a relay and check it boots

**You need:** a Raspberry Pi running EE Workbench, a 1-channel 5 V relay module, and a DUT with a pin that goes high when it has booted (or a UART message; see the variant at the end).

**Wiring** (see [Hardware → GPIO](01-hardware-and-resources.md#raspberry-pi-gpio-header)):

| From | To |
|---|---|
| Pi pin 2 (5 V) | relay module VCC |
| Pi pin 6 (GND) | relay module GND |
| Pi pin 38 (GPIO20) | relay module IN |
| Supply + | relay COM; relay NO → DUT + |
| Supply − | DUT − |
| DUT "booted" pin (3.3 V logic) | 1 kΩ → Pi pin 40 (GPIO21) |
| DUT GND | Pi GND (pin 39) |

**In the app:**

1. Open the supply and `GPIO::…`. In the GPIO tab, set GPIO20 to **Output**, named `relay`, with **Active low** ticked (most relay modules switch on when IN is low). Set GPIO21 to **Input**, named `booted`, **Pull** down.
2. Check by hand: **ON** in the relay row should click the relay, and the DUT should start. **Esc** turns it off.
3. **Tools ▾ → Sequencer**, with these steps:

| Action | Instrument | Setting | Value | Min | Max | Results column |
|---|---|---|---|---|---|---|
| Set | 66321D | voltage | 3.3 | | | |
| Set | 66321D | output | on | | | |
| Set | GPIO | relay | on | | | |
| Wait for | GPIO | booted | 1 timeout=5 | | 1.5 | boot_s |
| Measure | 66321D | current | 1 | | 0.02 | I_run |
| Set | GPIO | relay | off | | | |
| Wait | | | 2 | | | |

4. Choose **Repeat** with 200 passes. Leave **Stop on first fail** off for a soak test, so every failure is counted, or turn it on to stop at the first failure and look. Tick **Outputs off at the end**.
5. **▶ Run.** Each row gives the boot time and the running current, marked PASS or FAIL. Plot `boot_s` against `pass` to see drift. When the run finishes, use **Report…**.

**Variant: boot detected on the UART.** Replace *Wait for* with *Capture Logic* (with the analyzer set to **Trigger** on TX's falling edge, and a UART decoder) and **Min** `1`. The pass then fails if nothing was printed.

---

## Run a pass/fail test and get a report

1. Open every instrument the test needs, and give the tabs short names (e.g. "DUT rail", "3V3 out").
2. In the sequencer, build the steps: power up, wait, then **Measure** each quantity with its **Min** / **Max**. Limits accept expressions, e.g. `3.3 * 0.98` and `3.3 * 1.02`.
3. **Repeat** 1, tick **Stop on first fail** and **Outputs off at the end**.
4. **▶ Run.** The status line shows **PASS** or **FAIL**, failing cells are red, and the `failures` column says what was wrong and what was wanted.
5. **Report…** → `board-0042.pdf`. The report holds the verdict, the instruments with their IDs, the results and plots, the steps, and the log.
6. **Tools ▾ → Setups… → Save current as…** "Board test". Next time it's **Recall**, then **Run**.

To test many boards and keep one file, use **Repeat** with the number of boards, and add a **Note** step `Board {i}: swap the DUT` followed by a **Wait for** on a GPIO "next" button. **Save CSV…** at the end gives one row per board.

---

## Sweep the supply voltage and plot current

1. Sequencer → **Sweep x**, from `1.8` to `3.6`, step `0.1`.
2. Steps: *Set PPK2 voltage* `x` (or a supply's voltage) → *Wait* `0.5` → *Measure PPK2 current* `2`, column `I_avg` → *Note* `{x} V done`.
3. **▶ Run**, then plot `I_avg` against `x`. **Save CSV…** or **Report…**.

For a log sweep (e.g. load current from 1 µA to 100 mA), sweep `x` from `-6` to `-1` and set `10 ** x`.

---

## Find a DUT's brown-out voltage

1. Open the supply powering the DUT, and something that shows the DUT is alive: a GPIO input on a heartbeat pin, or the logic analyzer, or just the supply's current.
2. Start a session (**⏺ Record**) so that everything lands on one time axis.
3. Supply tab → **Profile**: **Vary** = voltage, profile `ramp 3.3..1.5 for=90 rate=10`. Turn the output on, then press **▶ Run profile**.
4. Stop the session and open it. In the viewer, tick the supply's voltage and the alive signal (GPIO pin or current), turn on **Cursors**, and put A where the alive signal stops. **at A** for the voltage is the brown-out level.
5. For the power-on threshold, run `ramp 1.5..3.3 for=90`. Use `steps 2.4..1.8 n=13 dwell=5` if the DUT needs time at each level to show the problem.

---

## Measure a frequency response

**You need:** a function generator and the 3456A (or any DMM) in ACV.

1. Generator CH1 → the circuit's input. Set **Sine**, the amplitude you want (mind the 50 Ω / High-Z setting, see [Hardware](01-hardware-and-resources.md#function-generators)), offset 0, and **CH1 output** on.
2. DMM across the output, function **ACV**, integration 10 PLC.
3. Sequencer → **Sweep x** from `1` to `5` step `0.1` (that's 10 Hz to 100 kHz, 41 points).
4. Steps: *Set FGEN frequency* `10 ** x` → *Wait* `1.5` → *Measure 3456A value* `0`, column `Vout`.
5. Plot `Vout` against `x`. Add a math step in a script if you want dB:

```python
for x in np.arange(1, 5.01, 0.1):
    f = 10 ** x
    bench.set("FGEN", "frequency", f)
    bench.wait(1.5)
    v = bench.measure("3456A", "value")
    bench.record(f_Hz=round(f, 1), Vout=v, gain_dB=20 * math.log10(v / 0.3536))   # 1 Vpp in = 0.3536 Vrms
```

Alternatively, without the sequencer: generator **Profile** tab, **Vary** = frequency, `logsteps 10..100000 n=41 dwell=1.5`, and record the DMM.

---

## Measure a regulator's efficiency

1. 6632A → regulator input (tab named "in"). The 66321D, or a resistor with the 66321D measuring it, on the output (tab named "out").
2. **Tools ▾ → Math channels → ＋ Channel**: name `eff`, expression `_66321d_out.power / _6632a_in.power * 100`, unit `%`. (The **Signals** list shows the exact names; double-click one to insert it.)
3. Add `P_loss` = `_6632a_in.power - _66321d_out.power`, unit `W`.
4. Vary the load and read `eff` live. For a curve, use the sequencer to sweep the load current and **Measure** the two powers, then compute the efficiency in a script with `bench.record(...)`.

---

## Protect a DUT with the watchdog

**Tools ▾ → Watchdog → ＋ Rule** for each case:

| Protect against | Rule |
|---|---|
| A shorted or latched-up DUT | PPK2 · current **>** `80m` for `5` ms → **all outputs off** |
| Overheating a regulator under test | 66321D · power **>** `2.5` for `1000` ms → **turn off** 66321D |
| Back-powering a DUT through its inputs | DUT rail · voltage **<** `2.9` → **turn off** FGEN (and the GPIO tab) |
| An operator pressing an e-stop | GPIO · `gpio26` (button to GND, Pull up) **<** `0.5` → **all outputs off** |
| A sequence running on after a failure | 3V3 out · value **<** `3.0` for `200` ms → **stop sequence** |

Keep **Watchdog armed** ticked. A trip shows a message, writes a note into the session, and marks the rule **TRIPPED** until you press **Re-arm tripped**. The supplies' own OCP / OVP still act first, in hardware; the watchdog adds rules that span instruments.

---

## Switch between bench setups

1. Arrange the bench for one job: instruments, tab names, decoders, GPIO pins, the sequence, watchdog rules and math channels.
2. **Tools ▾ → Setups… → Save current as…** "nRF52 sleep current", with a description.
3. Arrange it for another job and save that as "Regulator efficiency".
4. To switch, select a setup and press **Recall**: the tabs close and the saved ones open. Outputs stay off and setpoints are read from the instruments.
5. **Export…** writes a setup to a JSON file you can email or commit to a project repo. **Import…** on another bench loads it. Resource names (GPIB addresses, serial numbers) must match that bench's, so edit them in the file if they don't.

---

## Drive the bench from Jupyter

1. In the app, open the instruments and switch on **Tools ▾ → Remote API**.
2. In a notebook (any Python 3.9+; pandas and matplotlib for the plotting below):

```python
import sys; sys.path.insert(0, "/home/me/ee-workbench")
from ee_workbench.client import connect
import numpy as np, pandas as pd

bench = connect()
bench.instruments()

rows = []
for v in np.arange(1.8, 3.61, 0.2):
    bench.set("PPK2", "voltage", float(v))
    bench.wait(0.5)
    rows.append({"V": v, "I_uA": bench.measure("PPK2", "current", 2.0) * 1e6})
df = pd.DataFrame(rows)
df.plot(x="V", y="I_uA", marker="o")
```

3. You can watch it happen in the app. **Esc** stops the running call, which raises `BenchError: stopped`.

Pass plain Python numbers (`float(v)`, not numpy scalars) as arguments, because they travel as JSON.

---

## Run hardware tests from pytest or CI

On a bench computer (a Pi is fine) running the app with `api = true` in `config.toml`, so the API is up after a reboot:

```python
# tests_hw/test_board.py
import pytest
from ee_workbench.client import connect, BenchError

@pytest.fixture(scope="module")
def bench():
    try:
        b = connect()
        b.instruments()
    except BenchError as e:
        pytest.skip(f"no bench: {e}")
    b.set("DUT rail", "voltage", 3.3)
    b.set("DUT rail", "output", "on")
    b.wait(0.5)
    yield b
    b.outputs_off()

def test_boots(bench):
    bench.gpio("GPIO", "reset", "pulse 0.05")
    assert bench.wait_for("GPIO", "booted", 1, timeout=5) < 1.5

def test_sleep_current(bench):
    i = bench.measure("PPK2", "current", 5.0)
    assert bench.expect("I_sleep", i, hi=10e-6), f"sleep current {i * 1e6:.1f} µA"

def test_says_hello(bench):
    cap = bench.capture("Logic")
    assert any("hello" in str(r).lower() for r in cap["rows"])
```

Run it as `pytest tests_hw -v`. As a CI step on a self-hosted runner on the bench machine, add `--junitxml=hw.xml` for the CI's test report. Each `expect()` and `record()` also shows in the app's sequencer results, and in the session if one is recording, so a failed CI run can be looked at in the session viewer.

---

## Analyse a recorded session

1. **Tools ▾ → Open session…** and pick the `.eewb` file.
2. Tick the signals to plot. Scroll to zoom, or click a capture, note or decoded row to jump to it.
3. Turn on **Cursors** and drag **A** and **B** around the stretch you care about. The table gives the value at each cursor, the difference, the mean, the min/max, and the charge or energy between them.
4. **Math…** derives a new trace, e.g. power = `_66321d.voltage * ppk2.current`, or energy with **Integrate**.
5. Zoom to the stretch to keep, then use **Export CSV…** (the ticked signals), **Save PNG…**, or **Report…** (HTML/PDF with plots, notes and results).

Other tools can read the file too. It's SQLite: `sqlite3 run.eewb "select * from notes"`, or in Python `pandas.read_sql("select * from s_1", sqlite3.connect("run.eewb"))`. The tables are listed in the [Technical Reference](04-technical-reference.md#session-files-sessionpy).

---

## Connect a LAN function generator

1. On the generator: **Utility → I/O → LAN**. Set a static IP (e.g. 192.168.1.50/24), or reserve one in your router, and make sure LAN/VXI-11 is enabled.
2. From the computer, `ping 192.168.1.50` should answer.
3. In the app, type `TCPIP::192.168.1.50::INSTR` into the **Resource** box, set **Model** to *Auto-detect* (or *Function generator*), and **Connect**. On a touch screen, use **＋ Add instrument → Type a resource…**.
4. The tab shows the generator's current settings and outputs, read from it. Nothing is changed by connecting.
5. To have it open at every start, connect it once (`open = "last"` remembers it), or add it to `config.toml`:

```toml
[[instrument]]
resource = "TCPIP::192.168.1.50::INSTR"
model = "FGEN"
```

If VXI-11 doesn't answer, use the raw socket form: `TCPIP::192.168.1.50::5025::SOCKET` for Keysight and Siglent, or `::5555::SOCKET` for Rigol.

## Recover from a pulled cable or a crash

**An instrument stopped answering** (its tab shows ⚠ and **⟳ Reconnect**):

1. Fix the cause: switch it back on, replug the USB cable, reseat the GPIB connector.
2. Tap **⟳ Reconnect** on the tab. It closes and reopens in the same place, with its tab settings kept, and reads the instrument's state as usual (nothing is switched on).
3. If you were recording a [session](03-users-guide.md#sessions-everything-in-one-file), it carries on; the gap shows as a gap.

**A window "EE Workbench: internal error" appeared:** the app caught a problem and carried on. Close the window and keep working. To report it, send `~/.config/ee-workbench/crash.log`.

**The app closed by itself:**

1. Check the bench first: outputs stay as they were when the app ended.
2. Start it again (`./run.sh`). It reopens the same instruments; supply setpoints are read from the supplies.
3. The cause is in `~/.config/ee-workbench/fault.log` (a crash in Qt or a driver) or `crash.log`.
4. On a screen nobody watches, launch it with `./run.sh --restart-on-crash --fullscreen` so it comes back by itself (the Pi's autostart already does). Add a [watchdog](#protect-a-dut-with-the-watchdog) rule if the DUT must be protected meanwhile.

**Esc reported "⚠ FAILED for …":** every other output was switched off; switch the named instrument off at its front panel, then reconnect it.
