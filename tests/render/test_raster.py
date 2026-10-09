"""Tests for the headless numpy z-buffer rasterizer (``piforge.render.render``)."""

from __future__ import annotations

import time

import numpy as np
import pytest
import trimesh

from piforge.core.errors import NotFoundError, PiForgeError, ValidationError
from piforge.render import VIEWS, RenderItem, render


def _box(extents, center=(0.0, 0.0, 0.0)) -> trimesh.Trimesh:
    m = trimesh.creation.box(extents=extents)
    m.apply_translation(center)
    return m


def _rgb(img) -> np.ndarray:
    return np.asarray(img.convert("RGB")).astype(int)


def _lum(img) -> np.ndarray:
    return np.asarray(img.convert("L")).astype(int)


def _erode(mask: np.ndarray, n: int) -> np.ndarray:
    """``mask`` shrunk by ``n`` px (4-neighbourhood, outside = False): what is left is clear of its border."""
    for _ in range(n):
        p = np.pad(mask, 1)
        mask = p[1:-1, 1:-1] & p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
    return mask


def _box_with_hole() -> trimesh.Trimesh:
    import manifold3d as mf

    solid = mf.Manifold.cube([40, 30, 10], True) - mf.Manifold.cylinder(20, 6, 6, 64, True)
    m = solid.to_mesh()
    return trimesh.Trimesh(m.vert_properties[:, :3], m.tri_verts)


# --- tests named in the task brief ---------------------------------------------------------------


def test_top_view_square_silhouette():
    cube = _box((20, 20, 20))
    img = render(cube, view="top", size=(800, 600), background=None, margin=0.06)
    assert img.mode == "RGBA" and img.size == (800, 600)
    filled = np.asarray(img)[..., 3] >= 128
    side = 600 * (1 - 2 * 0.06)  # the height limits the fit: 528 px
    assert filled.sum() == pytest.approx(side**2, rel=0.03)
    rows = np.flatnonzero(filled.any(axis=1))
    cols = np.flatnonzero(filled.any(axis=0))
    assert rows[-1] - rows[0] + 1 == pytest.approx(side, abs=4)
    assert cols[-1] - cols[0] + 1 == pytest.approx(side, abs=4)
    assert (rows[0] + rows[-1]) / 2 == pytest.approx(299.5, abs=2)
    assert (cols[0] + cols[-1]) / 2 == pytest.approx(399.5, abs=2)
    inner = filled[rows[0] + 3 : rows[-1] - 2, cols[0] + 3 : cols[-1] - 2]
    assert inner.all(), "background pixels inside a solid face"


def test_depth_order_front():
    big = RenderItem(_box((40, 40, 40)), color="#2050ff")
    small = RenderItem(_box((10, 10, 10), center=(0, -30, 0)), color="#ff2020")
    for items in ([big, small], [small, big]):  # a z-buffer, not painter's order
        front = _rgb(render(items, view="front", size=(400, 300)))[150, 200]
        back = _rgb(render(items, view="back", size=(400, 300)))[150, 200]
        assert front[0] > 2 * front[2], f"front centre should be red, got {front}"
        assert back[2] > 2 * back[0], f"back centre should be blue, got {back}"


def test_transparent_background():
    box = _box((10, 10, 10))
    img = render(box, size=(200, 150), background=None)
    a = np.asarray(img)
    assert img.mode == "RGBA" and img.size == (200, 150)
    assert a[0, 0, 3] == 0 and a[-1, -1, 3] == 0 and a[0, -1, 3] == 0
    assert a[75, 100, 3] == 255
    opaque = render(box, size=(200, 150), background="#ffffff")
    assert opaque.mode == "RGB"
    assert opaque.getpixel((0, 0)) == (255, 255, 255)


def test_face_colors_used():
    box = _box((20, 20, 20))
    fc = np.tile(np.array([[0, 190, 0]], dtype=np.uint8), (len(box.faces), 1))
    fc[box.face_normals[:, 2] > 0.9] = (230, 0, 0)  # +Z faces red, the rest green
    item = RenderItem(box, color="#0000ff", face_colors=fc)
    top = _rgb(render(item, view="top", size=(200, 150)))[75, 100]
    front = _rgb(render(item, view="front", size=(200, 150)))[75, 100]
    assert top[0] > 150 and top[1] < 40 and top[2] < 40, top
    assert front[1] > 120 and front[0] < 40 and front[2] < 40, front
    # RGBA uint8 and 0..1 float arrays are accepted as well
    rgba = np.concatenate([fc, np.full((len(fc), 1), 255, np.uint8)], axis=1)
    as_float = fc.astype(float) / 255.0
    for alt in (rgba, as_float):
        px = _rgb(render(RenderItem(box, face_colors=alt), view="top", size=(200, 150)))[75, 100]
        assert np.abs(px - top).max() <= 2


