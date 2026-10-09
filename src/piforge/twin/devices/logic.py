"""Logic ICs: 74HC595 / 74HCT595 serial-in parallel-out shift-register chain.

Bit numbering (binding for ``bits`` and ``coil_source``): bit ``i`` is output ``Q(i % 8)`` of chip
``i // 8``; chip 0 is the one whose SER input is wired to the Pi (nearest the Pi), Q0 = QA … Q7 = QH.
Daisy chain semantics: data enters chip 0 at QA and moves towards QH, QH' feeds the next chip's SER.
So for one SPI transfer of ``N`` bytes (MSB first, as spidev sends them) into an ``N``-chip chain the
**first byte ends up in the last chip** and the last byte in chip 0: ``bits = int.from_bytes(data,
"big")``. Shorter transfers shift the old contents further down the chain; longer ones push the
first bytes out of QH' of the last chip.  # src: TI SN74HC595 (SCLS041J) fig. "Logic diagram", timing diagram
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from piforge.core.errors import ValidationError
from piforge.twin.bus import SPIDevice
from piforge.twin.devices.base import Device, PropSpec, register

BitsListener = Callable[[int, float], None]


class _SRCore(SPIDevice):
    """SPI side: bytes shift into the chain, chip-select release (RCLK on CE0) latches."""

    def __init__(self, dev: "ShiftRegister74HC595") -> None:
        self.dev = dev

    def transfer(self, data: bytes) -> bytes:
        out = bytearray()
        for byte in data:
            out.append(self.dev.shift_byte(byte))
        return bytes(out)

    def end(self) -> None:
        if "latch" not in self.dev.pins:           # RCLK tied to CE0: the CS rising edge latches
            self.dev.latch()


@register
class ShiftRegister74HC595(Device):
    """74HC595 chain of ``length`` chips (outputs ``bits``, ``bytes``).

    Two wirings:

    * **SPI** (default ``bus={"kind": "spi", "bus": 0, "cs": 0}``): MOSI → SER, SCLK → SRCLK and RCLK on
      the chip-select line (CE0 = GPIO8), so every ``spidev`` ``xfer``/``writebytes`` latches on CS
      release. With an optional ``latch`` pin (RCLK on a plain GPIO) the rising edge of that pin latches
      instead.
    * **bit-banged** ``pins={"data", "clock", "latch"}`` (gpiozero ``OutputDevice``/``RPi.GPIO``): SER
      sampled on each SRCLK rising edge, storage register loaded on each RCLK rising edge.

    Optional ``oe`` pin: OE is active-low; while it is HIGH the outputs are off (``bits`` reads 0, the
    storage register keeps its value). See the module docstring for the bit numbering.
    """

    type = "shift_register_74hc595"
    label = "74HC595 shift register chain"
    bus_kinds = ("spi",)
    pin_roles = ("data", "clock", "latch")
    optional_pins = ("oe",)
    pin_aliases = {"ser": "data", "ds": "data", "din": "data", "mosi": "data", "sdi": "data",
                   "srclk": "clock", "shcp": "clock", "sck": "clock", "sclk": "clock", "clk": "clock",
                   "rclk": "latch", "stcp": "latch", "cs": "latch", "ce": "latch", "le": "latch",
                   "oe_n": "oe", "noe": "oe", "g": "oe"}
    defaults = {"length": 1}
    outputs = {"bits": PropSpec("int", 0, None, default=0, label="Outputs (bit i = chip i//8, Q i%8)"),
               "bytes": PropSpec("text", default="00", label="Chip bytes (chip 0 first, hex)"),
               "latches": PropSpec("int", 0, None, default=0, label="Latch count"),
               "enabled": PropSpec("bool", default=True, label="Outputs enabled (OE low)")}
    example = {"bus": {"kind": "spi", "bus": 0, "cs": 0}, "params": {"length": 4}}

    # -- wiring -----------------------------------------------------------------------------------
    def uses_pins(self, pins: dict[str, int]) -> bool:
        return "data" in pins or "clock" in pins            # bit-banged; latch/oe alone go with SPI

    def needs_bus(self) -> bool:
        return not self.uses_pins(self.pins)

    def setup(self) -> None:
        n = self.params.get("length", 1)
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 64:
            raise ValidationError(f"{self.id} ({self.type}): length must be an integer 1…64 chips, got {n!r}")
        self.length = n
        self._mask = (1 << (8 * n)) - 1
        self._shift = 0                 # shift register contents (same bit numbering as ``bits``)
        self._latched = 0               # storage register
        self._latches = 0
        self._listeners: list[BitsListener] = []
        if self.uses_pins(self.pins):
            self.listen(self.pins["clock"], self._on_clock)
        else:
            assert self.bus is not None
            self.pi.spi[self.bus["bus"]].attach(self.bus["cs"], _SRCore(self), cs_gpio=self.bus.get("cs_pin"))
        if "latch" in self.pins:
            self.listen(self.pins["latch"], self._on_latch)
        if "oe" in self.pins:
            self.listen(self.pins["oe"], self._on_oe)

    # -- shifting ---------------------------------------------------------------------------------
    def shift_bit(self, bit: int) -> None:
        """One SRCLK rising edge with SER = ``bit``."""
        self._shift = ((self._shift << 1) | (1 if bit else 0)) & self._mask

    def shift_byte(self, byte: int) -> int:
        """Eight clocks MSB first; returns the byte that left QH' of the last chip (MISO if wired)."""
        out = (self._shift >> (8 * self.length - 8)) & 0xFF
        self._shift = ((self._shift << 8) | (byte & 0xFF)) & self._mask
        return out

    def latch(self, t: float | None = None) -> None:
        """RCLK rising edge: copy the shift register to the outputs and notify listeners."""
        self._latched = self._shift
        self._latches += 1
        self._notify(t)

    def _notify(self, t: float | None) -> None:
        when = self.pi.clock.now() if t is None else t
        bits = self.output_bits()
        for cb in list(self._listeners):
            cb(bits, when)

    def _level(self, role: str) -> int:
        bcm = self.pins[role]
        return 1 if self.pi.effective(bcm) >= 0.5 else 0

    def _on_clock(self, bcm: int, level: int, t: float) -> None:
        if level:
            self.shift_bit(self._level("data"))

    def _on_latch(self, bcm: int, level: int, t: float) -> None:
        if level:
            self.latch(t)

    def _on_oe(self, bcm: int, level: int, t: float) -> None:
        self._notify(t)

    # -- outputs ----------------------------------------------------------------------------------
    def enabled(self) -> bool:
        """False while OE (active low) is driven HIGH."""
        return "oe" not in self.pins or self._level("oe") == 0

    def output_bits(self) -> int:
        """Levels of all Q outputs as one int (0 while OE is high)."""
        return self._latched if self.enabled() else 0

    def bit(self, i: int) -> int:
        """Level of output ``i`` (chip ``i // 8``, Q ``i % 8``)."""
        return (self.output_bits() >> int(i)) & 1

    def add_bits_listener(self, cb: BitsListener) -> None:
        """``cb(bits, t)`` after every latch / OE change (called under ``pi.lock``; keep it quick)."""
        self._listeners.append(cb)

    def remove_bits_listener(self, cb: BitsListener) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def outputs_state(self) -> dict:
        bits = self.output_bits()
        chips = " ".join(f"{(bits >> (8 * i)) & 0xFF:02x}" for i in range(self.length))
        return {"bits": bits, "bytes": chips, "latches": self._latches, "enabled": self.enabled()}

    def on_input(self, prop: str, value: Any) -> None:  # pragma: no cover - no inputs
        pass
