import math

import pytest
import trimesh

from piforge.core import ValidationError
from piforge.fab import get_material, get_printer
from piforge.fab.estimate import PrintEstimate, estimate_print


def test_estimate_solid_cube(cube20):
    full = estimate_print(cube20, material="PLA", infill=1.0)
    assert isinstance(full, PrintEstimate)
    # 20 mm cube = 8 cm³; PLA 1.24 g/cm³ → 9.92 g when printed solid
    assert full.mass_g == pytest.approx(9.92, rel=0.05)
    shell_only = estimate_print(cube20, material="PLA", infill=0.0)
    sparse = estimate_print(cube20, material="PLA", infill=0.15)
    assert shell_only.mass_g < sparse.mass_g < 9.92


def test_shell_uses_perimeters_on_walls_and_layers_on_skins(cube20):
    est = estimate_print(cube20, "generic", "PLA", infill=0.0, perimeters=2, top_bottom_layers=4)
    # walls: 4 × 400 mm² × (2 perimeters × 0.45 mm); skins: 2 × 400 mm² × (4 layers × 0.2 mm)
    assert est.shell_volume_mm3 == pytest.approx(1600 * 0.9 + 800 * 0.8)
    assert est.infill_volume_mm3 == 0.0
    sparse = estimate_print(cube20, "generic", "PLA", infill=0.15)
    assert sparse.infill_volume_mm3 == pytest.approx((8000 - 2080) * 0.15)
    thicker = estimate_print(cube20, "generic", "PLA", infill=0.0, perimeters=4)
    assert thicker.shell_volume_mm3 > est.shell_volume_mm3


def test_filament_length_and_cost_follow_material(cube20):
    pla = estimate_print(cube20, material="PLA", infill=1.0)
    # 1.75 mm filament: 8000 mm³ / (π · 0.875² mm²) = 3326 mm
    assert pla.filament_m == pytest.approx(8000 / (math.pi * 0.875**2) / 1000, rel=1e-6)
    assert pla.cost == pytest.approx(pla.mass_g / 1000 * get_material("PLA").cost_per_kg)
    petg = estimate_print(cube20, material=get_material("petg"), infill=1.0)
    assert petg.mass_g / pla.mass_g == pytest.approx(1.27 / 1.24)
    assert petg.cost == pytest.approx(petg.mass_g / 1000 * 85.0)


def test_thin_part_is_all_shell():
    plate = trimesh.creation.box(extents=(40, 40, 0.5))
    est = estimate_print(plate, infill=0.5)
    assert est.shell_volume_mm3 == pytest.approx(800.0)  # capped at the solid volume
    assert est.infill_volume_mm3 == 0.0


def test_time_is_plausible_and_reacts_to_speed_and_layers(cube20):
    generic = estimate_print(cube20, "generic", "PLA", infill=0.15)
    # slicers report roughly 10–20 min for a 20 mm calibration cube at 0.2 mm layers
    assert 5 / 60 < generic.time_h < 30 / 60
    fast = estimate_print(cube20, get_printer("bambu_p1s"), "PLA", infill=0.15)
    assert fast.time_h < generic.time_h
    denser = estimate_print(cube20, "generic", "PLA", infill=0.5)
    assert denser.time_h > generic.time_h
    # same 8 cm³ of plastic, but 400 layers instead of 25 → more layer changes → slower
    plate = trimesh.creation.box(extents=(40, 40, 5))
    pillar = trimesh.creation.box(extents=(10, 10, 80))
    assert estimate_print(pillar, infill=0.15).time_h > estimate_print(plate, infill=0.15).time_h


@pytest.mark.parametrize("kwargs", [{"infill": 1.5}, {"infill": -0.1}, {"perimeters": -1},
                                    {"top_bottom_layers": -2}])
def test_invalid_parameters_rejected(cube20, kwargs):
    with pytest.raises(ValidationError):
        estimate_print(cube20, **kwargs)


def test_non_mesh_rejected():
    with pytest.raises(ValidationError):
        estimate_print("cube.stl")
