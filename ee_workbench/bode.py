"""Frequency response with a function generator and an oscilloscope.

At each frequency: set the generator, set the scope's timebase to show a few
cycles, take one acquisition of the input and output channels, fit a sine to
each (least squares, so noise and harmonics matter little), and divide. The
vertical scale follows the output, so a filter can be followed down its slope.

Knows nothing of Qt: it works through a sequencer.Bench, so Stop works at
every step and the scope's own tab keeps working.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from .sequencer import Bench, SequenceError, Unstopped

DIVS = 10.0                     # every scope here shows at least this many across
VDIVS = 8.0
VSTEPS = [m * 10.0 ** e for e in range(-3, 2) for m in (1, 2, 5)]          # 1 mV … 50 V/div


@dataclass
class BodeSettings:
    fgen: str = ""
    fgen_ch: int = 1
    scope: str = ""
    ch_in: int = 1
    ch_out: int = 2
    f_start: float = 100.0
    f_stop: float = 100e3
    per_decade: int = 10
    amplitude: float = 1.0           # Vpp
    offset: float = 0.0
    settle: float = 0.1              # s after each change (and at least 5 periods)
    cycles: float = 4.0              # shown on screen
    average: int = 1                 # acquisitions per point
    autoscale: bool = True
    off_at_end: bool = True

    def problem(self) -> str:
        if not (self.f_start > 0 and self.f_stop > self.f_start):
            return "The stop frequency must be above the start frequency, both above 0"
        if not 1 <= self.per_decade <= 200:
            return "Points per decade must be 1 to 200"
        if self.ch_in == self.ch_out:
            return "Input and output must be different scope channels"
        if self.amplitude <= 0:
            return "The amplitude must be above 0"
        if not 1 <= self.cycles <= 50:
            return "Show 1 to 50 cycles"
        if not self.fgen:
            return "Choose a function generator"
        if not self.scope:
            return "Choose an oscilloscope"
        return ""

    def frequencies(self) -> np.ndarray:
        n = max(2, int(round(math.log10(self.f_stop / self.f_start) * self.per_decade)) + 1)
        return np.logspace(math.log10(self.f_start), math.log10(self.f_stop), n)


@dataclass
class BodePoint:
    f: float
    gain_db: float
    phase: float                 # degrees, output relative to input, -180 … 180
    vin: float                   # V peak of the fitted sine
    vout: float
    fit: float                   # worst residual / amplitude (0 = a pure sine)
    note: str = ""


@dataclass
class SineFit:
    amplitude: float             # peak
    phase: float                 # rad, of cos(wt + phase)
    offset: float
    residual: float              # rms


def sine_fit(t: np.ndarray, v: np.ndarray, f: float) -> SineFit:
    """Least-squares a·cos(wt) + b·sin(wt) + c over the whole trace."""
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    if t.size < 8:
        raise ValueError("too few samples")
    w = 2 * math.pi * f
    m = np.column_stack([np.cos(w * t), np.sin(w * t), np.ones_like(t)])
    (a, b, c), *_ = np.linalg.lstsq(m, v, rcond=None)
    res = v - m @ np.array([a, b, c])
    return SineFit(math.hypot(a, b), math.atan2(-b, a), float(c),
                   float(np.sqrt(np.mean(res ** 2))))


def step125(x: float) -> float:
    """The next 1-2-5 step at or above x."""
    e = math.floor(math.log10(x))
    for m in (1, 2, 5, 10):
        s = m * 10.0 ** e
        if s >= x * (1 - 1e-9):
            return s
    return 10.0 ** (e + 1)


def wrap(deg: float) -> float:
    d = (deg + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d


def point_from(t_in, v_in, t_out, v_out, f: float) -> BodePoint:
    a, b = sine_fit(t_in, v_in, f), sine_fit(t_out, v_out, f)
    if a.amplitude <= 0:
        raise ValueError("no signal on the input channel")
    gain = 20 * math.log10(max(b.amplitude, 1e-12) / a.amplitude)
    fit = max(a.residual / a.amplitude, b.residual / max(b.amplitude, 1e-12))
    return BodePoint(f, gain, wrap(math.degrees(b.phase - a.phase)), a.amplitude, b.amplitude,
                     fit)


# ---------------------------------------------------------------- results
def arrays(points: list[BodePoint]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(f, gain dB, unwrapped phase °), points without a result left out."""
    p = [x for x in points if math.isfinite(x.gain_db) and math.isfinite(x.phase)]
    f = np.array([x.f for x in p])
    g = np.array([x.gain_db for x in p])
    ph = np.degrees(np.unwrap(np.radians([x.phase for x in p]))) if p else np.array([])
    return f, g, ph


def _cross(f, y, level: float, down: bool = True) -> float | None:
    """Where y first passes `level` (log-interpolated in f)."""
    for k in range(1, len(f)):
        a, b = y[k - 1] - level, y[k] - level
        if (a >= 0 > b) if down else (a < 0 <= b):
            u = a / (a - b)
            return float(10 ** (math.log10(f[k - 1]) + u * math.log10(f[k] / f[k - 1])))
    return None


