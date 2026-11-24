"""linux-gpib's C library (libgpib.so) through ctypes.

pyvisa-py drives GPIB with the Python modules "gpib" and "Gpib" that linux-gpib
can build for one particular Python version. This module offers the part of that
interface pyvisa-py uses, without building anything: install() puts it in
their place. Written from the library's public C header (gpib/ib.h).

libgpib's calls return ibsta and leave the error and byte count in variables
kept per thread; they are read in the same call here, so several instruments'
worker threads don't see each other's.
"""
from __future__ import annotations

import ctypes
import sys
import types
from ctypes import POINTER, byref, c_char, c_char_p, c_int, c_long, c_short, c_void_p

ERR, TIMO, END = 0x8000, 0x4000, 0x2000      # ibsta bits
EDVR = 0                                     # iberr: the system's error, errno in ibcnt

_SIGNATURES = {                              # name: argument types (all return int)
    "ibask": [c_int, c_int, POINTER(c_int)],
    "ibcac": [c_int, c_int],
    "ibclr": [c_int],
    "ibcmd": [c_int, c_void_p, c_long],
    "ibconfig": [c_int, c_int, c_int],
    "ibdev": [c_int] * 6,
    "ibfind": [c_char_p],
    "ibgts": [c_int, c_int],
    "iblines": [c_int, POINTER(c_short)],
    "ibln": [c_int, c_int, c_int, POINTER(c_short)],
    "ibonl": [c_int, c_int],
    "ibpct": [c_int],
    "ibrd": [c_int, c_void_p, c_long],
    "ibrsp": [c_int, POINTER(c_char)],
    "ibsic": [c_int],
    "ibsre": [c_int, c_int],
    "ibtmo": [c_int, c_int],
    "ibtrg": [c_int],
    "ibwait": [c_int, c_int],
    "ibwrt": [c_int, c_void_p, c_long],
    "ThreadIbsta": [],
    "ThreadIberr": [],
}

lib: ctypes.CDLL | None = None


class GpibError(Exception):
    def __init__(self, what: str, code: int = -1, detail: str = ""):
        super().__init__(f"{what}() error: {detail}" if detail else f"{what}() error")
        self.code = code                     # iberr


def load(name: str) -> None:
    """Load libgpib. OSError when there is none or it can't be loaded,
    AttributeError when it is some other library."""
    global lib
    cdll = ctypes.CDLL(name)
    for fn, args in _SIGNATURES.items():
        f = getattr(cdll, fn)
        f.argtypes, f.restype = args, c_int
    cdll.ThreadIbcntl.argtypes, cdll.ThreadIbcntl.restype = [], c_long
    cdll.ibvers.argtypes, cdll.ibvers.restype = [POINTER(c_char_p)], None
    try:                                     # not in old versions
        cdll.gpib_error_string.argtypes = [c_int]
        cdll.gpib_error_string.restype = c_char_p
    except AttributeError:
        pass
    lib = cdll


def install() -> None:
    """Make pyvisa-py use this. Before pyvisa_py is imported: it chooses once."""
    me = sys.modules[__name__]
    upper = types.ModuleType("Gpib", "ee_workbench.linux_gpib")
    upper.Gpib = Gpib
    sys.modules["gpib"], sys.modules["Gpib"] = me, upper
    # pyvisa-py would prefer this one if it is still installed.
    sys.modules["gpib_ctypes"] = None        # type: ignore[assignment]


def finish() -> None:
    """After pyvisa_py is imported: with the codes GpibError carries and the
    library, its precise error messages and bus control work here too (it has
    them for gpib-ctypes only)."""
    m = sys.modules.get("pyvisa_py.gpib")
    if m is not None and getattr(m, "gpib", None) is sys.modules[__name__]:
        m.GPIB_CTYPES, m.gpib_lib = True, lib


def _error(what: str) -> GpibError:
    code = lib.ThreadIberr()
    detail = f"iberr {code}"
    if hasattr(lib, "gpib_error_string"):
        detail = (lib.gpib_error_string(code) or b"").decode("ascii", "replace") or detail
    if code == EDVR:
        import os

        n = lib.ThreadIbcntl()
        if n > 0:
            detail += f" ({os.strerror(n)})"
    return GpibError(what, code, detail)


def _check(what: str, sta: int) -> int:
    if sta & ERR:
        raise _error(what)
    return sta


def _bytes(data) -> bytes:
    return data.encode("latin-1") if isinstance(data, str) else bytes(data)


