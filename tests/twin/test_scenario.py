"""Scripted scenarios: timed inputs + expectations → pass/fail Report."""

from __future__ import annotations

from pathlib import Path

import pytest

from piforge.core.errors import ValidationError
from piforge.twin.config import DeviceConfig, TwinConfig
from piforge.twin.scenario import Scenario, Step, run_scenario

FW = """\
from signal import pause
from gpiozero import LED, Button

led = LED(17)
button = Button(27)

def on():
    led.on()
    print("pressed!", flush=True)

button.when_pressed = on
button.when_released = led.off
print("ready", flush=True)
pause()
"""

CONFIG = TwinConfig(devices=[DeviceConfig("SW1", "button", {"pin": 27}),
                             DeviceConfig("D1", "led", {"pin": 17})])


def _fw(d: Path, text: str = FW) -> Path:
    p = d / "main.py"
    p.write_text(text, encoding="utf-8")
    return p


def _steps(expect_on: float) -> list[Step]:
    return [
        Step(at=0.3, action="input", device="SW1", prop="pressed", value=True),
        Step(at=0.8, action="expect", device="D1", prop="brightness", value=expect_on, tol=0.01),
        Step(at=0.9, action="expect_log", pattern=r"press(ed)?!"),
        Step(at=1.0, action="input", device="SW1", prop="pressed", value=False),
        Step(at=1.5, action="expect", device="D1", prop="on", value=False),
    ]


def test_scenario_pass_and_fail(spaced_tmp):
    fw = _fw(spaced_tmp)
    good = run_scenario(CONFIG, fw, Scenario("press lights LED", _steps(1.0), duration=2.0), cwd=spaced_tmp)
    assert good.ok, good.to_markdown()
    assert good.has("TWIN.SCENARIO_OK", "info")

    bad = run_scenario(CONFIG, fw, Scenario("wrong expectation", _steps(0.0), duration=2.0), cwd=spaced_tmp)
    assert not bad.ok
    fails = bad.by_code("TWIN.EXPECT_FAILED")
    assert len(fails) == 1 and fails[0].severity.name == "ERROR"
    assert fails[0].subject == "D1.brightness"
    assert not bad.has("TWIN.SCENARIO_OK")


def test_scenario_reports_firmware_crash(spaced_tmp):
    fw = _fw(spaced_tmp, "import time\ntime.sleep(0.2)\nraise RuntimeError('boom')\n")
    rep = run_scenario(CONFIG, fw, Scenario("crash", [Step(at=1.0, action="expect", device="D1",
                                                                prop="on", value=False)], duration=2.0))
    assert rep.has("TWIN.FIRMWARE_ERROR", "error")
    assert "boom" in rep.by_code("TWIN.FIRMWARE_ERROR")[0].message + str(rep.by_code("TWIN.FIRMWARE_ERROR")[0].data)


def test_scenario_missing_log_fails(spaced_tmp):
    rep = run_scenario(CONFIG, _fw(spaced_tmp), Scenario("log", [Step(at=0.5, action="expect_log",
                                                                       pattern="never printed")],
                                                          duration=1.0))
    assert rep.has("TWIN.EXPECT_FAILED", "error")


def test_step_validation():
    with pytest.raises(ValidationError):
        Step(at=0.1, action="explode")
    with pytest.raises(ValidationError):
        Step(at=0.1, action="expect", device="D1", prop="on", value=True, op="=~=")
    with pytest.raises(ValidationError):
        Step(at=-1.0, action="input", device="SW1", prop="pressed", value=True)


def test_scenario_rejects_unknown_device_before_running(spaced_tmp):
    rep = run_scenario(CONFIG, _fw(spaced_tmp), Scenario("typo", [
        Step(at=0.1, action="input", device="SW9", prop="pressed", value=True)], duration=0.5))
    assert rep.has("TWIN.SCENARIO_INVALID", "error")


def test_step_settle_overrides_the_scenario_window(spaced_tmp):
    """A step's own ``settle`` widens (or narrows) its window; to_dict/from_dict keep it."""
    fw = _fw(spaced_tmp, "import time\ntime.sleep(1.2)\nprint('late hello', flush=True)\ntime.sleep(5)\n")
    late = Step(at=0.1, action="expect_log", pattern="late hello", settle=2.0)
    assert Step.from_dict(late.to_dict()) == late and "settle" not in Step(at=0.1, action="expect_log",
                                                                          pattern="x").to_dict()
    ok = run_scenario(CONFIG, fw, Scenario("wide", [late], duration=2.5), cwd=spaced_tmp)
    assert ok.ok, ok.to_markdown()
    short = run_scenario(CONFIG, fw, Scenario("narrow", [Step(at=0.1, action="expect_log", pattern="late hello")],
                                              duration=1.0, settle=0.3), cwd=spaced_tmp)
    assert short.has("TWIN.EXPECT_FAILED", "error")
    with pytest.raises(ValidationError):
        Step(at=0.1, action="expect_log", pattern="x", settle=-1)
