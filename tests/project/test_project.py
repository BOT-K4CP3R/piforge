"""Project API and project loading (``piforge.project``) — mostly CAD-free."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from piforge.core.errors import NotFoundError, ValidationError
from piforge.project import Project, ProjectLoadError, load_project

from .conftest import ELEC_PROJECT, FIRMWARE, elec_source, write_project


class _Shape:
    """Stand-in for a build123d shape: PartSpec only needs ``bounding_box``."""

    def bounding_box(self):  # pragma: no cover - never called by these tests
        raise NotImplementedError


def _project(tmp: Path, **kw) -> Project:
    return Project("demo", root=tmp, **kw)


# ----------------------------------------------------------------------------------- Project API
def test_defaults_and_meta(tmp_path):
    p = _project(tmp_path)
    assert (p.name, p.printer, p.material, p.board, p.description) == ("demo", "generic", "PETG", None, "")
    assert p.root == tmp_path.resolve()
    assert p.printed == [] and p.checks == [] and p.benches == [] and p.scenarios == []
    assert p.thermal_inputs is None and p.firmware_path is None
    p.meta(name="gauge", description="A gauge", board="Pi 4", printer="Prusa MK4", material="petg")
    assert (p.name, p.description, p.board, p.printer, p.material) == (
        "gauge", "A gauge", "rpi4b", "prusa_mk4", "PETG")
    assert p.circuit.name == "gauge"


def test_meta_rejects_unknown_names_with_suggestions(tmp_path):
    p = _project(tmp_path)
    with pytest.raises(NotFoundError, match="prusa_mk4"):
        p.meta(printer="prusa_mk5")
    with pytest.raises(NotFoundError):
        p.meta(material="unobtainium")
    with pytest.raises(NotFoundError, match="rpi5"):
        p.meta(board="rpi6")
    with pytest.raises(NotFoundError):
        Project("x", root=tmp_path, printer="nope")


@pytest.mark.slow  # DuplicateIdError lives in piforge.mech.assembly (imports the CAD kernel)
def test_add_printed_duplicate_names_raise(tmp_path):
    from piforge.mech import DuplicateIdError, PartSpec

    p = _project(tmp_path)
    lid = PartSpec("lid", _Shape(), material="PETG")
    p.add_printed(lid, PartSpec("base", _Shape(), material="PETG"))
    assert [x.name for x in p.printed] == ["lid", "base"]
    with pytest.raises(DuplicateIdError, match="lid"):
        p.add_printed(PartSpec("lid", _Shape()))
    with pytest.raises(DuplicateIdError):  # same file name on a case-insensitive file system
        p.add_printed(PartSpec("LID", _Shape()))
    with pytest.raises(DuplicateIdError):  # duplicates inside one call
        p.add_printed(PartSpec("knob", _Shape()), PartSpec("knob", _Shape()))
    assert [x.name for x in p.printed] == ["lid", "base"]


def test_add_printed_rejects_non_printed(tmp_path):
    from piforge.mech.part import PartSpec

    p = _project(tmp_path)
    with pytest.raises(ValidationError, match="printed"):
        p.add_printed(PartSpec("pi", _Shape(), kind="pcb"))
    with pytest.raises(ValidationError):
        p.add_printed("not a part")  # type: ignore[arg-type]


def test_firmware_must_exist(tmp_path):
    (tmp_path / "firmware").mkdir()
    (tmp_path / "firmware" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    p = _project(tmp_path)
    with pytest.raises(NotFoundError, match="firmware/main.py"):  # close match suggested
        p.firmware("firmware/mian.py")
    p.firmware("firmware/main.py")
    assert p.firmware_path == (tmp_path / "firmware" / "main.py").resolve()


def test_spice_params_validated_immediately(tmp_path):
    from piforge.spice import BenchParamError

    p = _project(tmp_path)
    with pytest.raises(BenchParamError, match="r_series"):
        p.spice("led_driver", r_series=0)
    with pytest.raises(BenchParamError):
        p.spice("led_driver", resistance=330)
    with pytest.raises(NotFoundError, match="led_driver"):
        p.spice("led_drver")
    p.spice("led_driver", r_series="330", led="led_red")
    p.spice("led_driver", r_series=1000)  # same bench again: label gets a suffix
    p.spice("rc_filter", label="debounce rc", r=10e3, c=100e-9)
    assert [b[0] for b in p.benches] == ["led_driver", "led_driver_2", "debounce_rc"]
    assert p.benches[0][1] == "led_driver" and p.benches[0][2]["r_series"] == "330"
    with pytest.raises(ValidationError, match="debounce_rc"):  # explicit duplicate label
        p.spice("rc_filter", label="debounce_rc")
    assert len(p.benches) == 3


def test_scenarios_checks_thermal(tmp_path):
    from piforge.analysis.thermal import ThermalInputs
    from piforge.core.report import Report
    from piforge.twin.scenario import Scenario

    p = _project(tmp_path)
    p.scenario(Scenario("boot", duration=1.0))
    with pytest.raises(ValidationError, match="boot"):
        p.scenario(Scenario("boot", duration=2.0))
    with pytest.raises(ValidationError):
        p.scenario({"name": "x"})  # type: ignore[arg-type]

    def my_check() -> Report:
        return Report("mine")

    p.add_check(my_check)
    assert p.checks == [my_check]
    with pytest.raises(ValidationError):
        p.add_check("not callable")  # type: ignore[arg-type]
    inputs = ThermalInputs(power_w=4.0, outer_mm=(100, 70, 35))
    p.thermal(inputs)
    assert p.thermal_inputs is inputs
    with pytest.raises(ValidationError):
        p.thermal({"power_w": 4})  # type: ignore[arg-type]


def test_thermal_multiple_cases(tmp_path):
    from piforge.analysis.thermal import ThermalInputs

    p = _project(tmp_path)
    off = ThermalInputs(power_w=4.0, outer_mm=(100, 70, 35))
    on = ThermalInputs(power_w=4.0, outer_mm=(100, 70, 35), fan_cfm=2.0)
    p.thermal(off, label="fan off")
    p.thermal(on, label="fan on")
    assert [(lab, inp) for lab, inp in p.thermal_cases] == [("fan off", off), ("fan on", on)]
    assert p.thermal_inputs is off  # first case (backward compatible attribute)
    with pytest.raises(ValidationError):
        p.thermal(on, label="fan on")  # labels are unique


def test_twin_config_from_circuit_with_overrides(tmp_path):
    from piforge.twin.config import DeviceConfig

    p = _project(tmp_path)
    c = p.circuit
    pi = c.add("rpi4b", "U1")
    sw, led, r = c.add("pushbutton", "SW1"), c.add("led", "D1"), c.add("resistor", "R1", value=330)
    c.connect(pi["GPIO17"], sw["A"])
    c.connect(sw["B"], pi["GND"])
    c.connect(pi["GPIO27"], r["1"])
    c.connect(r["2"], led["A"])
    c.connect(led["K"], pi["GND"])
    p.twin_override("D1", color="green")
    p.twin_device(DeviceConfig("TEMP1", "ds18b20", pins={"pin": 4}))
    cfg = p.twin_config()
    assert cfg.board == "rpi4b"
    assert cfg.device("SW1").type == "button" and cfg.device("SW1").pins == {"pin": 17}
    assert cfg.device("D1").pins == {"pin": 27} and cfg.device("D1").params["color"] == "green"
    assert cfg.device("TEMP1").type == "ds18b20"
    p.twin_override("D9", color="blue")
    with pytest.raises(NotFoundError, match="D1"):
        p.twin_config()


def test_twin_config_without_circuit_uses_project_board(tmp_path):
    p = _project(tmp_path)
    p.meta(board="rpi5")
    cfg = p.twin_config()
    assert cfg.board == "rpi5" and cfg.devices == []


def test_twin_config_without_a_pi_keeps_extra_devices(tmp_path):
    from piforge.twin.config import DeviceConfig

    p = _project(tmp_path)
    p.circuit.add("pushbutton", "SW1")  # parts, but no Raspberry Pi
    p.twin_device(DeviceConfig("TEMP1", "ds18b20", pins={"pin": 4}))
    cfg = p.twin_config()
    assert cfg.board == "rpi4b" and [d.id for d in cfg.devices] == ["TEMP1"]


def test_lint_scenario_references(tmp_path):
    from piforge.twin.scenario import Scenario, Step

    (tmp_path / "fw.py").write_text("print('ok')\n", encoding="utf-8")
    p = _project(tmp_path)
    pi, sw, led = p.circuit.add("rpi4b", "U1"), p.circuit.add("pushbutton", "SW1"), p.circuit.add("led", "D1")
    p.circuit.connect(pi["GPIO17"], sw["A"])
    p.circuit.connect(sw["B"], pi["GND"])
    p.circuit.connect(pi["GPIO27"], led["A"])
    p.circuit.connect(led["K"], pi["GND"])
    p.firmware("fw.py")
    p.scenario(Scenario("typos", duration=1.0, steps=[
        Step(at=0.1, action="input", device="SW1", prop="push", value=True),
        Step(at=0.2, action="expect", device="D1", prop="glow", value=1),
        Step(at=0.3, action="expect", device="D1", prop="brightness", value=1),
        Step(at=0.4, action="expect_log", pattern="ok")]))
    assert p.lint().findings == []  # without a twin config only the firmware is checked
    rep = p.lint(p.twin_config())
    assert [(f.code, f.severity.name) for f in rep.findings] == [
        ("PROJECT.SCENARIO_INVALID", "ERROR"), ("PROJECT.SCENARIO_INVALID", "WARNING")]
    assert "push" in rep.findings[0].message and "pressed" in rep.findings[0].message
    assert "glow" in rep.findings[1].message and rep.findings[0].source == "project"


def test_euler_xyz_rejects_non_matrices():
    from piforge.project import euler_xyz

    for bad in ([1, 2, 3], [[1, 0], [0, 1]], "abc", None):
        with pytest.raises(ValidationError):
            euler_xyz(bad)
    assert euler_xyz([[1, 0, 0], [0, 1, 0], [0, 0, 1]]) == (0.0, 0.0, 0.0)


# ----------------------------------------------------------------------------------- load_project
def test_load_project_from_spaced_path(elec_project):
    p = load_project(elec_project)
    assert p.name == "elec project"  # defaults to the directory name
    assert p.root == elec_project.resolve()
    assert p.description.startswith("Button lights")
    assert p.printer == "prusa_mk4" and p.board == "rpi4b"
    assert [x.ref for x in p.circuit.parts] == ["U1", "SW1", "D1", "R1"]
    assert p.firmware_path == (elec_project / "firmware" / "main.py").resolve()
    assert [s.name for s in p.scenarios] == ["press_lights_led"]
    assert p.printed == [] and not p.has_assembly


def test_load_project_accepts_project_py_path(elec_project):
    assert load_project(elec_project / "project.py").root == elec_project.resolve()


def test_projects_with_same_named_helper_modules_do_not_clash(spaced_tmp):
    src = ('from helpers import LABEL\n'
           'def build(p):\n'
           '    p.meta(description=LABEL)\n')
    a = write_project(spaced_tmp / "a", src, {"helpers.py": "LABEL = 'from a'\n"})
    b = write_project(spaced_tmp / "b", src, {"helpers.py": "LABEL = 'from b'\n"})
    assert load_project(a).description == "from a"
    assert load_project(b).description == "from b"
    assert "helpers" not in sys.modules
    (a / "helpers.py").write_text("LABEL = 'edited'\n", encoding="utf-8")
    assert load_project(a).description == "edited"  # re-loading sees edits (no stale module)


def test_load_project_syntax_error_mentions_line(spaced_tmp):
    root = write_project(spaced_tmp / "broken", "from piforge.project import Project\n\n"
                         "def build(p):\n    p.meta(description='oops'\n")
    with pytest.raises(ProjectLoadError) as exc:
        load_project(root)
    msg = str(exc.value)
    assert "line 4" in msg and "project.py" in msg and "SyntaxError" in msg


def test_load_project_error_inside_build_has_traceback(spaced_tmp):
    root = write_project(spaced_tmp / "bad fw", elec_source().replace(
        'p.firmware("firmware/main.py")', 'p.firmware("firmware/missing.py")'))
    with pytest.raises(ProjectLoadError) as exc:
        load_project(root)
    msg = str(exc.value)
    assert "missing.py" in msg and "Traceback" in msg and "in build" in msg
    assert isinstance(exc.value.__cause__, NotFoundError)


def test_load_project_missing_file_or_build(spaced_tmp):
    with pytest.raises(ProjectLoadError, match="project.py"):
        load_project(spaced_tmp / "nothing here")
    root = write_project(spaced_tmp / "no build", "X = 1\n")
    with pytest.raises(ProjectLoadError, match="build"):
        load_project(root)


def test_project_module_names_are_unique(spaced_tmp):
    a = write_project(spaced_tmp / "one", "def build(p):\n    p.meta(description=__name__)\n")
    b = write_project(spaced_tmp / "two", "def build(p):\n    p.meta(description=__name__)\n")
    na, nb = load_project(a).description, load_project(b).description
    assert na.startswith("piforge_project_") and nb.startswith("piforge_project_") and na != nb


def test_elec_fixture_source_is_valid():
    assert "EXTRA" not in elec_source("p.meta(description='x')") and "EXTRA" in ELEC_PROJECT
    assert "gpiozero" in FIRMWARE