def test_views_differ():
    # a cube with one marker is ambiguous (front and bottom both show "marker on the right"),
    # so use three coloured markers on +X, +Y and +Z
    part = _marker_scene()
    imgs = {v: _rgb(render(part, view=v, size=(160, 120))) for v in VIEWS}
    names = sorted(imgs)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            assert np.abs(imgs[a] - imgs[b]).mean() > 1.0, f"views {a!r} and {b!r} look identical"


def test_performance_100k():
    sphere = trimesh.creation.icosphere(subdivisions=6, radius=25.0)
    assert len(sphere.faces) == 81_920
    render(_box((1, 1, 1)), size=(64, 48))  # warm-up: lazy imports, caches
    t0 = time.perf_counter()
    img = render(sphere, size=(800, 600))
    elapsed = time.perf_counter() - t0
    assert img.size == (800, 600)
    assert elapsed < 4.0, f"81 920-face render took {elapsed:.2f} s"
    # and it is a correct, filled disc: area of a circle with radius 0.44 * 600 px
    covered = (_lum(img) < 250).sum()
    assert covered == pytest.approx(np.pi * 264.0**2, rel=0.03)


def test_empty_raises():
    with pytest.raises(PiForgeError):
        render([])
    with pytest.raises(PiForgeError):
        render(trimesh.Trimesh())
    with pytest.raises(PiForgeError):
        render(RenderItem(trimesh.Trimesh()))


# --- orientation, geometry and edges --------------------------------------------------------------

_MARKERS = {  # colour name -> (centre of an 8 mm cube sticking out of a 30 mm cube, RGB)
    "red": ((19.0, 0.0, 0.0), "#dd2222"),  # +X
    "green": ((0.0, 19.0, 0.0), "#22bb22"),  # +Y
    "blue": ((0.0, 0.0, 19.0), "#2244dd"),  # +Z
}

# view -> {marker: (horizontal side, vertical side)}; +1 = right/up of the image centre, 0 = centred,
# None = hidden behind the big cube.
_EXPECTED_SIDES = {
    "front": {"red": (1, 0), "green": None, "blue": (0, 1)},
    "back": {"red": (-1, 0), "green": (0, 0), "blue": (0, 1)},
    "right": {"red": (0, 0), "green": (1, 0), "blue": (0, 1)},
    "left": {"red": None, "green": (-1, 0), "blue": (0, 1)},
    "top": {"red": (1, 0), "green": (0, 1), "blue": (0, 0)},
    "bottom": {"red": (1, 0), "green": (0, -1), "blue": None},
}


def _marker_scene() -> list[RenderItem]:
    items = [RenderItem(_box((30, 30, 30)), color="#9aa4b2")]
    for centre, color in _MARKERS.values():
        items.append(RenderItem(_box((8, 8, 8), center=centre), color=color))
    return items


def _colour_mask(rgb: np.ndarray, name: str) -> np.ndarray:
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    if name == "red":
        return (r > 1.6 * g + 20) & (r > 1.6 * b + 20)
    if name == "green":
        return (g > 1.6 * r + 20) & (g > 1.6 * b + 20)
    return (b > 1.6 * r + 20) & (b > 1.3 * g + 20)


@pytest.mark.parametrize("view", sorted(_EXPECTED_SIDES))
def test_view_orientation_not_mirrored(view):
    rgb = _rgb(render(_marker_scene(), view=view, size=(400, 400)))
    # the scene bbox spans -15..23 on every axis; its centre (4, 4, 4) maps to the image centre
    for name, expected in _EXPECTED_SIDES[view].items():
        mask = _colour_mask(rgb, name)
        if expected is None:
            assert mask.sum() < 20, f"{name} marker should be hidden in the {view} view"
            continue
        assert mask.sum() > 500, f"{name} marker missing in the {view} view"
        ys, xs = np.nonzero(mask)
        dx, dy = xs.mean() - 199.5, 199.5 - ys.mean()  # +dy = up
        for got, want in ((dx, expected[0]), (dy, expected[1])):
            if want == 0:
                assert abs(got) < 70, (view, name, dx, dy)
            else:
                assert got * want > 70, (view, name, dx, dy)


