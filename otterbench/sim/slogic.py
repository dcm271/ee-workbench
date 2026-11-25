"""Simulated Saleae Logic Pro 16 behind a simulated Logic 2 automation server.

It answers the same calls as `instruments.saleae.Logic2Link` and writes real
Logic 2 binary export files, so the driver's parser is exercised too.

Signals (idle channels stay low):
  D0  UART 115200 8N1, "OtterBench\\r\\n" every 2 ms (idle high)
  D1  1 kHz square, 25 % duty
  D2  10 kHz square, 50 % duty
  D3  bursts of 8 clocks at 1 MHz, every 50 µs
  D7  the shared debug pin (sim.shared), also on the simulated PPK2's D7
  A0  1 kHz sine, 0..3.3 V, plus noise
  A1  D1 seen through a 20 µs RC, 0..3.3 V
  other analog channels: noise around 0 V
"""
from __future__ import annotations

import csv
import os
import struct
import threading
import time
from contextlib import contextmanager

import numpy as np

from ..clock import now as app_now
from ..instruments.saleae import DIGITAL_RATES, MAGIC, LogicConfig
from .shared import SHARED_CH, shared_edges

UART_TEXT = b"OtterBench\r\n"
UART_BAUD = 115200
UART_EVERY = 2e-3
DEVICE_ID = "SIM16"


def _uart_pattern():
    """(offsets, levels) of one UART message, starting idle-high at 0."""
    bit = 1.0 / UART_BAUD
    levels = []
    for byte in UART_TEXT:
        levels += [0] + [(byte >> k) & 1 for k in range(8)] + [1]   # start, LSB first, stop
    t = np.arange(len(levels)) * bit + 10e-6
    return t, np.array(levels, np.uint8)


def _square(period, high):
    return np.array([0.0, high]), np.array([1, 0], np.uint8)


def _burst(period=50e-6, clocks=8, clk=1e-6):
    t = np.arange(2 * clocks) * clk / 2
    return t, (np.arange(2 * clocks) % 2 == 0).astype(np.uint8)


PATTERNS = {   # channel: (period, offsets, levels, level before the first offset)
    0: (UART_EVERY, *_uart_pattern(), 1),
    1: (1e-3, *_square(1e-3, 0.25e-3), 0),
    2: (1e-4, *_square(1e-4, 0.5e-4), 0),
    3: (50e-6, *_burst(), 0),
}


def channel_edges(ch: int, a: float, b: float, rate: float, app0: float | None = None):
    """Initial level at a and the transitions in (a, b], on the sample grid.
    With app0 (the app time of capture time 0), D7 carries the shared debug pin."""
    if ch == SHARED_CH and app0 is not None:
        init, e = shared_edges(app0 + a, app0 + b)
        e = np.round((e - app0) * rate) / rate
        return init, e[(e > a) & (e <= b)]
    if ch not in PATTERNS:
        return 0, np.empty(0)
    period, off, lv, idle = PATTERNS[ch]
    k0, k1 = int(np.floor(a / period)) - 1, int(np.ceil(b / period)) + 1
    starts = np.arange(k0, k1 + 1) * period
    t = (starts[:, None] + off[None, :]).ravel()
    lvl = np.tile(lv, starts.size)
    t =np.round(t * rate) / rate
    before = t <= a
    init = int(lvl[before][-1]) if before.any() else idle
    keep = (t > a) & (t <= b)
    t, lvl = t[keep], lvl[keep]
    # drop non-changes
    prev = np.concatenate(([init], lvl[:-1]))
    return init, t[lvl != prev]


def uart_frames(a: float, b: float):
    """(start time, byte) of every UART byte that starts and ends in [a, b]."""
    bit = 1.0 / UART_BAUD
    out = []
    k0, k1 = int(np.floor(a / UART_EVERY)), int(np.ceil(b / UART_EVERY))
    for k in range(k0, k1 + 1):
        for i, byte in enumerate(UART_TEXT):
            t = k * UART_EVERY + 10e-6 + i * 10 * bit
            if t >= a and t + 10 * bit <= b:
                out.append((t, byte))
    return out


