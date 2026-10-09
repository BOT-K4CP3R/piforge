"""Multi-view review sheets and PNG export.

A sheet is a grid of orthographic views of the same items. Every cell has a header band with the
view name and camera position, a scale bar (straight views only) and an XYZ axis triad (X red,
Y green, Z blue; an axis pointing at the viewer is drawn as ⊙, away from the viewer as ⊗). The
straight views (front/back/left/right/top/bottom) share one scale so sizes compare across cells;
iso and custom views are fitted individually.
"""

from __future__ import annotations

import functools
import logging
import math
import os
from collections.abc import Iterable, Sequence
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo

from piforge.core.errors import ValidationError
from piforge.render.camera import Camera, camera_for
from piforge.render.colors import heat_colors
from piforge.render.raster import RenderInput, check_size, fit_scale, prepare_scene, render_scene

__all__ = ["add_colorbar", "render_views", "save_png"]

log = logging.getLogger(__name__)

ViewSpec = str | tuple[float, float] | Camera

_PAPER = (255, 255, 255)
_INK = (33, 37, 41)
_MUTED = (108, 117, 125)
_RULE = (206, 212, 218)
_AXIS_RGB = {"X": (214, 39, 40), "Y": (34, 150, 34), "Z": (31, 104, 200)}
_CELL_MARGIN = 0.07  # free border around the model inside a cell's drawing area
_MIN_CELL = 48  # px: smaller cells are unreadable

# (regular, bold) font candidates: (file, face index). Pillow's bundled font is the fallback.
_FONTS: tuple[tuple[tuple[str, int], tuple[str, int]], ...] = (
    (("/System/Library/Fonts/Helvetica.ttc", 0), ("/System/Library/Fonts/Helvetica.ttc", 1)),
    (
        ("/System/Library/Fonts/Supplemental/Arial.ttf", 0),
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
    ),
    (
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
    ),
    (("DejaVuSans.ttf", 0), ("DejaVuSans-Bold.ttf", 0)),
    (("C:/Windows/Fonts/arial.ttf", 0), ("C:/Windows/Fonts/arialbd.ttf", 0)),
)

Font = ImageFont.FreeTypeFont | ImageFont.ImageFont


