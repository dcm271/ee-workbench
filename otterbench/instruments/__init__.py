"""Instrument drivers and model selection."""
from __future__ import annotations

from ..transport import TransportTimeout
from .agilent663xx import Agilent663xx
from .bk393 import BK393, BK393B
from .dps150 import DPS150
from .fgen import FunctionGenerator, looks_like_fgen
from .gpio import GpioDriver
from .hp3456a import HP3456A
from .hp6632a import HP6632A
from .ppk2 import PPK2
from . import scopes
from .saleae import SaleaeLogic
from .scpi_generic import (ScpiDriver, definition_for, definitions, match_idn,
                           resource_definition)
from .sigrok import SigrokLogic
from .uart import UartDriver, parse_resource as _uart_resource
from .xdm import OwonXDM

MODELS = {
    "auto": "Auto-detect (asks *IDN?)",
    "663xx": "Agilent 66309B / 66311B / 66321B / 66321D",
    "6632A": "HP 6632A / 6633A / 6634A",
    "DPS150": "FNIRSI DPS-150 (USB)",
    "3456A": "HP 3456A DMM",
    "BK393": "B&K Precision 393 handheld DMM (USB)",
    "BK393B": "B&K Precision 393B handheld DMM (USB)",
    "XDM": "OWON XDM1041 / XDM1241 / XDM2041 bench DMM (USB)",
    "PPK2": "Nordic Power Profiler Kit II",
    "SALEAE": "Saleae Logic 8 / Pro 8 / Pro 16 (via Logic 2)",
    "SIGROK": "Logic analyzer via sigrok (fx2lafw, DSLogic, …)",
    "FGEN": "Function generator (Siglent SDG, Rigol DG, Keysight 33xxx)",
    "GPIO": "Raspberry Pi GPIO header (or a Linux GPIO chip)",
    "UART": "DUT serial console (UART text: log, values, commands)",
}
BUILTIN_MODELS = dict(MODELS)


def all_models() -> dict[str, str]:
    """Model key -> label: the built-in drivers, the oscilloscopes and every
    generic SCPI definition (built in or in ~/.config/otterbench/instruments)."""
    out = dict(BUILTIN_MODELS)
    for key, (label, _) in scopes.MODELS.items():
        out[key] = label
    for d in sorted(definitions().values(), key=lambda d: (d.type, d.name)):
        out[d.model_key] = f"{d.name}  [{'' if d.protocol else 'SCPI '}{d.type}]"
    return out


def resolve_model(name: str) -> str:
    """A model as typed (command line, config.toml, a setup): '34461A',
    'SCPI:34461A', 'DS1054Z', 'scope:rigol' -> its model key, else unchanged."""
    s = str(name).strip()
    if not s:
        return "auto"
    if s.upper() in SERIAL_ALIASES:
        return SERIAL_ALIASES[s.upper()]
    if s.upper() in SERIAL_DMMS:
        return s.upper()
    if s.upper().startswith("SCPI:"):
        d = definition_for(s)
        return d.model_key if d else s
    key = scopes.resolve(s)
    if key:
        return key
    d = definition_for(s)
    if d is not None:
        return d.model_key
    return s

_CLASSES = {"663xx": Agilent663xx, "6632A": HP6632A, "DPS150": DPS150, "3456A": HP3456A, "BK393": BK393,
            "BK393B": BK393B, "XDM": OwonXDM, "PPK2": PPK2,
            "SALEAE": SaleaeLogic, "SIGROK": SigrokLogic, "FGEN": FunctionGenerator,
            "GPIO": GpioDriver}

SIM_MODELS = {"SIM::66321D": "663xx", "SIM::66309B": "663xx", "SIM::6632A": "6632A", "SIM::3456A": "3456A",
              "SIM::PPK2": "PPK2", "SIM::SALEAE": "SALEAE",
              "SIM::SIGROK": "SIGROK", "SIM::FGEN": "FGEN", "SIM::SDG": "FGEN",
              "SIM::GPIO": "GPIO", "SIM::DPS150": "DPS150",
              "SIM::BK393": "BK393", "SIM::BK393B": "BK393B",
              "SIM::XDM": "XDM", "SIM::XDM1041": "XDM", "SIM::XDM1241": "XDM",
              "SIM::XDM2041": "XDM", "SIM::UART": "UART"}

