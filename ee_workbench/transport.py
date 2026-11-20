"""Byte-level transports: a real VISA session or a built-in simulator.

Every transport exposes the same small surface used by the drivers:
    write(cmd), read() -> str, query(cmd) -> str, read_stb() -> int,
    clear(), close(), temp_timeout(s), and a `timeout_s` property.
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager


class TransportTimeout(Exception):
    """The instrument did not answer (usually: it rejected the command)."""


class TransportError(Exception):
    pass


SIM_RESOURCES = ["SIM::66321D", "SIM::6632A", "SIM::3456A"]


def _visa_library() -> str:
    # '@py' = pyvisa-py (uses linux-gpib for GPIB, pyserial for ASRL).
    # Set EEWB_VISA_LIB=@ivi to use NI-VISA / Keysight IO Libraries instead.
    return os.environ.get("EEWB_VISA_LIB", "@py")


def scan_gpib(board: int = 0) -> list[int]:
    """Primary addresses with a device listening on the bus.

    Uses ibln, which only checks for a listener and sends no commands,
    so it is safe for instruments without an ID query (e.g. HP 3456A).
    """
    try:
        from gpib_ctypes import gpib

        return [pad for pad in range(1, 31) if gpib.listener(board, pad)]
    except Exception:
        return []


def list_resources(scan: bool = True) -> list[str]:
    """GPIB listeners plus USB serial ports, then the simulators.

    Uses a passive GPIB scan (~0.2 s) rather than pyvisa's list_resources(),
    which probes every interface type and took ~8 s on the test machine.
    Anything else can be typed in by hand.
    """
    import glob

    found = [f"GPIB0::{pad}::INSTR" for pad in scan_gpib()] if scan else []
    if not found:
        found.append("GPIB0::5::INSTR")
    for dev in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
        found.append(f"ASRL{dev}::INSTR")
    return found + SIM_RESOURCES


def describe(resource: str) -> str:
    m = re.match(r"GPIB(\d+)::(\d+)", resource, re.I)
    if m:
        return f"GPIB{m.group(1)} @{m.group(2)}"
    if resource.upper().startswith("SIM"):
        return "simulated"
    return resource.replace("::INSTR", "")


def open_transport(resource: str, baud: int = 9600):
    if resource.upper().startswith("SIM"):
        from . import sim

        return sim.open_sim(resource)
    return VisaTransport(resource, baud=baud)


class VisaTransport:
    def __init__(self, resource: str, baud: int = 9600):
        import pyvisa
        from pyvisa import constants

        self._pyvisa = pyvisa
        self.resource = resource
        self.rm = pyvisa.ResourceManager(_visa_library())
        try:
            self.inst = self.rm.open_resource(resource)
        except Exception as e:  # pragma: no cover - hardware dependent
            raise TransportError(f"Could not open {resource}: {e}") from e
        self.inst.read_termination = "\n"
        self.inst.write_termination = "\n"
        self.inst.timeout = 5000
        if resource.upper().startswith("ASRL"):
            self.inst.baud_rate = baud
            self.inst.data_bits = 8
            self.inst.parity = constants.Parity.none
            self.inst.stop_bits = constants.StopBits.one
            self.inst.flow_control = constants.ControlFlow.none

    @property
    def timeout_s(self) -> float:
        return self.inst.timeout / 1000.0

    @timeout_s.setter
    def timeout_s(self, value: float) -> None:
        self.inst.timeout = int(value * 1000)

    @contextmanager
    def temp_timeout(self, seconds: float):
        old = self.timeout_s
        self.timeout_s = seconds
        try:
            yield
        finally:
            self.timeout_s = old

    def _wrap(self, e, what):
        if e.error_code == self._pyvisa.constants.StatusCode.error_timeout:
            return TransportTimeout(f"No reply to {what!r}")
        return TransportError(str(e))

    def write(self, cmd: str) -> None:
        try:
            self.inst.write(cmd)
        except self._pyvisa.errors.VisaIOError as e:
            raise self._wrap(e, cmd) from e

    def read(self) -> str:
        try:
            return self.inst.read().strip()
        except self._pyvisa.errors.VisaIOError as e:
            raise self._wrap(e, "read") from e

    def query(self, cmd: str) -> str:
        try:
            return self.inst.query(cmd).strip()
        except self._pyvisa.errors.VisaIOError as e:
            raise self._wrap(e, cmd) from e

    def read_stb(self) -> int:
        try:
            return int(self.inst.read_stb())
        except Exception:
            return 0

    def clear(self) -> None:
        try:
            self.inst.clear()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.inst.close()
        finally:
            try:
                self.rm.close()
            except Exception:
                pass