def render_views(
    items: RenderInput,
    *,
    views: Sequence[ViewSpec] = ("iso", "front", "top", "right"),
    size: tuple[int, int] = (1200, 900),
    title: str | None = None,
    dims: bool = True,
) -> Image.Image:
    """Render several views of ``items`` onto one labelled sheet.

    Args:
        items: anything :func:`piforge.render.render` accepts.
        views: view names / ``(azimuth, elevation)`` pairs, laid out in a near-4:3-cell grid.
        size: sheet ``(width, height)``: integers up to 4096 px per side, as for ``render``.
            Each view is supersampled 2× unless its cell exceeds ~4 megapixels.
        title: optional heading printed across the top.
        dims: print the overall bounding box (X × Y × Z in mm) in a footer.

    Returns:
        An RGB image whose ``info`` holds ``piforge.views`` (comma-separated view names),
        ``piforge.dims`` (e.g. ``"89.0 × 60.0 × 30.0 mm"``) and ``piforge.title``;
        :func:`save_png` stores them as PNG text chunks.
    """
    width, height = check_size(size)
    cams = [camera_for(v) for v in _view_list(views)]
    if not cams:
        raise ValidationError("views must contain at least one view.")
    title = None if title is None else str(title)
    scene = prepare_scene(items)
    ext = scene.bounds[1] - scene.bounds[0]
    dims_text = " × ".join(f"{v:.1f}" for v in ext) + " mm"

    title_h = min(max(round(height * 0.05), 26), 64) if title else 0
    foot_h = min(max(round(height * 0.034), 20), 40) if dims else 0
    grid_h = height - title_h - foot_h
    cols, rows = _grid_shape(len(cams), width, grid_h)
    cell_w, cell_h = width / cols, grid_h / rows
    label_px = min(max(round(min(cell_h * 0.045, cell_w * 0.034)), 11), 22)
    band_h = label_px + 24
    if cell_w < _MIN_CELL or cell_h - band_h < _MIN_CELL:
        raise ValidationError(f"Sheet {size!r} is too small for {len(cams)} views.")

    sheet = Image.new("RGB", (width, height), _PAPER)
    draw = ImageDraw.Draw(sheet)
    boxes = [_cell_box(i, cols, rows, width, grid_h, title_h) for i in range(len(cams))]
    regions = [(x0 + 1, y0 + band_h, x1 - 1, y1 - 1) for x0, y0, x1, y1 in boxes]
    straight = [
        fit_scale(scene, cam, (r[2] - r[0], r[3] - r[1]), _CELL_MARGIN)
        for cam, r in zip(cams, regions, strict=True)
        if cam.axis_aligned
    ]
    common = min(straight) if straight else None
    bar_mm = _bar_length_mm(draw, cams, boxes, band_h, label_px, common)  # same bar in every cell
    bar = None if bar_mm is None or common is None else (bar_mm, bar_mm * common)

    for cam, box, (rx0, ry0, rx1, ry1) in zip(cams, boxes, regions, strict=True):
        frame = render_scene(
            scene,
            cam,
            (rx1 - rx0, ry1 - ry0),
            margin=_CELL_MARGIN,
            px_per_mm=common if cam.axis_aligned else None,
        )
        sheet.paste(frame.image, (rx0, ry0))
        _draw_header(draw, cam, box, band_h, label_px, bar)

    _draw_rules(draw, cols, rows, width, grid_h, title_h)
    if title:
        tp = round(title_h * 0.56)
        draw.text((12, (title_h - tp) // 2 - 1), title, font=_font(tp, bold=True), fill=_INK)
        draw.line([(0, title_h - 1), (width, title_h - 1)], fill=_RULE, width=1)
    if dims:
        fp = round(foot_h * 0.6)
        y = height - foot_h + (foot_h - fp) // 2 - 1
        draw.line([(0, height - foot_h), (width, height - foot_h)], fill=_RULE, width=1)
        size_text, bold = f"Size X × Y × Z: {dims_text}", _font(fp, bold=True)
        draw.text((12, y), size_text, font=bold, fill=_INK)
        note = f"{scene.n_items} item{'s' if scene.n_items != 1 else ''} · {len(scene.faces):,} triangles"
        if len(straight) > 1:
            note += " · straight views share one scale"
        f = _font(fp)
        note_x = width - 12 - draw.textlength(note, font=f)
        if note_x > 12 + draw.textlength(size_text, font=bold) + 24:  # the size always wins the space
            draw.text((note_x, y), note, font=f, fill=_MUTED)

    sheet.info["piforge.views"] = ",".join(c.name for c in cams)
    sheet.info["piforge.dims"] = dims_text
    if title:
        sheet.info["piforge.title"] = title
    return sheet


def save_png(img: Image.Image, path: str | os.PathLike[str]) -> Path:
    """Write ``img`` as PNG (creating parent folders) and return the path.

    ``info`` entries whose key starts with ``piforge.`` are stored as PNG text chunks, so a
    sheet's view names, dimensions and title travel with the file.
    """
    if not isinstance(img, Image.Image):
        raise ValidationError(f"save_png needs a PIL image, got {type(img).__name__}.")
    if not isinstance(path, (str, os.PathLike)):
        raise ValidationError(f"save_png needs a file path, got {type(path).__name__}.")
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = PngInfo()
    for key, value in img.info.items():
        if isinstance(key, str) and key.startswith("piforge.") and isinstance(value, str):
            meta.add_itxt(key, value)
    img.save(out, format="PNG", pnginfo=meta)
    log.debug("saved %s (%dx%d)", out, *img.size)
    return out


def add_colorbar(
    img: Image.Image,
    vmin: float,
    vmax: float,
    *,
    cmap: str = "turbo",
    label: str | None = None,
) -> Image.Image:
    """Return a copy of ``img`` with a heat-map legend appended below it.

    The bar uses the same mapping as :func:`~piforge.render.heat_colors` (``vmin`` at the left
    end, ``vmax`` at the right) with five labelled ticks; ``label`` (e.g. ``"wall [mm]"``) is
    printed to its left. ``info["piforge.colorbar"]`` records the colormap and range.
    """
    if not isinstance(img, Image.Image):
        raise ValidationError(f"add_colorbar needs a PIL image, got {type(img).__name__}.")
    try:
        lo, hi = float(vmin), float(vmax)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"vmin/vmax must be numbers, got {vmin!r}, {vmax!r}.") from exc
    if not (math.isfinite(lo) and math.isfinite(hi) and hi > lo):
        raise ValidationError(f"Need finite vmin < vmax for a colour bar, got {vmin!r}, {vmax!r}.")
    label = None if label is None else str(label)
    w, h = img.size
    band = min(max(round(w * 0.06), 44), 72)
    fpx = max(10, round(band * 0.27))
    mode = "RGBA" if img.mode == "RGBA" else "RGB"
    out = Image.new(mode, (w, h + band), _PAPER + ((255,) if mode == "RGBA" else ()))
    out.paste(img.convert(mode), (0, 0))
    draw = ImageDraw.Draw(out)
    bar_y, bar_h = h + 8, round(band * 0.3)
    x0 = 12.0
    if label:
        bold = _font(fpx, bold=True)
        draw.text((x0, bar_y + (bar_h - fpx) / 2 - 1), label, font=bold, fill=_INK)
        x0 += draw.textlength(label, font=bold) + 14
    ticks = [lo + i * (hi - lo) / 4 for i in range(5)]
    tick_font = _font(fpx)
    x1 = w - 12 - draw.textlength(f"{ticks[-1]:.3g}", font=tick_font) / 2
    n = int(x1 - x0)
    if n < 20:
        raise ValidationError(f"Image too narrow ({w} px) for a colour bar.")
    colors = heat_colors(np.linspace(lo, hi, n), lo, hi, cmap)
    out.paste(Image.fromarray(np.repeat(colors[None], bar_h, axis=0)).convert(mode), (round(x0), bar_y))
    draw.rectangle((round(x0) - 1, bar_y - 1, round(x0) + n, bar_y + bar_h), outline=_INK, width=1)
    for i, value in enumerate(ticks):
        x = round(x0) + i * (n - 1) / 4
        draw.line([(x, bar_y + bar_h), (x, bar_y + bar_h + 4)], fill=_INK, width=1)
        _centered_text(draw, (x, bar_y + bar_h + 6 + fpx / 2), f"{value:.3g}", tick_font, _INK)
    out.info.update(img.info)
    out.info["piforge.colorbar"] = f"{cmap} {lo:g} .. {hi:g}" + (f" {label}" if label else "")
    return out


# --- layout ---------------------------------------------------------------------------------------


def _view_list(views: Sequence[ViewSpec] | ViewSpec) -> list[ViewSpec]:
    if isinstance(views, (str, Camera)):
        return [views]
    if not isinstance(views, Iterable):
        raise ValidationError(f"views must be a sequence of view names or (az, el) pairs, got {views!r}.")
    seq = list(views)
    if len(seq) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in seq):
        return [(float(seq[0]), float(seq[1]))]  # a single (azimuth, elevation) pair
    return seq


