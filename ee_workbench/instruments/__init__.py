"""Instrument drivers and model selection."""
from __future__ import annotations

from ..transport import TransportTimeout
from .agilent663xx import Agilent663xx
from .fgen import FunctionGenerator, looks_like_fgen
from .gpio import GpioDriver
from .hp3456a import HP3456A
from .hp6632a import HP6632A
from .ppk2 import PPK2
from .saleae import SaleaeLogic
from .sigrok import SigrokLogic

MODELS = {
    "auto": "Auto-detect (power supplies)",
    "663xx": "Agilent 66311B / 66321B / 66321D",
    "6632A": "HP 6632A / 6633A / 6634A",
    "3456A": "HP 3456A DMM",
    "PPK2": "Nordic Power Profiler Kit II",
    "SALEAE": "Saleae Logic 8 / Pro 8 / Pro 16 (via Logic 2)",
    "SIGROK": "Logic analyzer via sigrok (fx2lafw, DSLogic, …)",
    "FGEN": "Function generator (Siglent SDG, Rigol DG, Keysight 33xxx)",
    "GPIO": "Raspberry Pi GPIO header (or a Linux GPIO chip)",
}

_CLASSES = {"663xx": Agilent663xx, "6632A": HP6632A, "3456A": HP3456A, "PPK2": PPK2,
            "SALEAE": SaleaeLogic, "SIGROK": SigrokLogic, "FGEN": FunctionGenerator,
            "GPIO": GpioDriver}

SIM_MODELS = {"SIM::66321D": "663xx", "SIM::6632A": "6632A", "SIM::3456A": "3456A",
              "SIM::PPK2": "PPK2", "SIM::SALEAE": "SALEAE",
              "SIM::SIGROK": "SIGROK", "SIM::FGEN": "FGEN", "SIM::SDG": "FGEN",
              "SIM::GPIO": "GPIO"}


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
                       "(the HP 3456A always has to be selected by hand).")


def model_for(resource: str, model: str = "auto") -> str:
    """The model implied by the resource name, if any (simulators, PPK2 ports)."""
    if model != "auto":
        return model
    if resource.upper().startswith("PPK2::"):
        return "PPK2"
    if resource.upper().startswith("SALEAE::"):
        return "SALEAE"
    if resource.upper().startswith("SIGROK::"):
        return "SIGROK"
    if resource.upper().startswith("GPIO::"):
        return "GPIO"
    return SIM_MODELS.get(resource.upper(), "auto")


def make_driver(model: str, transport, resource: str = ""):
    model = model_for(resource, model)
    if model == "auto":
        model = detect(transport)
    if model in ("SALEAE", "SIGROK"):
        # the resource names the analyzer (SIM::…: whichever the simulator has)
        sim = resource.upper().startswith("SIM")
        if model == "SALEAE":
            return SaleaeLogic(transport, "" if sim else resource)
        return SigrokLogic(transport, "fx2lafw" if sim else resource)
    return _CLASSES[model](transport)