def analyse(points: list[BodePoint]) -> dict:
    """Peak gain, -3 dB points (relative to the peak), unity-gain crossing and
    the phase and gain margins of a loop gain measured this way."""
    f, g, ph = arrays(points)
    if f.size < 2:
        return {}
    k = int(np.argmax(g))
    out = {"peak_db": float(g[k]), "peak_f": float(f[k])}
    hi = _cross(f[k:], g[k:], g[k] - 3.0103)
    lo = _cross(f[:k + 1], g[:k + 1], g[k] - 3.0103, down=False)
    if hi:
        out["f_high"] = hi
    if lo:
        out["f_low"] = lo
    if hi and lo:
        out["bandwidth"] = hi - lo
    fc = _cross(f, g, 0.0)
    if fc:
        p = float(np.interp(math.log10(fc), np.log10(f), ph))
        out["f_unity"] = fc
        out["phase_at_unity"] = wrap(p)
        # a loop gain measured around an inverting error amplifier has its 180°
        # in it already: the margin is then the phase itself
        out["phase_margin"] = wrap(p + 180)
    f180 = _cross(f, ph, -180.0)
    if f180:
        out["f_180"] = f180
        out["gain_margin"] = -float(np.interp(math.log10(f180), np.log10(f), g))
    return out


def describe(a: dict) -> str:
    if not a:
        return ""
    def hz(x):
        for s, u in ((1e6, "MHz"), (1e3, "kHz")):
            if x >= s:
                return f"{x / s:.4g} {u}"
        return f"{x:.4g} Hz"
    out = [f"Peak {a['peak_db']:.2f} dB at {hz(a['peak_f'])}"]
    if "f_low" in a:
        out.append(f"−3 dB low {hz(a['f_low'])}")
    if "f_high" in a:
        out.append(f"−3 dB high {hz(a['f_high'])}")
    if "f_unity" in a:
        out.append(f"0 dB at {hz(a['f_unity'])}, phase {a['phase_at_unity']:.1f}°, phase margin "
                   f"{a['phase_margin']:.1f}°")
    if "gain_margin" in a:
        out.append(f"−180° at {hz(a['f_180'])}, gain margin {a['gain_margin']:.1f} dB")
    return "   ·   ".join(out)


def normalise(points: list[BodePoint], ref: list[BodePoint]) -> list[BodePoint]:
    """Divide by a reference sweep (probes and cables, DUT bridged), interpolated
    in log f; points outside the reference's range are dropped."""
    f, g, ph = arrays(ref)
    if f.size < 2:
        return list(points)
    lf = np.log10(f)
    out = []
    for p in points:
        if not f[0] * 0.999 <= p.f <= f[-1] * 1.001:
            continue
        x = math.log10(p.f)
        out.append(BodePoint(p.f, p.gain_db - float(np.interp(x, lf, g)),
                             wrap(p.phase - float(np.interp(x, lf, ph))), p.vin, p.vout, p.fit,
                             p.note))
    return out


# ---------------------------------------------------------------- the sweep
@dataclass
class _Scale:
    scale: float
    offset: float = 0.0


