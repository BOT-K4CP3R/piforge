"""VirtualPi: pin modes, pulls, drivers, contention, listeners, PWM and the event scheduler."""

from __future__ import annotations

import pytest

from piforge.core.errors import PiForgeError
from piforge.twin.clock import ManualClock
from piforge.twin.config import DeviceConfig
from piforge.twin.devices import DEVICE_TYPES
from piforge.twin.gpio import VirtualPi


def _button(pi: VirtualPi, pin: int = 27, dev_id: str = "SW1"):
    return DEVICE_TYPES["button"](DeviceConfig(id=dev_id, type="button", pins={"pin": pin}), pi)


def test_pull_and_button():
    pi = VirtualPi()
    pi.setup(27, "input", pull="up")
    assert pi.read(27) == 1
    events: list[tuple[int, int, float]] = []
    pi.add_listener(27, lambda bcm, level, t: events.append((bcm, level, t)))
    btn = _button(pi)
    btn.set_input("pressed", True)          # button wired GPIO27 → GND
    assert pi.read(27) == 0
    btn.set_input("pressed", False)
    assert pi.read(27) == 1
    assert [e[1] for e in events] == [0, 1]
    assert all(e[0] == 27 for e in events)
    assert all(isinstance(e[2], float) for e in events)
    assert events[0][2] <= events[1][2]


def test_contention_event():
    pi = VirtualPi()
    pi.setup(17, "output")
    pi.write(17, 1)
    pi.drive(17, "SW1", 0)                   # a device shorts the high output to GND
    hits = [e for e in pi.events if e["code"] == "TWIN.CONTENTION"]
    assert hits, pi.events
    assert hits[0]["bcm"] == 17
    assert pi.read(17) == 0                  # documented rule: low wins on contention
    pi.drive(17, "SW1", None)
    assert pi.read(17) == 1


def test_no_contention_when_levels_agree():
    pi = VirtualPi()
    pi.setup(17, "output")
    pi.write(17, 0)
    pi.drive(17, "SW1", 0)
    assert not [e for e in pi.events if e["code"] == "TWIN.CONTENTION"]


def test_default_pulls_follow_bcm2711_reset_state():
    pi = VirtualPi()
    assert pi.read(4) == 1       # GPIO0–8 reset with pull-up
    assert pi.read(20) == 0      # GPIO9–27 reset with pull-down
    assert pi.snapshot()[4]["pull"] == "up"


def test_i2c_pins_have_board_pullups():
    pi = VirtualPi()
    pi.setup(2, "input", pull="none")
    assert pi.read(2) == 1                   # 1.8 kΩ on-board pull-up to 3V3
    assert not [e for e in pi.events if e["code"] == "TWIN.FLOATING_INPUT"]


def test_external_pull_from_config():
    pi = VirtualPi()
    pi.set_external_pull(22, "up")
    pi.setup(22, "input", pull="none")
    assert pi.read(22) == 1
    assert not [e for e in pi.events if e["code"] == "TWIN.FLOATING_INPUT"]


def test_floating_input_warns_once():
    pi = VirtualPi()
    pi.setup(5, "input", pull="none")
    pi.read(5)
    pi.read(5)
    hits = [e for e in pi.events if e["code"] == "TWIN.FLOATING_INPUT"]
    assert len(hits) == 1 and hits[0]["bcm"] == 5


def test_output_write_and_snapshot_pwm():
    pi = VirtualPi()
    pi.setup(18, "output")
    pi.write(18, 1)
    assert pi.read(18) == 1
    pi.set_pwm(18, 50.0, 0.075)
    snap = pi.snapshot()[18]
    assert snap["mode"] == "output"
    assert snap["pwm_freq"] == 50.0 and snap["pwm_duty"] == pytest.approx(0.075)
    assert pi.effective(18) == pytest.approx(0.075)
    pi.set_pwm(18, None, None)
    assert pi.snapshot()[18]["pwm_freq"] is None
    assert pi.effective(18) == 1.0


def test_invalid_pin_raises_clear_error():
    pi = VirtualPi()
    with pytest.raises(PiForgeError, match="0–27"):
        pi.read(40)
    with pytest.raises(PiForgeError):
        pi.setup(3, "sideways")


def test_scheduler_runs_due_events_lazily_with_exact_timestamps():
    clock = ManualClock()
    pi = VirtualPi(clock=clock)
    pi.setup(5, "input", pull="down")
    seen: list[tuple[int, float]] = []
    pi.add_listener(5, lambda bcm, level, t: seen.append((level, t)))
    pi.call_at(0.5, lambda: pi.drive(5, "X", 1, t=0.5))
    pi.call_at(0.7, lambda: pi.drive(5, "X", 0, t=0.7))
    assert pi.read(5) == 0
    clock.advance(0.6)
    assert pi.read(5) == 1                   # due event applied on read
    clock.advance(0.4)
    pi.run_due()
    assert seen == [(1, 0.5), (0, 0.7)]      # edge times are the scheduled times


def test_listener_removal():
    pi = VirtualPi()
    calls = []
    cb = lambda bcm, level, t: calls.append(level)  # noqa: E731
    pi.add_listener(20, cb)                  # GPIO20 resets pulled down
    pi.drive(20, "X", 1)
    pi.remove_listener(20, cb)
    pi.drive(20, "X", 0)
    assert calls == [1]
