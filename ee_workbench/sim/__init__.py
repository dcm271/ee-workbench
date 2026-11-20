"""Simulated instruments, selected by resource name (SIM::<model>)."""
from __future__ import annotations


def open_sim(resource: str):
    name = resource.upper().split("::", 1)[-1]
    if "3456" in name:
        from .s3456a import Sim3456A

        return Sim3456A()
    if "6632A" in name or "6633A" in name or "6634A" in name:
        from .s6632a import Sim6632A

        return Sim6632A()
    from .s663xx import Sim663xx

    return Sim663xx()
