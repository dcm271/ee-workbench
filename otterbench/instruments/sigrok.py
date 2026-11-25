"""Logic analyzers supported by sigrok (fx2lafw clones and the original Saleae
Logic, DreamSourceLab DSLogic, Saleae Logic16, Hantek 4032L, ...), through the
`sigrok-cli` program.

Unlike Logic 2, sigrok runs on the Raspberry Pi. Each capture is one
`sigrok-cli` run writing a sigrok session file (.sr); protocol decoders are
further runs over that file. Checked against sigrok-cli 0.7.2 / libsigrok
0.5.2 and its demo device:

* `.sr` is a zip: `metadata` (INI: samplerate, probeN = name, unitsize) and
  raw sample chunks `logic-1-1`, `logic-1-2`, ... Channel probeN is bit N-1
  of each sample, even when other channels are off.
* Triggers are done in software, checked from the very first sample: the
  first match fires, with up to captureratio % of the samples from before it
  (less if it came sooner after the start). The file doesn't say where it
  was, so t = 0 is put at the first sample in the data that meets the
  trigger condition.
* A continuous capture ends cleanly on SIGINT (killed by the signal, or exit
  status 130 from a shell), but also as soon as its stdin reaches end-of-file,
  so it is given a pipe that stays open.
* Decoder output with --protocol-decoder-samplenum: "509-579 uart-1: 48".
  A wrong channel name only prints "No channel with name ..." to stderr and
  still exits 0, so stderr is checked.
"""
from __future__ import annotations

import configparser
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import zipfile

import numpy as np

from ..clock import now as app_now
from .logic import DigitalTrace, LogicCapture, LogicCaps, LogicConfig, fmt_rate

# USB logic-analyzer drivers worth scanning. Serial-port drivers are left out on
# purpose: scanning them would send bytes to whatever is on /dev/ttyACM* (a PPK2).
SCAN_DRIVERS = ["fx2lafw", "saleae-logic16", "dreamsourcelab-dslogic", "hantek-4032l",
                "sysclk-lwla", "lecroy-logicstudio", "asix-sigma", "kingst-la2016"]
TRIGGER_CODES = {"rising": "r", "falling": "f", "either": "e", "high": "1", "low": "0"}
DECODERS = {
    "UART": [("rx", 0, "ch"), ("baudrate", 115200, "int")],
    "I2C": [("scl", 0, "ch"), ("sda", 1, "ch")],
    "SPI": [("clk", 0, "ch"), ("mosi", 1, "ch"), ("miso", 2, "ch"), ("cs", 3, "ch")],
}
PD_IDS = {"UART": "uart", "I2C": "i2c", "SPI": "spi"}
# The annotation classes worth a table row (the rest are single bits etc.)
PD_ROWS = {"uart": "rx-data:tx-data:rx-warnings:tx-warnings",
           "i2c": "start:repeat-start:stop:ack:nack:address-read:address-write:"
                  "data-read:data-write:warnings",
           "spi": "mosi-data:miso-data:warnings"}
STD_RATES = [24e6, 16e6, 12e6, 8e6, 6e6, 4e6, 2e6, 1e6, 500e3, 250e3, 100e3, 50e3, 20e3]
MAX_SAMPLES = 400_000_000          # what we're willing to hold in memory per capture

_SI = {"": 1, "k": 1e3, "M": 1e6, "G": 1e9}


def sigrok_cli() -> str | None:
    return os.environ.get("OTTERBENCH_SIGROK_CLI") or shutil.which("sigrok-cli")


def parse_hz(text: str) -> float | None:
    m = re.match(r"\s*([\d.]+)\s*([kMG]?)Hz", text)
    return float(m.group(1)) * _SI[m.group(2)] if m else None


def device_spec(resource: str) -> str:
    """'SIGROK::fx2lafw:conn=1.4' -> 'fx2lafw:conn=1.4'"""
    r = resource.strip()
    return r[8:] if r.upper().startswith("SIGROK::") else r


def parse_scan(text: str) -> list[dict]:
    """sigrok-cli --scan: 'fx2lafw:conn=1.4 - Saleae Logic with 8 channels: D0 D1 ...'"""
    out = []
    for line in text.splitlines():
        m = re.match(r"^(\S+) - (.+?) with (\d+) channels?: (.*)$", line.strip())
        if m:
            out.append({"spec": m.group(1), "name": m.group(2), "channels": m.group(4).split()})
    return out


