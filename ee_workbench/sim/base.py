"""Transport plumbing shared by the simulated instruments."""
from __future__ import annotations

import time
from contextlib import contextmanager

from ..transport import TransportTimeout


class SimBase:
    """Subclasses implement write(); replies go into self._out. Several replies
    produced by one message are joined with ';' like a real SCPI instrument."""

    def __init__(self):
        self.timeout_s = 5.0
        self._out: list[str] = []
        self.stb = 0

    @contextmanager
    def temp_timeout(self, seconds):
        old, self.timeout_s = self.timeout_s, seconds
        try:
            yield
        finally:
            self.timeout_s = old

    def write(self, cmd: str):  # pragma: no cover - abstract
        raise NotImplementedError

    def read(self) -> str:
        time.sleep(0.002)
        if not self._out:
            raise TransportTimeout("No reply (simulated)")
        r = ";".join(self._out)
        self._out.clear()
        return r

    def query(self, cmd: str) -> str:
        self._out.clear()
        self.write(cmd)
        try:
            return self.read()
        except TransportTimeout:
            raise TransportTimeout(f"No reply to {cmd!r}") from None

    def read_stb(self) -> int:
        s, self.stb = self.stb, 0
        return s

    def clear(self):
        self._out.clear()

    def close(self):
        pass
