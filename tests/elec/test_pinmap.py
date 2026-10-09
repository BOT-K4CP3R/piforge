"""Pin allocator: fixed-function interfaces, hardware PWM channels, config.txt lines."""

from __future__ import annotations

import pytest

from piforge.core.errors import NotFoundError, PiForgeError, ValidationError
from piforge.elec.pinmap import AllocationError, PinAllocation, Requirement, allocate_pins

HW_PWM = {12, 13, 18, 19}


def _all_pins(a: PinAllocation) -> list[int]:
    return [p for pins in a.assignments.values() for p in pins]


@pytest.mark.parametrize("board", ["rpi4b", "rpi5", "rpi3bp", "rpizero2w"])
def test_allocate_pins(board):
    reqs = [
        Requirement("i2c", label="sensors"),
        Requirement("spi", count=2, label="adc"),
        Requirement("pwm", count=2, label="servos"),
        Requirement("gpio_in", count=3, label="buttons"),
    ]
    a = allocate_pins(board, reqs)
    assert isinstance(a, PinAllocation)
    pins = _all_pins(a)
    assert len(pins) == len(set(pins)), "no overlaps"
    assert a.assignments["sensors"] == (2, 3)                    # SDA, SCL
    assert a.assignments["adc"] == (10, 9, 11, 8, 7)             # MOSI, MISO, SCLK, CE0, CE1
    assert set(a.assignments["servos"]) <= HW_PWM and len(a.assignments["servos"]) == 2
    assert len(a.assignments["buttons"]) == 3
    assert not set(pins) & {0, 1}, "ID EEPROM pins reserved"
    assert "dtparam=i2c_arm=on" in a.config_lines and "dtparam=spi=on" in a.config_lines
    assert any(line.startswith("dtoverlay=pwm-2chan") for line in a.config_lines)
    assert set(a.interfaces) >= {"i2c1", "spi0", "pwm"}
    assert set(a.free).isdisjoint(pins) and 0 not in a.free
    assert len(a.free) + len(pins) == 26  # 28 GPIOs minus the 2 reserved


def test_pwm_channels_are_distinct_on_bcm():
    a = allocate_pins("rpi4b", [Requirement("pwm", 2)])
    p = set(a.assignments["pwm"])
    # GPIO12/18 share PWM0 channel 0 and GPIO13/19 share channel 1 on the BCM2711
    assert len(p & {12, 18}) == 1 and len(p & {13, 19}) == 1
    with pytest.raises(AllocationError):
        allocate_pins("rpi4b", [Requirement("pwm", 3)])  # only two channels on the header


def test_impossible_requests():
    with pytest.raises(AllocationError):
        allocate_pins("rpi4b", [Requirement("pwm", count=5)])
    with pytest.raises(AllocationError):
        allocate_pins("rpi5", [Requirement("pwm", count=5)])
    with pytest.raises(AllocationError):
        allocate_pins("rpi4b", [Requirement("gpio_out", count=27)])
    assert issubclass(AllocationError, PiForgeError)
    with pytest.raises(ValidationError):
        allocate_pins("rpi4b", [Requirement("teleport")])
    with pytest.raises(NotFoundError):
        allocate_pins("rpi9", [])


def test_pi5_has_four_pwm_channels():
    a = allocate_pins("rpi5", [Requirement("pwm", 4)])
    assert set(a.assignments["pwm"]) == HW_PWM


def test_pcm_forces_pwm_onto_12_13():
    a = allocate_pins("rpi4b", [Requirement("pcm"), Requirement("pwm", 2)])
    assert a.assignments["pcm"] == (18, 19, 20, 21)
    assert set(a.assignments["pwm"]) == {12, 13}
    assert "dtparam=i2s=on" in a.config_lines


def test_onewire_uart_and_prefer():
    a = allocate_pins("rpi4b", [Requirement("onewire"), Requirement("uart"),
                                Requirement("gpio_out", 2, label="leds", prefer=(17, 27))])
    assert a.assignments["onewire"] == (4,)
    assert a.assignments["uart"] == (14, 15)
    assert a.assignments["leds"] == (17, 27)
    assert "dtoverlay=w1-gpio,gpiopin=4" in a.config_lines
    assert "enable_uart=1" in a.config_lines
    a5 = allocate_pins("rpi5", [Requirement("uart")])
    assert "dtoverlay=uart0-pi5" in a5.config_lines


def test_spi_three_chip_selects_uses_spi1():
    a = allocate_pins("rpi4b", [Requirement("spi", 3, label="disp")])
    assert a.assignments["disp"] == (20, 19, 21, 18, 17, 16)
    assert "dtoverlay=spi1-3cs" in a.config_lines
    one = allocate_pins("rpi4b", [Requirement("spi", 1)])
    assert one.assignments["spi"] == (10, 9, 11, 8)
    assert "dtoverlay=spi0-1cs" in one.config_lines and 7 in one.free


def test_reserved_and_labels():
    a = allocate_pins("rpi4b", [Requirement("gpio_in", 2), Requirement("gpio_in", 1)], reserved=(0, 1, 17))
    assert 17 not in _all_pins(a)
    assert set(a.assignments) == {"gpio_in", "gpio_in_2"}


def test_second_uart_and_second_i2c():
    a = allocate_pins("rpi4b", [Requirement("uart"), Requirement("uart", label="gps"),
                                Requirement("i2c"), Requirement("i2c", label="bus2")])
    assert a.assignments["uart"] == (14, 15) and a.assignments["gps"] == (4, 5)  # BCM2711 UART3 (ALT4)
    assert "dtoverlay=uart3" in a.config_lines
    sda, scl = a.assignments["bus2"]
    assert any(line == f"dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda={sda},i2c_gpio_scl={scl}" for line in a.config_lines)
    a5 = allocate_pins("rpi5", [Requirement("uart"), Requirement("uart")])
    assert "dtoverlay=uart2-pi5" in a5.config_lines
    with pytest.raises(AllocationError):
        allocate_pins("rpi3bp", [Requirement("uart"), Requirement("uart")])  # only UART0 on the header