def write_digital(path, init, begin, end, transitions, rate, version=1):
    with open(path, "wb") as fh:
        fh.write(MAGIC + struct.pack("<ii", version, 0))
        if version == 1:
            fh.write(struct.pack("<Q", 1))
            fh.write(struct.pack("<Id", init, rate))
        else:
            fh.write(struct.pack("<I", init))
        fh.write(struct.pack("<ddQ", begin, end, transitions.size))
        fh.write(np.asarray(transitions, "<f8").tobytes())


def write_analog(path, begin, rate, down, samples, version=1):
    samples = np.asarray(samples, "<f4")
    with open(path, "wb") as fh:
        fh.write(MAGIC + struct.pack("<ii", version, 1))
        if version == 1:
            fh.write(struct.pack("<Q", 1))
            fh.write(struct.pack("<dddqQ", begin, begin, float(rate), down, samples.size))
        else:
            fh.write(struct.pack("<dqqQ", begin, int(rate), down, samples.size))
        fh.write(samples.tobytes())


class SimLogic2:
    def __init__(self, seed: int = 1, version: int = 1, speed: float = 1.0):
        self.rng = np.random.default_rng(seed)
        self.version = version        # binary export format to write
        self.speed = speed            # >1: captures finish faster than real time (tests)
        self.timeout_s = 3.0
        self.cap = None               # {"cfg", "a", "b", "closed": Event, "stopped"}
        self._analyzers = {}
        self._next = 1
        self.calls: list[str] = []

    @contextmanager
    def temp_timeout(self, seconds):
        old, self.timeout_s = self.timeout_s, seconds
        try:
            yield
        finally:
            self.timeout_s = old

    def app_info(self):
        return {"app_version": "2.4.22 (simulated)", "api": "1.0.0"}

    def devices(self, simulation=False):
        return [{"id": DEVICE_ID, "type": "LOGIC_PRO_16", "sim": False}]

    # ---- capture ---------------------------------------------------------------
    def start(self, device_id: str, cfg: LogicConfig, buffer_mb=None):
        self.calls.append("start")
        if device_id != DEVICE_ID:
            raise RuntimeError(f"MissingDeviceError: device {device_id} not found")
        if self.cap is not None:
            raise RuntimeError("InvalidRequestError: a capture is already open")
        if cfg.digital and cfg.digital_rate not in DIGITAL_RATES["Pro"]:
            raise RuntimeError(f"InvalidRequestError: invalid digital sample rate "
                               f"{cfg.digital_rate:g}")
        if cfg.mode == "timed":
            a, b, run_s = 0.0, cfg.duration, cfg.duration
        elif cfg.mode == "trigger":
            # first matching edge a little after the start
            ch = cfg.trig_channel
            start = 0.0123
            init, edges = channel_edges(ch, start, start + 1.0, cfg.digital_rate)
            lv = (np.arange(edges.size) + init + 1) % 2
            want = 0 if cfg.trig_type in ("falling", "pulse_low") else 1
            hits = edges[lv == want]
            if not hits.size:
                raise RuntimeError("never triggers")   # wait() would hang in Logic 2
            t0 = hits[0]
            a, b, run_s = t0 - cfg.pre_trigger, t0 + cfg.after_trigger, cfg.after_trigger + 0.0123
            self._shift = t0                 # times are relative to the trigger
        else:
            a, b, run_s = 0.0, None, None
        if cfg.mode != "trigger":
            self._shift = 0.0
        self._app0 = app_now()
        self.cap = {"cfg": cfg, "a": a, "b": b, "run_s": run_s, "t0": time.monotonic(),
                    "closed": threading.Event(), "done": False}

    def wait(self):
        cap = self.cap
        if cap["cfg"].mode == "manual":
            raise RuntimeError("wait() is only for timed and trigger captures")
        if cap["closed"].wait(cap["run_s"] / self.speed):
            raise RuntimeError("capture closed")
        cap["done"] = True

    def stop(self):
        cap = self.cap
        cap["b"] = (time.monotonic() - cap["t0"]) * self.speed
        cap["done"] = True

    def close_capture(self):
        self.calls.append("close")
        if self.cap is not None:
            self.cap["closed"].set()
        self.cap = None
        self._analyzers.clear()

    def _need(self):
        if self.cap is None or not self.cap["done"]:
            raise RuntimeError("InvalidRequestError: no finished capture")
        return self.cap

    # ---- exports ---------------------------------------------------------------
    def export_binary(self, directory, digital, analog, downsample):
        cap = self._need()
        cfg, sh = cap["cfg"], self._shift
        a, b = cap["a"], cap["b"]
        for ch in digital:
            init, edges = channel_edges(ch, a, b, cfg.digital_rate, self._app0)
            write_digital(os.path.join(directory, f"digital_{ch}.bin"), init, a - sh, b - sh,
                          edges - sh, cfg.digital_rate, self.version)
        for ch in analog:
            rate = cfg.analog_rate / downsample
            n = int((b - a) * rate)
            t = a + np.arange(n) / rate
            v = self.rng.normal(0, 0.01, n)
            if ch == 0:
                v += 1.65 + 1.65 * np.sin(2 * np.pi * 1e3 * t)
            elif ch == 1:
                ph = np.mod(t, 1e-3)
                rise = np.minimum(ph, 0.25e-3)
                fall = np.clip(ph - 0.25e-3, 0, None)
                hi = 3.3 * (1 - np.exp(-rise / 20e-6))
                v += np.where(ph < 0.25e-3, hi, 3.3 * (1 - np.exp(-0.25e-3 / 20e-6))
                              * np.exp(-fall / 20e-6))
            write_analog(os.path.join(directory, f"analog_{ch}.bin"), a - sh, cfg.analog_rate,
                         downsample, v, self.version)

    def export_csv(self, directory, digital, analog):
        cap = self._need()
        cfg, sh = cap["cfg"], self._shift
        with open(os.path.join(directory, "digital.csv"), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Time [s]"] + [f"Channel {c}" for c in digital])
            chans = [channel_edges(c, cap["a"], cap["b"], cfg.digital_rate, self._app0)
                     for c in digital]
            times = np.unique(np.concatenate([[cap["a"]]] + [e for _, e in chans]))
            cols = []
            for init, e in chans:
                cols.append((init + np.searchsorted(e, times, side="right")) % 2)
            for i, t in enumerate(times):
                w.writerow([f"{t - sh:.9f}"] + [int(c[i]) for c in cols])

    def add_analyzer(self, name, label, settings):
        self._need()
        if name not in ("Async Serial", "I2C", "SPI"):
            raise RuntimeError(f"InvalidRequestError: unknown analyzer '{name}'")
        h = self._next
        self._next += 1
        self._analyzers[h] = (name, label, dict(settings))
        return h

    def remove_analyzer(self, h):
        self._analyzers.pop(h, None)

    def export_table(self, path, handles):
        cap = self._need()
        rows = []
        for h in handles:
            name, label, s = self._analyzers[h]
            if (name == "Async Serial" and s.get("Input Channel") == 0
                    and s.get("Bit Rate (Bits/s)") == UART_BAUD and 0 in cap["cfg"].digital):
                for t, byte in uart_frames(cap["a"], cap["b"]):
                    rows.append([label, "data", f"{t - self._shift:.9f}",
                                 f"{10 / UART_BAUD:.9f}", f"0x{byte:02X}", ""])
        rows.sort(key=lambda r: float(r[2]))
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["name", "type", "start_time", "duration", "data", "error"])
            w.writerows(rows)

    def save(self, path):
        self._need()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("Simulated Saleae capture: there is nothing for Logic 2 to open.\n")

    def clear(self):
        pass

    def read_stb(self):
        return 0

    def close(self):
        self.close_capture()
