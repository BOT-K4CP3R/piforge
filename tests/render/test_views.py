"""Tests for named views, multi-view sheets and PNG export (``piforge.render``)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import trimesh
from PIL import Image

from piforge.core.errors import PiForgeError, ValidationError
from piforge.render import VIEWS, RenderItem, render, render_views, save_png


def _part() -> list[RenderItem]:
    base = trimesh.creation.box(extents=(40, 20, 10))
    boss = trimesh.creation.box(extents=(6, 6, 4))
    boss.apply_translation((14, 0, 7))  # sits on the top face: overall bbox 40 x 20 x 14 mm
    return [RenderItem(base), RenderItem(boss, color="#e0a458")]


def _silhouette_size(img) -> tuple[int, int]:
    filled = np.asarray(img)[..., 3] >= 128
    rows = np.flatnonzero(filled.any(axis=1))
    cols = np.flatnonzero(filled.any(axis=0))
    return cols[-1] - cols[0] + 1, rows[-1] - rows[0] + 1


def test_views_dict_conventions():
    assert {"iso", "front", "back", "left", "right", "top", "bottom"} <= set(VIEWS)
    az, el = VIEWS["iso"]
    assert az == pytest.approx(-45.0) and el == pytest.approx(35.264, abs=1e-3)
    assert VIEWS["front"] == (-90.0, 0.0) and VIEWS["right"] == (0.0, 0.0)
    assert VIEWS["top"][1] == 90.0 and VIEWS["bottom"][1] == -90.0


def test_view_projections_show_the_right_axes():
    box = trimesh.creation.box(extents=(40, 20, 10))
    sizes = {v: _silhouette_size(render(box, view=v, size=(800, 600), background=None)) for v in VIEWS}
    ratios = {v: w / h for v, (w, h) in sizes.items()}
    assert ratios["front"] == pytest.approx(40 / 10, rel=0.03)  # X x Z
    assert ratios["back"] == pytest.approx(40 / 10, rel=0.03)
    assert ratios["top"] == pytest.approx(40 / 20, rel=0.03)  # X x Y
    assert ratios["bottom"] == pytest.approx(40 / 20, rel=0.03)
    assert ratios["right"] == pytest.approx(20 / 10, rel=0.03)  # Y x Z
    assert ratios["left"] == pytest.approx(20 / 10, rel=0.03)


def test_sheet_size_and_labels():
    plain = render_views(_part(), size=(1200, 900), title=None, dims=False)
    assert plain.size == (1200, 900)
    g = np.asarray(plain.convert("L")).astype(int)
    labels = []
    for r in range(2):  # 4 views -> 2 x 2 grid of 600 x 450 cells, label in each top-left corner
        for c in range(2):
            patch = g[r * 450 + 2 : r * 450 + 30, c * 600 + 2 : c * 600 + 260] < 110
            assert patch.sum() > 30, f"no label text in cell {(r, c)}"
            labels.append(patch)
    for i in range(4):
        for j in range(i + 1, 4):
            assert (labels[i] != labels[j]).sum() > 20, "cell labels should differ"

    titled = render_views(_part(), size=(1200, 900), title="Enclosure base", dims=True)
    t = np.asarray(titled.convert("L")).astype(int)
    assert titled.size == (1200, 900)
    assert (t[:30] < 110).sum() > 50, "title text missing"
    assert (t[-24:] < 110).sum() > 50, "dimension text missing"
    assert titled.info["piforge.views"] == "iso,front,top,right"
    assert titled.info["piforge.dims"] == "40.0 × 20.0 × 14.0 mm"
    assert titled.info["piforge.title"] == "Enclosure base"


def test_sheet_custom_views_and_layout():
    sheet = render_views(
        trimesh.creation.box(extents=(10, 10, 10)),
        views=("front", "back", "left", "right", "top", "bottom", (-30.0, 20.0)),
        size=(900, 700),
    )
    assert sheet.size == (900, 700) and sheet.mode == "RGB"
    assert sheet.info["piforge.views"].split(",")[:6] == ["front", "back", "left", "right", "top", "bottom"]
    single = render_views(trimesh.creation.box(extents=(10, 10, 10)), views=("iso",), size=(300, 200))
    assert single.size == (300, 200)


@pytest.mark.parametrize("band_h", [31, 36, 42])
@pytest.mark.parametrize("view", sorted(VIEWS))
def test_axis_triad_fits_its_header_band(view, band_h):
    from PIL import ImageDraw

    from piforge.render.camera import camera_for
    from piforge.render.views import _draw_triad

    img = Image.new("RGB", (200, 120), "white")
    left = _draw_triad(ImageDraw.Draw(img), camera_for(view), 190, 40, band_h)
    ink = np.asarray(img).min(axis=2) < 250
    rows, cols = np.flatnonzero(ink.any(axis=1)), np.flatnonzero(ink.any(axis=0))
    assert rows.size and rows.min() >= 40 and rows.max() < 40 + band_h, (rows.min(), rows.max())
    assert cols.min() >= left - 1 and cols.max() <= 191


def test_sheet_rejects_bad_input():
    with pytest.raises(PiForgeError):
        render_views([])
    with pytest.raises(ValidationError):
        render_views(trimesh.creation.box(), views=())
    with pytest.raises(PiForgeError):
        render_views(trimesh.creation.box(), views=("sideways",))


@pytest.mark.parametrize(
    "size",
    [
        (1200.5, 900),  # a float is never silently truncated
        (1200, 900.0),
        (True, 900),
        ("1200", 900),
        (1200,),
        None,
        (5000, 400),  # beyond MAX_SIZE (4096 px per side); would render fine without the cap
        (100, 4097),
        (0, 900),
    ],
)
def test_sheet_size_errors_are_validation_errors(size):
    with pytest.raises(ValidationError):
        render_views(trimesh.creation.box(), size=size)


class _StopRaster(Exception):
    """Raised by the z-buffer spy: the test only wants to know the resolution it was asked for."""


def test_big_sheet_cells_are_rasterised_at_output_resolution(monkeypatch):
    from piforge.render import raster

    seen: list[tuple[int, int]] = []

    def spy(tx, ty, tz, planes, w, h):
        seen.append((w, h))
        raise _StopRaster

    monkeypatch.setattr(raster, "_rasterize", spy)
    box = trimesh.creation.box(extents=(10, 10, 10))
    with pytest.raises(_StopRaster):  # one cell of ~4094 x ~4000 px (16 MP): no supersampling
        render_views(box, views=("iso",), size=(4096, 4096))
    (w, h) = seen[-1]
    assert w * h > 4_000_000 and w <= 4096 and h <= 4096, (w, h)
    seen.clear()
    with pytest.raises(_StopRaster):  # a small cell is supersampled 2x
        render_views(box, views=("iso",), size=(600, 450))
    (w, h) = seen[-1]
    assert w > 600 and h > 450, (w, h)


def test_save_png_roundtrip(spaced_tmp):
    sheet = render_views(_part(), size=(400, 300), title="T")
    path = save_png(sheet, spaced_tmp / "sub dir" / "sheet ż.png")
    assert isinstance(path, Path) and path.exists() and path.suffix == ".png"
    with Image.open(path) as im:
        assert im.size == (400, 300)
        assert im.info.get("piforge.dims") == sheet.info["piforge.dims"]
    single = save_png(render(trimesh.creation.box(), size=(64, 48)), spaced_tmp / "one.png")
    assert single.exists()
    with pytest.raises(ValidationError):
        save_png("not an image", spaced_tmp / "x.png")
    with pytest.raises(ValidationError):
        save_png(sheet, 123)


def test_add_colorbar_argument_errors():
    from piforge.render import add_colorbar

    img = render(trimesh.creation.box(), size=(300, 200))
    assert add_colorbar(img, 0, 1, label=5).info["piforge.colorbar"] == "turbo 0 .. 1 5"
    for bad in (("a", 1.0), (0.0, None), (1.0, 1.0), (0.0, float("inf"))):
        with pytest.raises(ValidationError):
            add_colorbar(img, *bad)
