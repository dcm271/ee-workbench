"""Function / arbitrary waveform generators over VISA (LAN, USB-TMC or GPIB).

Two command dialects cover most bench generators:

  scpi     Keysight / Agilent 33210A, 33220A, 33500B, 33600A; Rigol DG800, DG900,
           DG1000Z, DG4000; most others that follow SCPI:
             SOUR1:FUNC SIN   SOUR1:FREQ 1000   SOUR1:VOLT 1.0   SOUR1:VOLT:OFFS 0
             SOUR1:FUNC:SQU:DCYC 50   OUTP1 ON
  siglent  Siglent SDG1000X, SDG2000X, SDG6000X, SDG800 (and the Owon/Teledyne
           LeCroy rebadges):  C1:BSWV WVTP,SINE,FRQ,1000,AMP,1,OFST,0,DUTY,50
                              C1:OUTP ON

Amplitude is always peak-to-peak volts into the generator's set load (most
default to 50 Ω: into a high-impedance input the voltage is twice what it
says; set the output load to High Z on the generator if that is your case).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..transport import TransportTimeout

FUNCS = ("sine", "square", "ramp", "pulse", "noise", "dc", "arb")
SCPI_FUNC = {"sine": "SIN", "square": "SQU", "ramp": "RAMP", "pulse": "PULS", "noise": "NOIS",
             "dc": "DC", "arb": "ARB"}
SIGLENT_FUNC = {"sine": "SINE", "square": "SQUARE", "ramp": "RAMP", "pulse": "PULSE",
                "noise": "NOISE", "dc": "DC", "arb": "ARB"}
KNOWN = ("SDG", "DG8", "DG9", "DG1", "DG2", "DG4", "DG5", "33210", "33220", "33250", "33500",
         "33509", "33510", "33511", "33512", "33519", "33520", "33521", "33522", "33600",
         "33611", "33612", "33621", "33622", "AFG")


def looks_like_fgen(idn: str) -> bool:
    u = idn.upper()
    return any(k in u for k in KNOWN)


@dataclass
class FgenChannel:
    function: str = "sine"
    frequency: float = 1000.0
    amplitude: float = 1.0      # Vpp
    offset: float = 0.0
    duty: float = 50.0
    output: bool = False


@dataclass
class FgenState:
    channels: list = field(default_factory=lambda: [FgenChannel()])
    selected: int = 1


@dataclass
class FgenCaps:
    idn: str = ""
    note: str = ""
    dialect: str = "scpi"
    nch: int = 1
    max_freq: float = 20e6
    model: str = "FGEN"


@dataclass
class FgenReading:
    t: float
    errors: list = field(default_factory=list)


class FunctionGenerator:
    kind = "fgen"
    family = "fgen"

    def __init__(self, transport):
        self.t = transport
        self.caps = FgenCaps()
        self.state = FgenState()
        self.ch = 1                  # channel that set() acts on
        self.log = None

    # ---- plumbing ------------------------------------------------------------
    def write(self, cmd: str):
        if self.log:
            self.log(f"> {cmd}")
        self.t.write(cmd)

    def query(self, cmd: str) -> str:
        r = self.t.query(cmd).strip()
        if self.log:
            self.log(f"> {cmd}  < {r}")
        return r

    @property
    def cfg(self) -> FgenState:
        return FgenState([FgenChannel(**vars(c)) for c in self.state.channels], self.ch)

    def errors(self) -> list[str]:
        if self.caps.dialect != "scpi":
            return []
        out = []
        for _ in range(10):
            try:
                r = self.query("SYST:ERR?")
            except TransportTimeout:
                break
            if r.startswith(("0", "+0")):
                break
            out.append(r)
        return out

    # ---- identification ---------------------------------------------------------
    def identify(self) -> FgenCaps:
        self.t.clear()
        idn = self.query("*IDN?")
        parts = [p.strip() for p in idn.split(",")]
        maker = parts[0].upper() if parts else ""
        model = parts[1] if len(parts) > 1 else idn
        dialect = "siglent" if ("SIGLENT" in maker or model.upper().startswith("SDG")) else "scpi"
        nch = 2
        if dialect == "scpi":
            # single-channel generators reject the second channel
            try:
                with self.t.temp_timeout(1.0):
                    self.query("SOUR2:FUNC?")
                errs = self.errors()
                nch = 1 if errs else 2
            except TransportTimeout:
                self.t.clear()
                nch = 1
        self.caps = FgenCaps(idn=idn, dialect=dialect, nch=nch, model=model or "FGEN",
                             note=f"{'Siglent' if dialect == 'siglent' else 'SCPI'} commands, "
                                  f"{nch} channel{'s' if nch > 1 else ''}")
        self.state = FgenState([FgenChannel() for _ in range(nch)])
        for ch in range(1, nch + 1):
            self.read_channel(ch)
        return self.caps

    # ---- reading back -------------------------------------------------------------
    def _p(self, ch: int) -> str:
        return f"SOUR{ch}:"

    def read_channel(self, ch: int) -> FgenChannel:
        c = self.state.channels[ch - 1]
        if self.caps.dialect == "siglent":
            r = self.query(f"C{ch}:BSWV?")
            body = r.split("BSWV", 1)[-1].strip()
            items = [x.strip() for x in body.split(",")]
            kv = dict(zip(items[0::2], items[1::2]))
            back = {v: k for k, v in SIGLENT_FUNC.items()}
            c.function = back.get(kv.get("WVTP", "SINE").upper(), "sine")
            c.frequency = _num(kv.get("FRQ"), c.frequency)
            c.amplitude = _num(kv.get("AMP"), c.amplitude)
            c.offset = _num(kv.get("OFST"), c.offset)
            c.duty = _num(kv.get("DUTY"), c.duty)
            o = self.query(f"C{ch}:OUTP?")
            c.output = bool(re.search(r"OUTP\s+ON", o.upper()))
        else:
            p = self._p(ch)
            back = {v: k for k, v in SCPI_FUNC.items()}
            f = self.query(p + "FUNC?").strip('"').upper()
            if f.startswith("USER"):        # an arbitrary waveform on the older ones
                f = "ARB"
            c.function = next((v for k, v in back.items() if f.startswith(k)), "sine")
            c.frequency = _num(self.query(p + "FREQ?"), c.frequency)
            c.amplitude = _num(self.query(p + "VOLT?"), c.amplitude)
            c.offset = _num(self.query(p + "VOLT:OFFS?"), c.offset)
            if c.function == "square":
                c.duty = _num(self.query(p + "FUNC:SQU:DCYC?"), c.duty)
            o = self.query(f"OUTP{ch}?").upper()
            c.output = o.startswith(("1", "ON"))
        return c

    def read_all(self):
        for ch in range(1, self.caps.nch + 1):
            self.read_channel(ch)

    # ---- setting ----------------------------------------------------------------------
    def select(self, ch: int):
        if not 1 <= ch <= self.caps.nch:
            raise ValueError(f"channel {ch} doesn't exist (this one has {self.caps.nch})")
        self.ch = ch

    def set(self, what: str, value, ch: int | None = None):
        ch = ch or self.ch
        self.select(ch)
        c = self.state.channels[ch - 1]
        what = what.lower()
        if what == "function":
            f = str(value).lower()
            if f not in FUNCS:
                raise ValueError(f"function must be one of {', '.join(FUNCS)}")
            if self.caps.dialect == "siglent":
                self.write(f"C{ch}:BSWV WVTP,{SIGLENT_FUNC[f]}")
            else:
                word = "USER" if f == "arb" and self.arb_style() != "keysight" else SCPI_FUNC[f]
                self.write(f"{self._p(ch)}FUNC {word}")
            c.function = f
        elif what in ("frequency", "amplitude", "offset", "duty"):
            v = float(value)
            if what == "frequency" and v <= 0:
                raise ValueError("frequency must be above 0")
            if what == "amplitude" and v <= 0:
                raise ValueError("amplitude must be above 0")
            if what == "duty" and not 0 < v < 100:
                raise ValueError("duty must be between 0 and 100 %")
            if self.caps.dialect == "siglent":
                key = {"frequency": "FRQ", "amplitude": "AMP", "offset": "OFST",
                       "duty": "DUTY"}[what]
                self.write(f"C{ch}:BSWV {key},{v:.10g}")
            else:
                cmd = {"frequency": "FREQ", "amplitude": "VOLT", "offset": "VOLT:OFFS",
                       "duty": "FUNC:SQU:DCYC"}[what]
                self.write(f"{self._p(ch)}{cmd} {v:.10g}")
            setattr(c, what, v)
        elif what == "output":
            on = bool(value)
            if self.caps.dialect == "siglent":
                self.write(f"C{ch}:OUTP {'ON' if on else 'OFF'}")
            else:
                self.write(f"OUTP{ch} {'ON' if on else 'OFF'}")
            c.output = on
        else:
            raise ValueError(f"can't set '{what}'")

    # ---- arbitrary waveforms -------------------------------------------------------
    def arb_style(self) -> str:
        """How this one takes a waveform: siglent | rigol | keysight | user."""
        if self.caps.dialect == "siglent":
            return "siglent"
        u = self.caps.idn.upper()
        if "RIGOL" in u:
            return "rigol"
        # 33500B / 33600A and their relatives; the 33210A … 33250A are older
        return "keysight" if re.search(r"\b(335|336|EDU33)", u) else "user"

    def upload(self, wave, ch: int | None = None):
        """Load an arb.Wave and select it, with its amplitude, offset and
        period. Refused while the output is on: the circuit would get whatever
        comes in between. (Untried on hardware.)"""
        ch = ch or self.ch
        self.select(ch)
        if self.read_channel(ch).output:
            raise ValueError("switch the output off before loading a waveform")
        style, p, freq = self.arb_style(), self._p(ch), 1.0 / wave.seconds
        unit = wave.unit()
        text = ",".join(f"{x:.5f}" for x in unit)
        with self.t.temp_timeout(30.0):
            if style == "siglent":
                import struct
                codes = [int(round(x * 32767)) for x in unit]
                head = (f"C{ch}:WVDT WVNM,otter{ch},TYPE,8,FREQ,{freq:.10g},"
                        f"AMPL,{wave.amplitude:.6g},OFST,{wave.offset:.6g},PHASE,0.0,WAVEDATA,")
                if self.log:
                    self.log(f"> {head}<{2 * len(codes)} bytes>")
                self.t.write_bytes(head.encode() + struct.pack(f"<{len(codes)}h", *codes))
                self.write(f"C{ch}:ARWV NAME,otter{ch}")
                self.write(f"C{ch}:BSWV WVTP,ARB")
                self.write(f"C{ch}:BSWV FRQ,{freq:.10g},AMP,{wave.amplitude:.6g},"
                           f"OFST,{wave.offset:.6g}")
            else:
                if style == "keysight":
                    self.write(f"{p}DATA:VOL:CLE")
                    self.write(f"{p}DATA:ARB otter,{text}")
                    self.write(f"{p}FUNC:ARB otter")
                    self.write(f"{p}FUNC:ARB:SRAT {unit.size * freq:.10g}")
                    self.write(f"{p}FUNC ARB")
                else:
                    if style == "rigol":
                        self.write(f"{p}DATA VOLATILE,{text}")
                    else:
                        self.write(f"DATA VOLATILE,{text}")
                        self.write(f"{p}FUNC:USER VOLATILE")
                    self.write(f"{p}FUNC USER")
                    self.write(f"{p}FREQ {freq:.10g}")
                self.write(f"{p}VOLT {wave.amplitude:.6g}")
                self.write(f"{p}VOLT:OFFS {wave.offset:.6g}")
            try:                            # wait until it has digested the points
                self.query("*OPC?")
            except TransportTimeout:
                self.t.clear()
        errs = self.errors()
        self.read_channel(ch)
        if errs:
            raise RuntimeError("the generator didn't take the waveform: " + "; ".join(errs))

    def set_output(self, on: bool):
        """All outputs off (Esc) or on: every channel."""
        for ch in range(1, self.caps.nch + 1):
            self.set("output", on, ch)

    def poll(self, t: float, _cfg=None) -> FgenReading:
        return FgenReading(t)

    def close(self):
        pass


def _num(s, default: float) -> float:
    if s is None:
        return default
    m = re.match(r"\s*([-+]?[\d.]+(?:[eE][-+]?\d+)?)", str(s))
    return float(m.group(1)) if m else default