def _grid_shape(n: int, width: int, height: int) -> tuple[int, int]:
    """Columns × rows whose cells are closest to 4:3, penalising empty cells."""
    best: tuple[float, int, int] | None = None
    for cols in range(1, n + 1):
        rows = -(-n // cols)
        aspect = (width / cols) / max(height / rows, 1e-9)
        score = abs(math.log(aspect / (4.0 / 3.0))) + 0.25 * (cols * rows - n)
        if best is None or score < best[0]:
            best = (score, cols, rows)
    assert best is not None
    return best[1], best[2]


def _cell_box(i: int, cols: int, rows: int, width: int, grid_h: int, top: int) -> tuple[int, int, int, int]:
    r, c = divmod(i, cols)
    return (
        round(c * width / cols),
        top + round(r * grid_h / rows),
        round((c + 1) * width / cols),
        top + round((r + 1) * grid_h / rows),
    )


def _draw_rules(draw: ImageDraw.ImageDraw, cols: int, rows: int, width: int, grid_h: int, top: int) -> None:
    """Thin separators between grid cells (empty trailing cells simply stay blank)."""
    for c in range(1, cols):
        x = round(c * width / cols)
        draw.line([(x, top), (x, top + grid_h)], fill=_RULE, width=1)
    for r in range(1, rows):
        y = top + round(r * grid_h / rows)
        draw.line([(0, y), (width, y)], fill=_RULE, width=1)


# --- cell header: label, scale bar, axis triad ----------------------------------------------------


def _label_width(draw: ImageDraw.ImageDraw, cam: Camera, label_px: int) -> float:
    name, _, rest = cam.label.partition(" (")
    width = draw.textlength(name + (" " if rest else ""), font=_font(label_px, bold=True))
    return width + (draw.textlength("(" + rest, font=_font(label_px)) if rest else 0.0)


def _bar_length_mm(
    draw: ImageDraw.ImageDraw,
    cams: list[Camera],
    boxes: list[tuple[int, int, int, int]],
    band_h: int,
    label_px: int,
    px_per_mm: float | None,
) -> float | None:
    """One scale-bar length (mm) that fits between label and triad in every straight cell."""
    rooms = []
    for cam, (x0, _, x1, _) in zip(cams, boxes, strict=True):
        if cam.axis_aligned:
            xs = _triad_layout(cam, band_h)[3]
            triad_left = x1 - 10 - (max(xs) - min(xs))
            rooms.append(min(0.22 * (x1 - x0), triad_left - 14 - (x0 + 8 + _label_width(draw, cam, label_px)) - 16))
    if not rooms or px_per_mm is None:
        return None
    nice = [m * 10.0**k for k in range(-2, 6) for m in (1.0, 2.0, 5.0)]
    fitting = [v for v in nice if 12 <= v * px_per_mm <= min(rooms)]
    return max(fitting) if fitting else None


def _draw_header(
    draw: ImageDraw.ImageDraw,
    cam: Camera,
    box: tuple[int, int, int, int],
    band_h: int,
    label_px: int,
    bar: tuple[float, float] | None,
) -> None:
    """View label (left), scale bar (``(mm, px)``, straight views) and axis triad (right)."""
    x0, y0, x1, _ = box
    name, _, rest = cam.label.partition(" (")
    bold = _font(label_px, bold=True)
    ty = y0 + max(3, (band_h - label_px) // 2 - 3)
    draw.text((x0 + 8, ty), name, font=bold, fill=_INK)
    if rest:
        tx = x0 + 8 + draw.textlength(name + " ", font=bold)
        draw.text((tx, ty), "(" + rest, font=_font(label_px), fill=_MUTED)
    right = _draw_triad(draw, cam, x1 - 10, y0, band_h)
    if cam.axis_aligned and bar is not None:
        _draw_scale_bar(draw, bar[0], bar[1], right - 14, y0, band_h)


def _triad_shapes(cam: Camera, length: float, fpx: int) -> tuple[list, float, list, list]:
    """Triad geometry relative to its origin: shapes, circle radius and the x/y extents."""
    axes = [(n, float(e @ cam.right), -float(e @ cam.up), float(e @ cam.toward)) for n, e in zip("XYZ", np.eye(3))]
    # an axis along the line of sight is a ⊙/⊗ circle; its letter sits beside it, on the side
    # where no other axis points
    side = -1.0 if any(dx > 0.5 for _, dx, dy, _ in axes if math.hypot(dx, dy) >= 0.25) else 1.0
    rad = length * 0.32
    shapes = []  # (axis, toward, line end or None, label centre)
    for name, dx, dy, toward in axes:
        unit = math.hypot(dx, dy)
        if unit >= 0.25:
            end = (dx * length, dy * length)
            lab = (end[0] + dx / unit * (0.5 * fpx + 1), end[1] + dy / unit * (0.5 * fpx + 1))
            shapes.append((name, toward, end, lab))
        else:
            shapes.append((name, toward, None, (side * (rad + 0.6 * fpx + 1), 0.0)))
    xs = [-rad, rad] + [v for s in shapes for v in ((s[2] or (0, 0))[0], s[3][0] - fpx / 2, s[3][0] + fpx / 2)]
    ys = [-rad, rad] + [v for s in shapes for v in ((s[2] or (0, 0))[1], s[3][1] - fpx / 2, s[3][1] + fpx / 2)]
    return shapes, rad, xs, ys


def _triad_layout(cam: Camera, band_h: int) -> tuple[int, list, float, list, list]:
    """Triad sized so that it and its letters fit the band height: ``(font px, shapes, …)``."""
    length = band_h * 0.5
    for _ in range(4):
        fpx = max(9, round(length * 0.6))
        shapes, rad, xs, ys = _triad_shapes(cam, length, fpx)
        height = max(ys) - min(ys)
        if height <= band_h - 4:
            break
        length *= (band_h - 4) / height
    return fpx, shapes, rad, xs, ys


def _draw_triad(draw: ImageDraw.ImageDraw, cam: Camera, right: float, top: int, band_h: int) -> float:
    """Draw the XYZ triad right-aligned at ``right`` inside the band; return its left edge."""
    fpx, shapes, rad, xs, ys = _triad_layout(cam, band_h)
    font = _font(fpx, bold=True)
    ox = right - max(xs)
    oy = top + band_h / 2 - (min(ys) + max(ys)) / 2
    gap = rad + 1 if any(s[2] is None for s in shapes) else 0.0  # lines start outside a ⊙/⊗ circle
    for name, toward, end, lab in sorted(shapes, key=lambda s: s[1]):  # far axes first
        color = _AXIS_RGB[name]
        if end is None:
            draw.ellipse((ox - rad, oy - rad, ox + rad, oy + rad), outline=color, width=2)
            if toward > 0:  # pointing at the viewer
                draw.ellipse((ox - 2, oy - 2, ox + 2, oy + 2), fill=color)
            else:  # pointing away
                k = rad * 0.55
                draw.line([(ox - k, oy - k), (ox + k, oy + k)], fill=color, width=2)
                draw.line([(ox - k, oy + k), (ox + k, oy - k)], fill=color, width=2)
        else:
            f = gap / math.hypot(*end)
            draw.line([(ox + end[0] * f, oy + end[1] * f), (ox + end[0], oy + end[1])], fill=color, width=2)
        _centered_text(draw, (ox + lab[0], oy + lab[1]), name, font, color)
    return ox + min(xs)


def _draw_scale_bar(
    draw: ImageDraw.ImageDraw, length_mm: float, bar: float, right: float, top: int, band_h: int
) -> None:
    """A ``bar``-pixel bar labelled ``length_mm``, right-aligned at ``right``."""
    font = _font(max(9, round(band_h * 0.34)))
    y = top + band_h - 8
    x0 = right - bar
    draw.line([(x0, y), (right, y)], fill=_INK, width=2)
    draw.line([(x0, y - 4), (x0, y + 1)], fill=_INK, width=2)
    draw.line([(right, y - 4), (right, y + 1)], fill=_INK, width=2)
    _centered_text(draw, ((x0 + right) / 2, y - 4 - band_h * 0.24), f"{length_mm:g} mm", font, _INK)


def _centered_text(
    draw: ImageDraw.ImageDraw, at: tuple[float, float], text: str, font: Font, fill: tuple[int, ...]
) -> None:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((at[0] - (left + right) / 2, at[1] - (top + bottom) / 2), text, font=font, fill=fill)


@functools.lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> Font:
    """A TrueType font of ``size`` px (bold if available), falling back to Pillow's own font."""
    regular = [r for r, _ in _FONTS]
    candidates = [b for _, b in _FONTS] + regular if bold else regular
    for path, index in candidates:
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError, ImportError):  # pragma: no cover - Pillow without FreeType
        return ImageFont.load_default()
