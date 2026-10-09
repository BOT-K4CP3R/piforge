"""Analog-to-digital converters: MCP3008 (8 channels, 10 bit, SPI)."""

from __future__ import annotations

from typing import Any

from piforge.twin.bus import SPIDevice
from piforge.twin.devices.base import Device, PropSpec, register


class _MCP3008Core(SPIDevice):
    """Bit-level MCP3008 protocol (works for byte-framed and bit-banged SPI).

    After CS falls the chip waits for a start bit, reads SGL/DIFF + D2 D1 D0, samples, then shifts
    out a null bit and B9…B0 MSB-first (and B1…B9 LSB-first if clocked further). DOUT changes on
    falling clock edges, so masters sampling on the rising edge (mode 0) and code that reads after
    the falling edge both see the datasheet waveform.  # src: Microchip MCP3008 DS21295 §5.0, fig. 6-1
    """

    def __init__(self, dev: "MCP3008") -> None:
        self.dev = dev
        self.reset()

    def reset(self) -> None:
        self._k = 0
        self._start: int | None = None
        self._cfg: list[int] = []
        self._result: int | None = None

    def out_bit(self) -> int:
        """DOUT for the upcoming clock (index ``self._k``)."""
        if self._start is None or self._result is None:
            return 0
        rel = self._k - self._start
        if 7 <= rel <= 16:
            return (self._result >> (16 - rel)) & 1
        if 17 <= rel <= 25:
            return (self._result >> (rel - 16)) & 1
        return 0                                                 # x / null bit / trailing zeros

    def shift_in(self, mosi: int) -> None:
        """Rising clock edge: sample DIN."""
        if self._start is None:
            if mosi:
                self._start = self._k
        else:
            rel = self._k - self._start
            if 1 <= rel <= 4:
                self._cfg.append(1 if mosi else 0)
                if rel == 4:
                    sgl, d2, d1, d0 = self._cfg
                    self._result = self.dev.convert(bool(sgl), (d2 << 2) | (d1 << 1) | d0)
        self._k += 1

    # byte-framed SPI (spidev, gpiozero hardware SPI, busio)
    def begin(self) -> None:
        self.reset()

    def transfer(self, data: bytes) -> bytes:
        out = bytearray()
        for byte in data:
            rx = 0
            for bit in range(7, -1, -1):
                rx |= self.out_bit() << bit
                self.shift_in((byte >> bit) & 1)
            out.append(rx)
        return bytes(out)


@register
class MCP3008(Device):
    """Microchip MCP3008 ADC: inputs ``ch0``…``ch7`` in volts, code = ⌊1024·Vin/Vref⌋ (clipped).

    Attach to hardware SPI (``bus={"kind":"spi","bus":0,"cs":0}``) or bit-banged pins
    (``pins={"clk","mosi","miso","cs"}``).  # src: MCP3008 DS21295 eq. 4-1, table 5-2 (diff pairs)
    """

    type = "mcp3008"
    label = "MCP3008 ADC"
    bus_kinds = ("spi",)
    pin_roles = ("clk", "mosi", "miso", "cs")
    pin_aliases = {"sclk": "clk", "sck": "clk", "din": "mosi", "dout": "miso", "shdn": "cs", "csshdn": "cs",
                   "ss": "cs", "ce": "cs"}
    defaults = {"vref": 3.3}
    inputs = {f"ch{i}": PropSpec("float", 0.0, 5.5, unit="V", default=0.0, label=f"CH{i}", widget="slider")
              for i in range(8)}
    outputs = {"conversions": PropSpec("int", 0, None, default=0, label="Conversions")}
    example = {"bus": {"kind": "spi", "bus": 0, "cs": 0}}

    def uses_pins(self, pins: dict[str, int]) -> bool:
        return bool(pins)

    def setup(self) -> None:
        self._count = 0
        self.core = _MCP3008Core(self)
        if self.pins:
            self.listen(self.pins["cs"], self._on_cs)
            self.listen(self.pins["clk"], self._on_clk)
        else:
            assert self.bus is not None
            self.pi.spi[self.bus["bus"]].attach(self.bus["cs"], self.core, cs_gpio=self.bus.get("cs_pin"))

    def convert(self, single: bool, sel: int) -> int:
        """10-bit result for single-ended channel ``sel`` or differential pair ``sel``."""
        vref = float(self.params.get("vref", 3.3))
        v = [float(self._inputs[f"ch{i}"]) for i in range(8)]
        if single:
            vin = v[sel]
        else:
            a, b = (sel & 6), (sel & 6) + 1                     # pairs (0,1),(2,3),(4,5),(6,7)
            vin = v[a] - v[b] if sel % 2 == 0 else v[b] - v[a]
        self._count += 1
        return max(0, min(1023, int(1024.0 * vin / vref)))

    # -- bit-banged mode ------------------------------------------------------------------------
    def _selected(self) -> bool:
        cs = self.pins["cs"]
        return self.pi.mode(cs) == "output" and self.pi.effective(cs) < 0.5

    def _on_cs(self, bcm: int, level: int, t: float) -> None:
        miso = self.pins["miso"]
        if self._selected():
            self.core.reset()
            self.pi.drive(miso, self.id, self.core.out_bit(), t=t)
        else:
            self.pi.drive(miso, self.id, None, t=t)             # DOUT high-Z when deselected

    def _on_clk(self, bcm: int, level: int, t: float) -> None:
        if not self._selected():
            return
        if level:
            self.core.shift_in(1 if self.pi.effective(self.pins["mosi"]) >= 0.5 else 0)
        else:
            self.pi.drive(self.pins["miso"], self.id, self.core.out_bit(), t=t)

    def outputs_state(self) -> dict:
        return {"conversions": self._count}

    def on_input(self, prop: str, value: Any) -> None:
        pass
