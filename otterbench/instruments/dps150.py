"""Driver for the FNIRSI DPS-150 USB-C bench supply (30 V / 5.1 A / 150 W).

The DPS-150 is a USB CDC serial device (VID 0x2E3C, PID 0x5740, an Artery
AT32 virtual COM port) with a binary protocol, not SCPI. FNIRSI publishes no
documentation; this follows the reverse-engineered descriptions in
cho45/fnirsi-dps-150 (dps-150.js) and Philipp-IoT/fnirsi_protocol.

Frame:  DIR START CMD LEN DATA[LEN] CHK,   CHK = (CMD + LEN + sum(DATA)) & 0xFF
  DIR    0xF1 host -> device, 0xF0 device -> host
  START  0xA1 query (and every device frame), 0xB1 write, 0xC1 connect,
         0xB0 baud rate
Values are little-endian float32, flags single bytes, names ASCII.

After connecting, the device pushes readings by itself (output V/I/P, input
voltage, temperature, ...). Each poll here also asks for the 139-byte
"all values" block, which carries the setpoints, protection thresholds,
output state, CC/CV mode and protection state, so a poll never depends on
the push timing.
"""
from __future__ import annotations

import struct
import time

from .base import SupplyCaps, SupplyPoll, SupplyReading, Setpoints

USB_VID, USB_PID = 0x2E3C, 0x5740
BAUD = 115200

HOST, DEVICE = 0xF1, 0xF0
START_GET, START_SET, START_CONNECT, START_BAUD = 0xA1, 0xB1, 0xC1, 0xB0
BAUDS = (9600, 19200, 38400, 57600, 115200)
DEVICE_STARTS = (START_GET, START_SET, START_CONNECT, START_BAUD)


class Reg:
    INPUT_VOLTAGE = 0xC0        # push, float
    VOLTAGE_SET = 0xC1
    CURRENT_SET = 0xC2
    OUTPUT_VIP = 0xC3           # push, 3 floats: V, I, P
    TEMPERATURE = 0xC4          # push, float
    OVP = 0xD1
    OCP = 0xD2
    OPP = 0xD3
    OTP = 0xD4
    LVP = 0xD5
    BRIGHTNESS = 0xD6
    VOLUME = 0xD7
    METERING = 0xD8
    CAPACITY = 0xD9             # Ah
    ENERGY = 0xDA               # Wh
    OUTPUT = 0xDB               # 1 on, 0 off
    PROTECTION = 0xDC           # index into PROTECTION_NAMES
    MODE = 0xDD                 # 0 CC, 1 CV
    MODEL_NAME = 0xDE
    HW_VERSION = 0xDF
    FW_VERSION = 0xE0
    UPPER_VOLTAGE = 0xE2        # highest settable output voltage (follows the input)
    UPPER_CURRENT = 0xE3
    ALL = 0xFF


# The app's flag names: "OV"/"OCP" get the panel's "press Clear trip" hint.
PROTECTION_NAMES = {1: "OV", 2: "OCP", 3: "OPP", 4: "OTP", 5: "LVP", 6: "REP"}

V_MAX, I_MAX, P_MAX = 30.0, 5.1, 150.0
WRITE_GAP_S = 0.05             # the reference client waits this long after every frame


def frame(start: int, cmd: int, data: bytes) -> bytes:
    """A host -> device frame."""
    data = bytes(data)
    chk = (cmd + len(data) + sum(data)) & 0xFF
    return bytes([HOST, start, cmd, len(data)]) + data + bytes([chk])


def f32(x: float) -> bytes:
    return struct.pack("<f", float(x))


