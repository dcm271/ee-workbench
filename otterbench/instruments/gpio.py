"""General-purpose I/O pins: the Raspberry Pi's header (or any Linux GPIO chip)
through libgpiod, so a sequence can press a DUT's buttons, switch a relay,
cycle power or wait for a "done" pin.

Pins are named by their GPIO (BCM) number, `GPIO17`, or by the name you give
them in the tab ("relay", "reset"). Each pin is Off (not touched), Output or
Input (with pull-up/-down). With `active_low` on, "on" drives the pin low,
as most relay boards want. Esc (all outputs off) makes every output inactive.

Works with python3-libgpiod 1.x (Raspberry Pi OS Bookworm) and 2.x. On a
Pi 5 the header is the chip labelled pinctrl-rp1, on earlier Pis
pinctrl-bcm2711 / -bcm2835; `find_chip` picks it. Lines are released when the
tab closes, which returns them to inputs.
"""
from __future__ import annotations

import glob
import os
import time
from dataclasses import dataclass, field

HEADER = {   # BCM GPIO -> physical header pin (40-pin header, all Pi models since B+)
    2: 3, 3: 5, 4: 7, 17: 11, 27: 13, 22: 15, 10: 19, 9: 21, 11: 23, 5: 29, 6: 31, 13: 33,
    19: 35, 26: 37, 14: 8, 15: 10, 18: 12, 23: 16, 24: 18, 25: 22, 8: 24, 7: 26, 12: 32,
    16: 36, 20: 38, 21: 40,
}
PI_LABELS = ("pinctrl-rp1", "pinctrl-bcm2711", "pinctrl-bcm2835", "pinctrl-bcm2712")
MODES = ("off", "out", "in")
PULLS = ("none", "up", "down")


@dataclass
class PinConfig:
    gpio: int
    mode: str = "off"
    name: str = ""
    pull: str = "none"
    active_low: bool = False

    @property
    def label(self) -> str:
        return self.name or f"GPIO{self.gpio}"


@dataclass
class GpioCaps:
    idn: str = ""
    note: str = ""
    chip: str = ""
    lines: list = field(default_factory=list)     # usable GPIO numbers
    model: str = "GPIO"


@dataclass
class GpioState:
    pins: dict = field(default_factory=dict)      # gpio -> PinConfig
    outputs: dict = field(default_factory=dict)   # gpio -> bool (logical, last set)


@dataclass
class GpioReading:
    t: float
    levels: dict = field(default_factory=dict)    # gpio -> 0/1 (logical) for in and out pins
    errors: list = field(default_factory=list)


def is_raspberry_pi() -> bool:
    try:
        with open("/proc/device-tree/model", encoding="utf-8") as fh:
            return "raspberry pi" in fh.read().lower()
    except OSError:
        return False


def pi_model() -> str:
    try:
        with open("/proc/device-tree/model", encoding="utf-8") as fh:
            return fh.read().strip("\x00\n ")
    except OSError:
        return ""


def _gpiod():
    try:
        import gpiod
        return gpiod
    except ImportError:
        raise RuntimeError("The libgpiod Python module is missing: sudo apt install "
                           "python3-libgpiod   (then recreate .venv, or re-run the setup "
                           "script)") from None


