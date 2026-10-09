"""Digit artwork for split-flap modules: sticker files (SVG + PNG) and a 2-colour inlay flap.

Display coordinates (binding): ``u`` to the viewer's right, ``w`` up, origin on the split line
(the spool axis height) at the window centre; digits are ``digit_height`` tall and centred on it.

Which half goes where (see :mod:`piforge.mech.splitflap`): while flap ``k`` stands in the window, its
**front** shows the **top half of digit k** (``w ∈ [seam/2, seam/2 + flap_height]``) and the flap
that fell before it shows its **back** with the **bottom half** of the same digit. So the back of
flap ``k`` carries the bottom half of digit ``k + 1`` (``w ∈ [−seam/2 − flap_height, −seam/2]``),
the flap after it in the sequence (mod 20).

Orientation of the exported files: every per-flap file is drawn *as seen on the display* (front:
pin edge at the bottom; back: pin edge at the top, because the fallen flap hangs upside down). To
apply a back sticker flip the flap over its pin axis (pin edge up, back face towards you) and stick
it on upright — no mirroring is involved. ``sheet_front.svg``/``sheet_back.svg`` lay the stickers
out for duplex printing (flip on the long edge): the back sheet's cells are mirrored left↔right and
their content turned 180°, exactly as the back of each front cell.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import build123d as bd
from build123d import Location

from piforge.core.errors import ValidationError
from piforge.mech.part import PartSpec
from piforge.mech.primitives import EPS, _as_part

if TYPE_CHECKING:  # pragma: no cover
    from piforge.mech.splitflap import SplitFlapModule

__all__ = ["comma_faces", "digit_artwork", "digit_faces", "flap_art", "flap_with_inlay"]

_BASE = 100.0  # font size used to measure the font


def _module(module: "SplitFlapModule | None") -> "SplitFlapModule":
    if module is None:
        from piforge.mech.splitflap import SplitFlapModule

        return SplitFlapModule()
    return module


@lru_cache(maxsize=16)
def _metrics(font: str, bold: bool) -> tuple[float, float]:
    """(figure height, figure centre y) of the font at size ``_BASE``, measured on "1" (flat top and
    baseline; text placement is metric-based so every glyph shares this vertical frame)."""
    t = bd.Text("1", _BASE, font=font, font_style=_style(bold))
    bb = t.bounding_box()
    return bb.max.Y - bb.min.Y, (bb.max.Y + bb.min.Y) / 2


def _style(bold: bool) -> bd.FontStyle:
    return bd.FontStyle.BOLD if bold else bd.FontStyle.REGULAR


@lru_cache(maxsize=64)
def _glyph(text: str, digit_height: float, font: str, bold: bool) -> tuple:
    """Faces of ``text`` in display coordinates: figure height = ``digit_height``, centred on w = 0,
    horizontally centred on the glyph's own bounding box."""
    h, yc = _metrics(font, bold)
    size = _BASE * digit_height / h
    sk = bd.Text(text, size, font=font, font_style=_style(bold))
    bb = sk.bounding_box()
    moved = sk.moved(Location((-(bb.min.X + bb.max.X) / 2, -yc * size / _BASE, 0)))
    return tuple(moved.faces())


def digit_faces(digit: int, digit_height: float = 50.0, *, font: str = "Arial", bold: bool = True) -> list[bd.Face]:
    """Faces of ``digit`` (0–9) in display coordinates (see the module docstring)."""
    if not (isinstance(digit, int) and 0 <= digit <= 9):
        raise ValidationError(f"digit must be an integer 0–9, got {digit!r}")
    return list(_glyph(str(digit), float(digit_height), font, bool(bold)))


def comma_faces(module: "SplitFlapModule | None" = None, *, font: str = "Arial", bold: bool = True) -> list[bd.Face]:
    """Faces of "," at the digits' scale and baseline, centred horizontally on u = 0."""
    m = _module(module)
    return list(_glyph(",", float(m.spec.digit_height), font, bool(bold)))


def _regions(m: "SplitFlapModule") -> dict[str, tuple[float, float, float, float]]:
    """(u0, u1, w0, w1) of the front (upper flap) and back (lower flap) sticker areas."""
    w2 = m.spec.flap_width / 2
    h0 = m.spec.seam / 2
    return {"front": (-w2, w2, h0, h0 + m.flap_height), "back": (-w2, w2, -h0 - m.flap_height, -h0)}


def _clip(faces: list[bd.Face], box: tuple[float, float, float, float]) -> list[bd.Face]:
    u0, u1, w0, w1 = box
    rect = bd.Rectangle(u1 - u0, w1 - w0).moved(Location(((u0 + u1) / 2, (w0 + w1) / 2, 0)))
    out: list[bd.Face] = []
    for f in faces:
        res = f & rect
        out.extend(res.faces() if res is not None else [])
    return [f for f in out if f.area > 1e-6]