def parse_show(text: str) -> dict:
    """The parts of sigrok-cli --show we use: sample rates, triggers, captureratio."""
    rates, lo, hi, triggers = [], None, None, []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("samplerate - supported samplerates"):
            for nxt in lines[i + 1:]:
                hz = parse_hz(nxt.strip())
                if hz is None or not nxt.startswith("      "):
                    break
                rates.append(hz)
        m = re.match(r"samplerate \((.+?) - (.+?) in steps of", s)
        if m:
            lo, hi = parse_hz(m.group(1)), parse_hz(m.group(2))
        if s.startswith("Supported triggers:"):
            triggers = s.split(":", 1)[1].split()
    if not rates and hi:
        rates = [r for r in STD_RATES if lo <= r <= hi]
    codes = {v: k for k, v in TRIGGER_CODES.items()}
    return {"rates": sorted(set(rates), reverse=True) or list(STD_RATES),
            "triggers": [codes[c] for c in ("r", "f", "e", "1", "0") if c in triggers],
            "captureratio": "captureratio" in text}


# ---------------------------------------------------------------- session files
def read_sr(path: str) -> tuple[float, dict[int, str], np.ndarray]:
    """(sample rate, {bit: channel name}, samples) of a sigrok session file."""
    with zipfile.ZipFile(path) as z:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_string(z.read("metadata").decode())
        dev = cp["device 1"]
        rate = parse_hz(dev.get("samplerate", "")) or 0.0
        unit = int(dev.get("unitsize", "1"))
        base = dev.get("capturefile", "logic-1")
        names = {int(k[5:]) - 1: v for k, v in dev.items()
                 if k.startswith("probe") and k[5:].isdigit()}
        chunks = sorted((n for n in z.namelist() if n == base or n.startswith(base + "-")),
                        key=lambda n: int(n.rsplit("-", 1)[1]) if n != base else 0)
        raw = b"".join(z.read(n) for n in chunks)
    dtype = {1: np.uint8, 2: "<u2", 4: "<u4", 8: "<u8"}[unit]
    data = np.frombuffer(raw[: len(raw) // unit * unit], dtype)
    return rate, names, data


def write_sr(path: str, rate: float, names: dict[int, str], data: np.ndarray):
    """Write a sigrok session file (the simulator and tests use this)."""
    unit = data.dtype.itemsize
    meta = ["[global]", "sigrok version=0.5.2", "", "[device 1]", "capturefile=logic-1",
            f"total probes={8 * unit}", f"samplerate={_hz_text(rate)}", "total analog=0"]
    meta += [f"probe{b + 1}={n}" for b, n in sorted(names.items())]
    meta += [f"unitsize={unit}", ""]
    raw = np.ascontiguousarray(data).astype(data.dtype.newbyteorder("<")).tobytes()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("version", "2")
        z.writestr("metadata", "\n".join(meta))
        step = 4 * 1024 * 1024
        for i, k in enumerate(range(0, max(len(raw), 1), step)):
            z.writestr(f"logic-1-{i + 1}", raw[k:k + step])


def _hz_text(hz: float) -> str:
    for div, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if hz >= div:
            return f"{hz / div:g} {unit}"
    return f"{hz:g} Hz"


def trigger_index(bits: np.ndarray, kind: str) -> int:
    """First sample meeting the trigger condition (0 if none)."""
    if kind in ("high", "low"):
        hit = np.flatnonzero(bits == (1 if kind == "high" else 0))
        return int(hit[0]) if hit.size else 0
    d = np.diff(bits.astype(np.int8))
    want = {"rising": d > 0, "falling": d < 0, "either": d != 0}[kind]
    hit = np.flatnonzero(want)
    return int(hit[0]) + 1 if hit.size else 0


def traces_from_samples(data: np.ndarray, rate: float, channels: dict[int, int],
                        trig: int = 0) -> dict[int, DigitalTrace]:
    """{channel: bit} of raw samples -> DigitalTraces, timed so sample `trig` is 0."""
    n = data.size
    t0, end = -trig / rate, (n - trig) / rate
    out = {}
    for ch, bit in sorted(channels.items()):
        bits = ((data >> bit) & 1).astype(np.uint8)
        if n == 0:
            out[ch] = DigitalTrace(ch, np.array([t0]), np.array([0], np.uint8), t0)
            continue
        idx = np.flatnonzero(bits[1:] != bits[:-1]) + 1
        t = np.concatenate(([t0], (idx - trig) / rate))
        out[ch] = DigitalTrace(ch, t, np.concatenate((bits[:1], bits[idx])), end)
    return out


def parse_annotations(text: str, label: str, decoder: str, rate: float, trig: int):
    """'509-579 uart-1: 48' lines -> data-table rows."""
    rows = []
    for line in text.splitlines():
        m = re.match(r"^(\d+)-(\d+) [^:]+: (.*)$", line.strip())
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            rows.append([label, decoder, f"{(a - trig) / rate:.9f}", f"{(b - a) / rate:.9f}",
                         m.group(3)])
    return rows


# ---------------------------------------------------------------- transport
class SigrokLink:
    """Runs sigrok-cli. The simulator (`sim.ssigrok.SimSigrokCli`) answers the
    same two calls by interpreting the arguments itself."""

    def __init__(self, exe: str | None = None):
        self.exe = exe or sigrok_cli()
        if not self.exe:
            raise RuntimeError("sigrok-cli isn't installed: sudo apt install sigrok-cli "
                               "sigrok-firmware-fx2lafw   (or run scripts/setup-ubuntu.sh)")
        self.timeout_s = 20.0

    def run(self, args: list[str], timeout: float | None = None) -> tuple[int, str, str]:
        p = subprocess.run([self.exe] + args, capture_output=True, text=True,
                           timeout=timeout or self.timeout_s, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout, p.stderr

    def spawn(self, args: list[str]) -> subprocess.Popen:
        # stdin must stay open: a continuous capture ends at once when it reads
        # end-of-file there (as it would from /dev/null under a desktop launcher).
        return subprocess.Popen([self.exe] + args, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace")

    # what the worker expects of every transport
    def clear(self):
        pass

    def read_stb(self) -> int:
        return 0

    def close(self):
        pass


def find_sigrok(drivers: list[str] | None = None) -> list[str]:
    """SIGROK::<spec> for each USB logic analyzer sigrok can see (~20 ms per
    driver, run side by side), plus its built-in demo device."""
    exe = sigrok_cli()
    if not exe:
        return []
    found: dict[str, list[str]] = {}

    def scan(drv):
        try:
            p = subprocess.run([exe, "-d", drv, "--scan"], capture_output=True, text=True,
                               timeout=5, encoding="utf-8", errors="replace")
            found[drv] = [d["spec"] for d in parse_scan(p.stdout)]
        except Exception:
            found[drv] = []
    threads = [threading.Thread(target=scan, args=(d,), daemon=True)
               for d in (drivers or SCAN_DRIVERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(6)
    specs = [s for d in (drivers or SCAN_DRIVERS) for s in found.get(d, [])]
    return [f"SIGROK::{s}" for s in specs] + ["SIGROK::demo"]


def _close_pipes(proc):
    for f in (getattr(proc, "stdin", None), getattr(proc, "stderr", None)):
        try:
            if f:
                f.close()
        except Exception:
            pass


# ---------------------------------------------------------------- driver
class SigrokLogic:
    kind = "logic"
    family = "sigrok"

    def __init__(self, transport, resource: str = ""):
        self.t = transport
        self.spec = device_spec(resource) or "demo"
        self.caps = LogicCaps(model="sigrok", analog=0)
        self.state = LogicConfig(digital_rate=1e6)
        self.log = None
        self.have_capture = False
        self._run: dict | None = None
        self._dir = tempfile.mkdtemp(prefix="otterbench-sigrok-")
        self._sr = os.path.join(self._dir, "capture.sr")
        self._rate = 1.0
        self._trig = 0

    @property
    def cfg(self) -> LogicConfig:
        return LogicConfig(**{k: (list(v) if isinstance(v, list) else v)
                              for k, v in vars(self.state).items()})

    def errors(self) -> list[str]:
        return []

    def _log(self, text):
        if self.log:
            self.log(text)

    def _cli(self, args, timeout=None) -> str:
        self._log("sigrok-cli " + " ".join(args))
        rc, out, err = self.t.run(args, timeout)
        problem = [ln for ln in err.splitlines()
                   if ln.startswith(("Error", "cli: No channel", "Failed")) or "failed" in ln.lower()]
        if rc != 0 or problem:
            raise RuntimeError("sigrok-cli: " + ("; ".join(problem) or err.strip()
                                                 or f"exit status {rc}"))
        return out

    # ---- connect ------------------------------------------------------------
    def identify(self) -> LogicCaps:
        drv = self.spec.split(":", 1)[0]
        devs = parse_scan(self._cli(["-d", self.spec, "--scan"]))
        if not devs:
            raise RuntimeError(f"sigrok finds no {drv} device. Is it plugged in? (Cheap "
                               "24 MHz analyzers need sigrok-firmware-fx2lafw; USB access "
                               "needs the libsigrok udev rules: replug after installing.)")
        dev = devs[0]
        self.spec = dev["spec"]
        show = parse_show(self._cli(["-d", self.spec, "--show"]))
        names = [n for n in dev["channels"] if not re.match(r"^A\d+$", n)]   # logic only
        self.caps = LogicCaps(
            idn=f"{dev['name']}  ({self.spec})", model="sigrok",
            note="via sigrok-cli · digital channels only", device_id=self.spec,
            digital=len(names), analog=0, thresholds=[], digital_rates=show["rates"],
            analog_rates=[], channel_names=names,
            trigger_types=show["triggers"] or ["rising", "falling"],
            decoders=DECODERS, capture_filter="sigrok session (*.sr)", capture_suffix=".sr",
            info={"captureratio": show["captureratio"]})
        s = self.state
        s.digital = [c for c in s.digital if c < len(names)] or [0]
        s.analog = []
        if s.digital_rate not in show["rates"]:
            s.digital_rate = min(show["rates"], key=lambda r: abs(r - 1e6))
        s.threshold = None
        return self.caps

    def configure(self, cfg: LogicConfig):
        if not cfg.digital:
            raise ValueError("Turn on at least one digital channel")
        if cfg.mode == "trigger":
            if cfg.trig_channel not in cfg.digital:
                raise ValueError(f"The trigger channel {self.caps.channel_name(cfg.trig_channel)} "
                                 "isn't enabled")
            if cfg.trig_type not in self.caps.trigger_types:
                raise ValueError(f"This analyzer can't trigger on {cfg.trig_type}")
        cfg.analog = []
        self.state = cfg

    def _samples(self, seconds: float, rate: float) -> int:
        n = int(round(seconds * rate))
        if n > MAX_SAMPLES:
            raise ValueError(f"{seconds:g} s at {fmt_rate(rate)} is {n / 1e6:.0f} M samples; "
                             f"the limit is {MAX_SAMPLES / 1e6:.0f} M. Lower the rate or the time.")
        return max(n, 16)

    # ---- capture --------------------------------------------------------------
    def arm(self, cfg: LogicConfig | None = None) -> float:
        if cfg is not None:
            self.configure(cfg)
        self.discard()
        cfg = self.cfg
        chans = ",".join(self.caps.channel_name(c) for c in cfg.digital)
        conf = f"samplerate={int(cfg.digital_rate)}"
        args = ["-d", self.spec, "--channels", chans, "-o", self._sr]
        if cfg.mode == "timed":
            args += ["--samples", str(self._samples(cfg.duration, cfg.digital_rate))]
        elif cfg.mode == "trigger":
            total = cfg.pre_trigger + cfg.after_trigger
            args += ["--samples", str(self._samples(total, cfg.digital_rate)),
                     "--triggers",
                     f"{self.caps.channel_name(cfg.trig_channel)}={TRIGGER_CODES[cfg.trig_type]}"]
            if self.caps.info.get("captureratio") and total > 0:
                conf += f":captureratio={int(round(100 * cfg.pre_trigger / total))}"
        else:
            args += ["--continuous"]
        args[:0] = ["--config", conf]
        self._log("sigrok-cli " + " ".join(args))
        proc = self.t.spawn(args)
        self._run = {"cfg": cfg, "proc": proc, "t0": time.monotonic(), "t_armed": app_now(),
                     "stopped": False}
        return -1.0

    def fire(self):
        pass

    def stop_capture(self):
        run = self._run
        if run and run["proc"].poll() is None:
            if run["cfg"].mode != "manual":
                raise RuntimeError("Only a manual capture can be stopped early; use Abort")
            run["stopped"] = True
            run["proc"].send_signal(signal.SIGINT)
            run["proc"].wait(15)

    def capture_done(self) -> bool:
        run = self._run
        return run is None or run["proc"].poll() is not None

    def abort(self):
        run, self._run = self._run, None
        if run and run["proc"].poll() is None:
            run["proc"].kill()
            run["proc"].wait(5)
        if run:
            _close_pipes(run["proc"])
        self._remove_file()

    def discard(self):
        if self._run is not None:
            self.abort()
        self.have_capture = False
        self._remove_file()

    def _remove_file(self):
        try:
            os.remove(self._sr)
        except OSError:
            pass

    def fetch(self, t_app: float = 0.0) -> LogicCapture:
        run, self._run = self._run, None
        if run is None:
            raise RuntimeError("No capture to fetch")
        proc, cfg = run["proc"], run["cfg"]
        err = proc.stderr.read() if proc.stderr else ""
        _close_pipes(proc)
        rc = proc.returncode
        ok_rc = rc == 0 or (run["stopped"] and rc in (130, -signal.SIGINT))
        if not ok_rc or not os.path.exists(self._sr):
            raise RuntimeError("sigrok-cli: " + (err.strip() or f"exit status {rc}"))
        rate, names, data = read_sr(self._sr)
        self._rate = rate or cfg.digital_rate
        by_name = {n: b for b, n in names.items()}
        chans = {c: by_name[self.caps.channel_name(c)] for c in cfg.digital
                 if self.caps.channel_name(c) in by_name}
        trig = 0
        if cfg.mode == "trigger" and cfg.trig_channel in chans:
            bits = ((data >> chans[cfg.trig_channel]) & 1).astype(np.uint8)
            trig = trigger_index(bits, cfg.trig_type)
        self._trig = trig
        self.have_capture = True
        res = LogicCapture(cfg, traces_from_samples(data, self._rate, chans, trig), {},
                           t_app=t_app or app_now(), wall=time.time(),
                           elapsed=time.monotonic() - run["t0"], t_armed=run["t_armed"])
        for line in err.splitlines():
            if "only sent" in line:            # e.g. "Device only sent 996 samples."
                res.errors.append(line.strip())
        if cfg.decoders:
            try:
                res.table_header, res.table_rows = self.decode(cfg.decoders)
            except Exception as e:      # noqa: BLE001 - keep the waveform even if a decoder fails
                res.errors.append(f"Decoding failed: {e}")
        return res

    # ---- on the held capture --------------------------------------------------
    def _pd_arg(self, d: dict) -> tuple[str, str]:
        name = d["name"]
        pd = PD_IDS.get(name, name)
        spec = DECODERS.get(name)
        parts = [pd]
        for key, value in d.get("settings", {}).items():
            kind = next((k for s, _, k in spec if s == key), "str") if spec else "str"
            parts.append(f"{key}={self.caps.channel_name(int(value)) if kind == 'ch' else value}")
        return pd, ":".join(parts)

    def decode(self, decoders: list) -> tuple[list[str], list[list[str]]]:
        if not self.have_capture:
            raise RuntimeError("Capture something first")
        rows = []
        for d in decoders:
            pd, arg = self._pd_arg(d)
            args = ["-i", self._sr, "-P", arg, "--protocol-decoder-samplenum"]
            if pd in PD_ROWS:
                args += ["-A", f"{pd}={PD_ROWS[pd]}"]
            out = self._cli(args, timeout=120)
            rows += parse_annotations(out, d.get("label") or d["name"], pd, self._rate, self._trig)
        rows.sort(key=lambda r: float(r[2]))
        return ["name", "type", "start_time", "duration", "data"], rows

    def save(self, path: str):
        if not self.have_capture:
            raise RuntimeError("Capture something first")
        shutil.copyfile(self._sr, path)

    def export_csv(self, directory: str):
        if not self.have_capture:
            raise RuntimeError("Capture something first")
        self._cli(["-i", self._sr, "-O", "csv:time=true:label=channel",
                   "-o", os.path.join(directory, "digital.csv")],
                  timeout=300)

    def poll(self, t: float, _cfg=None):
        return None

    def close(self):
        try:
            self.abort()
        finally:
            shutil.rmtree(self._dir, ignore_errors=True)

