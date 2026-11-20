"""Driver for the HP 6632A / 6633A / 6634A System DC Power Supplies.

These predate SCPI. Command set and tables from the Operating Manual
(p/n 5957-6360), Section VI "Remote Programming":
  Table 6-1 instruction set, 6-2 ranges, 6-4 status bits, 6-6 error codes.

The instrument has no setpoint queries (no VSET?/ISET?/OUT?), so setpoints
are whatever this driver last sent; they start out unknown.
"""
from __future__ import annotations

from ..transport import TransportTimeout
from .base import SupplyCaps, SupplyPoll, SupplyReading, Setpoints, parse_float

# Table 6-2: (V max, I min, I max, OVP max)
MODELS = {
    "HP6632A": (20.475, 0.02, 5.1188, 22.0),
    "HP6633A": (51.188, 0.008, 2.0475, 55.0),
    "HP6634A": (102.38, 0.004, 1.0238, 110.0),
}

# Table 6-4: STS? / ASTS? / FAULT? bits
ST_CV, ST_CCP, ST_UNR, ST_OV, ST_OT = 1, 2, 4, 8, 16
ST_OC, ST_ERR, ST_INH, ST_CCN, ST_FAST, ST_NORM = 64, 128, 256, 512, 1024, 2048

# Table 6-6
ERRORS = {
    1: "EEPROM save failed", 2: "Second PON after power-on",
    4: "Second DC PON after power-on", 5: "No relay option present",
    8: "Addressed to talk and nothing to say", 10: "Header expected",
    11: "Unrecognized header", 20: "Number expected", 21: "Number syntax",
    22: "Number out of internal range", 30: "Comma expected",
    31: "Terminator expected", 41: "Parameter out of range",
    42: "Voltage programming error", 43: "Current programming error",
    44: "Overvoltage programming error", 45: "Delay programming error",
    46: "Mask programming error", 50: "Multiple CSAVE commands",
    51: "EEPROM checksum", 52: "Calibration mode disabled",
    53: "CAL channel out of range", 54: "CAL full scale out of range",
    55: "CAL offset out of range", 59: "CAL disable jumper in",
}


class HP6632A:
    kind = "supply"
    family = "6632A"

    def __init__(self, transport):
        self.t = transport
        self.caps = SupplyCaps()
        self.sp = Setpoints()
        self.log = None

    def write(self, cmd: str) -> None:
        if self.log:
            self.log(f"> {cmd}")
        self.t.write(cmd)

    def query(self, cmd: str) -> str:
        r = self.t.query(cmd)
        if self.log:
            self.log(f"> {cmd}  < {r}")
        return r

    def errors(self) -> list[str]:
        code = int(parse_float(self.t.query("ERR?")))
        return [f"ERR {code}: {ERRORS.get(code, 'unknown')}"] if code else []

    def identify(self) -> SupplyCaps:
        self.t.clear()
        idn = self.t.query("ID?").strip()
        model = idn.replace(" ", "")
        vmax, imin, imax, ovpmax = MODELS.get(model, MODELS["HP6632A"])
        self.caps = SupplyCaps(
            idn=idn, model=model.removeprefix("HP"), v_max=vmax, i_min=imin, i_max=imax,
            ovp_max=ovpmax, setpoint_readback=False, reset_label="CLR",
            note=f"The {model.removeprefix('HP')} can't report its setpoints. "
                 "Values shown are what this app last sent.")
        self.errors()  # clear anything left over (reading ERR? clears it)
        return self.caps

    def read_setpoints(self) -> Setpoints:
        return Setpoints(**vars(self.sp))

    # ---- programming ---------------------------------------------------
    def set_voltage(self, v: float):
        self.write(f"VSET {v:.4f}")
        self.sp.voltage = v

    def set_current(self, a: float):
        a = max(a, self.caps.i_min)  # supply silently clamps to minimum anyway
        self.write(f"ISET {a:.5f}")
        self.sp.current = a

    def set_output(self, on: bool):
        self.write(f"OUT {1 if on else 0}")
        self.sp.output = on

    def set_ovp(self, v: float):
        self.write(f"OVSET {v:.2f}")
        self.sp.ovp = v

    def set_ocp(self, on: bool):
        self.write(f"OCP {1 if on else 0}")
        self.sp.ocp = on

    def clear_protection(self):
        self.write("RST")

    def reset(self):
        """CLR: power-on defaults (0 V, min current, output ON, OVP max, OCP off)."""
        self.write("CLR")
        self.sp = Setpoints(voltage=0.0, current=self.caps.i_min, output=True,
                            ovp=self.caps.ovp_max, ocp=False)

    # ---- measurement ---------------------------------------------------
    def poll(self, t: float, cfg: SupplyPoll) -> SupplyReading:
        v = parse_float(self.t.query("VOUT?")) if cfg.measure_voltage else None
        i = parse_float(self.t.query("IOUT?"))
        sts = int(parse_float(self.t.query("STS?")))
        r = SupplyReading(t=t, voltage=v, current=i, mode="--", output=self.sp.output)
        flags = {ST_OV: "OV", ST_OC: "OCP", ST_OT: "OT", ST_UNR: "UNREG", ST_INH: "INH"}
        r.flags = [name for bit, name in flags.items() if sts & bit]
        if sts & ST_CCP:
            r.mode = "CC"
        elif sts & ST_CCN:
            r.mode = "CC-"
        elif sts & ST_CV:
            r.mode = "CV"
        if self.sp.output is False:
            r.mode = "OFF"
        if sts & ST_ERR:
            try:
                r.errors = self.errors()
            except TransportTimeout:
                pass
        return r
