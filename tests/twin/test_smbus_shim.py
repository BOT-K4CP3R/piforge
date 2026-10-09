"""``smbus2`` / ``smbus`` / ``spidev`` shims against twin bus devices."""

from __future__ import annotations

import pytest

from piforge.twin.config import DeviceConfig

BME = DeviceConfig("ENV1", "bme280", bus={"kind": "i2c", "bus": 1, "address": 0x76})


def test_smbus_shim(shims, make_twin):
    make_twin(BME)
    from smbus2 import SMBus

    with SMBus(1) as bus:
        assert bus.read_byte_data(0x76, 0xD0) == 0x60    # BME280 chip id  # src: BME280 DS §5.4.1
        with pytest.raises(OSError) as ei:
            bus.read_byte_data(0x50, 0x00)               # nothing at 0x50
        assert ei.value.errno == 121
    import smbus

    legacy = smbus.SMBus(1)
    assert legacy.read_byte_data(0x76, 0xD0) == 0x60
    legacy.close()


def test_smbus2_block_word_and_rdwr(shims, make_twin):
    make_twin(BME)
    from smbus2 import SMBus, i2c_msg

    bus = SMBus("/dev/i2c-1")
    calib = bus.read_i2c_block_data(0x76, 0x88, 24)
    assert isinstance(calib, list) and len(calib) == 24
    word = bus.read_word_data(0x76, 0x88)                  # little-endian dig_T1
    assert word == calib[0] | (calib[1] << 8)
    bus.write_byte_data(0x76, 0xF2, 0x01)
    assert bus.read_byte_data(0x76, 0xF2) == 0x01
    w = i2c_msg.write(0x76, [0xD0])
    r = i2c_msg.read(0x76, 1)
    bus.i2c_rdwr(w, r)
    assert list(r) == [0x60]
    assert bus.read_byte(0x76) in range(256)
    bus.close()


def test_smbus_scan_like_i2cdetect(shims, make_twin):
    make_twin(BME, DeviceConfig("OLED1", "ssd1306", bus={"kind": "i2c", "bus": 1, "address": 0x3C}))
    from smbus2 import SMBus

    found = []
    with SMBus(1) as bus:
        for addr in range(0x03, 0x78):
            try:
                bus.write_quick(addr)
                found.append(addr)
            except OSError:
                pass
    assert found == [0x3C, 0x76]


def test_spidev_shim_with_mcp3008(shims, make_twin):
    twin = make_twin(DeviceConfig("ADC1", "mcp3008", bus={"kind": "spi", "bus": 0, "cs": 0}))
    twin.set_input("ADC1", "ch1", 3.3)
    import spidev

    spi = spidev.SpiDev()
    spi.open(0, 0)
    spi.max_speed_hz = 1_000_000
    spi.mode = 0
    rx = spi.xfer2([1, (8 + 1) << 4, 0])
    assert ((rx[1] & 3) << 8) | rx[2] == 1023
    rx = spi.xfer([1, (8 + 0) << 4, 0])
    assert ((rx[1] & 3) << 8) | rx[2] == 0
    spi.close()
    with pytest.raises(OSError):
        spidev.SpiDev().open(7, 0)                         # no /dev/spidev7.0
