"""Oscilloscopes over SCPI (LAN, USB-TMC or GPIB): channels, timebase,
trigger, run / stop / single, and the waveforms on screen.

Four command dialects cover most bench scopes:

  rigol     Rigol DS1000Z (DS1054Z, DS1104Z, MSO1000Z), DS2000A, MSO5000, DHO800/900
            :WAV:DATA? bytes, value = (byte - yorigin - yreference) * yincrement
  keysight  Keysight / Agilent InfiniiVision 1000X-6000X (DSOX/MSOX/EDUX)
            :WAV:DATA? bytes, value = (byte - yreference) * yincrement + yorigin
  tek       Tektronix TBS1000C / TBS2000, MDO3, MSO2/4/5/6, DPO2000/3000
            CURVE? signed bytes, value = (code - YOFF) * YMULT + YZERO
  siglent   Siglent SDS1000X-E / SDS2000X-E / SDS1000X (classic command set)
            C1:WF? DAT2 signed bytes, value = code * VDIV / 25 - OFST

A capture is returned as a logic.LogicCapture with analog traces (one per
channel shown), so sessions, the sequencer and reports treat it like a
logic-analyzer capture.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import numpy as np

from ..analysis import measure as analyse
from ..clock import now as app_now
from ..transport import TransportTimeout
from .logic import AnalogTrace, LogicCapture, LogicConfig

DIALECTS = ("rigol", "keysight", "tek", "siglent")
MODELS = {
    "SCOPE:RIGOL": ("Rigol oscilloscope (DS1000Z, DS2000A, MSO5000, DHO800/900)", "rigol"),
    "SCOPE:KEYSIGHT": ("Keysight / Agilent InfiniiVision oscilloscope (1000X–6000X)",
                       "keysight"),
    "SCOPE:TEK": ("Tektronix oscilloscope (TBS1000C/2000, MDO3, MSO2/4/5/6, DPO2000/3000)",
                  "tek"),
    "SCOPE:SIGLENT": ("Siglent oscilloscope (SDS1000X-E, SDS2000X-E, SDS1000X)", "siglent"),
}
SLOPES = ("rising", "falling", "either")
SWEEPS = ("auto", "normal", "single")
MEASUREMENTS = ("vpp", "vmax", "vmin", "vavg", "vrms", "freq", "period", "duty", "rise",
                "fall")
_SIM = {"SCOPE": "rigol", "DS1054Z": "rigol", "DSOX": "keysight", "TBS": "tek", "SDS": "siglent"}
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def dialect_of(model: str) -> str:
    return MODELS.get(model.upper(), ("", "rigol"))[1]


def model_for_dialect(dialect: str) -> str:
    return next(k for k, (_, d) in MODELS.items() if d == dialect)


def sim_dialect(name: str) -> str | None:
    """SIM::SCOPE / SIM::DS1054Z (Rigol), SIM::DSOX, SIM::TBS, SIM::SDS."""
    return _SIM.get(name.upper())


def resolve(name: str) -> str | None:
    """'DS1054Z', 'rigol-scope', 'SCOPE:TEK', 'MSO4', … -> SCOPE:… (or None)."""
    s = name.strip().upper()
    if s in MODELS:
        return s
    if s in ("SCOPE", "SCOPE:"):
        return "SCOPE:RIGOL"
    for pat, d in ((r"^(DS[124]\d|MSO[1578]\d|DHO|RIGOL)", "rigol"),
                   (r"^(DSO-?X|MSO-?X|EDU-?X|KEYSIGHT-?SCOPE|INFINIIVISION)", "keysight"),
                   (r"^(TBS|MDO|MSO[2456]|DPO|TEK)", "tek"),
                   (r"^(SDS|SIGLENT-?SCOPE)", "siglent")):
        if re.match(pat, s):
            return model_for_dialect(d)
    if s.startswith("SCOPE:") and s[6:].lower() in DIALECTS:
        return model_for_dialect(s[6:].lower())
    return None


def detect_idn(idn: str) -> str | None:
    """The scope model key for an *IDN? answer, if it is a scope we know."""
    u = idn.upper()
    if "RIGOL" in u and re.search(r",(DS[124]\d|MSO[1578]\d|DHO\d)", u):
        return "SCOPE:RIGOL"
    if ("KEYSIGHT" in u or "AGILENT" in u) and re.search(r",(DSO-?X|MSO-?X|EDU-?X)", u):
        return "SCOPE:KEYSIGHT"
    if "TEKTRONIX" in u and re.search(r",(TBS|MDO|MSO|DPO)", u):
        return "SCOPE:TEK"
    if "SIGLENT" in u and ",SDS" in u:
        return "SCOPE:SIGLENT"
    return None


def num(text: str) -> float:
    m = _NUM.search(text or "")
    if not m:
        raise ValueError(f"no number in {text!r}")
    return float(m.group(0))


@dataclass
class ScopeChannel:
    on: bool = False
    scale: float = 1.0           # V/div
    offset: float = 0.0          # V


@dataclass
class ScopeState:
    nch: int = 4
    channels: list = field(default_factory=list)   # [ScopeChannel]
    tscale: float = 1e-3         # s/div
    tpos: float = 0.0            # s (trigger position / delay)
    trig_source: int = 1
    trig_level: float = 0.0
    trig_slope: str = "rising"
    sweep: str = "auto"
    status: str = ""             # run | stop | wait | triggered | auto


@dataclass
class ScopeCaps:
    idn: str = ""
    model: str = "Scope"
    note: str = ""
    nch: int = 4
    dialect: str = "rigol"

    @property
    def model_key(self) -> str:
        return model_for_dialect(self.dialect)


@dataclass
class ScopeReading:
    t: float
    status: str = ""
    errors: list = field(default_factory=list)


def trace_stats(tr: AnalogTrace) -> dict:
    """Scope-style measurements of one channel (NaN when not applicable)."""
    m = analyse(tr.t, tr.v, unit="V")
    g = m.get
    out = {"vpp": g("pk-pk"), "vmax": g("max"), "vmin": g("min"), "vavg": g("mean"),
           "vrms": g("rms"), "freq": g("frequency"), "period": g("period"),
           "duty": None if g("duty") is None else 100 * g("duty"), "rise": g("rise"),
           "fall": g("fall")}
    return {k: (float("nan") if v is None else float(v)) for k, v in out.items()}


class ScopeDriver:
    kind = "scope"
    family = "SCOPE"

    def __init__(self, transport, dialect: str = "rigol"):
        if dialect not in DIALECTS:
            raise ValueError(f"unknown scope dialect {dialect!r}")
        self.t = transport
        self.dialect = dialect
        self.log = None
        self.state = ScopeState()
        self.caps = ScopeCaps(dialect=dialect)
        self._armed: dict | None = None

    # ---- plumbing --------------------------------------------------------------
    def _log(self, s):
        if self.log:
            self.log(s)

    def write(self, cmd: str):
        self._log("> " + cmd)
        self.t.write(cmd)

    def query(self, cmd: str) -> str:
        self._log("> " + cmd)
        r = self.t.query(cmd)
        self._log("< " + r)
        return r

    def _raw(self, cmd: str) -> bytes:
        self._log("> " + cmd)
        data = self.t.query_raw(cmd)
        self._log(f"< {len(data)} bytes")
        return data

    @property
    def cfg(self) -> ScopeState:
        import copy
        return copy.deepcopy(self.state)

    def errors(self) -> list[str]:
        if self.dialect == "siglent":
            return []
        q = {"tek": "ALLEV?", "rigol": ":SYST:ERR?", "keysight": ":SYST:ERR?"}[self.dialect]
        out = []
        for _ in range(5):
            try:
                with self.t.temp_timeout(2.0):
                    e = self.query(q)
            except TransportTimeout:
                break
            code = e.split(",", 1)[0].strip().strip('"')
            if self.dialect == "tek":
                code = re.sub(r"^:?ALLEV\s*", "", code)
            try:
                if int(float(code)) == 0:
                    break
            except ValueError:
                break
            out.append(e.strip())
        return out

    # ---- connect -----------------------------------------------------------------
    def identify(self) -> ScopeCaps:
        with self.t.temp_timeout(3.0):
            idn = self.query("*IDN?")
        if self.dialect != "siglent":
            self.write("*CLS")
        if self.dialect == "siglent":
            self.write("CHDR SHORT")
        nch = 4
        for ch in (3, 2):           # a 2-channel scope rejects CH3
            try:
                with self.t.temp_timeout(1.5):
                    self._ch_query(ch, "on")
                break
            except (TransportTimeout, ValueError):
                nch = ch - 1
                self.errors()
        self.state = ScopeState(nch=nch, channels=[ScopeChannel() for _ in range(nch)])
        self.read_state()
        parts = idn.split(",")
        model = parts[1].strip() if len(parts) > 1 else "Scope"
        note = {"siglent": "Classic Siglent command set (SDS1000X-E / SDS2000X-E). Newer HD "
                           "models may need their legacy command option."}.get(self.dialect, "")
        self.caps = ScopeCaps(idn=idn, model=model, note=note, nch=nch, dialect=self.dialect)
        return self.caps

    # ---- per-dialect commands ------------------------------------------------------
    def _cmds(self, ch: int) -> dict:
        d = self.dialect
        if d == "rigol":
            return {"on": (f":CHAN{ch}:DISP {{}}", f":CHAN{ch}:DISP?"),
                    "scale": (f":CHAN{ch}:SCAL {{}}", f":CHAN{ch}:SCAL?"),
                    "offset": (f":CHAN{ch}:OFFS {{}}", f":CHAN{ch}:OFFS?")}
        if d == "keysight":
            return {"on": (f":CHAN{ch}:DISP {{}}", f":CHAN{ch}:DISP?"),
                    "scale": (f":CHAN{ch}:SCAL {{}}", f":CHAN{ch}:SCAL?"),
                    "offset": (f":CHAN{ch}:OFFS {{}}", f":CHAN{ch}:OFFS?")}
        if d == "tek":
            return {"on": (f"SEL:CH{ch} {{}}", f"SEL:CH{ch}?"),
                    "scale": (f"CH{ch}:SCA {{}}", f"CH{ch}:SCA?"),
                    "offset": (f"CH{ch}:OFFS {{}}", f"CH{ch}:OFFS?")}
        return {"on": (f"C{ch}:TRA {{}}", f"C{ch}:TRA?"),
                "scale": (f"C{ch}:VDIV {{}}", f"C{ch}:VDIV?"),
                "offset": (f"C{ch}:OFST {{}}", f"C{ch}:OFST?")}

    def _ch_query(self, ch: int, what: str):
        ans = self.query(self._cmds(ch)[what][1])
        if what == "on":
            a = ans.upper().split()[-1] if ans.strip() else ""
            if a in ("1", "ON"):
                return True
            if a in ("0", "OFF"):
                return False
            raise ValueError(f"channel {ch}: {ans!r}")
        return num(ans.split(" ", 1)[-1] if self.dialect == "siglent" else ans)

    def _bool(self, on: bool) -> str:
        return ("ON" if on else "OFF") if self.dialect in ("siglent", "tek") else \
            ("1" if on else "0")

    def read_state(self):
        st = self.state
        for i, c in enumerate(st.channels, start=1):
            try:
                c.on = self._ch_query(i, "on")
                c.scale = self._ch_query(i, "scale")
                c.offset = self._ch_query(i, "offset")
            except (TransportTimeout, ValueError):
                pass
        d = self.dialect
        q = {"rigol": (":TIM:SCAL?", ":TIM:OFFS?", ":TRIG:EDG:SOUR?", ":TRIG:EDG:LEV?",
                       ":TRIG:EDG:SLOP?", ":TRIG:SWE?"),
             "keysight": (":TIM:SCAL?", ":TIM:POS?", ":TRIG:EDGE:SOUR?", ":TRIG:EDGE:LEV?",
                          ":TRIG:EDGE:SLOP?", ":TRIG:SWE?"),
             "tek": ("HOR:SCA?", "HOR:DEL:TIM?", "TRIG:A:EDGE:SOU?", "TRIG:A:LEV?",
                     "TRIG:A:EDGE:SLO?", "TRIG:A:MOD?"),
             "siglent": ("TDIV?", "TRDL?", "TRSE?", "C1:TRLV?", "C1:TRSL?", "TRMD?")}[d]
        try:
            st.tscale = num(self.query(q[0]).split(" ", 1)[-1])
            st.tpos = num(self.query(q[1]).split(" ", 1)[-1])
            src = self.query(q[2])
            m = re.search(r"(?:CHAN|CH|C)(\d)", src.upper())
            st.trig_source = int(m.group(1)) if m else 1
            lev_q = q[3] if d != "siglent" else f"C{st.trig_source}:TRLV?"
            st.trig_level = num(self.query(lev_q).split(" ", 1)[-1])
            slope_q = q[4] if d != "siglent" else f"C{st.trig_source}:TRSL?"
            s = self.query(slope_q).upper()
            st.trig_slope = ("falling" if ("NEG" in s or "FALL" in s) else
                             "either" if ("EITH" in s or "RFAL" in s or "WINDOW" in s or
                                          "ALT" in s) else "rising")
            sw = self.query(q[5]).upper()
            st.sweep = "single" if "SING" in sw else "normal" if "NORM" in sw else "auto"
        except (TransportTimeout, ValueError):
            pass
        st.status = self.status()

    # ---- settings -------------------------------------------------------------------
    def set_channel(self, ch: int, on: bool | None = None, scale: float | None = None,
                    offset: float | None = None):
        c = self._cmds(ch)
        if on is not None:
            self.write(c["on"][0].format(self._bool(on)))
        if scale is not None:
            self.write(c["scale"][0].format(f"{scale:.6g}"))
        if offset is not None:
            self.write(c["offset"][0].format(f"{offset:.6g}"))
        self.read_state()

    def set_timebase(self, scale: float | None = None, pos: float | None = None):
        d = self.dialect
        if scale is not None:
            self.write({"rigol": ":TIM:SCAL {}", "keysight": ":TIM:SCAL {}",
                        "tek": "HOR:SCA {}", "siglent": "TDIV {}"}[d].format(f"{scale:.6g}"))
        if pos is not None:
            self.write({"rigol": ":TIM:OFFS {}", "keysight": ":TIM:POS {}",
                        "tek": "HOR:DEL:TIM {}", "siglent": "TRDL {}"}[d].format(f"{pos:.6g}"))
        self.read_state()

    def set_trigger(self, source: int | None = None, level: float | None = None,
                    slope: str | None = None, sweep: str | None = None):
        d, st = self.dialect, self.state
        src = source or st.trig_source
        if source is not None:
            self.write({"rigol": f":TRIG:EDG:SOUR CHAN{src}",
                        "keysight": f":TRIG:EDGE:SOUR CHAN{src}",
                        "tek": f"TRIG:A:EDGE:SOU CH{src}",
                        "siglent": f"TRSE EDGE,SR,C{src},HT,OFF"}[d])
        if level is not None:
            self.write({"rigol": f":TRIG:EDG:LEV {level:.6g}",
                        "keysight": f":TRIG:EDGE:LEV {level:.6g}",
                        "tek": f"TRIG:A:LEV:CH{src} {level:.6g}",
                        "siglent": f"C{src}:TRLV {level:.6g}"}[d])
        if slope is not None:
            if slope not in SLOPES:
                raise ValueError(f"slope must be one of {', '.join(SLOPES)}")
            word = {"rigol": {"rising": "POS", "falling": "NEG", "either": "RFAL"},
                    "keysight": {"rising": "POS", "falling": "NEG", "either": "EITH"},
                    "tek": {"rising": "RIS", "falling": "FALL", "either": "EITH"},
                    "siglent": {"rising": "POS", "falling": "NEG", "either": "WINDOW"}}[d][slope]
            self.write({"rigol": f":TRIG:EDG:SLOP {word}", "keysight": f":TRIG:EDGE:SLOP {word}",
                        "tek": f"TRIG:A:EDGE:SLO {word}",
                        "siglent": f"C{src}:TRSL {word}"}[d])
        if sweep is not None:
            if sweep not in SWEEPS:
                raise ValueError(f"sweep must be one of {', '.join(SWEEPS)}")
            if sweep == "single":
                self.single()
            else:
                w = "AUTO" if sweep == "auto" else "NORM"
                self.write({"rigol": f":TRIG:SWE {w}", "keysight": f":TRIG:SWE {w}",
                            "tek": f"TRIG:A:MOD {w}", "siglent": f"TRMD {w}"}[d])
        self.read_state()

    def set(self, name: str, value):
        """By name, for sequence steps and scripts: acquire (run / stop /
        single), timebase (s/div), position (s), trigger_source (1-4),
        trigger_level (V), trigger_slope, sweep, ch<N>_on, ch<N>_scale (V/div),
        ch<N>_offset (V)."""
        n = name.strip().lower()
        sval = str(value).strip().lower()
        if n == "acquire":
            fn = {"run": self.run, "stop": self.stop, "single": self.single}.get(sval)
            if fn is None:
                raise ValueError("acquire must be run, stop or single")
            fn()
            return
        if n == "timebase":
            return self.set_timebase(scale=float(value))
        if n == "position":
            return self.set_timebase(pos=float(value))
        if n == "trigger_source":
            m = re.search(r"(\d)", sval)
            if not m:
                raise ValueError("trigger_source is a channel number, e.g. 1 or CH1")
            return self.set_trigger(source=int(m.group(1)))
        if n == "trigger_level":
            return self.set_trigger(level=float(value))
        if n == "trigger_slope":
            return self.set_trigger(slope=sval)
        if n == "sweep":
            return self.set_trigger(sweep=sval)
        m = re.match(r"^ch(\d)_(on|scale|offset)$", n)
        if m:
            ch = int(m.group(1))
            if not 1 <= ch <= self.state.nch:
                raise ValueError(f"this scope has channels 1-{self.state.nch}")
            if m.group(2) == "on":
                on = sval in ("1", "1.0", "on", "true", "yes")
                return self.set_channel(ch, on=on)
            return self.set_channel(ch, **{m.group(2): float(value)})
        raise ValueError(f"a scope can't set '{name}' (acquire, timebase, position, "
                         "trigger_source, trigger_level, trigger_slope, sweep, chN_on, "
                         "chN_scale, chN_offset)")

    def run(self):
        self.write({"rigol": ":RUN", "keysight": ":RUN", "siglent": "TRMD AUTO",
                    "tek": "ACQ:STOPA RUNST;:ACQ:STATE RUN"}[self.dialect])

    def stop(self):
        self.write({"rigol": ":STOP", "keysight": ":STOP", "siglent": "STOP",
                    "tek": "ACQ:STATE STOP"}[self.dialect])

    def single(self):
        self.write({"rigol": ":SING", "keysight": ":SING", "siglent": "TRMD SINGLE;ARM",
                    "tek": "ACQ:STOPA SEQ;:ACQ:STATE RUN"}[self.dialect])

    def status(self) -> str:
        """run | stop | wait | triggered | auto."""
        d = self.dialect
        try:
            if d == "rigol":
                s = self.query(":TRIG:STAT?").upper()
                return {"TD": "triggered", "WAIT": "wait", "RUN": "run", "AUTO": "auto",
                        "STOP": "stop"}.get(s.strip(), s.lower())
            if d == "keysight":
                cond = int(num(self.query(":OPER:COND?")))
                return "run" if cond & 8 else "stop"
            if d == "tek":
                return "run" if int(num(self.query("ACQ:STATE?"))) else "stop"
            s = self.query("SAST?").upper()
            return ("stop" if "STOP" in s else "triggered" if "TRIG" in s else
                    "wait" if ("READY" in s or "ARM" in s) else "auto" if "AUTO" in s else "run")
        except (TransportTimeout, ValueError):
            return ""

    # ---- waveforms ---------------------------------------------------------------------
    def fetch_channel(self, ch: int) -> AnalogTrace:
        d = self.dialect
        if d in ("rigol", "keysight"):
            self.write(f":WAV:SOUR CHAN{ch}")
            if d == "rigol":
                self.write(":WAV:MODE NORM")
            else:
                self.write(":WAV:POIN:MODE NORM")
                self.write(":WAV:UNS 1")
            self.write(":WAV:FORM BYTE")
            pre = [num(x) for x in self.query(":WAV:PRE?").split(",")]
            if len(pre) < 10:
                raise RuntimeError(f"unexpected :WAV:PRE? answer ({len(pre)} fields)")
            xinc, xor, xref, yinc, yor, yref = pre[4:10]
            codes = np.frombuffer(self._raw(":WAV:DATA?"), np.uint8).astype(float)
            v = ((codes - yor - yref) * yinc if d == "rigol" else (codes - yref) * yinc + yor)
            t0 = (0 - xref) * xinc + xor
            return AnalogTrace(ch, float(t0), float(xinc), v.astype(np.float32))
        if d == "tek":
            self.write(f"DATA:SOU CH{ch}")
            self.write("DATA:ENC RIB")
            self.write("DATA:WID 1")
            try:
                n = int(num(self.query("HOR:RECO?")))
            except (TransportTimeout, ValueError):
                n = 10000
            self.write("DATA:STAR 1")
            self.write(f"DATA:STOP {min(n, 1_000_000)}")
            xinc = num(self.query("WFMO:XIN?"))
            xzero = num(self.query("WFMO:XZE?"))
            ymult = num(self.query("WFMO:YMU?"))
            yoff = num(self.query("WFMO:YOF?"))
            yzero = num(self.query("WFMO:YZE?"))
            codes = np.frombuffer(self._raw("CURV?"), np.int8).astype(float)
            v = (codes - yoff) * ymult + yzero
            return AnalogTrace(ch, float(xzero), float(xinc), v.astype(np.float32))
        vdiv = num(self.query(f"C{ch}:VDIV?").split(" ", 1)[-1])
        ofst = num(self.query(f"C{ch}:OFST?").split(" ", 1)[-1])
        tdiv = num(self.query("TDIV?").split(" ", 1)[-1])
        sara_txt = self.query("SARA?").split(" ", 1)[-1].upper()
        sara = num(sara_txt) * (1e9 if "G" in sara_txt else 1e6 if "M" in sara_txt
                                else 1e3 if "K" in sara_txt else 1)
        trdl = num(self.query("TRDL?").split(" ", 1)[-1])
        codes = np.frombuffer(self._raw(f"C{ch}:WF? DAT2"), np.int8).astype(float)
        v = codes * (vdiv / 25.0) - ofst
        t0 = -tdiv * 7 - trdl
        return AnalogTrace(ch, float(t0), 1.0 / sara, v.astype(np.float32))

    def grab(self, channels: list[int] | None = None, t_app: float = 0.0) -> LogicCapture:
        """The waveforms on screen now (every channel shown, or `channels`)."""
        self.read_state()
        chans = channels or [i for i, c in enumerate(self.state.channels, 1) if c.on]
        if not chans:
            raise RuntimeError("No channel is switched on")
        traces = {ch: self.fetch_channel(ch) for ch in chans}
        t_app = t_app or app_now()
        res = LogicCapture(LogicConfig(digital=[], analog=list(chans)), {}, traces,
                           t_app=t_app, wall=time.time())
        armed = self._armed
        res.t_armed = armed["t_app"] if armed else None
        return res

    # ---- the worker's capture protocol (see worker.request_capture) ------------------
    def arm(self, _spec=None) -> float:
        """Single acquisition; capture_done() says when it triggered."""
        self.single()
        self._armed = {"t_app": app_now(), "t0": time.monotonic()}
        return -1.0

    def fire(self):
        pass

    def capture_done(self) -> bool:
        if self._armed and time.monotonic() - self._armed["t0"] < 0.3:
            return False           # give the scope time to leave "stop"
        return self.status() in ("stop", "triggered") and \
            (self.dialect != "rigol" or self.status() == "stop")

    def fetch(self, t_app: float = 0.0) -> LogicCapture:
        try:
            return self.grab(t_app=t_app)
        finally:
            self._armed = None

    def abort(self):
        self._armed = None
        self.stop()

    def measure(self, what: str) -> float:
        """'vpp1', 'freq2', … from the waveform on screen now."""
        m = re.match(r"^([a-z]+)(\d)$", what.lower())
        if not m or m.group(1) not in MEASUREMENTS:
            raise ValueError(f"measure one of {', '.join(MEASUREMENTS)} followed by the "
                             "channel number, e.g. vpp1 or freq2")
        return trace_stats(self.fetch_channel(int(m.group(2))))[m.group(1)]

    def poll(self, t: float, _cfg=None) -> ScopeReading:
        return ScopeReading(t, self.status())

    def set_output(self, on: bool):
        """Scopes have no outputs; Esc stops nothing here."""

    def close(self):
        pass
