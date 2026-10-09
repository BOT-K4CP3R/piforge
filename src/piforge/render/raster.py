"""Headless numpy z-buffer rasterizer: meshes → shaded, outlined images (no OpenGL, no GPU).

Pipeline (vectorised numpy throughout):

1. Items are concatenated into one face list. Closed, outward-wound meshes are back-face culled;
   open meshes are drawn double-sided (their inside is lit like the outside).
2. Orthographic projection (:mod:`piforge.render.camera`) fits the scene into the image with
   ``margin`` on every side; the image is rendered at ``SSAA``× resolution.
3. Every (triangle, pixel row) pair gets its exact span of covered pixel centres from the edge
   functions (cost ∝ covered pixels, any triangle size). Fragments are resolved with
   ``np.maximum.at`` on an int64 key = quantised depth (high bits) | face index (low bits), so
   exact depth ties (coplanar overlaps) deterministically go to the later item.
4. Flat Lambert shading: ambient + key light from the camera's upper-left + camera fill light;
   faces with ``face_colors`` get half the darkening so heat-map colours stay readable.
5. Image-space edges: background and part boundaries, creases (visible normals differ by more
   than ``edge_angle_deg``) and depth jumps between faces that share no vertex (occlusions).
6. Box-filter downsampling gives anti-aliased output; lines are 1.5 px (creases) / 2 px (outlines).
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import numpy as np
import trimesh
from PIL import Image

from piforge.core.errors import ValidationError
from piforge.render.camera import VIEWS, Camera, camera_for
from piforge.render.colors import DEFAULT_COLOR, PALETTE, ColorLike, as_number, face_color_array, parse_color

__all__ = ["SSAA", "VIEWS", "Frame", "RenderItem", "Scene", "prepare_scene", "render", "render_scene"]

log = logging.getLogger(__name__)

SSAA = 2  # supersampling factor: render at 2× and box-filter down …
_SSAA_MAX_PIXELS = 4_000_000  # … up to 4 MP of output (16 MP internally, peak RSS +0.46 GB); 1× above
MAX_SIZE = 4096  # largest accepted width/height (px); measured peak RSS at 4096²: +0.60 GB (+0.85 GB with alpha)

_FID_BITS = 26  # low key bits: face index (≤ 67 M faces)
_FID_MASK = (1 << _FID_BITS) - 1
_ZQ_MAX = (1 << (63 - _FID_BITS)) - 1  # high key bits: quantised depth
_ROW_CHUNK = 1 << 18  # (triangle, row) spans per vectorised batch (bounds temporary memory)
_PIX_CHUNK = 1 << 20  # fragments per vectorised batch (~100 B of temporaries each)

_DEPTH_STEPS_PER_PX = 4096.0  # depth quantum: exact coplanar overlaps tie (later item wins), no speckle
_AMBIENT, _KEY, _FILL = 0.35, 0.40, 0.35  # Lambert mix: ambient + key·max(0, n·l) + fill·(n·view)
_KEY_DIR = (-0.30, 0.60, 0.75)  # key light in camera axes (right, up, toward): upper-left, in front
_FACE_COLOR_SHADING = 0.5  # face_colors (heat-maps) get half the darkening: colours stay readable
_EDGE_RGB = np.array([0.11, 0.12, 0.14], dtype=np.float32)
_CREASE_PX, _OUTLINE_PX = 1.5, 2.0  # line widths in output pixels
_CREASE_INK = 0.9  # crease lines are slightly lighter than outlines
_GAP_TOL_PX = 0.5  # depth jump (output px) between non-adjacent faces that counts as an outline


@dataclass
class RenderItem:
    """One mesh to draw.

    ``color`` takes any :func:`~piforge.render.colors.parse_color` form. ``face_colors`` (uint8
    RGB/RGBA with one row per face, e.g. from :func:`~piforge.render.heat_colors`) overrides it
    and is shaded at half strength. ``edges=False`` suppresses this item's outline and crease
    lines. Renders are opaque: alpha components are ignored.
    """

    mesh: trimesh.Trimesh
    color: ColorLike = DEFAULT_COLOR
    face_colors: np.ndarray | None = None
    edges: bool = True


RenderInput = RenderItem | trimesh.Trimesh | trimesh.Scene | Iterable[RenderItem | trimesh.Trimesh]


@dataclass
class Scene:
    """Camera-independent, validated geometry of all items (reused across the views of a sheet)."""

    vertices: np.ndarray  # (V, 3) world mm
    faces: np.ndarray  # (F, 3) int64 indices into ``vertices``
    owner: np.ndarray  # (F,) item index of every face
    normals: np.ndarray  # (F, 3) unit normals from the winding (zero for degenerate faces)
    colors: np.ndarray  # (F, 3) float32 base colours 0..1
    shading: np.ndarray  # (F,) float32 strength of the light/shadow modulation (1 = full)
    cullable: np.ndarray  # (F,) face of a closed, outward-wound mesh: back side never visible
    item_edges: np.ndarray  # (n_items,) bool
    bounds: np.ndarray  # (2, 3) world bbox of all faces

    @property
    def n_items(self) -> int:
        """Number of items that contributed faces."""
        return int(np.unique(self.owner).size)


@dataclass
class Frame:
    """A rendered image plus what is needed to annotate it (axis triads, scale bars)."""

    image: Image.Image
    camera: Camera
    px_per_mm: float  # output pixels per millimetre (orthographic: uniform over the image)


def render(
    items: RenderInput,
    *,
    view: str | tuple[float, float] = "iso",
    size: tuple[int, int] = (800, 600),
    background: ColorLike | None = "#ffffff",
    edges: bool = True,
    edge_angle_deg: float = 30.0,
    margin: float = 0.06,
) -> Image.Image:
    """Render meshes to a PIL image (orthographic, Z up, scene fitted to ``size``).

    Args:
        items: a ``trimesh.Trimesh``, a :class:`RenderItem`, a ``trimesh.Scene`` or a list of
            meshes/items. Bare meshes in a list get distinct palette colours.
        view: a :data:`~piforge.render.camera.VIEWS` name (``"iso"``, ``"front"`` = camera at −Y,
            ``"right"`` = camera at +X, ``"top"`` = camera at +Z, …) or ``(azimuth, elevation)``.
        size: output ``(width, height)``: integers up to :data:`MAX_SIZE` (4096) per side. Up to
            ~4 megapixels the image is supersampled 2×; larger images are rendered at 1×.
        background: background colour (opaque → RGB image). ``None`` (fully transparent) or a
            colour with alpha below 1 such as ``"#ffffff80"`` gives an RGBA image.
        edges: draw outline and crease lines.
        edge_angle_deg: faces whose normals differ by more than this get a crease line.
        margin: free border on every side, as a fraction of the image size.

    Returns:
        An ``RGB`` image (``RGBA`` for a transparent or translucent background) with
        ``info["piforge.view"]`` and ``info["piforge.px_per_mm"]`` set.
    """
    camera = camera_for(view)
    check_size(size)
    return render_scene(
        prepare_scene(items),
        camera,
        size,
        background=background,
        edges=edges,
        edge_angle_deg=edge_angle_deg,
        margin=margin,
    ).image


def check_size(size: tuple[int, int]) -> tuple[int, int]:
    """Validate an output ``(width, height)`` of integers in 1..MAX_SIZE and return it as ints."""
    try:
        w, h = size
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"size must be (width, height) in pixels, got {size!r}.") from exc
    if any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer)) for v in (w, h)):
        raise ValidationError(f"size must be two integers (width, height) in pixels, got {size!r}.")
    if not (1 <= w <= MAX_SIZE and 1 <= h <= MAX_SIZE):
        raise ValidationError(f"size must be within 1..{MAX_SIZE} px per side, got {size!r}.")
    return int(w), int(h)


def _ssaa_for(w: int, h: int) -> int:
    """Supersampling factor for a ``w``×``h`` output: ``SSAA`` up to ~4 MP, 1 above (memory)."""
    return SSAA if w * h <= _SSAA_MAX_PIXELS else 1


def prepare_scene(items: RenderInput) -> Scene:
    """Validate the items and concatenate them into one :class:`Scene`."""
    entries = _as_items(items)
    verts, faces, owner, colors, shading, cull = [], [], [], [], [], []
    offset = 0
    for i, item in enumerate(entries):
        f = np.asarray(item.mesh.faces, dtype=np.int64).reshape(-1, 3)
        v = np.asarray(item.mesh.vertices, dtype=np.float64).reshape(-1, 3)
        base = np.asarray(parse_color(item.color)[:3], dtype=np.float32)
        col = None if item.face_colors is None else face_color_array(item.face_colors, len(f))
        if len(f) == 0:
            continue
        if not np.isfinite(v).all():
            raise ValidationError(f"Item {i}: the mesh has non-finite vertex coordinates.")
        if f.min() < 0 or f.max() >= len(v):
            raise ValidationError(f"Item {i}: face indices point outside the vertex array.")
        verts.append(v)
        faces.append(f + offset)
        offset += len(v)
        owner.append(np.full(len(f), i, dtype=np.int64))
        colors.append(np.broadcast_to(base, (len(f), 3)) if col is None else col)
        shading.append(np.full(len(f), 1.0 if col is None else _FACE_COLOR_SHADING, dtype=np.float32))
        cull.append(np.full(len(f), _is_closed(item.mesh)))
    if not faces:
        raise ValidationError("Nothing to render: no items, or every mesh has zero faces.")
    v_all, f_all = np.concatenate(verts), np.concatenate(faces)
    if len(f_all) > _FID_MASK:
        raise ValidationError(f"Too many faces to render ({len(f_all):,} > {_FID_MASK:,}).")
    tri = v_all[f_all]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    length = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.divide(n, length, out=np.zeros_like(n), where=length > 0)
    return Scene(
        vertices=v_all,
        faces=f_all,
        owner=np.concatenate(owner),
        normals=n,
        colors=np.concatenate(colors).astype(np.float32),
        shading=np.concatenate(shading),
        cullable=np.concatenate(cull),
        item_edges=np.array([bool(e.edges) for e in entries], dtype=bool),
        bounds=np.stack([tri.min(axis=(0, 1)), tri.max(axis=(0, 1))]),
    )


def fit_scale(scene: Scene, camera: Camera, size: tuple[int, int], margin: float) -> float:
    """Pixels per mm that fit the projected scene into ``size`` leaving ``margin`` on each side."""
    return _fit(*_projected_extent(scene, camera), size, margin)


def _fit(lo: np.ndarray, hi: np.ndarray, size: tuple[int, int], margin: float) -> float:
    avail = np.array(size, dtype=np.float64) * (1.0 - 2.0 * margin)
    fits = [a / e for a, e in zip(avail, hi - lo, strict=True) if e > 1e-9]
    return float(min(fits)) if fits else 1.0  # a point-like scene: any scale works


def render_scene(
    scene: Scene,
    camera: Camera,
    size: tuple[int, int],
    *,
    background: ColorLike | None = "#ffffff",
    edges: bool = True,
    edge_angle_deg: float = 30.0,
    margin: float = 0.06,
    px_per_mm: float | None = None,
    ssaa: int | None = None,
) -> Frame:
    """Render a prepared scene; ``px_per_mm`` fixes the scale instead of fitting (sheets).

    ``ssaa=None`` picks the supersampling factor from the output size (:func:`_ssaa_for`).
    """
    w, h = check_size(size)
    margin = as_number(margin, "margin")
    if not 0.0 <= margin < 0.5:
        raise ValidationError(f"margin must be in [0, 0.5), got {margin!r}.")
    edge_angle_deg = as_number(edge_angle_deg, "edge_angle_deg")
    if not 0.0 < edge_angle_deg < 180.0:
        raise ValidationError(f"edge_angle_deg must be in (0, 180), got {edge_angle_deg!r}.")
    if px_per_mm is not None and as_number(px_per_mm, "px_per_mm") <= 0.0:
        raise ValidationError(f"px_per_mm must be positive, got {px_per_mm!r}.")
    ssaa = _ssaa_for(w, h) if ssaa is None else ssaa
    if isinstance(ssaa, bool) or not isinstance(ssaa, int) or not 1 <= ssaa <= 4:
        raise ValidationError(f"ssaa must be an integer in 1..4, got {ssaa!r}.")
    bg = None if background is None else np.asarray(parse_color(background), dtype=np.float32)  # RGBA
    t0 = time.perf_counter()

    lo, hi = _projected_extent(scene, camera)
    scale = _fit(lo, hi, (w, h), margin) if px_per_mm is None else as_number(px_per_mm, "px_per_mm")
    mid = (lo + hi) / 2.0
    cam = scene.vertices @ camera.basis.T  # (V, 3): right, up, toward
    ws, hs, s = w * ssaa, h * ssaa, scale * ssaa
    sx = ws / 2.0 + s * (cam[:, 0] - mid[0])
    sy = hs / 2.0 - s * (cam[:, 1] - mid[1])
    sz = s * cam[:, 2]

    facing = scene.normals @ camera.toward
    draw = np.flatnonzero(~(scene.cullable & (facing <= 0.0)))
    nvis = scene.normals[draw] * np.where(facing[draw] < 0.0, -1.0, 1.0)[:, None]
    key = np.asarray(_KEY_DIR) @ camera.basis
    key /= np.linalg.norm(key)
    shade = _AMBIENT + _KEY * np.clip(nvis @ key, 0.0, None) + _FILL * np.abs(facing[draw])
    shade = 1.0 - scene.shading[draw] * (1.0 - np.minimum(shade, 1.0))
    face_rgb = np.clip(scene.colors[draw] * shade[:, None].astype(np.float32), 0.0, 1.0)

    tris = scene.faces[draw]
    tx, ty, tz = sx[tris], sy[tris], sz[tris]
    planes = _depth_planes(tx, ty, tz)
    keybuf = _rasterize(tx, ty, tz, planes, ws, hs)
    hit = keybuf >= 0
    fid = (keybuf & _FID_MASK).astype(np.int32)  # face ids < 2**26 fit; int32 halves the buffer
    fid[~hit] = -1
    del keybuf
    t1 = time.perf_counter()

    cover = None
    if edges and scene.item_edges.any():
        cos_th = math.cos(math.radians(edge_angle_deg))
        ctx = _EdgeContext(tris, scene.owner[draw], nvis, planes, scene.item_edges, cos_th)
        cover = _edge_cover(fid, ws, hs, ssaa, ctx)
    t2 = time.perf_counter()

    image = _compose(fid, hit, face_rgb, cover, bg, w, h, ssaa)
    image.info["piforge.view"] = camera.name
    image.info["piforge.px_per_mm"] = f"{scale:.5g}"
    log.debug(
        "render %s %dx%d: %d faces drawn, raster %.3fs, edges %.3fs, total %.3fs",
        camera.name, w, h, len(draw), t1 - t0, t2 - t1, time.perf_counter() - t0,
    )  # fmt: skip
    return Frame(image, camera, scale)


# --- scene preparation ----------------------------------------------------------------------------


def _as_items(items: RenderInput) -> list[RenderItem]:
    if isinstance(items, (RenderItem, trimesh.Trimesh)):
        seq: list[object] = [items]
    elif isinstance(items, trimesh.Scene):
        seq = [g for g in items.dump() if isinstance(g, trimesh.Trimesh)]
    elif isinstance(items, (str, bytes)) or not isinstance(items, Iterable):
        raise ValidationError(
            f"Cannot render {type(items).__name__}: pass a trimesh.Trimesh, a RenderItem or a list of them."
        )
    else:
        seq = list(items)
    out: list[RenderItem] = []
    n_bare = 0
    for x in seq:
        if isinstance(x, trimesh.Trimesh):
            out.append(RenderItem(x, color=PALETTE[n_bare % len(PALETTE)]))
            n_bare += 1
        elif isinstance(x, RenderItem) and isinstance(x.mesh, trimesh.Trimesh):
            out.append(x)
        else:
            raise ValidationError(
                f"Cannot render {type(x).__name__}: items must be trimesh.Trimesh or RenderItem(mesh=Trimesh)."
            )
    return out


def _is_closed(mesh: trimesh.Trimesh) -> bool:
    """Closed, consistently and outward-wound mesh: back faces can never be seen (safe to cull)."""
    try:
        return bool(mesh.is_watertight and mesh.is_winding_consistent and mesh.volume > 0.0)
    except Exception:  # noqa: BLE001 - culling is only an optimisation; double-sided is always correct
        log.debug("closedness check failed, drawing the mesh double-sided", exc_info=True)
        return False


def _projected_extent(scene: Scene, camera: Camera) -> tuple[np.ndarray, np.ndarray]:
    used = np.zeros(len(scene.vertices), dtype=bool)
    used[scene.faces.ravel()] = True
    pts = scene.vertices[used] @ camera.basis[:2].T
    return pts.min(axis=0), pts.max(axis=0)


# --- rasterization --------------------------------------------------------------------------------


def _depth_planes(tx: np.ndarray, ty: np.ndarray, tz: np.ndarray) -> np.ndarray:
    """Per triangle ``(x0, y0, z0, gx, gy)``: depth ``z = z0 + gx·(x − x0) + gy·(y − y0)``."""
    x0, y0, z0 = tx[:, 0], ty[:, 0], tz[:, 0]
    ex1, ey1, ez1 = tx[:, 1] - x0, ty[:, 1] - y0, tz[:, 1] - z0
    ex2, ey2, ez2 = tx[:, 2] - x0, ty[:, 2] - y0, tz[:, 2] - z0
    det = ex1 * ey2 - ex2 * ey1
    flat = det == 0.0
    safe = np.where(flat, 1.0, det)
    gx = np.where(flat, 0.0, (ez1 * ey2 - ez2 * ey1) / safe)
    gy = np.where(flat, 0.0, (ex1 * ez2 - ex2 * ez1) / safe)
    return np.stack([x0, y0, z0, gx, gy], axis=1)


def _batches(counts: np.ndarray, limit: int) -> Iterator[tuple[int, int]]:
    """Split ``range(len(counts))`` into consecutive runs whose ``counts`` sum to ≤ ``limit``."""
    ends = np.cumsum(counts)
    start = 0
    while start < len(counts):
        base = int(ends[start - 1]) if start else 0
        stop = max(start + 1, int(np.searchsorted(ends, base + limit, side="right")))
        yield start, stop
        start = stop


def _rasterize(
    tx: np.ndarray, ty: np.ndarray, tz: np.ndarray, planes: np.ndarray, w: int, h: int
) -> np.ndarray:
    """Z-buffer the triangles; return the ``(h·w,)`` int64 key buffer (−1 = empty pixel).

    Every (triangle, pixel row) pair gets its exact span of covered pixel centres from the three
    edge functions, so the cost is proportional to the covered pixels for triangles of any size.
    """
    keybuf = np.full(w * h, -1, dtype=np.int64)
    if len(tx) == 0:
        return keybuf
    x0, x1, x2 = tx.T
    y0, y1, y2 = ty.T
    area2 = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
    # pixel i covers [i, i + 1) and is drawn when its centre i + 0.5 lies in the triangle
    i0 = np.clip(np.ceil(tx.min(axis=1) - 0.5), 0, w)
    i1 = np.clip(np.floor(tx.max(axis=1) - 0.5), -1, w - 1)
    j0 = np.clip(np.ceil(ty.min(axis=1) - 0.5), 0, h).astype(np.int64)
    rows = np.clip(np.floor(ty.max(axis=1) - 0.5), -1, h - 1).astype(np.int64) - j0 + 1
    tri = np.flatnonzero((i1 >= i0) & (rows > 0) & (area2 != 0.0))
    if tri.size == 0:
        return keybuf
    i0, i1, j0, rows = i0[tri], i1[tri], j0[tri], rows[tri]
    # Edge functions E_k(p) = A_k·x + B_k·y + C_k, oriented so that the inside is ≥ 0. A shared
    # edge gets exactly negated coefficients in its two triangles; widening both by 1e-6 px
    # (≫ rounding) means a pixel centre exactly on it is drawn by both and never by neither.
    sgn = np.where(area2[tri] > 0.0, 1.0, -1.0)[:, None]
    xa, xb, xc, ya, yb, yc = x0[tri], x1[tri], x2[tri], y0[tri], y1[tri], y2[tri]
    ea = np.stack([yb - yc, yc - ya, ya - yb], axis=1) * sgn
    eb = np.stack([xc - xb, xa - xc, xb - xa], axis=1) * sgn
    ec = np.stack([xb * yc - xc * yb, xc * ya - xa * yc, xa * yb - xb * ya], axis=1) * sgn
    ec += 1e-6 * np.hypot(ea, eb)
    pl = planes[tri]
    zlo, zhi = tz[tri].min(axis=1), tz[tri].max(axis=1)
    zmin = float(zlo.min())
    zscale = min(_DEPTH_STEPS_PER_PX, _ZQ_MAX / max(float(zhi.max()) - zmin, 1e-9))

    for s0, s1 in _batches(rows, _ROW_CHUNK):
        r = rows[s0:s1]
        t = np.repeat(np.arange(s0, s1), r)  # triangle of every span
        jj = j0[t] + (np.arange(len(t)) - np.repeat(np.cumsum(r) - r, r))
        yc_ = jj + 0.5
        xl, xr = np.full(len(t), -np.inf), np.full(len(t), np.inf)
        dead = np.zeros(len(t), dtype=bool)
        with np.errstate(divide="ignore", invalid="ignore"):
            for k in range(3):  # A·x ≥ −(B·y + C): a lower bound if A > 0, an upper one if A < 0
                a = ea[t, k]
                num = -(eb[t, k] * yc_ + ec[t, k])
                xk = num / a
                xl = np.where(a > 0.0, np.maximum(xl, xk), xl)
                xr = np.where(a < 0.0, np.minimum(xr, xk), xr)
                dead |= (a == 0.0) & (num > 0.0)
        il = np.maximum(np.ceil(xl - 0.5), i0[t])
        n = np.where(dead, 0, np.maximum(np.minimum(np.floor(xr - 0.5), i1[t]) - il + 1, 0)).astype(np.int64)
        il = il.astype(np.int64)
        zs = pl[t, 2] + pl[t, 3] * (il + 0.5 - pl[t, 0]) + pl[t, 4] * (yc_ - pl[t, 1])  # depth at span start
        for p0, p1 in _batches(n, _PIX_CHUNK):
            nn = n[p0:p1]
            total = int(nn.sum())
            if total == 0:
                continue
            sp = np.repeat(np.arange(p0, p1), nn)
            off = np.arange(total) - np.repeat(np.cumsum(nn) - nn, nn)
            tt = t[sp]
            z = zs[sp] + pl[tt, 3] * off
            np.clip(z, zlo[tt], zhi[tt], out=z)  # guards slivers with huge depth gradients
            keys = (((z - zmin) * zscale).astype(np.int64) << _FID_BITS) | tri[tt]
            np.maximum.at(keybuf, jj[sp] * w + il[sp] + off, keys)
    return keybuf


# --- edges ----------------------------------------------------------------------------------------


@dataclass
class _EdgeContext:
    tris: np.ndarray  # (n, 3) vertex ids of the drawn faces (shared ids = touching faces)
    owner: np.ndarray  # (n,) item index
    nvis: np.ndarray  # (n, 3) normals of the visible side
    planes: np.ndarray  # (n, 5) screen-space depth planes
    item_edges: np.ndarray  # (n_items,) bool
    cos_th: float  # crease threshold


def _edge_cover(fid: np.ndarray, w: int, h: int, ssaa: int, ctx: _EdgeContext) -> np.ndarray:
    """Per-pixel line coverage ``(h·w,)`` float32 from face-id/depth/normal discontinuities."""
    outline = np.zeros(w * h, dtype=bool)
    crease = np.zeros(w * h, dtype=bool)
    grid = fid.reshape(h, w)
    for a, b, ncols, step in ((grid[:, :-1], grid[:, 1:], w - 1, 1), (grid[:-1], grid[1:], w, w)):
        pairs = np.flatnonzero(a != b)
        for k in np.array_split(pairs, -(-pairs.size // _PIX_CHUNK) or 1):  # bounded temporaries
            if k.size == 0:
                continue
            r, c = np.divmod(k, ncols)
            p = r * w + c
            q = p + step
            is_out, is_crease = _classify_pairs(p, q, fid[p], fid[q], w, ssaa, ctx)
            outline[p[is_out]] = True
            outline[q[is_out]] = True
            crease[p[is_crease]] = True
            crease[q[is_crease]] = True
    cover = np.zeros(w * h, dtype=np.float32)
    if crease.any():
        cover[_thicken(crease, w, h, round(_CREASE_PX * ssaa))] = _CREASE_INK
    if outline.any():
        cover[_thicken(outline, w, h, round(_OUTLINE_PX * ssaa))] = 1.0
    return cover


def _classify_pairs(
    p: np.ndarray, q: np.ndarray, fa: np.ndarray, fb: np.ndarray, w: int, ssaa: int, ctx: _EdgeContext
) -> tuple[np.ndarray, np.ndarray]:
    """Decide which neighbouring pixel pairs with different faces get an outline / crease line."""
    bg = (fa < 0) | (fb < 0)
    obj = np.where(fa < 0, fb, fa)
    is_out = bg & ctx.item_edges[ctx.owner[obj]]
    is_crease = np.zeros(len(p), dtype=bool)
    both = np.flatnonzero(~bg)
    if both.size == 0:
        return is_out, is_crease
    a, b = fa[both], fb[both]
    oa, ob = ctx.owner[a], ctx.owner[b]
    cross = oa != ob
    drawable = np.where(cross, ctx.item_edges[oa] | ctx.item_edges[ob], ctx.item_edges[oa])
    ta, tb = ctx.tris[a], ctx.tris[b]
    touching = (ta[:, :, None] == tb[:, None, :]).any(axis=(1, 2))  # share a vertex: continuous
    # Depth jump between faces that share no vertex: evaluate each face's plane at the other
    # pixel and keep the smaller mismatch, so a steep (nearly edge-on) face cannot fake a jump.
    jump = np.zeros(len(both), dtype=bool)
    far = np.flatnonzero(~touching)
    if far.size:
        pa, pb = ctx.planes[a[far]], ctx.planes[b[far]]
        pp, qq = p[both[far]], q[both[far]]
        xp, yp, xq, yq = (pp % w) + 0.5, (pp // w) + 0.5, (qq % w) + 0.5, (qq // w) + 0.5

        def depth(pl: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
            return pl[:, 2] + pl[:, 3] * (x - pl[:, 0]) + pl[:, 4] * (y - pl[:, 1])

        gap = np.minimum(
            np.abs(depth(pa, xq, yq) - depth(pb, xq, yq)), np.abs(depth(pb, xp, yp) - depth(pa, xp, yp))
        )
        jump[far] = gap > _GAP_TOL_PX * ssaa
    bend = (ctx.nvis[a] * ctx.nvis[b]).sum(axis=1) < ctx.cos_th
    is_out[both] = drawable & jump
    is_crease[both] = drawable & ~jump & (bend | cross)
    return is_out, is_crease


def _thicken(mask: np.ndarray, w: int, h: int, width: int) -> np.ndarray:
    """Grow the 2-px-wide pair band to ``width`` px (symmetric steps, then one one-sided step)."""
    m = mask.reshape(h, w)
    extra = max(0, width - 2)
    for _ in range(extra // 2):
        m = _dilate(m, both_ways=True)
    if extra % 2:
        m = _dilate(m, both_ways=False)
    return m.ravel()


def _dilate(m: np.ndarray, *, both_ways: bool) -> np.ndarray:
    d = m.copy()
    d[1:, :] |= m[:-1, :]
    d[:, 1:] |= m[:, :-1]
    if both_ways:
        d[:-1, :] |= m[1:, :]
        d[:, :-1] |= m[:, 1:]
    return d


# --- compositing ----------------------------------------------------------------------------------


def _compose(
    fid: np.ndarray,
    hit: np.ndarray,
    face_rgb: np.ndarray,
    cover: np.ndarray | None,
    bg: np.ndarray | None,
    w: int,
    h: int,
    ssaa: int,
) -> Image.Image:
    """Shade, draw lines over, box-filter ``ssaa``×``ssaa`` blocks and convert to 8-bit.

    ``bg`` is an RGBA background (0..1) or None (transparent): an opaque one gives an RGB image,
    otherwise the result is RGBA composited over the background's own alpha.
    """
    lut = np.vstack([face_rgb, np.zeros((1, 3), dtype=np.float32)])  # fid −1 → last row: empty
    prem = lut[fid]  # premultiplied colour
    alpha = hit.astype(np.float32)
    if cover is not None:
        e = np.flatnonzero(cover)
        c = cover[e]
        prem[e] = prem[e] * (1.0 - c)[:, None] + c[:, None] * _EDGE_RGB
        alpha[e] = alpha[e] * (1.0 - c) + c
    prem = _downsample(prem.reshape(h * ssaa, w * ssaa, 3), ssaa)
    alpha = _downsample(alpha.reshape(h * ssaa, w * ssaa), ssaa)
    if bg is not None and bg[3] >= 1.0:  # opaque background, composited in place (big images)
        clear = 1.0 - alpha
        for c in range(3):
            prem[..., c] += clear * bg[c]
        out = prem
    else:
        if bg is not None and bg[3] > 0.0:  # translucent background under the model
            prem = prem + ((1.0 - alpha) * bg[3])[..., None] * bg[:3]
            alpha = alpha + (1.0 - alpha) * bg[3]
        rgb = np.divide(prem, alpha[..., None], out=np.zeros_like(prem), where=alpha[..., None] > 0)
        if bg is not None:
            rgb[alpha <= 0.0] = bg[:3]  # fully clear pixels keep the requested colour
        out = np.dstack([rgb, alpha])
    np.clip(out, 0.0, 1.0, out=out)
    out *= 255.0
    return Image.fromarray(np.rint(out, out=out).astype(np.uint8))


def _downsample(a: np.ndarray, ssaa: int) -> np.ndarray:
    """Average ``ssaa``×``ssaa`` pixel blocks (box filter) of an ``(H·s, W·s[, C])`` array."""
    if ssaa == 1:
        return a
    out = a[::ssaa, ::ssaa].astype(np.float32, copy=True)
    for dy in range(ssaa):
        for dx in range(ssaa):
            if dy or dx:
                out += a[dy::ssaa, dx::ssaa]
    out *= 1.0 / (ssaa * ssaa)
    return out
