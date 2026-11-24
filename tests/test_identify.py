"""“Identify port”: each simulated device is recognised, by reading only."""
import os
import threading

import pytest

from ee_workbench import identify as idn
from ee_workbench.gui.main_window import parse_target
from ee_workbench.instruments import definition_for
from ee_workbench.sim import sadapters, sbk393, suart, sxdm
from ee_workbench.transport import serial_path


class At:
    """A port whose device only understands one baud rate."""

    def __init__(self, baud, make):
        self.baud, self.make, self.sent, self.bauds = baud, make, [], []
        self.dev = None

    def __call__(self, path, baud):
        self.bauds.append(baud)
        port = self
        if baud == self.baud:
            self.dev = self.dev or self.make()
        dev = self.dev if baud == self.baud else None

        class Link:
            def write(self, data):
                port.sent.append(bytes(data))
                if dev is not None:
                    dev.write(data)

            def read_available(self):
                return dev.read_available() if dev is not None else b""

            def reset_input(self):
                if dev is not None:
                    dev.reset_input()

            def close(self):
                pass
        return Link()


class Scpi(sadapters._Link):
    """A plain serial SCPI instrument."""

    def __init__(self, answer, end=b"\n"):
        super().__init__()
        self.answer, self.end, self._in = answer, end, b""

    def on_bytes(self, data):
        self._in += data
        while b"\n" in self._in:
            line, self._in = self._in.split(b"\n", 1)
            if line.strip() == b"*IDN?":
                self._out += self.answer + self.end


class Streaming393(sadapters._Link):
    def read_available(self):
        return sbk393.frame393(2, 0, 49987)

    def on_bytes(self, data):
        pass


@pytest.mark.parametrize("baud,make,model,resource", [
    (9600, Streaming393, "BK393", "BK393::/dev/ttyUSB0"),
    (9600, sbk393.SimBK393B, "BK393B", "BK393B::/dev/ttyUSB0"),
    (115200, lambda: sxdm.SimXDM("XDM1041", True), "XDM", "XDM::/dev/ttyUSB0"),
    (115200, sadapters.SimRiden, "SCPI:RD60XX", "RD60XX::/dev/ttyUSB0"),
    (115200, sadapters.SimSpm, "SCPI:DC310PRO", "DC310PRO::/dev/ttyUSB0"),
    (115200, lambda: sadapters.SimSpm("KIPRIM,DC605Pro,1,FV:V2.1.0"), "SCPI:DC605PRO",
     "DC605PRO::/dev/ttyUSB0"),
    (9600, sadapters.SimKorad, "SCPI:KA3005P", "KA3005P::/dev/ttyUSB0"),
    (9600, sadapters.SimPx100, "SCPI:PX100", "PX100::/dev/ttyUSB0"),
    (9600, sadapters.SimCenter, "SCPI:CENTER309", "CENTER309::/dev/ttyUSB0"),
    (9600, lambda: sadapters.SimAck(definition_for("ET44")), "SCPI:ET44", "ET44::/dev/ttyUSB0"),
    (9600, lambda: Scpi(b"RIGOL TECHNOLOGIES,DP832,DP8A,00.01"), "SCPI:DP832",
     "ASRL/dev/ttyUSB0::INSTR"),
    (115200, lambda: Scpi(b"Siglent Technologies,SDM3055,SDM,1.0", b"\r\n"), "SCPI:SDM3055",
     "ASRL/dev/ttyUSB0@115200::INSTR"),
    (57600, lambda: Scpi(b"KEITHLEY INSTRUMENTS,DMM6500,1,1"), "SCPI:DMM6500",
     "ASRL/dev/ttyUSB0@57600::INSTR"),
    (115200, suart.SimUart, "UART", "UART::/dev/ttyUSB0@115200"),
])
def test_recognised(baud, make, model, resource):
    port = At(baud, make)
    hit = idn.identify_port("/dev/ttyUSB0", port, quick=0.3 if make is not suart.SimUart else 1)
    assert hit is not None and (hit.model, hit.resource) == (model, resource), hit


def test_streaming_devices_are_sent_nothing():
    for baud, make in ((9600, Streaming393), (115200, suart.SimUart)):
        port = At(baud, make)
        assert idn.identify_port("/dev/ttyUSB0", port)
        assert port.sent == []


def test_silent_port_and_only_questions_are_sent():
    port = At(0, None)
    assert idn.identify_port("/dev/ttyUSB0", port, quick=0.02) is None
    allowed = {b"*IDN?", b"\r\n", b"*IDN?\r\n", b"*IDN?\n", b"A", bytes.fromhex("b1b2110000b6"),
               idn.appa_frame(idn.CMD_INFO)}
    assert {s for s in port.sent if not s.startswith(b"\x01\x03")} <= allowed
    assert {s[1] for s in port.sent if s.startswith(b"\x01\x03")} == {3}     # Modbus: read


def test_unknown_instrument_is_reported_not_guessed():
    hit = idn.identify_port("/dev/ttyUSB0", At(9600, lambda: Scpi(b"ACME,Frobnicator,1,2")),
                            quick=0.3)
    assert hit.text.startswith("ACME") and hit.model == "" and not hit.sure


def test_an_echo_is_not_an_answer():
    class Echo(sadapters._Link):
        def on_bytes(self, data):
            self._out += data
    for baud in (9600, 115200):
        assert idn.identify_port("/dev/ttyUSB0", At(baud, Echo), quick=0.1) is None


def test_cancel():
    stop = threading.Event()
    stop.set()
    with pytest.raises(idn.Cancelled):
        idn.identify_port("/dev/ttyUSB0", At(0, None), stop=stop)


def test_probeable_and_baud_in_visa_names():
    assert idn.probeable("ASRL/dev/ttyUSB0::INSTR") == "/dev/ttyUSB0"
    assert idn.probeable("ASRL/dev/ttyUSB0@115200::INSTR") == "/dev/ttyUSB0"
    assert idn.probeable("UART::/dev/serial/by-id/usb-x_1-if00") == "/dev/serial/by-id/usb-x_1-if00"
    for r in ("GPIB0::5::INSTR", "SIM::XDM", "PPK2::/dev/ttyACM0", "DPS150::/dev/ttyACM1",
              "TCPIP::1.2.3.4::INSTR", "W1::", "MAX31855::/dev/spidev0.0"):
        assert idn.probeable(r) == ""
    assert serial_path("ASRL/dev/ttyUSB0@115200::INSTR") == "/dev/ttyUSB0"
    assert parse_target("ASRL/dev/ttyUSB0@115200::INSTR")[0] == "auto"
    assert parse_target("SDM3055@ASRL/dev/ttyUSB0@115200::INSTR") == (
        "SCPI:SDM3055", "ASRL/dev/ttyUSB0@115200::INSTR")


def test_dialog():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    global _APP
    _APP = QApplication.instance() or QApplication([])
    from ee_workbench.gui.identify_dialog import IdentifyDialog
    d = IdentifyDialog(None, "/dev/ttyUSB0", At(9600, sadapters.SimKorad), quick=0.2)
    import time
    end = time.monotonic() + 8
    while d.found is None and time.monotonic() < end:
        _APP.processEvents()
        time.sleep(0.01)
    assert d.found.model == "SCPI:KA3005P" and d.use.isEnabled()
    assert "Korad" in d.text.text()
    d.accept()


_APP = None
