"""Colour helpers for renders: colour parsing, the part palette and heat-map colouring.

``heat_colors`` maps one scalar per face (overhang angle, wall thickness, temperature, …) to
uint8 RGB face colours for :class:`piforge.render.RenderItem`. The default ``"turbo"`` colormap
is built in (no matplotlib import); any other matplotlib colormap name is looked up lazily.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from PIL import ImageColor

from piforge.core.errors import NotFoundError, ValidationError

ColorLike = str | Sequence[float] | np.ndarray

DEFAULT_COLOR = "#9aa4b2"  # slate grey-blue: readable shading, dark edges stay visible
PALETTE: tuple[str, ...] = (  # assigned in order to bare meshes passed as a list
    "#9aa4b2",  # slate
    "#e0a458",  # amber
    "#5b9bd5",  # blue
    "#7fb77e",  # green
    "#d9746a",  # coral
    "#a98bd6",  # lavender
    "#d4c05a",  # mustard
    "#5fb8b2",  # teal
)
NAN_COLOR: tuple[int, int, int] = (150, 150, 150)  # faces whose value is NaN ("no data")

# Google's Turbo colormap (Mikhailov 2019), every 5th entry of the 256-entry LUT (as shipped by
# matplotlib); linear interpolation reproduces the full LUT within 1/255.
_TURBO = (
    np.array(
        [
            48, 18, 59, 54, 33, 95, 59, 47, 128, 63, 62, 156, 66, 75, 181, 69, 89, 203, 70, 102, 221,
            71, 115, 235, 70, 128, 246, 69, 140, 253, 64, 153, 255, 56, 165, 251, 47, 178, 244, 39,
            190, 233, 31, 201, 221, 26, 212, 208, 24, 221, 194, 26, 228, 182, 34, 235, 170, 47, 241,
            155, 63, 246, 138, 82, 250, 122, 101, 253, 105, 121, 254, 89, 139, 255, 75, 156, 254, 64,
            169, 251, 57, 183, 247, 53, 195, 241, 52, 208, 234, 52, 219, 226, 54, 229, 217, 56, 238,
            207, 58, 245, 197, 58, 250, 186, 57, 253, 174, 53, 254, 161, 48, 254, 147, 42, 252, 132,
            35, 249, 117, 29, 244, 102, 23, 239, 88, 17, 232, 75, 12, 225, 65, 9, 216, 55, 6, 206,
            45, 4, 195, 37, 3, 183, 29, 2, 169, 22, 1, 155, 15, 1, 139, 9, 2, 122, 4, 3,
        ],
        dtype=np.float64,
    ).reshape(-1, 3)
    / 255.0
)


def parse_color(color: ColorLike) -> tuple[float, float, float, float]:
    """Return ``(r, g, b, a)`` floats in 0..1.

    Accepts ``"#rgb"``, ``"#rrggbb"``, ``"#rrggbbaa"``, CSS colour names (``"red"``), and 3/4-element
    tuples/arrays. Tuples whose components are all ≤ 1 are read as 0..1 floats, otherwise as 0..255.
    """
    if isinstance(color, str):
        try:
            vals = np.array(ImageColor.getrgb(color.strip()), dtype=np.float64) / 255.0
        except ValueError as exc:
            raise ValidationError(
                f"Unknown colour {color!r}: use '#rrggbb', '#rgb', a CSS name or an RGB tuple."
            ) from exc
    else:
        try:
            vals = np.asarray(color, dtype=np.float64).reshape(-1)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Cannot read colour {color!r}.") from exc
        if vals.size not in (3, 4) or not np.isfinite(vals).all():
            raise ValidationError(f"A colour tuple needs 3 or 4 finite numbers, got {color!r}.")
        if vals.max() > 1.0:
            vals = vals / 255.0
        if vals.min() < 0.0 or vals.max() > 1.0:
            raise ValidationError(f"Colour components must be 0..1 or 0..255, got {color!r}.")
    if vals.size == 3:
        vals = np.append(vals, 1.0)
    r, g, b, a = (float(v) for v in vals)
    return r, g, b, a


def face_color_array(face_colors: np.ndarray, n_faces: int) -> np.ndarray:
    """Validate per-face colours and return them as ``(n_faces, 3)`` float32 in 0..1.

    Integer arrays are 0..255; float arrays are 0..1 (or 0..255 when any value exceeds 1). An
    alpha column is accepted and ignored (renders are opaque).
    """
    fc = np.asarray(face_colors)
    if fc.dtype.kind not in "buif":
        raise ValidationError(f"face_colors must be a numeric array, got dtype {fc.dtype}.")
    if fc.ndim != 2 or fc.shape[0] != n_faces or fc.shape[1] not in (3, 4):
        raise ValidationError(
            f"face_colors must have shape ({n_faces}, 3) or ({n_faces}, 4), got {fc.shape}."
        )
    rgb = fc[:, :3].astype(np.float32)
    if not np.isfinite(rgb).all():
        raise ValidationError("face_colors contains non-finite values.")
    if fc.dtype.kind != "f" or (rgb.size and rgb.max() > 1.0):
        rgb /= 255.0
    return np.clip(rgb, 0.0, 1.0)


def heat_colors(
    values: np.ndarray | Sequence[float],
    vmin: float | None = None,
    vmax: float | None = None,
    cmap: str = "turbo",
) -> np.ndarray:
    """Map one scalar per face to ``(n, 3)`` uint8 RGB colours.

    ``vmin``/``vmax`` default to the finite min/max of ``values``; values outside are clipped to
    the colormap ends (``±inf`` too). NaN maps to :data:`NAN_COLOR`. When ``vmin == vmax`` every
    value at or below it gets the low end and values above it the high end.
    """
    try:
        v = np.asarray(values, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"values must be numbers, got {type(values).__name__}.") from exc
    nan = np.isnan(v)  # before any branch: NaN must survive a constant (vmin == vmax) range
    finite = np.isfinite(v)
    if vmin is None:
        lo = float(v[finite].min()) if finite.any() else 0.0
    else:
        lo = as_number(vmin, "vmin")
    if vmax is None:
        hi = float(v[finite].max()) if finite.any() else 1.0
    else:
        hi = as_number(vmax, "vmax")
    if hi < lo:
        raise ValidationError(f"vmax ({hi}) is smaller than vmin ({lo}).")
    with np.errstate(invalid="ignore"):
        t = (v - lo) / (hi - lo) if hi > lo else np.where(v > lo, 1.0, 0.0)
    t = np.clip(np.where(nan, 0.0, t), 0.0, 1.0)
    out = np.round(_colormap(cmap, t) * 255.0).astype(np.uint8)
    out[nan] = NAN_COLOR
    return out


def as_number(value: object, name: str) -> float:
    """``value`` as a finite float; None, strings, bools or non-finite values → ValidationError."""
    if value is None or isinstance(value, (str, bytes, bool, np.bool_)):
        raise ValidationError(f"{name} must be a number, got {value!r}.")
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be a number, got {value!r}.") from exc
    if not math.isfinite(out):
        raise ValidationError(f"{name} must be finite, got {value!r}.")
    return out


def _colormap(name: str, t: np.ndarray) -> np.ndarray:
    """Evaluate colormap ``name`` at ``t`` (0..1) → ``(n, 3)`` float RGB in 0..1."""
    if not isinstance(name, str):
        raise ValidationError(f"cmap must be a colormap name, got {type(name).__name__}.")
    if name.strip().lower() == "turbo":
        knots = np.linspace(0.0, 1.0, len(_TURBO))
        return np.stack([np.interp(t, knots, _TURBO[:, c]) for c in range(3)], axis=-1)
    try:
        import matplotlib  # lazy: only for colormaps other than the built-in turbo
    except ImportError as exc:  # pragma: no cover - matplotlib is a hard dependency
        raise NotFoundError("colormap", name, ["turbo"]) from exc
    try:
        cm = matplotlib.colormaps[name]
    except KeyError:
        raise NotFoundError("colormap", name, ["turbo", *matplotlib.colormaps]) from None
    return np.asarray(cm(t), dtype=np.float64)[:, :3]
