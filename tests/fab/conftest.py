"""Mesh fixtures for fabrication tests — trimesh/manifold3d only (never build123d/OCP)."""

from __future__ import annotations

import pytest
import trimesh


def make_mushroom() -> trimesh.Trimesh:
    """Stem r=5 standing on the bed, cap r=15 h=3 on top; the cap underside sits at z=10.

    The stem is 10.5 mm tall so it pokes 0.5 mm into the cap: a clean (non-coplanar) union.
    """
    stem = trimesh.creation.cylinder(radius=5.0, height=10.5)
    stem.apply_translation((0.0, 0.0, 5.25))
    cap = trimesh.creation.cylinder(radius=15.0, height=3.0)
    cap.apply_translation((0.0, 0.0, 11.5))
    return trimesh.boolean.union([stem, cap], engine="manifold")


def make_gamma_bracket() -> trimesh.Trimesh:
    """An L-bracket lying wrong ("Γ"): a 4 mm leg standing up with a 40 mm arm at its top.

    Printed as modelled, the arm's 36×20 mm underside hangs in the air (720 mm² overhang).
    """
    leg = trimesh.creation.box(bounds=((0.0, 0.0, 0.0), (4.0, 20.0, 30.0)))
    arm = trimesh.creation.box(bounds=((0.0, 0.0, 26.0), (40.0, 20.0, 30.0)))
    return trimesh.boolean.union([leg, arm], engine="manifold")


@pytest.fixture
def cube20() -> trimesh.Trimesh:
    return trimesh.creation.box(extents=(20.0, 20.0, 20.0))


@pytest.fixture
def mushroom() -> trimesh.Trimesh:
    return make_mushroom()


@pytest.fixture
def gamma_bracket() -> trimesh.Trimesh:
    return make_gamma_bracket()