# ---------------------------------------------------------- the "gpib" module

def version() -> str:
    p = c_char_p()
    lib.ibvers(byref(p))
    return (p.value or b"").decode("ascii", "replace")


def find(name: str) -> int:
    ud = lib.ibfind(_bytes(name))
    if ud < 0:
        raise _error("find")
    return ud


def dev(board: int, pad: int, sad: int = 0, timeout_code: int = 13, send_eoi: int = 1,
        eos_mode: int = 0) -> int:
    ud = lib.ibdev(board, pad, sad, timeout_code, send_eoi, eos_mode)
    if ud < 0:
        raise _error("dev")
    return ud


def ask(ud: int, option: int) -> int:
    v = c_int()
    _check("ask", lib.ibask(ud, option, byref(v)))
    return v.value


def config(ud: int, option: int, value: int) -> int:
    return _check("config", lib.ibconfig(ud, option, value))


def read(ud: int, count: int = 512) -> bytes:
    buf = ctypes.create_string_buffer(max(1, count))
    _check("read", lib.ibrd(ud, buf, count))
    return ctypes.string_at(buf, lib.ThreadIbcntl())


def write(ud: int, data) -> int:
    d = _bytes(data)
    return _check("write", lib.ibwrt(ud, d, len(d)))


def command(ud: int, data) -> int:
    """Command bytes onto the bus; how many were sent."""
    d = _bytes(data)
    _check("command", lib.ibcmd(ud, d, len(d)))
    return lib.ThreadIbcntl()


def clear(ud: int) -> int:
    return _check("clear", lib.ibclr(ud))


def trigger(ud: int) -> int:
    return _check("trigger", lib.ibtrg(ud))


def serial_poll(ud: int) -> int:
    v = c_char()
    _check("serial_poll", lib.ibrsp(ud, byref(v)))
    return v.value[0]


def listener(ud: int, pad: int, sad: int | None = 0) -> bool:
    v = c_short()
    _check("listener", lib.ibln(ud, pad, sad or 0, byref(v)))
    return bool(v.value)


def lines(ud: int) -> int:
    v = c_short()
    _check("lines", lib.iblines(ud, byref(v)))
    return v.value


def timeout(ud: int, code: int) -> int:
    return _check("timeout", lib.ibtmo(ud, code))


def wait(ud: int, mask: int) -> int:
    return _check("wait", lib.ibwait(ud, mask))


def interface_clear(ud: int) -> int:
    return _check("interface_clear", lib.ibsic(ud))


def remote_enable(ud: int, on: int) -> int:
    return _check("remote_enable", lib.ibsre(ud, on))


def close(ud: int) -> int:
    return _check("close", lib.ibonl(ud, 0))


def ibsta() -> int:
    return lib.ThreadIbsta()


def ibcnt() -> int:
    return lib.ThreadIbcntl()


# ---------------------------------------------------------- the "Gpib" module

class Gpib:
    """A board (an index or a name from gpib.conf), or a device on one (board
    index and address)."""

    def __init__(self, name=0, pad=None, sad=0, timeout=13, send_eoi=1, eos_mode=0):
        self._own = False                    # a descriptor of its own, to close
        if isinstance(name, str):
            self.id, self._own = find(name), True
        elif pad is None:
            self.id = name
        else:
            self.id, self._own = dev(name, pad, sad, timeout, send_eoi, eos_mode), True

    def close(self):
        if self._own:
            self._own = False
            close(self.id)

    def __del__(self):
        try:
            self.close()
        except Exception:                    # noqa: BLE001 - the board may be gone
            pass

    def ibsta(self) -> int:
        return ibsta()

    def ibcnt(self) -> int:
        return ibcnt()

    def ask(self, option):
        return ask(self.id, option)

    def config(self, option, value):
        return config(self.id, option, value)

    def read(self, count=512):
        return read(self.id, count)

    def write(self, data):
        return write(self.id, data)

    def command(self, data):
        return command(self.id, data)

    def clear(self):
        return clear(self.id)

    def trigger(self):
        return trigger(self.id)

    def serial_poll(self):
        return serial_poll(self.id)

    def listener(self, pad, sad=0):
        return listener(self.id, pad, sad)

    def lines(self):
        return lines(self.id)

    def timeout(self, code):
        return timeout(self.id, code)

    def wait(self, mask):
        return wait(self.id, mask)

    def interface_clear(self):
        return interface_clear(self.id)

    def remote_enable(self, on):
        return remote_enable(self.id, on)
