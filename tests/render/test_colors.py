"""Tests for heat-map colouring and colour parsing (``piforge.render.colors``)."""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from piforge.core.errors import NotFoundError, ValidationError
from piforge.render import RenderItem, heat_colors, render
from piforge.render.colors import NAN_COLOR, parse_color


def test_heat_colors_shape_dtype_and_ends():
    c = heat_colors(np.linspace(0.0, 1.0, 52))
    assert c.shape == (52, 3) and c.dtype == np.uint8
    assert tuple(c[0]) == (48, 18, 59) and tuple(c[-1]) == (122, 4, 3)  # turbo end points


def test_heat_colors_matches_matplotlib_turbo():
    import matplotlib

    lut = np.asarray(matplotlib.colormaps["turbo"].colors)[:, :3] * 255.0
    ours = heat_colors(np.arange(256) / 255.0, vmin=0.0, vmax=1.0).astype(float)
    assert np.abs(ours - lut).max() <= 2.0


def test_heat_colors_range_clipping_and_nan():
    v = np.array([-5.0, 0.0, 5.0, 10.0, 20.0, np.nan, np.inf])
    c = heat_colors(v, vmin=0.0, vmax=10.0)
    assert (c[0] == c[1]).all() and (c[3] == c[4]).all() and (c[4] == c[6]).all()
    assert not (c[1] == c[3]).all()
    assert tuple(c[5]) == NAN_COLOR
    auto = heat_colors(v[:5])  # finite min/max by default
    assert tuple(auto[0]) == (48, 18, 59) and tuple(auto[4]) == (122, 4, 3)


def test_heat_colors_constant_input_and_errors():
    flat = heat_colors(np.full(4, 3.0))
    assert flat.shape == (4, 3) and (flat == flat[0]).all()
    with pytest.raises(ValidationError):
        heat_colors([1.0, 2.0], vmin=5.0, vmax=1.0)
    with pytest.raises(NotFoundError):
        heat_colors([1.0, 2.0], cmap="no-such-colormap")
    for bad in ({"vmin": "low"}, {"vmax": None, "vmin": [1, 2]}):
        with pytest.raises(ValidationError):
            heat_colors([1.0, 2.0], **bad)
    with pytest.raises(ValidationError):
        heat_colors(["a", "b"])


def test_heat_colors_nan_survives_a_constant_range():
    c = heat_colors([np.nan, 3.0, 3.0])  # vmin == vmax == 3
    assert tuple(c[0]) == NAN_COLOR and tuple(c[1]) == (48, 18, 59) and tuple(c[2]) == (48, 18, 59)
    c = heat_colors([np.nan, 1.0, 5.0], vmin=2.0, vmax=2.0)
    assert tuple(c[0]) == NAN_COLOR and tuple(c[1]) == (48, 18, 59) and tuple(c[2]) == (122, 4, 3)


def test_heat_colors_other_matplotlib_cmap():
    c = heat_colors([0.0, 1.0], cmap="viridis")
    assert tuple(c[0]) == pytest.approx((68, 1, 84), abs=2)


def test_heat_colors_drive_a_render():
    sphere = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
    fc = heat_colors(sphere.triangles_center[:, 2])  # blue-ish bottom, red top
    img = np.asarray(render(RenderItem(sphere, face_colors=fc), view="front", size=(200, 200))).astype(int)
    top, bottom = img[30, 100], img[170, 100]
    assert top[0] > top[2] + 40, top
    assert bottom[2] > bottom[0] + 20, bottom


def test_add_colorbar_appends_a_labelled_scale():
    from piforge.render import add_colorbar

    base = render(trimesh.creation.box(extents=(10, 10, 10)), size=(400, 300))
    out = add_colorbar(base, 0.0, 2.5, label="wall thickness [mm]")
    assert out.width == 400 and out.height > 300
    assert np.array_equal(np.asarray(out)[:300], np.asarray(base))  # the render is untouched
    band = np.asarray(out)[300:].astype(int)
    lo, hi = heat_colors([0.0, 2.5])
    dist = lambda c: np.abs(band - c).sum(axis=2).min()  # noqa: E731
    assert dist(lo) <= 6 and dist(hi) <= 6  # both ends of the colormap are drawn
    assert (band.mean(axis=2) < 110).sum() > 40  # tick values / label text
    assert out.info["piforge.colorbar"] == "turbo 0 .. 2.5 wall thickness [mm]"
    with pytest.raises(ValidationError):
        add_colorbar(base, 1.0, 1.0)


def test_parse_color_forms():
    assert parse_color("#ff8000") == pytest.approx((1.0, 128 / 255, 0.0, 1.0))
    assert parse_color("#f80") == pytest.approx((1.0, 136 / 255, 0.0, 1.0))
    assert parse_color("white") == pytest.approx((1.0, 1.0, 1.0, 1.0))
    assert parse_color((255, 0, 0)) == pytest.approx((1.0, 0.0, 0.0, 1.0))
    assert parse_color((0.0, 0.5, 1.0, 0.5)) == pytest.approx((0.0, 0.5, 1.0, 0.5))
    for bad in ("#12", "nope", (1, 2), (300, 0, 0), (-0.1, 0.0, 0.0), (np.nan, 0, 0)):
        with pytest.raises(ValidationError):
            parse_color(bad)