def parse_frames(buf: bytearray) -> list[tuple[int, int, bytes]]:
    """Take every complete, checksum-valid frame off the front of `buf`
    (modified in place) as (start, cmd, data). Garbage is skipped one byte at
    a time; an incomplete frame at the end is left for the next call."""
    out = []
    i = 0
    n = len(buf)
    while i < n:
        if buf[i] != DEVICE:
            i += 1
            continue
        if n - i < 2:
            break
        if buf[i + 1] not in DEVICE_STARTS:     # a stray 0xF0: don't trust its "length"
            i += 1
            continue
        if n - i < 5:
            break
        length = buf[i + 3]
        end = i + 5 + length
        if end > n:
            break
        data = bytes(buf[i + 4:end - 1])
        if (buf[i + 2] + length + sum(data)) & 0xFF == buf[end - 1]:
            out.append((buf[i + 1], buf[i + 2], data))
            i = end
        else:
            i += 1
    del buf[:i]
    return out


# Offsets into the 139-byte ALL block (little-endian floats unless noted).
ALL_LEN = 139
ALL_FLOATS = {
    "vin": 0, "vset": 4, "iset": 8, "vout": 12, "iout": 16, "pout": 20, "temp": 24,
    "ovp": 76, "ocp": 80, "opp": 84, "otp": 88, "lvp": 92,
    "capacity": 99, "energy": 103, "upper_v": 111, "upper_i": 115,
}
ALL_BYTES = {"brightness": 96, "volume": 97, "metering": 98, "output": 107,
             "protection": 108, "mode": 109}


def parse_all(data: bytes) -> dict:
    if len(data) < 119:
        return {}
    d = {k: struct.unpack_from("<f", data, off)[0] for k, off in ALL_FLOATS.items()}
    d.update({k: data[off] for k, off in ALL_BYTES.items()})
    d["presets"] = [struct.unpack_from("<ff", data, 28 + 8 * k) for k in range(6)]
    return d


