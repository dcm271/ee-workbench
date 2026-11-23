"""Client for OtterBench's remote API (standard library only, so it works
from any Python 3.9+, inside or outside the app's .venv).

    import sys; sys.path.insert(0, "/path/to/ee-workbench")   # if not installed
    from ee_workbench.client import connect

    bench = connect()                 # the app on this computer (Tools ▾ → Remote API)
    print(bench.instruments())
    bench.set("66321D", "voltage", 3.3)
    bench.set("66321D", "output", "on")
    bench.wait(0.5)
    i = bench.measure("PPK2", "current", 2.0)          # mean over 2 s
    ok = bench.expect("sleep current", i, hi=10e-6)
    cap = bench.capture("Logic")                      # {"header", "rows", "offset", ...}
    bench.gpio("GPIO", "relay", "on")
    bench.profile("66321D", "voltage", "ramp 3.0..3.6 for=5")
    bench.record(v=3.3, i=i)                          # a row in the session, if recording

Errors from the bench raise BenchError with the app's message.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


class BenchError(RuntimeError):
    pass


def _api_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "ee-workbench" / "api.json"


class RemoteBench:
    def __init__(self, url: str, token: str, timeout: float = 600.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, *args, **kwargs):
        body = json.dumps({"method": method, "args": list(args), "kwargs": kwargs}).encode()
        req = urllib.request.Request(self.url + "/call", body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())["result"]
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", str(e))
            except ValueError:
                msg = str(e)
            raise BenchError(msg) from None
        except urllib.error.URLError as e:
            raise BenchError(f"Can't reach OtterBench at {self.url}: {e.reason}. Is it "
                             "running with Tools ▾ → Remote API on?") from None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: self.call(name, *a, **k)

    def __repr__(self):
        return f"<RemoteBench {self.url}>"


def connect(url: str | None = None, token: str | None = None, timeout: float = 600.0
            ) -> RemoteBench:
    """The running app's API. url / token default to what it wrote to
    ~/.config/ee-workbench/api.json when the API was switched on."""
    if url is None or token is None:
        try:
            d = json.loads(_api_file().read_text())
        except (OSError, ValueError):
            raise BenchError("OtterBench's remote API isn't on (no "
                             f"{_api_file()}). Switch it on: Tools ▾ → Remote API.") from None
        url = url or d["url"]
        token = token or d["token"]
    return RemoteBench(url, token, timeout)
