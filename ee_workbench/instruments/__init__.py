"""Instrument drivers and model selection."""
from __future__ import annotations

from ..transport import TransportTimeout
from .agilent663xx import Agilent663xx
from .hp3456a import HP3456A
from .hp6632a import HP6632A
from .ppk2 import PPK2

MODELS = {
    "auto": "Auto-detect (power supplies)",
    "663xx": "Agilent 66311B / 66321B / 66321D",
    "6632A": "HP 6632A / 6633A / 6634A",
    "3456A": "HP 3456A DMM",
    "PPK2": "Nordic Power Profiler Kit II",
}

_CLASSES = {"663xx": Agilent663xx, "6632A": HP6632A, "3456A": HP3456A, "PPK2": PPK2}

SIM_MODELS = {"SIM::66321D": "663xx", "SIM::6632A": "6632A", "SIM::3456A": "3456A",
              "SIM::PPK2": "PPK2"}


def detect(transport) -> str:
    """Identify a power supply. The 3456A has no ID query and must be chosen
    explicitly (probing it with SCPI just sets its syntax-error bit)."""
    transport.clear()
    try:
        with transport.temp_timeout(1.5):
            idn = transport.query("*IDN?")
        if "663" in idn:
            return "663xx"
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
    return SIM_MODELS.get(resource.upper(), "auto")


def make_driver(model: str, transport, resource: str = ""):
    model = model_for(resource, model)
    if model == "auto":
        model = detect(transport)
    return _CLASSES[model](transport)
