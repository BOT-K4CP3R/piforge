"""Tests for piforge.mech.fasteners.

The metric table checks are pure data (fast); geometry checks need the OCC kernel (slow).
"""

from __future__ import annotations

import math

import pytest

from piforge.core.errors import NotFoundError, ValidationError
from piforge.fab.profiles import get_printer


def _bounds(shape):
    bb = shape.bounding_box()
    return (bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z)


def _extents(shape):
    lo, hi = _bounds(shape)
    return tuple(h - l for l, h in zip(lo, hi))


def test_metric_table_sane():
    from piforge.mech.fasteners import METRIC

    assert {"M2", "M2.5", "M3", "M4", "M5"} <= set(METRIC)
    for name, s in METRIC.items():
        assert s.name == name
        assert s.clearance_close < s.clearance_normal < s.clearance_loose, name
        assert s.clearance_close > s.d, name
        assert s.tap_plastic < s.d, name
        assert s.head_socket_d > s.clearance_loose, name
        assert s.head_csk_d > s.head_socket_d, name
        assert s.nut_af > s.d, name
        assert s.insert_hole_d > s.d and s.insert_depth > 0, name
        assert 0 < s.pitch < 1, name
    # ISO 273 medium series / ISO 4032 / ISO 4762 spot checks
    m3 = METRIC["M3"]
    assert (m3.clearance_close, m3.clearance_normal, m3.clearance_loose) == (3.2, 3.4, 3.6)
    assert (m3.nut_af, m3.head_socket_d, m3.head_socket_h) == (5.5, 5.5, 3.0)
    assert (m3.insert_hole_d, m3.insert_depth) == (4.0, 5.7)


def test_get_size_lookup():
    from piforge.mech.fasteners import METRIC, get_size

    assert get_size("m3") is METRIC["M3"]
    assert get_size(" M2.5 ") is METRIC["M2.5"]
    assert get_size(METRIC["M4"]) is METRIC["M4"]
    with pytest.raises(NotFoundError) as exc:
        get_size("M3.5")
    assert "M3" in str(exc.value)


def test_hole_compensation_defaults_to_generic_printer():
    from piforge.mech.fasteners import hole_compensation

    assert hole_compensation(None) == get_printer("generic").hole_compensation
    assert hole_compensation("prusa_mk4") == get_printer("prusa_mk4").hole_compensation
    custom = get_printer("generic").with_(hole_compensation=0.3)
    assert hole_compensation(custom) == 0.3


@pytest.mark.slow
def test_clearance_and_tap_holes():
    from piforge.mech.fasteners import EPS, METRIC, clearance_hole, tap_hole

    comp = get_printer("generic").hole_compensation
    for fit, d in (("close", 3.2), ("normal", 3.4), ("loose", 3.6)):
        h = clearance_hole("M3", 5, fit=fit)
        assert h.is_valid
        assert _extents(h)[0] == pytest.approx(d + comp, abs=0.01)
        assert _bounds(h)[0][2] == pytest.approx(-EPS, abs=1e-6)
        assert _bounds(h)[1][2] == pytest.approx(5 + EPS, abs=1e-6)
    with pytest.raises(ValidationError):
        clearance_hole("M3", 5, fit="tight")
    with pytest.raises(ValidationError):
        clearance_hole("M3", 0)

    t = tap_hole("M2.5", 6, printer="bambu_a1")
    assert _extents(t)[0] == pytest.approx(
        METRIC["M2.5"].tap_plastic + get_printer("bambu_a1").hole_compensation, abs=0.01)


@pytest.mark.slow
def test_insert_hole_matches_table():
    from piforge.mech.fasteners import EPS, METRIC, insert_hole

    printer = get_printer("prusa_mk4").with_(hole_compensation=0.2)
    h = insert_hole("M3", printer=printer)
    ext = _extents(h)
    assert ext[0] == pytest.approx(METRIC["M3"].insert_hole_d + printer.hole_compensation, abs=0.01)
    assert ext[1] == pytest.approx(METRIC["M3"].insert_hole_d + printer.hole_compensation, abs=0.01)
    assert ext[2] == pytest.approx(METRIC["M3"].insert_depth + 1.0 + 2 * EPS, abs=1e-6)
    deeper = insert_hole("M3", extra_depth=2.5, printer=printer)
    assert _extents(deeper)[2] == pytest.approx(METRIC["M3"].insert_depth + 2.5 + 2 * EPS, abs=1e-6)