# Names people type for the built-in drivers that aren't their model key. B&K
# never sold a "393A": it is taken to mean the original 393.
SERIAL_ALIASES = {"393": "BK393", "393A": "BK393", "BK393A": "BK393", "393B": "BK393B",
                  "XDM1041": "XDM", "XDM1141": "XDM", "XDM1241": "XDM", "XDM1000": "XDM",
                  "XDM2041": "XDM"}
SERIAL_DMMS = ("BK393", "BK393B", "XDM")       # "<key>::/dev/ttyUSB0" resources


def detect(transport) -> str:
    """Identify a power supply. The 3456A has no ID query and must be chosen
    explicitly (probing it with SCPI just sets its syntax-error bit)."""
    transport.clear()
    try:
        with transport.temp_timeout(1.5):
            idn = transport.query("*IDN?")
        if "663" in idn:
            return "663xx"
        if looks_like_fgen(idn):
            return "FGEN"
        scope = scopes.detect_idn(idn)
        if scope:
            return scope
        d = match_idn(idn)
        if d is not None:
            return d.model_key
    except TransportTimeout:
        transport.clear()
    try:
        with transport.temp_timeout(1.5):
            idn = transport.query("ID?")
        if idn.replace(" ", "").startswith("HP663"):
            transport.query("ERR?")  # clear the ERR 10 caused by '*IDN?'
            return "6632A"
    except TransportTimeout:
        transport.clear()
    raise RuntimeError("Couldn't identify the instrument. Choose the model explicitly "
                       "(the HP 3456A always has to be selected by hand; any SCPI meter, "
                       "load or supply can use a generic SCPI definition).")


def model_for(resource: str, model: str = "auto") -> str:
    """The model implied by the resource name, if any (simulators, PPK2 ports)."""
    if model != "auto":
        return model
    if resource.upper().startswith("REPLAY::"):
        from .. import tape
        try:
            return tape.load(tape.replay_path(resource)).model
        except tape.TapeError:
            return "auto"               # opening it says what is wrong
    if resource.upper().startswith("PPK2::"):
        return "PPK2"
    if resource.upper().startswith("SALEAE::"):
        return "SALEAE"
    if resource.upper().startswith("DPS150::"):
        return "DPS150"
    for key in SERIAL_DMMS:
        if resource.upper().startswith(key + "::"):
            return key
    if resource.upper().startswith("SIGROK::"):
        return "SIGROK"
    if resource.upper().startswith("GPIO::"):
        return "GPIO"
    if resource.upper().startswith("UART::"):
        return "UART"
    d = resource_definition(resource)
    if d is not None:
        return d.model_key
    base = sim_base(resource).upper()
    if base in SIM_MODELS:
        return SIM_MODELS[base]
    if base.startswith("SIM::"):
        name = base[5:]
        d = definition_for(name)
        if d is not None:
            return d.model_key
        dialect = scopes.sim_dialect(name)
        if dialect:
            return scopes.model_for_dialect(dialect)
    return "auto"


def sim_base(resource: str) -> str:
    """'SIM::PPK2#2' -> 'SIM::PPK2': a second (third, …) simulated unit of a model."""
    return resource.split("#", 1)[0] if resource.upper().startswith("SIM") else resource


def make_driver(model: str, transport, resource: str = ""):
    model = model_for(resource, model)
    if model == "auto":
        model = detect(transport)
    model = resolve_model(model)
    if model.startswith("SCPI:"):
        d = definition_for(model, resource)
        if d is None:
            raise RuntimeError(f"No generic SCPI definition '{model[5:]}' (built in, or in "
                               "~/.config/otterbench/instruments)")
        return ScpiDriver(transport, d)
    if model.startswith("SCOPE:"):
        return scopes.ScopeDriver(transport, scopes.dialect_of(model))
    if model in ("SALEAE", "SIGROK"):
        # the resource names the analyzer (SIM::…: whichever the simulator has)
        sim = resource.upper().startswith("SIM")
        if model == "SALEAE":
            return SaleaeLogic(transport, "" if sim else resource)
        return SigrokLogic(transport, "fx2lafw" if sim else resource)
    if model == "UART":
        return UartDriver(transport, _uart_resource(resource)[1])
    return _CLASSES[model](transport)
