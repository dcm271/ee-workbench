"""Common data types shared by all instrument drivers."""
from __future__ import annotations

from dataclasses import dataclass, field


def parse_float(s: str) -> float:
    return float(s.strip().split(",")[0])


def parse_bool(s: str) -> bool:
    return s.strip() in ("1", "ON", "+1")


# --------------------------------------------------------------- supplies
@dataclass
class SupplyCaps:
    idn: str = ""
    model: str = ""
    v_max: float = 15.0
    i_min: float = 0.0
    i_max: float = 3.0
    ovp_max: float = 22.0
    has_dvm: bool = False
    has_resistance: bool = False
    r_min: float = -0.04
    r_max: float = 1.0
    has_auto_range: bool = False
    has_meas_setup: bool = False      # digitizer points/interval, range, detector
    has_waveform: bool = False
    has_pulse_stats: bool = False
    setpoint_readback: bool = True    # False: app can only remember what it sent
    reset_label: str = "*RST"
    note: str = ""
    # What the "OVP" setpoint really is on this model, for the label/tooltip.
    ovp_label: str = "OVP"
    ovp_tip: str = "Over-voltage protection trip level"
    # (label, full-scale A) of each current measurement range
    current_ranges: list = field(default_factory=list)
    # 663xx extras (all probed at connect)
    has_trigger: bool = False         # triggered/pre-triggered capture + output transients
    has_ext_trigger: bool = False     # rear-port external measurement trigger
    has_ovp_tracking: bool = False
    has_prot_delay: bool = False
    has_window: bool = False
    has_sense_protect: bool = False
    comp_modes: list = field(default_factory=list)   # e.g. ["LLOC", "HLOC", "LREM", "HREM"]
    has_ri_dfi: bool = False
    has_port: bool = False            # DIG:FUNC / DIG:DATA
    has_display: bool = False
    saved_states: int = 0             # *SAV/*RCL locations (0..n-1)
    has_selftest: bool = False
    info: dict = field(default_factory=dict)        # firmware, options, cal date, ...

    @property
    def has_system(self) -> bool:
        return any((self.has_ovp_tracking, self.has_prot_delay, self.comp_modes,
                    self.has_ri_dfi, self.has_port, self.has_display, self.saved_states))


@dataclass
class Setpoints:
    """Programmed values. None = unknown (instrument can't report it)."""
    voltage: float | None = None
    current: float | None = None
    output: bool | None = None
    ovp: float | None = None
    ocp: bool | None = None
    resistance: float | None = None
    sweep_points: int = 2048
    sweep_tint: float = 15.6e-6
    current_range: str = ""
    detector: str = "ACDC"
    # 663xx extras; None = not supported / unknown
    ovp_tracking: bool | None = None
    prot_delay: float | None = None
    window: str | None = None
    sense_protect: bool | None = None
    comp_mode: str | None = None
    ri_mode: str | None = None
    dfi: bool | None = None
    dfi_source: str | None = None
    port_function: str | None = None
    port_data: int | None = None
    pon_state: str | None = None
    display_on: bool | None = None
    display_mode: str | None = None
    display_text: str | None = None
    volt_trig: float | None = None
    curr_trig: float | None = None
    res_trig: float | None = None


@dataclass
class SupplyPoll:
    pulse_stats: bool = False
    dvm: bool = False
    measure_voltage: bool = True


@dataclass
class SupplyReading:
    t: float
    voltage: float | None
    current: float
    mode: str
    output: bool | None = None
    flags: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dvm: float | None = None
    i_max: float | None = None
    i_min: float | None = None
    i_high: float | None = None
    i_low: float | None = None
    i_rms: float | None = None
    events: list[str] = field(default_factory=list)   # latched trips since last poll

    @property
    def power(self) -> float | None:
        return None if self.voltage is None else self.voltage * self.current


@dataclass
class CaptureSpec:
    """One waveform capture on a 663xx digitizer."""
    what: str = "CURR"            # CURR | VOLT
    points: int = 1024            # per segment
    tint: float = 15.6e-6
    trigger: str = "IMM"          # IMM | LEVEL | EXT | STEP
    level: float = 0.0
    slope: str = "POS"            # POS | NEG | EITH
    hyst: float = 0.0
    pre: int = 0                  # samples before the trigger (0..points-1)
    count: int = 1                # segments (one trigger each); count*points <= 4096
    step: dict = field(default_factory=dict)   # STEP: {"VOLT": v, "CURR": a, "RES": ohm}
    restore_after_step: bool = True


@dataclass
class CaptureResult:
    spec: CaptureSpec
    data: list                    # count*points samples
    stats: dict = field(default_factory=dict)    # instrument dc/rms/max/min/high/low
    restored: dict = field(default_factory=dict)  # levels put back after a STEP
    t_done: float = 0.0


# --------------------------------------------------------------- meters
@dataclass
class DmmCaps:
    idn: str = ""
    model: str = ""
    note: str = ""


@dataclass
class DmmConfig:
    function: str = "DCV"
    range: str = "AUTO"
    nplc: float = 1.0
    autozero: bool = True
    filter: bool = False
    null: bool = False


@dataclass
class DmmReading:
    t: float
    value: float
    unit: str
    overload: bool = False
    errors: list[str] = field(default_factory=list)
    # Meters that only report what their own knob is set to (B&K 393 / 393B):
    function: str = ""
    native: str = ""              # unit as displayed, e.g. "kΩ"; value is in `unit`
    mult: float = 1.0             # value = displayed number * mult
    decimals: int = 0
    text: str = ""                # a word on the display instead of a number
    flags: list[str] = field(default_factory=list)   # AUTO, HOLD, MAX ...
    secondary: str = ""           # the second display line, as text
    cfg: object = None            # OWON XDM: the settings the meter reported

    def display(self) -> str:
        """The reading as the meter shows it."""
        if self.overload:
            return "-OL" if self.value < 0 else "OL"
        if self.text:
            return self.text
        return f"{self.value / self.mult:+.{self.decimals}f} {self.native}".rstrip()