@pytest.mark.slow
def test_nut_trap_across_flats():
    from piforge.mech.fasteners import METRIC, nut_trap

    t = nut_trap("M3", 2.4, clearance=0.2)
    assert t.is_valid
    af = METRIC["M3"].nut_af + 2 * 0.2
    ext = _extents(t)
    assert ext[1] == pytest.approx(af, abs=0.01)  # flats parallel to X → across-flats along Y
    assert ext[0] == pytest.approx(af * 2 / math.sqrt(3), abs=0.01)  # across corners along X
    assert t.volume == pytest.approx(math.sqrt(3) / 2 * af**2 * ext[2], rel=1e-6)


@pytest.mark.slow
def test_counterbore_and_countersink():
    from piforge.mech.fasteners import EPS, METRIC, clearance_hole, counterbore_hole, countersink_hole

    comp = get_printer("generic").hole_compensation
    m3 = METRIC["M3"]
    cb = counterbore_hole("M3", 10)
    assert cb.is_valid
    lo, hi = _bounds(cb)
    assert hi[2] == pytest.approx(10 + EPS, abs=1e-6) and lo[2] == pytest.approx(-EPS, abs=1e-6)
    assert hi[0] - lo[0] == pytest.approx(m3.head_socket_d + 0.6 + comp, abs=0.01)
    r_c, r_b, hd = (m3.clearance_normal + comp) / 2, (m3.head_socket_d + 0.6 + comp) / 2, m3.head_socket_h + 0.2
    assert cb.volume == pytest.approx(math.pi * r_c**2 * (10 - hd + EPS) + math.pi * r_b**2 * (hd + EPS), rel=1e-6)

    cs = countersink_hole("M3", 6)
    assert cs.is_valid
    lo, hi = _bounds(cs)
    assert hi[0] - lo[0] >= m3.head_csk_d
    assert hi[2] == pytest.approx(6 + EPS, abs=1e-6)
    # a section just below the top surface is about as wide as the screw head
    top = cs.faces().sort_by()[-1]
    assert top.area == pytest.approx(math.pi * ((hi[0] - lo[0]) / 2) ** 2, rel=0.02)


@pytest.mark.slow
def test_screw_reference_models():
    from piforge.mech.fasteners import METRIC, screw

    s = screw("M3", 12)
    assert s.is_valid
    shank = math.pi * 1.5**2 * 12
    head = math.pi * (METRIC["M3"].head_socket_d / 2) ** 2 * METRIC["M3"].head_socket_h
    assert shank + 0.7 * head < s.volume < shank + head  # minus the hex socket recess
    lo, hi = _bounds(s)
    assert lo[2] == pytest.approx(-12, abs=1e-6)
    assert hi[2] == pytest.approx(METRIC["M3"].head_socket_h, abs=1e-6)
    assert hi[0] - lo[0] == pytest.approx(METRIC["M3"].head_socket_d, abs=0.01)

    csk = screw("M3", 12, head="countersunk")
    lo, hi = _bounds(csk)
    assert lo[2] == pytest.approx(-12, abs=1e-6) and hi[2] == pytest.approx(0, abs=1e-6)

    button = screw("M4", 8, head="button")
    assert button.is_valid and _bounds(button)[0][2] == pytest.approx(-8, abs=1e-6)
    with pytest.raises(ValidationError):
        screw("M3", 12, head="torx-flange")


@pytest.mark.slow
def test_standoff_variants_fit_pi_pads():
    from piforge.mech.fasteners import METRIC, standoff

    for hole in ("tap", "insert", "clearance", "none"):
        s = standoff("M2.5", 6, hole=hole)
        assert s.is_valid, hole
        lo, hi = _bounds(s)
        assert lo[2] == pytest.approx(0, abs=1e-6) and hi[2] == pytest.approx(6, abs=1e-6)
        assert hi[0] - lo[0] <= 6.0 + 1e-6, "default M2.5 standoff must fit the Pi's 6 mm pad"
    full = math.pi * 3.0**2 * 6
    comp = get_printer("generic").hole_compensation
    tap_d = METRIC["M2.5"].tap_plastic + comp
    assert standoff("M2.5", 6, hole="tap").volume == pytest.approx(full - math.pi * (tap_d / 2) ** 2 * 6, rel=1e-6)
    assert standoff("M2.5", 6, hole="none").volume == pytest.approx(full, rel=1e-6)
    ins = standoff("M3", 10, hole="insert")  # od = max(7, 4.0 + 2.4) = 7; pocket 5.7 + 1 deep
    ins_d = METRIC["M3"].insert_hole_d + comp
    assert ins.volume == pytest.approx(math.pi * 3.5**2 * 10 - math.pi * (ins_d / 2) ** 2 * 6.7, rel=1e-6)
    assert _extents(standoff("M3", 8, od=9.0))[0] == pytest.approx(9.0, abs=0.01)
    with pytest.raises(ValidationError):
        standoff("M3", 8, hole="glue")
    with pytest.raises(ValidationError):
        standoff("M3", 8, od=3.0)  # thinner than the hole
