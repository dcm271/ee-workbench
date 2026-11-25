"""Drive the open bench from outside the GUI (Jupyter, pytest, CI scripts).

The app runs a small JSON-over-HTTP server on 127.0.0.1 (Tools ▾ → Remote API,
or `api = true` in config.toml). Each call is one `bench` method, the same
ones the sequencer's Script tab uses, carried out on the instruments' own I/O
threads, so the tabs keep working while a script drives them:

    from otterbench.client import connect
    bench = connect()                          # reads ~/.config/otterbench/api.json
    bench.instruments()
    bench.set("66321D", "voltage", 3.3)
    i = bench.measure("PPK2", "current", 1.0)

Only this computer can connect (it listens on 127.0.0.1), and every request
must carry the token from api.json (mode 600), so other users on a shared
machine can't drive your supplies. To reach it from another computer use an
SSH tunnel:  ssh -L 8765:127.0.0.1:8765 bench-pi
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import config_dir

DEFAULT_PORT = 8765
MAX_BODY = 10_000_000        # bytes of one request
METHODS = ("instruments", "set", "measure", "capture", "wait", "note", "record", "gpio",
           "read_pin", "wait_for", "send", "wait_text", "profile", "expect", "outputs_off", "log")


def api_file():
    return config_dir() / "api.json"


def to_json(v):
    """Results the client can use: captures become a dict, numpy scalars floats."""
    if hasattr(v, "table_rows") and hasattr(v, "cfg"):
        a, b = v.span
        return {"header": list(getattr(v, "table_header", []) or []),
                "rows": [list(map(to_json, r)) for r in v.table_rows],
                "offset": v.offset, "span": [a, b],
                "channels": sorted(getattr(v, "digital", {}) or {})}
    if hasattr(v, "item") and not isinstance(v, (list, tuple, dict, str)):
        try:
            return v.item()
        except Exception:    # noqa: BLE001
            pass
    if isinstance(v, float) and v != v:
        return None
    if isinstance(v, (list, tuple)):
        return [to_json(x) for x in v]
    if isinstance(v, dict):
        return {str(k): to_json(x) for k, x in v.items()}
    return v


class ApiServer:
    """make_bench() -> a sequencer.Bench over the open instruments (called per
    request, so tabs opened later are seen). stop_event: set by Esc."""

    def __init__(self, make_bench, port: int = DEFAULT_PORT, on_call=None):
        self.make_bench = make_bench
        self.port = port
        self.on_call = on_call or (lambda text: None)
        self.token = secrets.token_urlsafe(24)
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.calls = 0

    @property
    def running(self) -> bool:
        return self.httpd is not None

    def start(self):
        if self.httpd:
            return
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/":
                    self._reply(200, {"app": "otterbench", "methods": list(METHODS)})
                else:
                    self._reply(404, {"error": "POST /call"})

            def do_POST(self):
                tok = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                if not hmac.compare_digest(tok, srv.token):
                    self._reply(401, {"error": "bad token (see ~/.config/otterbench/api.json)"})
                    return
                if self.path != "/call":
                    self._reply(404, {"error": "POST /call"})
                    return
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= n <= MAX_BODY:
                        raise ValueError(f"Content-Length {n} out of range")
                    req = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(req, dict):
                        raise ValueError("the body must be a JSON object")
                    method = str(req.get("method", ""))
                    args = req.get("args", []) or []
                    kwargs = req.get("kwargs", {}) or {}
                    if not isinstance(args, list) or not isinstance(kwargs, dict):
                        raise ValueError("args must be a list and kwargs an object")
                except (ValueError, TypeError) as e:
                    self._reply(400, {"error": f"bad request: {e}"})
                    return
                try:
                    self._reply(*srv.call(method, args, kwargs))
                except OSError:
                    pass            # the client went away (timeout, Ctrl+C) mid-call

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True,
                                       name="api")
        self.thread.start()
        try:
            p = api_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"url": f"http://127.0.0.1:{self.port}", "port": self.port,
                           "token": self.token, "pid": os.getpid()}, fh)
        except OSError:
            self.stop()          # no token file: nobody could use it, so don't leave it running
            raise

    def call(self, method: str, args: list, kwargs: dict) -> tuple[int, dict]:
        from .sequencer import SequenceError, Stopped
        if method not in METHODS:
            return 404, {"error": f"no method '{method}' (have: {', '.join(METHODS)})"}
        self.calls += 1
        self.on_call(f"{method}({', '.join([repr(a) for a in args] + [f'{k}={v!r}' for k, v in kwargs.items()])})")
        try:
            bench = self.make_bench()
            fn = getattr(bench, method)
            return 200, {"result": to_json(fn(*args, **kwargs))}
        except Stopped:
            return 409, {"error": "stopped (All outputs OFF / Esc)", "type": "Stopped"}
        except SequenceError as e:
            return 400, {"error": str(e), "type": "SequenceError"}
        except TypeError as e:
            return 400, {"error": f"{method}: {e}", "type": "TypeError"}
        except Exception as e:      # noqa: BLE001
            return 500, {"error": f"{type(e).__name__}: {e}", "type": type(e).__name__,
                         "trace": traceback.format_exc()[-2000:]}

    def stop(self):
        if self.httpd:
            httpd, self.httpd = self.httpd, None
            try:
                httpd.shutdown()
                httpd.server_close()
            except OSError:
                pass
        try:
            d = json.loads(api_file().read_text(encoding="utf-8"))
            if d.get("pid") == os.getpid():
                api_file().unlink()
        except (OSError, ValueError):
            pass
