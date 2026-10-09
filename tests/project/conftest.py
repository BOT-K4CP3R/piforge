"""Tiny fixture projects for the project / build / CLI tests.

Most fixtures are electronics-only so they load and build without the CAD kernel (fast). The
template project (``projects/_template``) is copied into a path with spaces, ``~`` and a non-ASCII
character and built once per session (slow: CAD kernel, renders, SPICE, one twin scenario).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "projects" / "_template"

FIRMWARE = '''\
"""Fixture firmware: the button on GPIO17 lights the LED on GPIO27 while pressed."""
from signal import pause

from gpiozero import LED, Button

led = LED(27)
button = Button(17)


def pressed():
    led.on()
    print("pressed -> LED on", flush=True)


button.when_pressed = pressed
button.when_released = led.off
print("fixture firmware ready", flush=True)
pause()
'''

ELEC_PROJECT = '''\
"""Electronics-only fixture: Pi 4B, a button on GPIO17 and an LED with 330 ohm on GPIO27."""
from piforge.project import Project
from piforge.twin.scenario import Scenario, Step


def build(p: Project) -> None:
    p.meta(description="Button lights an LED (electronics only)", board="rpi4b", printer="prusa_mk4")
    c = p.circuit
    pi = c.add("rpi4b", "U1")
    sw = c.add("pushbutton", "SW1")
    led = c.add("led", "D1", color="red")
    r = c.add("resistor", "R1", value=330)
    c.connect(pi["GPIO17"], sw["A"])
    c.connect(sw["B"], pi["GND"])
    c.configure(pi, pulls={"GPIO17": "up"})
    c.connect(pi["GPIO27"], r["1"])
    c.connect(r["2"], led["A"])
    c.connect(led["K"], pi["GND"])
    p.firmware("firmware/main.py")
    p.scenario(Scenario("press_lights_led", duration=2.0, steps=[
        Step(at=0.4, action="input", device="SW1", prop="pressed", value=True),
        Step(at=0.9, action="expect", device="D1", prop="brightness", value=1.0, tol=0.01),
        Step(at=1.0, action="expect_log", pattern="LED on"),
    ]))
    EXTRA
'''

HCSR04_PROJECT = '''\
"""Broken fixture: HC-SR04 powered from 5 V with ECHO wired straight to GPIO24."""
from piforge.project import Project


def build(p: Project) -> None:
    p.meta(description="5 V echo into a 3.3 V GPIO", board="rpi4b")
    c = p.circuit
    pi = c.add("rpi4b", "U1")
    us = c.add("hcsr04", "US1")
    c.connect(pi["5V"], us["VCC"])
    c.connect(pi["GND"], us["GND"])
    c.connect(pi["GPIO23"], us["TRIG"])
    c.connect(pi["GPIO24"], us["ECHO"])
'''


MECH_PROJECT = '''\
"""CAD fixture: a placed printed box, an auto-oriented spare that is never placed, a printed part
whose empty shape cannot be tessellated, and a placed printed node never registered for export."""
from build123d import Compound

from piforge.mech import PartSpec, rounded_box
from piforge.project import Project


def build(p: Project) -> None:
    p.meta(description="mech fixture", printer="prusa_mk4", material="PLA")
    box = PartSpec("box", rounded_box(30, 20, 10, radius=2), material="PLA", color="#22aa55",
                   print_rotation=(0, 0, 0))
    spare = PartSpec("spare", rounded_box(12, 10, 4), material="PETG")
    ghost = PartSpec("ghost", Compound([]), material="PLA")
    loose = PartSpec("loose", rounded_box(8, 8, 8), material="PLA")
    p.add_printed(box, spare, ghost)
    p.assembly.add(box, id="box")
    p.assembly.add(loose, (40, 0, 0), id="loose")
'''


def write_project(root: Path, source: str, files: dict[str, str] | None = None) -> Path:
    """Create ``root/project.py`` (+ extra files, relative paths) and return ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.py").write_text(source, encoding="utf-8")
    for rel, text in (files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def elec_source(extra: str = "") -> str:
    """The electronics-only fixture project, with ``extra`` statements appended to ``build(p)``."""
    lines = [ln for ln in extra.strip().splitlines()] or ["pass"]
    return ELEC_PROJECT.replace("    EXTRA\n", "".join(f"    {ln}\n" for ln in lines))


@pytest.fixture
def elec_project(spaced_tmp: Path) -> Path:
    """Electronics-only project (button + LED + firmware + one scenario); no CAD kernel needed."""
    return write_project(spaced_tmp / "elec project", elec_source(),
                         {"firmware/main.py": FIRMWARE})


@pytest.fixture
def hcsr04_project(spaced_tmp: Path) -> Path:
    """Project whose ERC must fail with ERC.LEVEL_MISMATCH."""
    return write_project(spaced_tmp / "hcsr04 project", HCSR04_PROJECT)


def copy_template(dest: Path) -> Path:
    """Copy ``projects/_template`` (without build outputs or caches) to ``dest``."""
    shutil.copytree(TEMPLATE_DIR, dest, ignore=shutil.ignore_patterns("build", "__pycache__", "*.pyc"))
    return dest


@pytest.fixture(scope="session")
def template_build(tmp_path_factory: pytest.TempPathFactory):
    """The template project built once (renders, SPICE, scenarios) in a spaced/unicode path."""
    from piforge.build import build_project

    base = tmp_path_factory.mktemp("tmpl") / "dir with spaces ~ż"
    root = copy_template(base / "template copy")
    messages: list[str] = []
    result = build_project(root, scenarios=True, progress=messages.append)
    result.progress_messages = messages  # type: ignore[attr-defined]
    return result