def test_iso_shows_three_distinct_faces():
    img = render(_box((20, 20, 20)), view="iso", size=(600, 600), edges=False)
    lum = _lum(img)
    # cube corner (10, -10, 10) projects to the image centre; the three visible face centres:
    s = 600 * 0.88 / (40 / np.sqrt(6) * 2)  # height-limited fit, px per mm
    top = (300, round(300 - 20 / np.sqrt(6) * s))
    front = (round(300 - 10 / np.sqrt(2) * s), round(300 + 10 / np.sqrt(6) * s))
    right = (round(300 + 10 / np.sqrt(2) * s), round(300 + 10 / np.sqrt(6) * s))
    vals = [lum[y - 3 : y + 4, x - 3 : x + 4].mean() for x, y in (top, front, right)]
    assert all(70 < v < 250 for v in vals), vals
    assert min(abs(a - b) for i, a in enumerate(vals) for b in vals[i + 1 :]) > 15, vals


def test_edges_outline_and_creases():
    cube = _box((20, 20, 20))
    with_edges = _lum(render(cube, view="iso", size=(600, 600)))
    without = _lum(render(cube, view="iso", size=(600, 600), edges=False))
    assert with_edges[298:303, 298:303].min() < 60, "crease lines meet at the projected corner"
    assert without[298:303, 298:303].min() > 80
    # silhouette: the outline is dark where it crosses the horizontal centre line
    row = with_edges[300]
    cols = np.flatnonzero(row < 250)
    assert row[cols[0] : cols[0] + 3].min() < 80 and row[cols[-1] - 2 : cols[-1] + 1].min() < 80


def test_smooth_surface_has_no_interior_lines():
    sphere = trimesh.creation.icosphere(subdivisions=4, radius=10.0)
    a = _lum(render(sphere, view="front", size=(400, 400)))
    b = _lum(render(sphere, view="front", size=(400, 400), edges=False))
    yy, xx = np.mgrid[:400, :400]
    interior = (xx - 199.5) ** 2 + (yy - 199.5) ** 2 < (0.9 * 176) ** 2
    assert np.abs(a - b)[interior].max() <= 2


def _assert_small_box_is_outlined(scene) -> None:
    """``scene``: a 10 mm box floating 10 mm in front of a 40 x 40 mm plate, same colour, seen from the front.

    Both front faces are parallel and equally lit, so without lines nothing shows where the small
    box ends; with ``edges=True`` its left and right sides must carry a dark outline.
    """
    with_edges = _lum(render(scene, view="front", size=(400, 400)))
    without = _lum(render(scene, view="front", size=(400, 400), edges=False))
    s = 400 * 0.88 / 40  # px per mm
    for x in (round(200 - 5 * s), round(200 + 5 * s)):  # left / right side of the small box
        assert with_edges[198:203, x - 3 : x + 4].min() < 60, x
        assert without[198:203, x - 3 : x + 4].min() > 120, x


def test_occlusion_outline_between_parallel_faces_of_one_mesh():
    # One mesh, so the faces belong to the same item and share no vertex: only the depth-jump
    # test (not the contact rule for different items) can reveal the small box's outline.
    mesh = trimesh.util.concatenate([_box((40, 10, 40), center=(0, 5, 0)), _box((10, 10, 10), center=(0, -15, 0))])
    _assert_small_box_is_outlined(mesh)


def test_occlusion_outline_between_items():
    items = [RenderItem(_box((40, 10, 40), center=(0, 5, 0))), RenderItem(_box((10, 10, 10), center=(0, -15, 0)))]
    _assert_small_box_is_outlined(items)


