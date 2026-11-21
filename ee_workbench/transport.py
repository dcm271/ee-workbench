"""Byte-level transports: a real VISA session or a built-in simulator.

Every transport exposes the same small surface used by the drivers:
    write(cmd), read() -> str, query(cmd) -> str, read_stb() -> int,
    clear(), close(), temp_timeout(s), and a `timeout_s` property.
"""
from __future__ import annotations

import os
import re
import threading
import time
from contextlib import contextmanager


class TransportTimeout(Exception):
    """The instrument did not answer (usually: it rejected the command)."""


class TransportError(Exception):
    pass


SIM_RESOURCES = ["SIM::66321D", "SIM::6632A", "SIM::3456A", "SIM::PPK2", "SIM::SALEAE"]


def _visa_library() -> str:
    # '@py' = pyvisa-py (uses linux-gpib for GPIB, pyserial for ASRL).
    # Set EEWB_VISA_LIB=@ivi to use NI-VISA / Keysight IO Libraries instead.
    return os.environ.get("EEWB_VISA_LIB", "@py")


# ---------------------------------------------------------------- libgpib
_GPIB_LOCK = threading.Lock()
_GPIB_STATE: tuple[bool, str] | None = None    # (loaded, problem description)


def load_gpib_lib() -> tuple[bool, str]:
    """Load linux-gpib's libgpib for gpib_ctypes (and so for pyvisa-py).

    gpib_ctypes only tries the bare name "libgpib.so.0" and reports any
    failure as "not found". Search the usual install locations, and if the
    library exists but won't load (e.g. it was built on an x86 PC and copied
    to a Pi), say so. Must run before pyvisa-py imports its GPIB support.
    Returns (ok, problem).
    """
    global _GPIB_STATE
    with _GPIB_LOCK:
        if _GPIB_STATE is not None:
            return _GPIB_STATE
        import ctypes
        import glob
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                from gpib_ctypes.gpib import gpib as g
            except ImportError:
                _GPIB_STATE = (False, "The gpib-ctypes Python package is missing: "
                                      ".venv/bin/pip install gpib-ctypes")
                return _GPIB_STATE
        if isinstance(g._lib, ctypes.CDLL):
            _GPIB_STATE = (True, "")
            return _GPIB_STATE
        env = os.environ.get("EEWB_GPIB_LIB")
        names = ([env] if env else []) + ["libgpib.so.0", "/usr/local/lib/libgpib.so.0",
                                          "/usr/lib/libgpib.so.0"]
        names += sorted(glob.glob("/usr/lib/*/libgpib.so.0") +
                        glob.glob("/usr/local/lib/*/libgpib.so.0") +
                        glob.glob("/usr/local/lib/libgpib.so*"))
        errors = []
        for name in dict.fromkeys(names):
            try:
                ctypes.CDLL(name)
            except OSError as e:
                if os.path.exists(name):
                    errors.append(str(e) if name in str(e) else f"{name}: {e}")
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                if g._load_lib(name):
                    _GPIB_STATE = (True, "")
                    return _GPIB_STATE
        fix = "Run scripts/install-gpib-driver.sh (Pi: scripts/setup-pi.sh) to build it on this machine."
        if errors:
            hint = (" It was probably built on another computer (wrong CPU type)."
                    if any("ELF" in e or "format" in e for e in errors) else "")
            problem = ("The GPIB library is installed but can't be loaded:\n  " +
                       "\n  ".join(errors) + "\n" + hint.strip() + ("\n" if hint else "") + fix)
        else:
            problem = "The linux-gpib library (libgpib.so.0) is not installed. " + fix
        _GPIB_STATE = (False, problem)
        return _GPIB_STATE


def scan_gpib(board: int = 0) -> list[int]:
    """Primary addresses with a device listening on the bus.

    Uses ibln, which only checks for a listener and sends no commands,
    so it is safe for instruments without an ID query (e.g. HP 3456A).
    """
    if not load_gpib_lib()[0]:
        return []
    try:
        from gpib_ctypes import gpib

        return [pad for pad in range(1, 31) if gpib.listener(board, pad)]
    except Exception:
        return []


