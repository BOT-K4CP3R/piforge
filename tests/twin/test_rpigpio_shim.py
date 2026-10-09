"""The ``RPi.GPIO`` shim: the classic API surface, backed by the twin."""

from __future__ import annotations

import threading
import time

import pytest

from piforge.twin.config import DeviceConfig


def _devices():
    return (DeviceConfig("SW1", "button", {"pin": 27}), DeviceConfig("D1", "led", {"pin": 17}),
            DeviceConfig("SRV1", "servo", {"pin": 18}))


def test_rpigpio_shim(shims, make_twin, wait_until):
    twin = make_twin(*_devices())
    import RPi.GPIO as GPIO

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    assert GPIO.getmode() == GPIO.BCM
    GPIO.setup(17, GPIO.OUT)
    GPIO.output(17, GPIO.HIGH)
    assert twin.devices["D1"].state()["brightness"] == 1.0
    assert GPIO.input(17) == GPIO.HIGH               # reading back an output is allowed
    GPIO.output(17, False)
    assert twin.devices["D1"].state()["brightness"] == 0.0

    GPIO.setup(27, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    assert GPIO.input(27) == GPIO.HIGH
    pressed: list[int] = []
    GPIO.add_event_detect(27, GPIO.FALLING, callback=lambda ch: pressed.append(ch), bouncetime=50)
    twin.set_input("SW1", "pressed", True)
    assert wait_until(lambda: pressed == [27])
    assert GPIO.input(27) == GPIO.LOW
    assert GPIO.event_detected(27) is True
    assert GPIO.event_detected(27) is False
    twin.set_input("SW1", "pressed", False)

    GPIO.setup(18, GPIO.OUT)
    pwm = GPIO.PWM(18, 50)
    pwm.start(7.5)                                   # 1.5 ms → centre
    snap = twin.pi.snapshot()[18]
    assert snap["pwm_freq"] == 50 and snap["pwm_duty"] == pytest.approx(0.075)
    pwm.ChangeDutyCycle(10.0)                        # 2.0 ms → +90°
    assert wait_until(lambda: abs(twin.devices["SRV1"].state()["angle"] - 90) < 2, timeout=3.0)
    pwm.ChangeFrequency(100)
    assert twin.pi.snapshot()[18]["pwm_freq"] == 100
    pwm.stop()
    assert twin.pi.snapshot()[18]["pwm_freq"] is None

    GPIO.cleanup()
    snap = twin.pi.snapshot()
    assert snap[17]["mode"] == "input" and snap[18]["mode"] == "input"


def test_rpigpio_board_numbering_and_lists(shims, make_twin):
    twin = make_twin(DeviceConfig("D1", "led", {"pin": 17}), DeviceConfig("D2", "led", {"pin": 27}))
    import RPi.GPIO as GPIO

    GPIO.setmode(GPIO.BOARD)
    GPIO.setup([11, 13], GPIO.OUT, initial=GPIO.LOW)   # physical 11 = GPIO17, 13 = GPIO27
    GPIO.output([11, 13], (GPIO.HIGH, GPIO.LOW))
    st = twin.state()["devices"]
    assert st["D1"]["brightness"] == 1.0 and st["D2"]["brightness"] == 0.0
    assert GPIO.gpio_function(11) == GPIO.OUT
    with pytest.raises(ValueError):
        GPIO.setup(1, GPIO.OUT)                        # physical pin 1 is 3V3
    GPIO.cleanup()


def test_rpigpio_errors_match_real_library(shims, make_twin):
    make_twin()
    import RPi.GPIO as GPIO

    with pytest.raises(RuntimeError, match="setmode"):
        GPIO.setup(17, GPIO.OUT)
    GPIO.setmode(GPIO.BCM)
    with pytest.raises(RuntimeError, match="OUTPUT"):
        GPIO.output(22, 1)
    GPIO.setup(22, GPIO.OUT)
    pwm = GPIO.PWM(22, 50)
    with pytest.raises(ValueError, match="0.0 to 100.0"):
        pwm.start(150)                                 # duty must be 0..100
    with pytest.raises(RuntimeError, match="already exists"):
        GPIO.PWM(22, 50)
    with pytest.raises(ValueError):
        GPIO.setup(40, GPIO.IN)                        # BCM 40 does not exist
    GPIO.cleanup()


def test_rpigpio_wait_for_edge(shims, make_twin):
    twin = make_twin(DeviceConfig("SW1", "button", {"pin": 27}))
    import RPi.GPIO as GPIO

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(27, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    assert GPIO.wait_for_edge(27, GPIO.FALLING, timeout=100) is None    # timeout in ms
    threading.Timer(0.1, lambda: twin.set_input("SW1", "pressed", True)).start()
    t0 = time.monotonic()
    assert GPIO.wait_for_edge(27, GPIO.FALLING, timeout=3000) == 27
    assert time.monotonic() - t0 < 2.0
    GPIO.cleanup()
