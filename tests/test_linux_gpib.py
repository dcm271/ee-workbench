"""GPIB through our own binding to libgpib (no gpib-ctypes), on a fake libgpib.

In a subprocess each: the library and pyvisa-py's choice last for a process."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CC = shutil.which("gcc") or shutil.which("cc")


@pytest.fixture(scope="module")
def fake(tmp_path_factory):
    if not CC:
        pytest.skip("no C compiler")
    so = tmp_path_factory.mktemp("gpib") / "libgpib.so.0"
    subprocess.run([CC, "-shared", "-fPIC", "-o", str(so), str(HERE / "fake_gpib.c")], check=True)
    return str(so)


def run(code, lib):
    env = dict(os.environ, OTTERBENCH_GPIB_LIB=lib, PYTHONPATH=str(HERE.parent))
    env.pop("OTTERBENCH_VISA_LIB", None)
    r = subprocess.run([sys.executable, "-W", "ignore", "-c", code], env=env, timeout=60,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


OPEN = "from otterbench.transport import *\nimport otterbench.linux_gpib as g\n"


def test_query_block_status_and_timeouts(fake):
    out = run(OPEN + """
t = VisaTransport("GPIB0::5::INSTR")
import pyvisa_py.gpib as p, sys
assert p.gpib is g and sys.modules["gpib_ctypes"] is None
print(t.query("*IDN?"), t.read_stb())
b = t.query_raw("BLOCK?")                       # longer than one read
assert b == bytes(i * 7 % 256 for i in range(100000))
t.timeout_s = 0.3
assert g.ask(105, 3) == 10                      # 300 ms
try:
    t.query("NOTHING?")
except TransportTimeout:
    print("timeout")
t.clear()
print(t.query("*IDN?"))
t.close()
assert g.lib.open_count() == 0
""", fake)
    assert out == ["FAKE,66321D,0,1", "192", "timeout", "FAKE,66321D,0,1"]


def test_nobody_there_says_so(fake):
    out = run(OPEN + """
t = VisaTransport("GPIB0::9::INSTR")
try:
    t.write("*RST")
except TransportError as e:
    print("NLISTENERS" if "NLISTENERS" in str(e) or "listener" in str(e) else e)
""", fake)
    assert out == ["NLISTENERS"]


def test_scan(fake):
    assert run(OPEN + "print(*scan_gpib())", fake) == ["5", "12"]


def test_gpib_works_after_another_kind_of_visa_instrument(fake):
    out = run(OPEN + """
try:
    VisaTransport("TCPIP::127.0.0.1::9::SOCKET")
except Exception:
    pass
print(VisaTransport("GPIB0::5::INSTR").query("*IDN?"))
""", fake)
    assert out == ["FAKE,66321D,0,1"]


def test_threads_keep_their_own_errors(fake):
    out = run(OPEN + """
import threading
good, bad, wrong = VisaTransport("GPIB0::5::INSTR"), VisaTransport("GPIB0::9::INSTR"), []
def ok():
    for _ in range(300):
        if good.query("*IDN?") != "FAKE,66321D,0,1":
            wrong.append("ok")
def fails():
    for _ in range(300):
        try:
            bad.write("X")
            wrong.append("no error")
        except TransportTimeout:
            wrong.append("timeout")
        except TransportError:
            pass
ts = [threading.Thread(target=f) for f in (ok, fails)]
[t.start() for t in ts]; [t.join() for t in ts]
print(len(wrong))
""", fake)
    assert out == ["0"]


def test_errors_and_the_rest_of_the_interface(fake):
    out = run(OPEN + """
load_gpib_lib()
print(g.version(), g.find("gpib0"))
for f in (lambda: g.dev(3, 5), lambda: g.find("nope"), lambda: g.write(109, "x")):
    try:
        f()
    except g.GpibError as e:
        print(e.code)
b = g.Gpib(0)
assert b.command(chr(1)) == 1 and b.lines() & 0x2000 and b.listener(5, None)
b.interface_clear(); b.remote_enable(1); b.close()
d = g.Gpib(0, 5); d.trigger(); d.wait(0x100); d.close(); d.close()
assert g.lib.open_count() == 0
""", fake)
    assert out == ["4.3.6-fake", "0", "7", "0", "2"]


def test_a_library_that_isnt_libgpib_is_reported(tmp_path):
    bad = tmp_path / "libgpib.so.0"
    bad.write_bytes(b"not a library")
    out = run("import otterbench.linux_gpib as g\n"
              f"try:\n    g.load({str(bad)!r})\nexcept OSError:\n    print('refused', g.lib)", "")
    assert out == ["refused", "None"]