@dataclass
class Sweep:
    s: BodeSettings
    points: list = field(default_factory=list)

    def run(self, bench: Bench, on_point=None):
        s = self.s
        if s.problem():
            raise SequenceError(s.problem())
        fgen, scope = bench.inst(s.fgen), bench.inst(s.scope)
        if fgen.kind != "fgen":
            raise SequenceError(f"{fgen.name} isn't a function generator")
        if scope.kind != "scope":
            raise SequenceError(f"{scope.name} isn't an oscilloscope")

        def fset(what, value):
            bench.check()
            fgen.run(lambda d: d.set(what, value, s.fgen_ch))

        def prepare(d):
            if max(s.ch_in, s.ch_out) > d.state.nch:
                raise ValueError(f"this scope has {d.state.nch} channels")
            for ch in (s.ch_in, s.ch_out):
                d.set_channel(ch, on=True)
            d.set_trigger(source=s.ch_in, level=s.offset, slope="rising", sweep="auto")
            d.run()
            st = d.state
            return {ch: _Scale(st.channels[ch - 1].scale, st.channels[ch - 1].offset)
                    for ch in (s.ch_in, s.ch_out)}

        try:
            fset("function", "sine")
            fset("amplitude", s.amplitude)
            fset("offset", s.offset)
            freqs = s.frequencies()
            fset("frequency", float(freqs[0]))
            scales = scope.run(prepare)
            fset("output", True)
            if s.autoscale:
                # generous to start with; each point then refines it
                for ch in (s.ch_in, s.ch_out):
                    self._vertical(scope, scales, ch, step125(s.amplitude / 5), 0.0)
            tdiv = step125(s.cycles / float(freqs[0]) / DIVS)
            scope.run(lambda d: d.set_timebase(scale=tdiv, pos=0.0))
            bench.wait(max(s.settle, 0.5, 14 * tdiv * 2))
            # trigger in the middle of what is really there: the DC may differ
            # from the generator's setting (coupling, 50 ohm / High Z)

            def level(d):
                v = d.grab([s.ch_in]).analog[s.ch_in].v
                d.set_trigger(level=float(np.max(v) + np.min(v)) / 2, sweep="normal")
            scope.run(level, timeout=60.0)
            for f in freqs:
                f = float(f)
                bench.check()
                fset("frequency", f)
                want = step125(s.cycles / f / DIVS)
                if want != tdiv:
                    tdiv = want
                    scope.run(lambda d: d.set_timebase(scale=tdiv, pos=0.0))
                bench.wait(max(s.settle, 5 / f))
                try:
                    p = self._point(bench, scope, scales, f, tdiv)
                except ValueError as e:
                    p = BodePoint(f, math.nan, math.nan, math.nan, math.nan, math.nan, str(e))
                self.points.append(p)
                bench.log(f"{f:.6g} Hz: {p.gain_db:.3f} dB, {p.phase:.2f}°"
                          + (f"  ({p.note})" if p.note else ""))
                if on_point:
                    on_point(p)
        finally:
            if s.off_at_end:
                try:
                    with Unstopped(bench):
                        fgen.run(lambda d: d.set("output", False, s.fgen_ch))
                except Exception as e:      # noqa: BLE001 - e.g. the generator was unplugged
                    bench.log(f"{fgen.name}: output off failed: {e}")
        return self.points

    @staticmethod
    def _vertical(scope, scales, ch, scale, centre):
        """Show `centre` volts mid-screen. Which way an offset moves the trace
        differs: Rigol and Siglent add it, Keysight and Tek name the centre."""
        sign = {"rigol": -1.0, "siglent": -1.0}

        def job(d):
            off = centre * sign.get(d.dialect, 1.0)
            d.set_channel(ch, scale=scale, offset=off)
        scope.run(job)
        scales[ch] = _Scale(scale, centre)

    def _acquire(self, bench, scope, f, tdiv):
        s = self.s
        scope.run(lambda d: d.arm())
        limit = time.monotonic() + max(3.0, 4 * tdiv * 14 + 5 / f)
        while not scope.run(lambda d: d.capture_done() and d.status() == "stop"):
            if time.monotonic() > limit:
                scope.run(lambda d: d.abort())
                raise SequenceError(f"The scope didn't trigger at {f:.6g} Hz: is the generator "
                                    f"connected to CH{s.ch_in}?")
            bench.wait(0.02)

        def take(d):
            try:
                cap = d.grab([s.ch_in, s.ch_out])
                return cap.analog[s.ch_in], cap.analog[s.ch_out]
            finally:
                d._armed = None
        return scope.run(take, timeout=60.0)

    def _point(self, bench, scope, scales, f, tdiv) -> BodePoint:
        s = self.s
        for _ in range(9):
            tr = self._acquire(bench, scope, f, tdiv)
            again = False
            if s.autoscale:
                for ch, x in zip((s.ch_in, s.ch_out), tr):
                    hi, lo = float(np.max(x.v)), float(np.min(x.v))
                    k = scales[ch]
                    full = k.scale * VDIVS
                    clipped = hi > k.offset + 0.47 * full or lo < k.offset - 0.47 * full
                    small = (hi - lo) < 0.3 * full
                    if clipped:
                        want = min(VSTEPS[-1], step125(k.scale * 2.01))
                        centre = (hi + lo) / 2
                    elif small:
                        want = max(VSTEPS[0], step125(max((hi - lo) / 6.0, 1e-4)))
                        centre = (hi + lo) / 2
                    else:
                        continue
                    if abs(want - k.scale) > 1e-9 * k.scale or \
                            clipped and abs(centre - k.offset) > 0.05 * full:
                        self._vertical(scope, scales, ch, want, centre)
                        again = True
            if not again:
                break
            bench.wait(max(s.settle, 5 / f))
        else:
            # a flat line stuck to the edge fits nothing: say so, invent nothing
            raise ValueError("couldn't get the signal on screen")
        pts = [point_from(tr[0].t, tr[0].v, tr[1].t, tr[1].v, f)]
        for _ in range(max(1, s.average) - 1):
            tr = self._acquire(bench, scope, f, tdiv)
            pts.append(point_from(tr[0].t, tr[0].v, tr[1].t, tr[1].v, f))
        p = pts[0]
        if len(pts) > 1:
            h = np.mean([10 ** (q.gain_db / 20) * np.exp(1j * math.radians(q.phase))
                         for q in pts])
            p = BodePoint(f, 20 * math.log10(abs(h)), math.degrees(np.angle(h)),
                          float(np.mean([q.vin for q in pts])),
                          float(np.mean([q.vout for q in pts])),
                          float(np.mean([q.fit for q in pts])))
        if p.fit > 0.3:
            p.note = "distorted or noisy: not much of a sine"
        return p
