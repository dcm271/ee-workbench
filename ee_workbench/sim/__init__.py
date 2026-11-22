"""Simulated instruments, selected by resource name (SIM::<model>)."""
from __future__ import annotations


def open_sim(resource: str):
    name = resource.upper().split("::", 1)[-1].split("#", 1)[0]
    from ..instruments import scopes
    from ..instruments.scpi_generic import definition_for

    d = definition_for(name)
    if d is not None:
        from .sscpi import SimScpi

        return SimScpi(d)
    if scopes.sim_dialect(name):
        from .sscope import SimScope

        return SimScope(scopes.sim_dialect(name))
    if "GPIO" in name:
        from .sgpio import SimGpio

        return SimGpio()
    if "FGEN" in name or "SDG" in name:
        from .sfgen import SimFgen

        return SimFgen(siglent="SDG" in name)
    if "SIGROK" in name:
        from .ssigrok import SimSigrokCli

        return SimSigrokCli()
    if "SALEAE" in name:
        from .slogic import SimLogic2

        return SimLogic2()
    if "DPS150" in name:
        from .sdps150 import SimDPS150

        return SimDPS150()
    if "PPK2" in name:
        from .sppk2 import SimPPK2

        return SimPPK2()
    if "3456" in name:
        from .s3456a import Sim3456A

        return Sim3456A()
    if "6632A" in name or "6633A" in name or "6634A" in name:
        from .s6632a import Sim6632A

        return Sim6632A()
    from .s663xx import Sim663xx

    return Sim663xx()