def test_edge_angle_controls_creases_but_not_the_silhouette():
    cube = _box((20, 20, 20))
    plain = _lum(render(cube, view="iso", size=(600, 600), edges=False))
    sharp = _lum(render(cube, view="iso", size=(600, 600)))  # 90° between faces > 30°: creases
    blunt = _lum(render(cube, view="iso", size=(600, 600), edge_angle_deg=100.0))  # 90° < 100°: none
    clear = _erode(plain < 250, 6)  # pixels well inside the silhouette, away from its own outline
    assert clear.sum() > 100_000
    assert np.abs(sharp - plain)[clear].max() > 40, "creases between the three visible faces"
    assert np.abs(blunt - plain)[clear].max() <= 2, "edge_angle_deg=100 must draw no crease lines at all"
    for name, img in (("default angle", sharp), ("edge_angle_deg=100", blunt)):  # the silhouette stays
        row = img[300]
        cols = np.flatnonzero(row < 250)
        assert row[cols[0] : cols[0] + 3].min() < 80 and row[cols[-1] - 2 : cols[-1] + 1].min() < 80, name


def test_touching_items_get_a_contact_line_but_one_mesh_does_not():
    left, right = _box((20, 20, 20), center=(-10, 0, 0)), _box((20, 20, 20), center=(10, 0, 0))
    items = [RenderItem(left), RenderItem(right)]  # same colour: only a line can separate them
    two = _lum(render(items, view="front", size=(400, 300)))
    plain = _lum(render(items, view="front", size=(400, 300), edges=False))
    one = _lum(render(trimesh.util.concatenate([left, right]), view="front", size=(400, 300)))
    seam = np.s_[140:160, 197:204]  # the shared x = 0 plane projects to column 200
    assert two[seam].min() < 80, "contact line between two items"
    assert plain[seam].min() > 120 and one[seam].min() > 120, "no line inside one flat face"


def test_unmerged_smooth_mesh_has_no_interior_lines():
    # STL-style soup: every face has its own vertices, so no two faces "touch" and every boundary
    # goes through the depth-jump test — which must stay quiet on a continuous, gently curved surface.
    sphere = trimesh.creation.icosphere(subdivisions=4, radius=10.0)
    n = len(sphere.faces)
    soup = trimesh.Trimesh(sphere.triangles.reshape(-1, 3), np.arange(3 * n).reshape(n, 3), process=False)
    a = _lum(render(soup, view="front", size=(400, 400)))
    b = _lum(render(soup, view="front", size=(400, 400), edges=False))
    yy, xx = np.mgrid[:400, :400]
    interior = (xx - 199.5) ** 2 + (yy - 199.5) ** 2 < (0.85 * 176) ** 2
    assert np.abs(a - b)[interior].max() <= 2


def test_hole_is_see_through_and_surfaces_have_no_holes():
    part = _box_with_hole()  # 40 x 30 x 10 plate, 12 mm through-hole along Z
    img = render(part, view="top", size=(800, 600), background=None)
    alpha = np.asarray(img)[..., 3]
    assert alpha[300, 400] == 0, "the through-hole must show the background"
    s = 800 * 0.88 / 40  # px per mm (both axes give 17.6)
    x0, x1 = round(400 - 20 * s) + 3, round(400 + 20 * s) - 3
    y0, y1 = round(300 - 15 * s) + 3, round(300 + 15 * s) - 3
    inside = alpha[y0:y1, x0:x1] < 128
    hole_px = inside.sum()
    assert hole_px == pytest.approx(np.pi * (6 * s) ** 2, rel=0.06)
    iso = render(part, view="iso", size=(600, 450), background=None)
    assert np.asarray(iso)[..., 3].max() == 255


@pytest.mark.parametrize("jitter", [0.0, 0.3])
def test_dense_mesh_has_no_pinholes(jitter):
    # 64 x 64 quads (8192 triangles) in one plane, seen from above and filling the whole image.
    # Without jitter at 400 px, grid lines and diagonals run exactly through supersampled pixel
    # centres: the worst case for shared-edge rules. No background may leak through anywhere.
    n = 64
    xs, ys = np.meshgrid(np.linspace(-32, 32, n + 1), np.linspace(-32, 32, n + 1))
    offs = np.random.default_rng(7).uniform(-jitter, jitter, size=(*xs.shape, 2))
    offs[[0, -1], :] = 0.0
    offs[:, [0, -1]] = 0.0
    verts = np.column_stack([(xs + offs[..., 0]).ravel(), (ys + offs[..., 1]).ravel(), np.zeros(xs.size)])
    k = np.arange(n * (n + 1)).reshape(n, n + 1)[:, :-1].ravel()  # lower-left corner of each quad
    faces = np.concatenate([np.column_stack([k, k + 1, k + n + 2]), np.column_stack([k, k + n + 2, k + n + 1])])
    mesh = trimesh.Trimesh(verts, faces, process=False)
    for size in ((400, 400), (512, 512), (333, 333)):
        img = render(mesh, view="top", size=size, background=None, edges=False, margin=0.0)
        alpha = np.asarray(img)[..., 3]
        assert (alpha[1:-1, 1:-1] == 255).all(), f"pinholes at {size}: {(alpha[1:-1, 1:-1] < 255).sum()}"