class DPS150:
    kind = "supply"
    family = "DPS150"

    def __init__(self, transport):
        self.t = transport
        self.caps = SupplyCaps()
        self.log = None
        self.st: dict = {}          # latest value of everything the device reported
        self._rx = bytearray()
        self._last_write = 0.0
        self._all_seq = 0           # bumped on every ALL block received

    # ---- framing ---------------------------------------------------------
    def _send(self, start: int, cmd: int, data: bytes = b"\x00"):
        pkt = frame(start, cmd, data)
        wait = self._last_write + WRITE_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        if self.log:
            self.log("> " + pkt.hex(" "))
        self.t.write(pkt)
        self._last_write = time.monotonic()

    def _set_float(self, reg: int, x: float):
        self._send(START_SET, reg, f32(x))

    def _set_byte(self, reg: int, b: int):
        self._send(START_SET, reg, bytes([b & 0xFF]))

    def _pump(self) -> None:
        """Read whatever has arrived and fold it into self.st."""
        chunk = self.t.read_available()
        if not chunk:
            return
        self._rx += chunk
        for start, cmd, data in parse_frames(self._rx):
            if self.log:
                self.log(f"< {cmd:02x} {data.hex(' ')}")
            self._absorb(cmd, data)

    def _absorb(self, cmd: int, data: bytes):
        st = self.st

        def fl(off=0):
            return struct.unpack_from("<f", data, off)[0] if len(data) >= off + 4 else None

        if cmd == Reg.ALL:
            st.update(parse_all(data))
            self._all_seq += 1
        elif cmd == Reg.OUTPUT_VIP and len(data) >= 12:
            st["vout"], st["iout"], st["pout"] = struct.unpack_from("<fff", data)
        elif cmd in (Reg.MODEL_NAME, Reg.HW_VERSION, Reg.FW_VERSION):
            name = {Reg.MODEL_NAME: "model", Reg.HW_VERSION: "hw", Reg.FW_VERSION: "fw"}[cmd]
            st[name] = data.decode("ascii", "replace").strip("\x00 ")
        elif cmd in (Reg.OUTPUT, Reg.PROTECTION, Reg.MODE, Reg.METERING) and data:
            st[{Reg.OUTPUT: "output", Reg.PROTECTION: "protection", Reg.MODE: "mode",
                Reg.METERING: "metering"}[cmd]] = data[0]
        else:
            key = {Reg.INPUT_VOLTAGE: "vin", Reg.TEMPERATURE: "temp",
                   Reg.CAPACITY: "capacity", Reg.ENERGY: "energy",
                   Reg.UPPER_VOLTAGE: "upper_v", Reg.UPPER_CURRENT: "upper_i",
                   Reg.VOLTAGE_SET: "vset", Reg.CURRENT_SET: "iset",
                   Reg.OVP: "ovp", Reg.OCP: "ocp", Reg.OPP: "opp", Reg.OTP: "otp",
                   Reg.LVP: "lvp"}.get(cmd)
            if key and fl() is not None:
                st[key] = fl()

    def refresh(self, timeout_s: float = 1.0) -> dict:
        """Ask for the ALL block and wait for it."""
        seq = self._all_seq
        self._send(START_GET, Reg.ALL)
        end = time.monotonic() + timeout_s
        while self._all_seq == seq:
            self._pump()
            if self._all_seq != seq:
                break
            if time.monotonic() > end:
                raise TimeoutError("The DPS-150 did not answer (is it switched on, and is "
                                   "another program using the port?)")
            time.sleep(0.01)
        return self.st

    def _wait_for(self, key: str, timeout_s: float = 0.5):
        end = time.monotonic() + timeout_s
        while key not in self.st and time.monotonic() < end:
            self._pump()
            time.sleep(0.01)
        return self.st.get(key)

    # ---- connection ------------------------------------------------------
    def identify(self) -> SupplyCaps:
        self.t.reset_input()
        self._send(START_CONNECT, 0x00, b"\x01")
        time.sleep(0.2)
        self._send(START_BAUD, 0x00, bytes([BAUDS.index(BAUD) + 1]))
        for reg in (Reg.MODEL_NAME, Reg.HW_VERSION, Reg.FW_VERSION):
            self._send(START_GET, reg)
        try:
            st = self.refresh(timeout_s=2.0)
        except TimeoutError:
            # one retry: the first frames after the port opens are sometimes lost
            self._send(START_CONNECT, 0x00, b"\x01")
            time.sleep(0.3)
            st = self.refresh(timeout_s=2.0)
        model = self._wait_for("model") or "DPS-150"
        hw, fw = st.get("hw", "?"), st.get("fw", "?")
        self.caps = SupplyCaps(
            idn=f"FNIRSI {model}  hw {hw}  fw {fw}", model=model,
            v_max=self._v_max(), i_min=0.0, i_max=I_MAX, ovp_max=V_MAX + 1,
            reset_label="Safe state",
            ovp_tip="Over-voltage protection: the output turns off above this voltage",
            note="Highest output voltage follows the input supply "
                 f"(now {st.get('upper_v', V_MAX):.2f} V from {st.get('vin', 0):.2f} V in). "
                 "OCP on trips the output at the current limit instead of regulating.",
            info={"Hardware": hw, "Firmware": fw})
        return self.caps

    def _v_max(self) -> float:
        up = self.st.get("upper_v")
        return min(V_MAX, up) if up and up > 0 else V_MAX

    def close(self):
        """Leave remote mode so the front panel works again."""
        try:
            self._send(START_CONNECT, 0x00, b"\x00")
        except Exception:       # noqa: BLE001 - unplugged
            pass

    def errors(self) -> list[str]:
        return []

    # ---- setpoints ---------------------------------------------------------
    def _ocp_on(self) -> bool:
        """OCP 'on' = the OCP threshold sits at the current limit."""
        ocp, iset = self.st.get("ocp"), self.st.get("iset")
        return ocp is not None and iset is not None and ocp <= iset + 0.005

    def read_setpoints(self) -> Setpoints:
        st = self.refresh()
        out = st.get("output")
        return Setpoints(voltage=st.get("vset"), current=st.get("iset"),
                         output=None if out is None else bool(out), ovp=st.get("ovp"),
                         ocp=self._ocp_on())

    def set_voltage(self, v: float):
        self._set_float(Reg.VOLTAGE_SET, max(0.0, min(v, self._v_max())))
        self.st["vset"] = v

    def set_current(self, a: float):
        a = max(0.0, min(a, I_MAX))
        tracking = self._ocp_on()
        self._set_float(Reg.CURRENT_SET, a)
        self.st["iset"] = a
        if tracking:
            self._set_float(Reg.OCP, a)
            self.st["ocp"] = a

    def set_output(self, on: bool):
        self._set_byte(Reg.OUTPUT, 1 if on else 0)
        self.st["output"] = 1 if on else 0

    def set_ovp(self, v: float):
        self._set_float(Reg.OVP, v)
        self.st["ovp"] = v

    def set_ocp(self, on: bool):
        a = self.st.get("iset", I_MAX) if on else I_MAX + 0.1
        self._set_float(Reg.OCP, a)
        self.st["ocp"] = a

    def set_protection(self, ovp=None, ocp=None, opp=None, otp=None, lvp=None):
        """Thresholds by value (V, A, W, °C, V); None leaves one as it is."""
        for reg, x in ((Reg.OVP, ovp), (Reg.OCP, ocp), (Reg.OPP, opp), (Reg.OTP, otp),
                       (Reg.LVP, lvp)):
            if x is not None:
                self._set_float(reg, x)

    def clear_protection(self):
        """There is no clear command; a trip latches the output off and
        turning it back on (after the cause is fixed) clears it."""
        self.set_output(False)

    def reset(self):
        """No reset command either: output off, 0 V, wide-open protection."""
        self.set_output(False)
        self.set_voltage(0.0)
        self.set_protection(ovp=V_MAX + 1, ocp=I_MAX + 0.1, opp=P_MAX + 5)

    # ---- console ---------------------------------------------------------------
    def raw(self, line: str, read: bool = True) -> str:
        """Console: hex bytes "START CMD [DATA...]" (LEN and checksum are added),
        e.g. "a1 de 00" asks for the model name, "b1 d6 0c" sets brightness 12.
        A line starting with f1 is sent exactly as typed. Returns what came back."""
        try:
            b = bytes.fromhex(line.replace("0x", "").replace(",", " "))
        except ValueError:
            return "hex bytes please: START CMD [DATA...], e.g.  a1 de 00"
        if len(b) < 2:
            return "need at least START and CMD, e.g.  a1 de 00"
        if b[0] == HOST:
            pkt = b
            if self.log:
                self.log("> " + pkt.hex(" "))
            self.t.write(pkt)
            self._last_write = time.monotonic()
        else:
            self._send(b[0], b[1], b[2:] or b"\x00")
        self._rx += self.t.read_available()   # keep any pushes that were waiting
        replies = []
        end = time.monotonic() + (0.4 if read else 0.0)
        while True:
            chunk = self.t.read_available()
            if chunk:
                self._rx += chunk
            for start, cmd, data in parse_frames(self._rx):
                self._absorb(cmd, data)
                if cmd not in (Reg.OUTPUT_VIP, Reg.INPUT_VOLTAGE, Reg.TEMPERATURE,
                               Reg.UPPER_VOLTAGE, Reg.UPPER_CURRENT):
                    replies.append(f"{cmd:02x}: {data.hex(' ')}")
            if time.monotonic() >= end:
                break
            time.sleep(0.02)
        return "  |  ".join(replies) or "(sent)"

    # ---- measurement ---------------------------------------------------------
    def poll(self, t: float, cfg: SupplyPoll) -> SupplyReading:
        st = self.refresh()
        out = st.get("output")
        r = SupplyReading(t=t, voltage=st.get("vout") if cfg.measure_voltage else None,
                          current=st.get("iout", 0.0), mode="--",
                          output=None if out is None else bool(out))
        prot = st.get("protection", 0)
        if prot:
            r.flags.append(PROTECTION_NAMES.get(prot, f"PROT{prot}"))
        if out == 0:
            r.mode = "OFF"
        elif st.get("mode") in (0, 1):
            r.mode = "CV" if st["mode"] == 1 else "CC"
        if self.caps.v_max != self._v_max():
            self.caps.v_max = self._v_max()
        return r