def flap_art(k: int, module: "SplitFlapModule | None" = None, *, font: str = "Arial",
             bold: bool = True) -> dict[str, list[bd.Face]]:
    """Clipped artwork of flap ``k``: ``{"front": top half of digit k, "back": bottom half of digit
    k + 1}`` as faces in display coordinates."""
    m = _module(module)
    n = m.spec.flaps
    if not (isinstance(k, int) and 0 <= k < n):
        raise ValidationError(f"flap index must be 0…{n - 1}, got {k!r}")
    reg = _regions(m)
    front = digit_faces(m.flap_digit(k), m.spec.digit_height, font=font, bold=bold)
    back = digit_faces(m.flap_digit((k + 1) % n), m.spec.digit_height, font=font, bold=bold)
    return {"front": _clip(front, reg["front"]), "back": _clip(back, reg["back"])}


# ---------------------------------------------------------------------------------------------
# polylines, SVG and PNG
# ---------------------------------------------------------------------------------------------
def _wire_points(wire: bd.Wire, deflection: float = 0.01) -> list[tuple[float, float]]:
    """Ordered (u, w) points of a closed wire (curves sampled to ``deflection`` mm)."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepTools import BRepTools_WireExplorer
    from OCP.GCPnts import GCPnts_QuasiUniformDeflection
    from OCP.TopAbs import TopAbs_REVERSED

    pts: list[tuple[float, float]] = []
    exp = BRepTools_WireExplorer(wire.wrapped)
    while exp.More():
        edge = exp.Current()
        curve = BRepAdaptor_Curve(edge)
        disc = GCPnts_QuasiUniformDeflection(curve, deflection)
        if disc.IsDone() and disc.NbPoints() >= 2:
            seg = [curve.Value(disc.Parameter(i)) for i in range(1, disc.NbPoints() + 1)]
        else:
            seg = [curve.Value(curve.FirstParameter()), curve.Value(curve.LastParameter())]
        if edge.Orientation() == TopAbs_REVERSED:
            seg.reverse()
        pts.extend((p.X(), p.Y()) for p in seg[:-1])
        exp.Next()
    return pts


def _face_loops(face: bd.Face) -> tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]:
    return _wire_points(face.outer_wire()), [_wire_points(w) for w in face.inner_wires()]


def _svg_path(faces: list[bd.Face], to_px) -> str:
    parts = []
    for f in faces:
        outer, holes = _face_loops(f)
        for loop in [outer] + holes:
            if len(loop) < 3:
                continue
            xy = [to_px(u, w) for u, w in loop]
            parts.append("M" + " L".join(f"{x:.3f},{y:.3f}" for x, y in xy) + " Z")
    return " ".join(parts)


def _svg_doc(width: float, height: float, body: str, paper: str | None) -> str:
    bg = f'<rect x="0" y="0" width="{width:.3f}" height="{height:.3f}" fill="{paper}"/>' if paper else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.3f}mm" height="{height:.3f}mm" '
            f'viewBox="0 0 {width:.3f} {height:.3f}">{bg}{body}</svg>\n')


def _png(faces: list[bd.Face], box: tuple[float, float, float, float], path: Path, dpi: int, ink: str,
         paper: str, rotate180: bool = False) -> None:
    from PIL import Image, ImageDraw

    u0, u1, w0, w1 = box
    k = dpi / 25.4
    wpx, hpx = max(1, round((u1 - u0) * k)), max(1, round((w1 - w0) * k))
    img = Image.new("RGB", (wpx, hpx), paper)
    draw = ImageDraw.Draw(img)

    def px(u: float, w: float) -> tuple[float, float]:
        return ((u - u0) * k, (w1 - w) * k)

    for f in faces:
        outer, holes = _face_loops(f)
        if len(outer) >= 3:
            draw.polygon([px(u, w) for u, w in outer], fill=ink)
        for h in holes:
            if len(h) >= 3:
                draw.polygon([px(u, w) for u, w in h], fill=paper)
    if rotate180:
        img = img.rotate(180)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", dpi=(dpi, dpi))


def digit_artwork(out_dir: "str | Path", module: "SplitFlapModule | None" = None, *, font: str = "Arial",
                  bold: bool = True, dpi: int = 600, ink: str = "#f2f2f2", paper: str = "#151515",
                  formats: tuple[str, ...] = ("svg", "png")) -> dict:
    """Export the sticker artwork of every flap into ``out_dir``.

    Files: ``flap_KK_front.svg|png`` (top half of digit KK mod 10) and ``flap_KK_back.svg|png``
    (bottom half of digit (KK+1) mod 10), each ``flap_width × flap_height`` mm in display orientation
    (see the module docstring), ``sheet_front.svg`` + ``sheet_back.svg`` (duplex A4 layout, flip on
    the long edge) and ``index.json`` (pairing, sizes, orientation notes). ``font`` is a font name
    (or a path to a .ttf/.otf file); unknown names fall back to the CAD kernel's default font.
    Returns the index as a dict (with ``files``: every path written).
    """
    m = _module(module)
    fmts = {f.lower() for f in formats}
    if not fmts <= {"svg", "png"} or not fmts:
        raise ValidationError(f"formats must be a subset of ('svg', 'png'), got {formats!r}")
    if not (isinstance(dpi, int) and 72 <= dpi <= 2400):
        raise ValidationError(f"dpi must be an integer 72…2400, got {dpi!r}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    reg = _regions(m)
    n = m.spec.flaps
    files: list[str] = []
    flaps = []
    arts = {k: flap_art(k, m, font=font, bold=bold) for k in range(n)}
    for k in range(n):
        rec = {"flap": k, "front_digit": m.flap_digit(k), "front_half": "top",
               "back_digit": m.flap_digit((k + 1) % n), "back_half": "bottom", "files": {}}
        for side in ("front", "back"):
            u0, u1, w0, w1 = reg[side]
            stem = f"flap_{k:02d}_{side}"
            if "svg" in fmts:
                path = _svg_path(arts[k][side], lambda u, w, u0=u0, w1=w1: (u - u0, w1 - w))
                body = f'<path d="{path}" fill="{ink}" fill-rule="evenodd"/>' if path else ""
                p = out / f"{stem}.svg"
                p.write_text(_svg_doc(u1 - u0, w1 - w0, body, paper), encoding="utf-8")
                files.append(str(p))
                rec["files"][f"{side}_svg"] = p.name
            if "png" in fmts:
                p = out / f"{stem}.png"
                _png(arts[k][side], reg[side], p, dpi, ink, paper)
                files.append(str(p))
                rec["files"][f"{side}_png"] = p.name
        flaps.append(rec)
    if "svg" in fmts:
        files += [str(p) for p in _sheets(out, m, arts, reg, ink, paper)]
    index = {
        "flaps": flaps,
        "size_mm": [m.spec.flap_width, m.flap_height],
        "digit_height_mm": m.spec.digit_height,
        "font": font, "bold": bold, "dpi": dpi,
        "orientation": ("Per-flap files are drawn as seen on the display. Front: pin edge (side tabs) at the "
                        "bottom. Back: pin edge at the top — flip the flap over its pin axis and apply upright. "
                        "sheet_back.svg is the duplex back of sheet_front.svg (flip on the long edge)."),
        "files": files,
    }
    p = out / "index.json"
    p.write_text(json.dumps(index, indent=1), encoding="utf-8")
    index["files"].append(str(p))
    return index


def _sheets(out: Path, m: "SplitFlapModule", arts: dict, reg: dict, ink: str, paper: str) -> list[Path]:
    """A4 portrait duplex sheets: cells of flap_width × flap_height with 4 mm gaps, hairline outlines."""
    sw, sh = 210.0, 297.0
    cw, ch = m.spec.flap_width, m.flap_height
    gap = 4.0
    cols = max(1, int((sw - 20 + gap) // (cw + gap)))
    rows = max(1, int((sh - 20 + gap) // (ch + gap)))
    n = m.spec.flaps
    if cols * rows < n:
        raise ValidationError(f"{n} stickers of {cw:g} × {ch:.1f} mm do not fit one A4 sheet")
    x0 = (sw - (cols * cw + (cols - 1) * gap)) / 2
    y0 = (sh - (rows * ch + (rows - 1) * gap)) / 2
    bodies = {"front": [], "back": []}
    for k in range(n):
        r, c = divmod(k, cols)
        cx, cy = x0 + c * (cw + gap), y0 + r * (ch + gap)
        # front cell: display orientation (pin edge at the bottom)
        u0, _u1, _w0, w1 = reg["front"]
        d = _svg_path(arts[k]["front"], lambda u, w, cx=cx, cy=cy, u0=u0, w1=w1: (cx + u - u0, cy + w1 - w))
        bodies["front"].append(_cell(cx, cy, cw, ch, d, ink, paper, f"{k:02d}"))
        # back cell: mirrored position, content turned 180° (pin edge at the bottom, like the front)
        bx = sw - cx - cw
        u0, u1, w0, w1 = reg["back"]
        d = _svg_path(arts[k]["back"], lambda u, w, bx=bx, cy=cy, u1=u1, w0=w0: (bx + u1 - u, cy + w - w0))
        bodies["back"].append(_cell(bx, cy, cw, ch, d, ink, paper, f"{k:02d}b"))
    paths = []
    for side in ("front", "back"):
        p = out / f"sheet_{side}.svg"
        p.write_text(_svg_doc(sw, sh, "".join(bodies[side]), None), encoding="utf-8")
        paths.append(p)
    return paths


def _cell(x: float, y: float, w: float, h: float, d: str, ink: str, paper: str, label: str) -> str:
    path = f'<path d="{d}" fill="{ink}" fill-rule="evenodd"/>' if d else ""
    return (f'<g id="flap_{label}"><rect x="{x:.3f}" y="{y:.3f}" width="{w:.3f}" height="{h:.3f}" fill="{paper}" '
            f'stroke="#999999" stroke-width="0.1"/>{path}'
            f'<text x="{x + 0.8:.3f}" y="{y + h + 2.6:.3f}" font-size="2.2" fill="#666666" '
            f'font-family="sans-serif">{label}</text></g>')


# ---------------------------------------------------------------------------------------------
# 2-colour flap
# ---------------------------------------------------------------------------------------------
def _inlay_solid(faces: list[bd.Face], side: str, m: "SplitFlapModule", z0: float, z1: float) -> "bd.Part | None":
    """Faces (display coords) → solid in the flap's local frame, ``z0…z1`` deep below the face.

    Front: (u, w) → (x = u, z = w − h) on y = −t/2, going +Y. Back (the flap hangs upside down when
    it shows): (u, w) → (x = u, z = −w − h) on y = +t/2, going −Y. ``h`` = pin height above the axis.
    """
    if not faces:
        return None
    t2 = m.spec.flap_thickness / 2
    h = m.spec.pitch_radius * math.sin(math.radians(m.pitch_angle / 2))
    solids = []
    for f in faces:
        ext = bd.extrude(f, z1 - z0).moved(Location((0, 0, -z1)))  # z ∈ [−z1, −z0]
        if side == "front":
            loc = Location((0, -t2, -h), (90, 0, 0))  # y = −t/2 − z ∈ [−t/2 + z0, −t/2 + z1]
        else:
            loc = Location((0, t2, -h), (-90, 0, 0))  # y = t/2 + z ∈ [t/2 − z1, t/2 − z0]
        solids.append(_as_part(ext.moved(loc)))
    out = solids[0]
    for s in solids[1:]:
        out = out + s
    return _as_part(out)


def flap_with_inlay(k: int, module: "SplitFlapModule | None" = None, *, depth: float = 0.4, font: str = "Arial",
                    bold: bool = True, margin: float = 0.3) -> list[PartSpec]:
    """2-colour variant of flap ``k`` for multi-material printing: ``[flap_KK, flap_KK_ink]``.

    ``flap_KK`` is the flap with ``depth``-deep pockets; ``flap_KK_ink`` fills them (top half of
    digit k on the front, bottom half of digit k + 1 on the back, both placed for the pose in which
    they are seen). Same local frame and print orientation as :attr:`SplitFlapModule.flap`; export
    both into one 3MF and assign the ink body the second filament. The art is kept ``margin`` mm
    inside the plate edges, so the seam shows a little wider than with stickers.
    """
    m = _module(module)
    t = m.spec.flap_thickness
    if not (0 < depth and 2 * depth + 0.4 <= t + 1e-9):
        raise ValidationError(f"inlay depth {depth} mm leaves < 0.4 mm core in a {t} mm flap (both faces)")
    art = flap_art(k, m, font=font, bold=bold)
    reg = _regions(m)
    clipped = {side: _clip(art[side], (reg[side][0] + margin, reg[side][1] - margin,
                                       reg[side][2] + margin, reg[side][3] - margin)) for side in art}
    base = m.flap.shape
    ink = []
    body = base
    for side in ("front", "back"):
        solid = _inlay_solid(clipped[side], side, m, 0.0, depth)
        if solid is None:
            continue
        ink.append(solid)
        body = _as_part(body - _inlay_solid(clipped[side], side, m, -EPS, depth))
    if not ink:
        raise ValidationError(f"flap {k} has no artwork")
    inlay = ink[0] if len(ink) == 1 else _as_part(ink[0] + ink[1])
    s = m.spec
    meta = {"flap": k, "front_digit": m.flap_digit(k), "back_digit": m.flap_digit((k + 1) % s.flaps)}
    return [PartSpec(f"flap_{k:02d}", body, material=s.material, color=s.flap_color, print_rotation=(90, 0, 0),
                     meta={"role": "splitflap flap (2-colour body)", **meta}),
            PartSpec(f"flap_{k:02d}_ink", inlay, material=s.material, color=s.ink_color, print_rotation=(90, 0, 0),
                     meta={"role": "splitflap flap (2-colour ink)", **meta})]