def test_open_mesh_is_double_sided():
    quad = trimesh.Trimesh(
        vertices=[[-10, 0, -10], [10, 0, -10], [10, 0, 10], [-10, 0, 10]],
        faces=[[0, 1, 2], [0, 2, 3]],
    )
    for view in ("front", "back"):
        img = render(quad, view=view, size=(200, 200), background=None)
        px = np.asarray(img)[100, 100]
        assert px[3] == 255 and px[:3].mean() > 60, (view, px)


def test_custom_view_tuple_matches_named_view():
    box = _box((30, 20, 10))
    named = np.asarray(render(box, view="front", size=(200, 150)))
    custom = np.asarray(render(box, view=VIEWS["front"], size=(200, 150)))
    assert np.array_equal(named, custom)
    oblique = np.asarray(render(box, view=(-60.0, 20.0), size=(200, 150)))
    assert np.abs(oblique.astype(int) - named.astype(int)).mean() > 1.0


def test_unknown_view_suggests_names():
    with pytest.raises(NotFoundError, match="front"):
        render(_box((1, 1, 1)), view="frnt")
    with pytest.raises(ValidationError):
        render(_box((1, 1, 1)), view=(0.0, 120.0))


@pytest.mark.parametrize("color", ["#ff0000", "#f00", "red", (255, 0, 0), (1.0, 0.0, 0.0)])
def test_colour_formats(color):
    px = _rgb(render(RenderItem(_box((10, 10, 10)), color=color), view="top", size=(100, 100)))[50, 50]
    assert px[0] > 200 and px[1] < 30 and px[2] < 30, px


def test_invalid_arguments():
    box = _box((10, 10, 10))
    with pytest.raises(ValidationError):
        render(box, size=(0, 100))
    with pytest.raises(ValidationError):
        render(box, margin=0.6)
    with pytest.raises(ValidationError):
        render(RenderItem(box, color="not-a-colour"))
    with pytest.raises(ValidationError):
        render(RenderItem(box, face_colors=np.zeros((3, 3), np.uint8)))
    with pytest.raises(ValidationError):
        render(["not a mesh"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"margin": None},
        {"margin": "x"},
        {"margin": float("nan")},
        {"edge_angle_deg": "x"},
        {"edge_angle_deg": None},
        {"size": (800.9, 600.2)},
        {"size": (800.0, 600)},
        {"size": (True, 5)},
        {"size": ("800", 600)},
        {"size": (5000, 10)},  # beyond MAX_SIZE (4096 px per side)
        {"background": "#12345"},
    ],
)
def test_usage_errors_are_validation_errors(kwargs):
    with pytest.raises(ValidationError):
        render(_box((10, 10, 10)), **kwargs)


def test_numpy_integer_sizes_are_fine():
    assert render(_box((10, 10, 10)), size=(np.int64(64), np.int32(48))).size == (64, 48)


def test_background_alpha_is_honoured():
    box = _box((10, 10, 10))
    clear = render(box, size=(100, 80), background="#ffffff00")
    assert clear.mode == "RGBA"
    assert clear.getpixel((0, 0))[3] == 0 and clear.getpixel((50, 40))[3] == 255
    tinted = render(box, size=(100, 80), background="#ff000080")
    assert tinted.mode == "RGBA" and tinted.getpixel((0, 0)) == (255, 0, 0, 128)
    assert tinted.getpixel((50, 40))[3] == 255
    assert render(box, size=(100, 80), background="#ff0000ff").mode == "RGB"  # opaque alpha: RGB


def test_large_requests_are_capped_and_not_supersampled():
    from piforge.render import raster

    assert raster.MAX_SIZE == 4096
    assert raster._ssaa_for(800, 600) == 2 and raster._ssaa_for(2000, 2000) == 2
    assert raster._ssaa_for(4096, 3072) == 1 and raster._ssaa_for(2400, 1800) == 1
    with pytest.raises(ValidationError):
        render(_box((1, 1, 1)), size=(4097, 10))


