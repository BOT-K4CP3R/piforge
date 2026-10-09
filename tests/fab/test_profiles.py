import pytest

from piforge.core import NotFoundError
from piforge.fab import MATERIALS, PRINTERS, get_material, get_printer


def test_lookup_is_forgiving():
    assert get_printer("Prusa MK4").name == "prusa_mk4"
    assert get_printer("bambu-a1").build_volume == (256, 256, 256)
    assert get_material("petg").name == "PETG"
    assert get_material("tpu").name == "TPU95A"
    p = get_printer("generic")
    assert get_printer(p) is p


def test_unknown_names_suggest():
    with pytest.raises(NotFoundError, match="prusa_mk4"):
        get_printer("prusa_mk3")
    with pytest.raises(NotFoundError):
        get_material("unobtainium")


@pytest.mark.parametrize("m", list(MATERIALS.values()), ids=lambda m: m.name)
def test_material_values_physical(m):
    assert 1.0 <= m.density_g_cm3 <= 1.4
    assert 40 <= m.max_service_c <= 150
    if m.name != "TPU95A":  # rigid materials soften around/after Tg; TPU is an elastomer (Tg < 0)
        assert m.max_service_c <= m.glass_transition_c + 60
    assert 0 < m.z_strength_factor <= 1
    assert 0 < m.allowable_strain < 1
    assert m.cost_per_kg > 0


@pytest.mark.parametrize("p", list(PRINTERS.values()), ids=lambda p: p.name)
def test_printer_values_physical(p):
    assert min(p.build_volume) >= 150
    assert p.nozzle_d <= p.line_w <= 2 * p.nozzle_d
    assert p.clearance_press < p.clearance_sliding < p.clearance_loose
    assert p.min_wall >= 2 * p.nozzle_d
    assert 30 <= p.max_overhang_deg <= 60


def test_with_copy():
    p = get_printer("prusa_mk4").with_(nozzle_d=0.6, line_w=0.68, min_wall=1.2)
    assert p.nozzle_d == 0.6 and get_printer("prusa_mk4").nozzle_d == 0.4
