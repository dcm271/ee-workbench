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
    current_range: str = "HIGH"
    detector: str = "ACDC"


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

    @property
    def power(self) -> float | None:
        return None if self.voltage is None else self.voltage * self.current


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