def test_render_applies_the_supersampling_rule(monkeypatch):
    from piforge.render import raster

    seen = []

    def spy(w: int, h: int) -> int:
        seen.append((w, h))
        return 1

    monkeypatch.setattr(raster, "_ssaa_for", spy)
    img = render(_box((1, 1, 1)), size=(64, 48))
    assert seen == [(64, 48)] and img.size == (64, 48)


class _StopRaster(Exception):
    """Raised by the z-buffer spy: the test only wants to know the resolution it was asked for."""


def test_big_requests_are_rasterised_at_output_resolution(monkeypatch):
    # The z-buffer resolution is what costs memory (about 30 B per buffer pixel). Spy on it and stop
    # there instead of really rendering 12 megapixels: up to 4 MP the buffer is 2x the image, above
    # that it is the image itself.
    from piforge.render import raster

    seen: list[tuple[int, int]] = []

    def spy(tx, ty, tz, planes, w, h):
        seen.append((w, h))
        raise _StopRaster

    monkeypatch.setattr(raster, "_rasterize", spy)
    box = _box((10, 10, 10))
    cases = {
        (800, 600): (1600, 1200),
        (2000, 2000): (4000, 4000),  # exactly 4 MP: still supersampled
        (2400, 1800): (2400, 1800),
        (4096, 3072): (4096, 3072),
        (4096, 4096): (4096, 4096),
    }
    for size, buffer in cases.items():
        seen.clear()
        with pytest.raises(_StopRaster):
            render(box, size=size)
        assert seen == [buffer], (size, seen)


def test_coplanar_overlap_is_deterministic():
    # B sits inside A with a coincident top face: the later item wins every tie, no speckles
    a = RenderItem(_box((40, 40, 10), center=(0, 0, 5)), color="#2050ff")
    b = RenderItem(_box((20, 20, 10), center=(0, 0, 5)), color="#ff2020")
    rgb = _rgb(render([a, b], view="top", size=(400, 400)))
    s = 400 * 0.88 / 40  # px per mm
    lo, hi = round(200 - 10 * s) + 4, round(200 + 10 * s) - 4
    inner = rgb[lo:hi, lo:hi].reshape(-1, 3)
    assert (inner == inner[0]).all() and inner[0][0] > 200 and inner[0][2] < 60


def test_face_colors_keep_their_hue_on_shadowed_faces():
    cube = _box((20, 20, 20))
    fc = np.tile(np.array([[154, 164, 178]], np.uint8), (len(cube.faces), 1))  # = DEFAULT_COLOR
    plain = _lum(render(RenderItem(cube), view="iso", size=(300, 300), edges=False))
    heat = _lum(render(RenderItem(cube, face_colors=fc), view="iso", size=(300, 300), edges=False))
    s = 300 * 0.88 / (80 / np.sqrt(6))
    x, y = round(150 + 10 / np.sqrt(2) * s), round(150 + 10 / np.sqrt(6) * s)  # +X face (shadow side)
    assert heat[y - 2 : y + 3, x - 2 : x + 3].mean() > plain[y - 2 : y + 3, x - 2 : x + 3].mean() + 20


def test_scene_input_and_per_item_edges():
    scene = trimesh.Scene([_box((10, 10, 10)), _box((5, 5, 5), center=(10, 0, 0))])
    assert render(scene, size=(200, 150)).size == (200, 150)
    quiet = _lum(render(RenderItem(_box((20, 20, 20)), edges=False), view="iso", size=(300, 300)))
    plain = _lum(render(_box((20, 20, 20)), view="iso", size=(300, 300), edges=False))
    assert np.array_equal(quiet, plain)


def test_odd_output_size_and_list_of_raw_meshes():
    meshes = [_box((10, 10, 10)), _box((5, 5, 5), center=(10, 0, 0))]
    img = render(meshes, size=(123, 77))
    assert img.size == (123, 77) and img.mode == "RGB"
    rgb = _rgb(img).reshape(-1, 3)
    # two raw meshes get two different palette colours: slate grey-blue and amber
    warm = ((rgb[:, 0] - rgb[:, 2]) > 60).sum()
    cool = ((rgb[:, 2] - rgb[:, 0]) > 10).sum()
    assert warm > 20 and cool > 20, (warm, cool)
