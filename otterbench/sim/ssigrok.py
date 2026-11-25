"""Simulated `sigrok-cli` with an fx2lafw-style 8-channel, 24 MHz analyzer.

It answers the same calls as `instruments.sigrok.SigrokLink` (run / spawn) by
reading the argument list the driver builds, so the argument building, the
.sr reader and the annotation parser are all exercised. The signals are the
Saleae simulator's (UART text on D0, squares on D1/D2, clock bursts on D3,
the shared debug pin on D7),
and its UART decoder really decodes D0 from the samples.
"""
from __future__ import annotations

import io
import os
import signal
import time

import numpy as np

from ..instruments.sigrok import parse_hz, read_sr, write_sr
from ..clock import now as app_now
from .slogic import channel_edges

SPEC = "fx2lafw:conn=1.4"
NAMES = [f"D{k}" for k in range(8)]
RATES = [20e3, 25e3, 50e3, 100e3, 200e3, 250e3, 500e3, 1e6, 2e6, 3e6, 4e6, 6e6, 8e6, 12e6,
         16e6, 24e6]
SCAN = f"The following devices were found:\n{SPEC} - Saleae Logic with 8 channels: " + \
    " ".join(NAMES) + "\n"


def _rate_text(hz):
    for div, unit in ((1e6, "MHz"), (1e3, "kHz")):
        if hz >= div:
            return f"{hz / div:g} {unit}"
    return f"{hz:g} Hz"


SHOW = (f"Driver functions:\n    Logic analyzer\n{SPEC} - Saleae Logic with 8 channels: "
        + " ".join(NAMES) + "\nChannel groups:\n    Logic: channels " + " ".join(NAMES) +
        "\nSupported configuration options:\n    continuous: on, off\n"
        "    limit_samples: 0 (current)\n    conn: 1.4 (current)\n"
        "    samplerate - supported samplerates:\n" +
        "".join(f"      {_rate_text(r)}\n" for r in RATES) +
        "    Supported triggers: 0 1 r f e \n    captureratio: 0 (current)\n")


def samples(start: float, n: int, rate: float, bits: list[int],
            app0: float | None = None) -> np.ndarray:
    t = start + np.arange(n) / rate
    data = np.zeros(n, np.uint8)
    for b in bits:
        init, edges = (channel_edges(b, t[0] - 1 / rate, t[-1] + 1 / rate, rate, app0)
                       if n else (0, []))
        lv = (init + np.searchsorted(edges, t + 0.5 / rate, side="right")) % 2
        data |= (lv.astype(np.uint8) << b)
    return data


def uart_decode(bits: np.ndarray, rate: float, baud: float):
    """(start sample, end sample, byte) for 8N1 frames: enough for the simulator."""
    out, i, n, spb = [], 1, bits.size, rate / baud
    while i < n:
        if bits[i - 1] == 1 and bits[i] == 0:
            end = i + int(round(10 * spb))
            if end > n:
                break
            centers = (i + (np.arange(1, 9) + 0.5) * spb).astype(int)
            byte = int(sum(int(bits[c]) << k for k, c in enumerate(centers)))
            out.append((i, end, byte))
            i = end - int(spb / 2)
        else:
            i += 1
    return out


