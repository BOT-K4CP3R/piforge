"""Printable mechanisms: snap-fits (with strain check), hinge, clips, funnel, chute, seats, mounts."""

from __future__ import annotations

import pytest

from piforge.core.errors import NotFoundError, ValidationError


def test_snap_fit_strain_math():
    from piforge.mech.mechanisms import snap_fit_strain

    assert snap_fit_strain(15, 1.5, 1.0) == pytest.approx(0.01)
    assert snap_fit_strain(15, 1.5, 3.0) == pytest.approx(0.03)


@pytest.mark.slow
def test_snap_fit_strain():
    from piforge.mech.mechanisms import snap_fit_cantilever

    part, rep = snap_fit_cantilever(15, 1.5, 5, 1.0, material="PLA")
    assert part.is_valid
    ok = rep.by_code("SNAP.STRAIN")
    assert ok and ok[0].severity.name == "INFO"
    assert ok[0].data["strain"] == pytest.approx(0.01)

    _, rep = snap_fit_cantilever(15, 1.5, 5, 3.0, material="PLA")
    worst = max(f.severity for f in rep.by_code("SNAP.STRAIN"))
    assert worst.name in ("WARNING", "ERROR")

    _, rep = snap_fit_cantilever(15, 1.5, 5, 3.0, material="PETG")  # 3 % is PETG's limit
    assert max(f.severity for f in rep.by_code("SNAP.STRAIN")).name == "INFO"

    with pytest.raises(ValidationError):
        snap_fit_cantilever(15, 1.5, 5, -1.0)


@pytest.mark.slow
def test_mechanisms_watertight():
    from piforge.mech.export import to_trimesh
    from piforge.mech.mechanisms import (
        bearing_seat,
        cable_clip,
        chute,
        din_rail_clip,
        funnel,
        hinge,
        servo_mount,
        shaft_coupler,
        snap_fit_cantilever,
    )

    a, b = hinge(40, pin_d=3.0, knuckles=5)
    parts = {
        "snap": snap_fit_cantilever(15, 1.5, 5, 1.0)[0],
        "hinge_a": a, "hinge_b": b,
        "clip": cable_clip(6.0), "clip_nobase": cable_clip(4.0, base=False),
        "din": din_rail_clip(),
        "funnel": funnel(60, 20, 40),
        "chute": chute(80, 30, 10), "chute_tilted": chute(80, 30, 10, angle=15),
        "seat608": bearing_seat("608"), "seat623": bearing_seat("623", press=False),
        "sg90": servo_mount("sg90"), "mg996r": servo_mount("mg996r"),
        "coupler": shaft_coupler(5, 8, 25),
    }
    for name, p in parts.items():
        assert p.is_valid, name
        mesh = to_trimesh(p)
        assert mesh.is_watertight, name
        assert mesh.volume > 0, name
        assert p.volume == pytest.approx(mesh.volume, rel=0.02), name  # compound-backed Part


@pytest.mark.slow
def test_hinge_leaves_do_not_collide():
    from piforge.mech.assembly import _common_volume
    from piforge.mech.mechanisms import hinge

    a, b = hinge(40, pin_d=3.0, knuckles=5, clearance=0.3)
    assert _common_volume(a, b) < 1e-6


@pytest.mark.slow
def test_bearing_seat_dims():
    from piforge.mech.mechanisms import BEARINGS, bearing_seat

    d, D, B = BEARINGS["608"]
    assert (d, D, B) == (8, 22, 7)
    seat = bearing_seat("608", wall=2.5)
    bb = seat.bounding_box()
    assert bb.max.Z - bb.min.Z >= B
    with pytest.raises(NotFoundError):
        bearing_seat("6299")


def test_hinge_docstring_recommends_print_in_place_clearance():
    from piforge.mech.mechanisms import hinge

    assert "0.4" in hinge.__doc__ and "0.5" in hinge.__doc__ and "print-in-place" in hinge.__doc__
