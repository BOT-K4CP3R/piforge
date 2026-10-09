"""I2C and SPI bus models."""

from __future__ import annotations

import pytest

from piforge.core.errors import PiForgeError
from piforge.twin.bus import I2CBus, RegisterI2CDevice, SPIBus, SPIDevice
from piforge.twin.gpio import VirtualPi


def test_i2c_register_device_auto_increment():
    bus = I2CBus(1)
    dev = RegisterI2CDevice()
    dev.regs[0x10:0x13] = b"\x01\x02\x03"
    bus.attach(0x40, dev)
    assert bus.scan() == [0x40]
    bus.write(0x40, bytes([0x10]))
    assert bus.read(0x40, 3) == b"\x01\x02\x03"
    assert bus.write_read(0x40, bytes([0x11]), 2) == b"\x02\x03"
    bus.write(0x40, bytes([0x20, 0xAA, 0xBB]))
    assert dev.regs[0x20] == 0xAA and dev.regs[0x21] == 0xBB


@pytest.mark.parametrize("op", ["write", "read", "write_read"])
def test_i2c_absent_address_raises_oserror_121(op):
    bus = I2CBus(1)
    with pytest.raises(OSError) as ei:
        if op == "write":
            bus.write(0x50, b"\x00")
        elif op == "read":
            bus.read(0x50, 1)
        else:
            bus.write_read(0x50, b"\x00", 1)
    assert ei.value.errno == 121


def test_i2c_address_conflict_rejected():
    bus = I2CBus(1)
    bus.attach(0x76, RegisterI2CDevice())
    with pytest.raises(PiForgeError, match="0x76"):
        bus.attach(0x76, RegisterI2CDevice())


def test_i2c_invalid_address_rejected():
    with pytest.raises(PiForgeError):
        I2CBus(1).attach(0x90, RegisterI2CDevice())


class _Inverter(SPIDevice):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def begin(self) -> None:
        self.calls.append("begin")

    def transfer(self, data: bytes) -> bytes:
        self.calls.append("xfer")
        return bytes(b ^ 0xFF for b in data)

    def end(self) -> None:
        self.calls.append("end")


def test_spi_bus_transfer_and_missing_device():
    bus = SPIBus(0)
    dev = _Inverter()
    bus.attach(0, dev)
    assert bus.transfer(0, b"\x00\x0f") == b"\xff\xf0"
    assert dev.calls == ["begin", "xfer", "end"]
    assert bus.transfer(1, b"\x01\x02") == b"\x00\x00"     # nothing on CE1 → MISO reads 0


def test_spi_gpio_chip_select_frames_transactions():
    pi = VirtualPi()
    dev = _Inverter()
    pi.spi[0].attach(0, dev, cs_gpio=5)
    pi.write(5, 1)                                           # latch high first: no CS glitch
    pi.setup(5, "output")
    assert pi.spi[0].exchange(b"\x00") == b"\x00"          # not selected
    pi.write(5, 0)                                           # CS asserted by firmware
    assert pi.spi[0].exchange(b"\x0f") == b"\xf0"
    assert pi.spi[0].exchange(b"\xf0") == b"\x0f"
    pi.write(5, 1)
    assert dev.calls == ["begin", "xfer", "xfer", "end"]


def test_pi_exposes_buses():
    pi = VirtualPi()
    assert set(pi.i2c) >= {0, 1}
    assert set(pi.spi) >= {0, 1}
    assert pi.i2c_bus(3) is pi.i2c[3]        # extra buses are created on demand