class FakeProc:
    def __init__(self, sim, args):
        self.sim, self.args = sim, args
        a = dict(zip(args[::2], args[1::2]))
        conf = dict(kv.split("=", 1) for kv in a["--config"].split(":"))
        self.rate = float(conf["samplerate"])
        self.ratio = int(conf.get("captureratio", 0))
        self.chans = [NAMES.index(c) for c in a["--channels"].split(",")]
        self.path = a["-o"]
        self.n = int(a["--samples"]) if "--samples" in a else None
        self.trig = a.get("--triggers")
        self.t0 = time.monotonic()
        self.app0 = app_now()
        self.returncode = None
        self.stderr = io.StringIO("")
        if self.trig:     # it waits a little for the trigger
            self.delay = 0.02
        else:
            self.delay = 0.0

    def _finish(self, n, rc):
        start = 0.0
        if self.trig:
            name, code = self.trig.split("=")
            ch = NAMES.index(name)
            pre = int(self.n * self.ratio / 100)
            # libsigrok's software trigger checks from the first sample: it fires
            # on the first match, with whatever pre-trigger data it has by then.
            look = 0.0123
            init, edges = channel_edges(ch, look, look + 1.0, self.rate)
            lv = (np.arange(edges.size) + init + 1) % 2
            want = {"r": lv == 1, "f": lv == 0, "e": np.ones(lv.size, bool)}.get(code)
            hit = edges[want] if want is not None else np.array([look])
            t_trig = hit[0] if hit.size else look
            start = max(look, t_trig - pre / self.rate)
        data = samples(start, n, self.rate, self.chans, self.app0)
        write_sr(self.path, self.rate, {b: NAMES[b] for b in self.chans}, data)
        self.returncode = rc

    def poll(self):
        if self.returncode is None and self.n is not None:
            if (time.monotonic() - self.t0) * self.sim.speed >= self.n / self.rate + self.delay:
                self._finish(self.n, 0)
        return self.returncode

    def send_signal(self, sig):
        if self.returncode is None and sig == signal.SIGINT:
            elapsed = (time.monotonic() - self.t0) * self.sim.speed
            self._finish(max(16, int(elapsed * self.rate)), 130)

    def kill(self):
        if self.returncode is None:
            self.returncode = -9

    def wait(self, timeout=None):
        end = time.monotonic() + (timeout or 1e9)
        while self.poll() is None and time.monotonic() < end:
            time.sleep(0.002)
        return self.returncode


class SimSigrokCli:
    def __init__(self, speed: float = 1.0):
        self.speed = speed
        self.timeout_s = 20.0
        self.calls: list[list[str]] = []

    def run(self, args, timeout=None):
        self.calls.append(list(args))
        if "--scan" in args:
            spec = args[args.index("-d") + 1]
            return 0, (SCAN if spec.split(":")[0] in ("fx2lafw",) else ""), ""
        if "--show" in args:
            return 0, SHOW, ""
        if "-i" in args and "-P" in args:
            return self._decode(args)
        if "-i" in args and "-O" in args:
            rate, names, data = read_sr(args[args.index("-i") + 1])
            path = args[args.index("-o") + 1]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("time," + ",".join(names[b] for b in sorted(names)) + "\n")
                for i, v in enumerate(data):
                    fh.write(f"{i / rate:.9f}," + ",".join(str((int(v) >> b) & 1)
                                                         for b in sorted(names)) + "\n")
            return 0, "", ""
        return 1, "", "Error: unsupported in the simulator"

    def _decode(self, args):
        path = args[args.index("-i") + 1]
        pd, *opts = args[args.index("-P") + 1].split(":")
        kv = dict(o.split("=", 1) for o in opts)
        if pd not in ("uart", "i2c", "spi"):
            return 1, "", f"srd: Failed to load decoder {pd}\nFailed to load protocol decoder {pd}."
        rate, names, data = read_sr(path)
        by_name = {n: b for b, n in names.items()}
        missing = [v for k, v in kv.items() if k in ("rx", "tx", "scl", "sda", "clk", "mosi",
                                                       "miso", "cs") and v not in by_name]
        if missing:
            return 0, "", f'cli: No channel with name "{missing[0]}" found.'
        if pd != "uart" or "rx" not in kv:
            return 0, "", ""
        bits = ((data >> by_name[kv["rx"]]) & 1).astype(np.uint8)
        lines = [f"{a}-{b} uart-1: {byte:02X}"
                 for a, b, byte in uart_decode(bits, rate, float(kv.get("baudrate", 115200)))]
        return 0, "\n".join(lines) + "\n", ""

    def spawn(self, args):
        self.calls.append(list(args))
        p = FakeProc(self, list(args))
        if not os.path.isdir(os.path.dirname(p.path) or "."):
            raise FileNotFoundError(p.path)
        return p

    def clear(self):
        pass

    def read_stb(self):
        return 0

    def close(self):
        pass


__all__ = ["SimSigrokCli", "parse_hz"]
