"""Real, unmodified gpiozero driving twin devices through :class:`TwinFactory` (real-time clock)."""

from __future__ import annotations

import warnings

import pytest

from piforge.twin.config import DeviceConfig


def test_gpiozero_led_button(gpiozero_twin, wait_until):
    from gpiozero import LED, Button

    twin = gpiozero_twin(DeviceConfig("SW1", "button", {"pin": 27}), DeviceConfig("D1", "led", {"pin": 17}))
    led = LED(17)
    button = Button(27)
    button.when_pressed = led.on
    button.when_released = led.off
    assert not button.is_pressed and not led.is_lit
    twin.set_input("SW1", "pressed", True)
    assert wait_until(lambda: led.is_lit)
    assert button.is_pressed
    assert twin.devices["D1"].state()["brightness"] == 1.0
    twin.set_input("SW1", "pressed", False)
    assert wait_until(lambda: not led.is_lit)
    assert twin.devices["D1"].state()["brightness"] == 0.0
    led.close()
    button.close()


def test_gpiozero_servo(gpiozero_twin, wait_until):
    from gpiozero import AngularServo

    twin = gpiozero_twin(DeviceConfig("SRV1", "servo", {"pin": 18}))
    servo = AngularServo(18, min_angle=-90, max_angle=90)
    servo.angle = 45
    dev = twin.devices["SRV1"]
    assert wait_until(lambda: abs(dev.state()["angle"] - 45) <= 2, timeout=3.0), dev.state()
    servo.angle = -30
    assert wait_until(lambda: abs(dev.state()["angle"] + 30) <= 2, timeout=3.0), dev.state()
    servo.close()


def test_gpiozero_distance(gpiozero_twin):
    from gpiozero import DistanceSensor

    twin = gpiozero_twin(DeviceConfig("US1", "hcsr04", {"trigger": 23, "echo": 24}))
    twin.set_input("US1", "distance", 0.5)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")      # "use pigpio for accuracy" fallback warning
        sensor = DistanceSensor(echo=24, trigger=23)
        try:
            assert sensor.distance == pytest.approx(0.5, abs=0.05)
            twin.set_input("US1", "distance", 0.2)
            import time
            time.sleep(1.0)                  # let the 9-sample median queue refill
            assert sensor.distance == pytest.approx(0.2, abs=0.05)
        finally:
            sensor.close()


def test_mcp3008_gpiozero(gpiozero_twin):
    from gpiozero import MCP3008

    twin = gpiozero_twin(DeviceConfig("ADC1", "mcp3008", bus={"kind": "spi", "bus": 0, "cs": 0}))
    twin.set_input("ADC1", "ch0", 1.65)
    twin.set_input("ADC1", "ch5", 0.825)
    adc0 = MCP3008(0)
    adc5 = MCP3008(5)
    assert adc0.value == pytest.approx(0.5, abs=0.01)
    assert adc5.value == pytest.approx(0.25, abs=0.01)
    assert adc0.voltage == pytest.approx(1.65, abs=0.02)
    adc0.close()
    adc5.close()


def test_mcp3008_gpiozero_software_spi(gpiozero_twin):
    """Bit-banged SPI on arbitrary pins (gpiozero falls back to software SPI)."""
    from gpiozero import MCP3008

    twin = gpiozero_twin(DeviceConfig("ADC2", "mcp3008", {"clk": 21, "mosi": 20, "miso": 19, "cs": 26}))
    twin.set_input("ADC2", "ch2", 2.475)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        adc = MCP3008(2, clock_pin=21, mosi_pin=20, miso_pin=19, select_pin=26)
    assert adc.value == pytest.approx(0.75, abs=0.01)
    adc.close()