def gpib_ready(board: int = 0) -> bool:
    """True once the adapter's device node exists and the board answers."""
    if not os.path.exists(f"/dev/gpib{board}") or not load_gpib_lib()[0]:
        return False
    try:
        from gpib_ctypes import gpib

        gpib.listener(board, 30)
        return True
    except Exception:
        return False


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
    ppk_data, ppk_all = find_ppk2()
    found += [f"PPK2::{dev}" for dev in ppk_data]
    from .instruments.saleae import find_logic2

    found += find_logic2()          # analyzers a running Logic 2 can see
    for dev in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
        if dev not in ppk_all:
            found.append(f"ASRL{dev}::INSTR")
    return found + SIM_RESOURCES


def find_ppk2() -> tuple[list[str], set[str]]:
    """Nordic PPK2s: (the data port of each, every port they own).

    A PPK2 shows up as two CDC-ACM ports; the samples come out of the one on
    the lower USB interface number (location "...:1.1")."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return [], set()
    from .instruments.ppk2 import USB_PID, USB_VID

    by_unit: dict[str, list[tuple[int, str]]] = {}
    for p in list_ports.comports():
        if p.vid == USB_VID and p.pid == USB_PID:
            m = re.search(r"\.(\d+)$", p.location or "")
            by_unit.setdefault(p.serial_number or p.device, []).append(
                (int(m.group(1)) if m else 99, p.device))
    data = sorted(_stable_path(min(ports)[1]) for ports in by_unit.values())
    return data, {dev for ports in by_unit.values() for _, dev in ports}


def _stable_path(dev: str) -> str:
    """/dev/serial/by-id/... for `dev` if udev made one: ttyACM numbers change
    when things are replugged, the by-id name (which has the serial number)
    doesn't, so a remembered session still finds the same unit."""
    import glob

    real = os.path.realpath(dev)
    for link in sorted(glob.glob("/dev/serial/by-id/*")):
        if os.path.realpath(link) == real:
            return link
    return dev


def describe(resource: str) -> str:
    m = re.match(r"GPIB(\d+)::(\d+)", resource, re.I)
    if m:
        return f"GPIB{m.group(1)} @{m.group(2)}"
    if resource.upper().startswith("SIM"):
        return "simulated"
    if resource.upper().startswith("PPK2::"):
        path = resource[6:]
        m = re.search(r"PPK2_([0-9A-F]+)", path, re.I)
        return f"PPK2 {m.group(1)[-6:]}" if m else path.removeprefix("/dev/")
    if resource.upper().startswith("SALEAE::"):
        return f"Saleae {resource[8:]}".strip()
    return resource.replace("::INSTR", "")


def serial_path(resource: str) -> str:
    """'PPK2::/dev/ttyACM0' or 'ASRL/dev/ttyACM0::INSTR' -> '/dev/ttyACM0'."""
    r = resource.strip()
    if r.upper().startswith("PPK2::"):
        return r[6:]
    m = re.match(r"ASRL(.+?)(::INSTR)?$", r, re.I)
    return m.group(1) if m else r


def open_transport(resource: str, baud: int = 9600, model: str = "auto"):
    if resource.upper().startswith("SIM"):
        from . import sim

        return sim.open_sim(resource)
    if resource.upper().startswith("PPK2::") or model == "PPK2":
        return SerialStream(serial_path(resource))
    if resource.upper().startswith("SALEAE::") or model == "SALEAE":
        from .instruments.saleae import Logic2Link, parse_resource

        _, host, port = parse_resource(resource)
        return Logic2Link(host, port)
    return VisaTransport(resource, baud=baud)