def chip_label(path: str) -> str:
    """The label of a gpiochip (e.g. pinctrl-rp1), from sysfs so no module is needed."""
    name = os.path.basename(path)
    for p in (f"/sys/bus/gpio/devices/{name}/label", f"/sys/class/gpio/{name}/label"):
        try:
            with open(p, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            pass
    try:
        gpiod = _gpiod()
        if hasattr(gpiod, "request_lines"):
            with gpiod.Chip(path) as c:
                return c.get_info().label
        c = gpiod.Chip(path)
        try:
            return c.label()
        finally:
            c.close()
    except Exception:
        return ""


def find_chip() -> str | None:
    """/dev/gpiochipN of the Pi's 40-pin header, if this is a Pi."""
    chips = sorted(glob.glob("/dev/gpiochip*"), key=lambda p: int(p[13:] or 0))
    for c in chips:
        if chip_label(c) in PI_LABELS:
            return c
    return chips[0] if chips and is_raspberry_pi() else None


def find_gpio() -> list[str]:
    chip = find_chip()
    return [f"GPIO::{chip}"] if chip else []


def resource_chip(resource: str) -> str:
    r = resource.strip()
    rest = r[6:] if r.upper().startswith("GPIO::") else r
    if not rest:
        return find_chip() or ""
    return rest if rest.startswith("/dev/") else f"/dev/{rest}"


# ---------------------------------------------------------------- line access
class GpioLink:
    """One requested line per configured pin, on libgpiod 1.x or 2.x."""

    def __init__(self, chip: str):
        self.chip = chip
        if not chip:
            raise RuntimeError("No GPIO chip found (this isn't a Raspberry Pi?). Name one: "
                               "GPIO::/dev/gpiochip0")
        if not os.path.exists(chip):
            raise RuntimeError(f"{chip} doesn't exist")
        if not os.access(chip, os.R_OK | os.W_OK):
            raise RuntimeError(f"No permission for {chip}: add yourself to the 'gpio' group "
                               "(sudo usermod -aG gpio $USER, then log in again)")
        self.gpiod = _gpiod()
        self.v2 = hasattr(self.gpiod, "request_lines")
        self._req: dict[int, object] = {}
        self.timeout_s = 2.0

    def info(self) -> tuple[str, int]:
        g = self.gpiod
        if self.v2:
            with g.Chip(self.chip) as c:
                i = c.get_info()
                return i.label, i.num_lines
        c = g.Chip(self.chip)
        try:
            return c.label(), c.num_lines()
        finally:
            c.close()

    def configure(self, pin: PinConfig, value: bool = False):
        self.release(pin.gpio)
        if pin.mode == "off":
            return
        g = self.gpiod
        if self.v2:
            from gpiod.line import Bias, Direction, Value
            bias = {"none": Bias.AS_IS, "up": Bias.PULL_UP, "down": Bias.PULL_DOWN}[pin.pull]
            if pin.mode == "out":
                ls = g.LineSettings(direction=Direction.OUTPUT, active_low=pin.active_low,
                                    output_value=Value.ACTIVE if value else Value.INACTIVE)
            else:
                ls = g.LineSettings(direction=Direction.INPUT, active_low=pin.active_low,
                                    bias=bias)
            self._req[pin.gpio] = g.request_lines(self.chip, consumer="otterbench",
                                                  config={pin.gpio: ls})
        else:
            chip = g.Chip(self.chip)
            line = chip.get_line(pin.gpio)
            flags = g.LINE_REQ_FLAG_ACTIVE_LOW if pin.active_low else 0
            if pin.mode == "in" and pin.pull != "none":
                flags |= {"up": getattr(g, "LINE_REQ_FLAG_BIAS_PULL_UP", 0),
                          "down": getattr(g, "LINE_REQ_FLAG_BIAS_PULL_DOWN", 0)}[pin.pull]
            kind = g.LINE_REQ_DIR_OUT if pin.mode == "out" else g.LINE_REQ_DIR_IN
            line.request(consumer="otterbench", type=kind, flags=flags,
                         default_val=int(bool(value)))
            self._req[pin.gpio] = (chip, line)

    def set(self, gpio: int, value: bool):
        r = self._req[gpio]
        if self.v2:
            from gpiod.line import Value
            r.set_value(gpio, Value.ACTIVE if value else Value.INACTIVE)
        else:
            r[1].set_value(int(bool(value)))

    def get(self, gpio: int) -> int:
        r = self._req[gpio]
        if self.v2:
            from gpiod.line import Value
            return int(r.get_value(gpio) == Value.ACTIVE)
        return int(r[1].get_value())

    def release(self, gpio: int):
        r = self._req.pop(gpio, None)
        if r is None:
            return
        try:
            if self.v2:
                r.release()
            else:
                r[1].release()
                r[0].close()
        except Exception:
            pass

    # what the worker expects of every transport
    def clear(self):
        pass

    def read_stb(self) -> int:
        return 0

    def close(self):
        for g in list(self._req):
            self.release(g)


# ---------------------------------------------------------------- driver
class GpioDriver:
    kind = "gpio"
    family = "gpio"

    def __init__(self, transport):
        self.t = transport
        self.caps = GpioCaps()
        self.state = GpioState()
        self.log = None

    @property
    def cfg(self) -> GpioState:
        return GpioState({g: PinConfig(**vars(p)) for g, p in self.state.pins.items()},
                         dict(self.state.outputs))

    def errors(self):
        return []

    def _log(self, s):
        if self.log:
            self.log(s)

    def identify(self) -> GpioCaps:
        label, n = self.t.info()
        pi = label in PI_LABELS or getattr(self.t, "simulated", False)
        lines = sorted(HEADER) if pi else list(range(n))
        model = pi_model() if not getattr(self.t, "simulated", False) else "Raspberry Pi (simulated)"
        self.caps = GpioCaps(
            idn=f"{model or 'GPIO'}  {self.t.chip} ({label}, {n} lines)",
            note="40-pin header, numbered by GPIO (BCM)" if pi else "all lines of the chip",
            chip=self.t.chip, lines=lines, model="GPIO")
        for g in lines:
            self.state.pins.setdefault(g, PinConfig(g))
        return self.caps

    def configure(self, pins: list[PinConfig]):
        """Apply pin settings (only pins whose settings changed are touched)."""
        for p in pins:
            old = self.state.pins.get(p.gpio)
            if old is not None and vars(old) == vars(p):
                continue
            value = self.state.outputs.get(p.gpio, False) if p.mode == "out" else False
            self._log(f"GPIO{p.gpio}: {p.mode}" + (f" pull-{p.pull}" if p.mode == "in" else "")
                      + (" active-low" if p.active_low else ""))
            self.t.configure(p, value)
            self.state.pins[p.gpio] = PinConfig(**vars(p))
            if p.mode == "out":
                self.state.outputs[p.gpio] = value
            else:
                self.state.outputs.pop(p.gpio, None)

    def pin(self, what) -> PinConfig:
        """A pin by name, 'GPIO17', 'gpio17' or 17."""
        s = str(what).strip()
        for p in self.state.pins.values():
            if p.name and p.name.lower() == s.lower():
                return p
        num = s[4:] if s.lower().startswith("gpio") else s
        try:
            g = int(num)
        except ValueError:
            names = [p.label for p in self.state.pins.values() if p.mode != "off"]
            raise ValueError(f"No pin called '{s}'" + (f" (configured: {', '.join(names)})"
                                                         if names else "")) from None
        if g not in self.state.pins:
            raise ValueError(f"GPIO{g} isn't available here")
        return self.state.pins[g]

    def set_pin(self, what, on: bool):
        p = self.pin(what)
        if p.mode != "out":
            raise ValueError(f"{p.label} isn't set up as an output (its tab says {p.mode})")
        self._log(f"{p.label} = {'on' if on else 'off'}")
        self.t.set(p.gpio, bool(on))
        self.state.outputs[p.gpio] = bool(on)

    def pulse(self, what, seconds: float):
        """Invert an output for `seconds`, then put it back."""
        p = self.pin(what)
        was = self.state.outputs.get(p.gpio, False)
        self.set_pin(p.gpio, not was)
        time.sleep(max(0.0, min(float(seconds), 30.0)))
        self.set_pin(p.gpio, was)

    def get_pin(self, what) -> int:
        p = self.pin(what)
        if p.mode == "off":
            raise ValueError(f"{p.label} is off; set it to Input or Output first")
        return self.t.get(p.gpio)

    def set_output(self, on: bool):
        """'All outputs off': every output to inactive (on is ignored when True)."""
        if on:
            return
        for g, p in self.state.pins.items():
            if p.mode == "out":
                self.t.set(g, False)
                self.state.outputs[g] = False

    def poll(self, t: float, _cfg=None) -> GpioReading:
        levels = {}
        for g, p in self.state.pins.items():
            if p.mode in ("in", "out"):
                levels[g] = self.t.get(g)
        return GpioReading(t, levels)

    def close(self):
        self.t.close()