def test_gpiozero_pwmled_and_rgbled(gpiozero_twin, wait_until):
    from gpiozero import PWMLED, RGBLED

    twin = gpiozero_twin(DeviceConfig("D1", "led", {"pin": 12}),
                         DeviceConfig("RGB1", "rgb_led", {"red": 5, "green": 6, "blue": 13}))
    led = PWMLED(12)
    led.value = 0.3
    assert twin.devices["D1"].state()["brightness"] == pytest.approx(0.3, abs=0.01)
    rgb = RGBLED(5, 6, 13)
    rgb.color = (1, 0, 1)
    assert twin.devices["RGB1"].state()["color"] == "#ff00ff"
    led.close()
    rgb.close()


def test_gpiozero_motor(gpiozero_twin, wait_until):
    from gpiozero import Motor

    twin = gpiozero_twin(DeviceConfig("MOT1", "dc_motor", {"in1": 5, "in2": 6, "pwm": 13}))
    motor = Motor(forward=5, backward=6, enable=13)
    motor.forward(1.0)
    dev = twin.devices["MOT1"]
    assert wait_until(lambda: dev.state()["speed"] > 0.9, timeout=3.0), dev.state()
    motor.backward(0.5)
    assert wait_until(lambda: abs(dev.state()["speed"] + 0.5) < 0.05, timeout=3.0), dev.state()
    motor.close()


def test_gpiozero_rotary_encoder(gpiozero_twin, wait_until):
    from gpiozero import RotaryEncoder

    twin = gpiozero_twin(DeviceConfig("ENC1", "rotary_encoder", {"a": 20, "b": 21}))
    enc = RotaryEncoder(20, 21, max_steps=0)
    twin.set_input("ENC1", "steps", 3)
    assert wait_until(lambda: enc.steps == 3), enc.steps
    twin.set_input("ENC1", "steps", -1)
    assert wait_until(lambda: enc.steps == 2), enc.steps
    enc.close()


def test_gpiozero_motion_sensor_and_buzzers(gpiozero_twin, wait_until):
    from gpiozero import Buzzer, MotionSensor, TonalBuzzer

    twin = gpiozero_twin(DeviceConfig("PIR1", "pir", {"pin": 4}),
                         DeviceConfig("BZ1", "buzzer", {"pin": 26}),
                         DeviceConfig("BZ2", "buzzer", {"pin": 16}, params={"active": False}))
    pir = MotionSensor(4)
    twin.set_input("PIR1", "motion", True)
    assert wait_until(lambda: pir.motion_detected, timeout=3.0)
    bz = Buzzer(26)
    bz.on()
    assert twin.devices["BZ1"].state()["on"] is True
    tb = TonalBuzzer(16)
    tb.play(440.0)
    st = twin.devices["BZ2"].state()
    assert st["on"] is True and st["frequency"] == pytest.approx(440.0, rel=0.01)
    for d in (pir, bz, tb):
        d.close()


def test_gpiozero_stepper_with_output_devices(gpiozero_twin):
    from gpiozero import OutputDevice

    twin = gpiozero_twin(DeviceConfig("M1", "stepper_28byj48", {"in1": 5, "in2": 6, "in3": 13, "in4": 19}))
    coils = [OutputDevice(p) for p in (5, 6, 13, 19)]
    seq = [(1, 0, 0, 0), (1, 1, 0, 0), (0, 1, 0, 0), (0, 1, 1, 0),
           (0, 0, 1, 0), (0, 0, 1, 1), (0, 0, 0, 1), (1, 0, 0, 1)]
    for i in range(513):
        for c, v in zip(coils, seq[i % 8]):
            c.value = v
    assert twin.devices["M1"].state()["angle"] == pytest.approx(45.0, abs=0.01)
    for c in coils:
        c.close()


def test_factory_reports_board_and_ticks(gpiozero_twin):
    from gpiozero import Device

    gpiozero_twin()
    f = Device.pin_factory
    assert f.board_info.model == "4B"
    t1 = f.ticks()
    t2 = f.ticks()
    assert f.ticks_diff(t2, t1) >= 0