class SerialStream:
    """Raw byte stream over a USB CDC serial port (pyserial), for devices that
    stream binary data rather than talk SCPI, such as the Nordic PPK2.

    A background thread reads the port continuously. A PPK2 sends 400 kB/s;
    reading it only when the I/O thread polls (every ~50 ms) lets the device's
    USB buffer overflow, and it then silently drops whole blocks."""

    MAX_BUFFER = 16 * 1024 * 1024        # ~40 s of PPK2 data if nobody reads

    def __init__(self, path: str):
        try:
            import serial
        except ImportError as e:
            raise TransportError("pyserial is missing: sudo apt install python3-serial") from e
        self.resource = path
        try:
            # exclusive: a second program reading the same stream would steal samples
            self.port = serial.Serial(path, timeout=0.05, write_timeout=2, exclusive=True)
        except serial.SerialException as e:
            hint = ""
            if "ermission" in str(e):
                hint = (" You need to be in the 'dialout' group: "
                        "sudo usermod -aG dialout $USER, then log out and back in.")
            elif "busy" in str(e).lower() or "lock" in str(e).lower():
                hint = " Another program has it open (nRF Connect Power Profiler?)."
            raise TransportError(f"Could not open {path}: {e}.{hint}") from e
        self.timeout_s = 2.0
        self.overflow_bytes = 0
        self.read_error: Exception | None = None
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._running = True
        self._thread = threading.Thread(target=self._reader, name=f"serial {path}", daemon=True)
        self._thread.start()

    def _reader(self):
        while self._running:
            try:
                chunk = self.port.read(65536)
            except Exception as e:          # unplugged, etc.
                if self._running:
                    self.read_error = e
                return
            if chunk:
                with self._lock:
                    self._buf += chunk
                    extra = len(self._buf) - self.MAX_BUFFER
                    if extra > 0:
                        del self._buf[:extra]
                        self.overflow_bytes += extra

    @contextmanager
    def temp_timeout(self, seconds: float):
        old, self.timeout_s = self.timeout_s, seconds
        try:
            yield
        finally:
            self.timeout_s = old

    def write(self, data) -> None:
        if isinstance(data, str):
            data = data.encode("latin1")
        self.port.write(data)
        self.port.flush()

    def read_available(self) -> bytes:
        with self._lock:
            out = bytes(self._buf)
            self._buf.clear()
        if not out and self.read_error is not None:
            raise TransportError(f"{self.resource}: {self.read_error}")
        return out

    def read_until(self, marker: bytes, timeout_s: float) -> bytes:
        buf = b""
        end = time.monotonic() + timeout_s
        while marker not in buf and time.monotonic() < end:
            chunk = self.read_available()
            if chunk:
                buf += chunk
            else:
                time.sleep(0.01)
        return buf

    def drain(self, quiet_s: float, max_s: float) -> None:
        """Discard input until nothing has arrived for `quiet_s`."""
        end = time.monotonic() + max_s
        quiet_until = time.monotonic() + quiet_s
        while time.monotonic() < min(end, quiet_until):
            if self.read_available():
                quiet_until = time.monotonic() + quiet_s
            else:
                time.sleep(0.01)

    def reset_input(self) -> None:
        with self._lock:
            self._buf.clear()

    def clear(self) -> None:
        self.reset_input()

    def read_stb(self) -> int:
        return 0

    def close(self) -> None:
        self._running = False
        self._thread.join(1.0)
        self.port.close()


class VisaTransport:
    def __init__(self, resource: str, baud: int = 9600):
        import pyvisa
        from pyvisa import constants

        if resource.upper().startswith("GPIB") and _visa_library() == "@py":
            ok, problem = load_gpib_lib()
            if not ok:
                raise TransportError(problem)
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

    def query_binary(self, cmd: str) -> list[float]:
        """IEEE-488.2 definite-length block of little-endian float32 (the
        instrument must be in FORM REAL,32 + FORM:BORD SWAP)."""
        try:
            return list(self.inst.query_binary_values(cmd, datatype="f", is_big_endian=False))
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
