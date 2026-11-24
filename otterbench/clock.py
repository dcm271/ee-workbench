"""The app's shared time base: seconds since start-up. Every reading, capture
and session record is stamped with it, so all instruments line up."""
from __future__ import annotations

import time

APP_T0 = time.monotonic()


def now() -> float:
    return time.monotonic() - APP_T0
