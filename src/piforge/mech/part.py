"""PartSpec — a named piece of geometry plus the metadata needed to print, export and assemble it.

The shape is any build123d ``Part``/``Compound``/``Solid`` in its *design frame* (mm, Z up). This
module never imports the CAD kernel itself; it only calls methods on the shape object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from piforge.core.errors import ValidationError
from piforge.fab.profiles import get_material

KINDS = ("printed", "reference", "fastener", "pcb", "wire")
_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

Vec3 = tuple[float, float, float]


def normalize_color(color: str) -> str:
    """Return ``color`` as lowercase ``#rrggbb``; accepts ``#rgb``/``#rrggbb`` (``#`` optional)."""
    m = _HEX.match(str(color).strip())
    if not m:
        raise ValidationError(f"Colour {color!r} is not a hex colour like '#4c8bf5'")
    digits = m.group(1).lower()
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return "#" + digits


def _vec3(value: Any, what: str) -> Vec3:
    try:
        vals = tuple(float(v) for v in value)
    except TypeError:
        raise ValidationError(f"{what} must be three numbers, got {value!r}") from None
    if len(vals) != 3:
        raise ValidationError(f"{what} must be three numbers (x, y, z), got {value!r}")
    return vals  # type: ignore[return-value]


@dataclass
class PartSpec:
    """A part of the device: geometry (design frame, mm) + kind, material, colour and print hints.

    ``kind`` is one of ``printed`` (exported for printing), ``reference`` (bought module or
    context geometry), ``fastener`` (screws, nuts, inserts), ``pcb`` (boards) or ``wire`` (a routed
    harness wire, :mod:`piforge.mech.harness`; world frame, never printed).
    ``print_rotation`` is (rx, ry, rz) in degrees about fixed X, then Y, then Z (same convention as
    :func:`piforge.fab.meshutil.rotation_matrix`), applied before the part is placed on the bed;
    ``None`` lets the exporter choose (auto-orientation).
    """

    name: str
    shape: object
    kind: str = "printed"
    material: str = "PLA"
    color: str = "#4c8bf5"
    quantity: int = 1
    print_rotation: tuple[float, float, float] | None = None
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValidationError("PartSpec.name must be a non-empty string")
        if self.shape is None or not hasattr(self.shape, "bounding_box"):
            raise ValidationError(
                f"PartSpec {self.name!r}: shape must be a build123d Part/Compound/Solid, "
                f"got {type(self.shape).__name__}")
        if self.kind not in KINDS:
            raise ValidationError(f"PartSpec {self.name!r}: kind {self.kind!r} is not one of {KINDS}")
        if self.kind == "printed":
            self.material = get_material(self.material).name  # NotFoundError lists close matches
        else:
            self.material = str(self.material)
        self.color = normalize_color(self.color)
        if int(self.quantity) != self.quantity or self.quantity < 1:
            raise ValidationError(f"PartSpec {self.name!r}: quantity must be a positive integer")
        self.quantity = int(self.quantity)
        if self.print_rotation is not None:
            self.print_rotation = _vec3(self.print_rotation, f"PartSpec {self.name!r}: print_rotation")
        self.meta = dict(self.meta or {})

    def bounds(self) -> tuple[Vec3, Vec3]:
        """Axis-aligned bounding box ((xmin, ymin, zmin), (xmax, ymax, zmax)) in mm, design frame."""
        bb = self.shape.bounding_box()  # type: ignore[attr-defined]
        return ((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))

    @property
    def volume(self) -> float:
        """Solid volume in mm³ (exact B-rep volume)."""
        return float(self.shape.volume)  # type: ignore[attr-defined]

    @property
    def size(self) -> Vec3:
        """Bounding-box extents (x, y, z) in mm."""
        lo, hi = self.bounds()
        return (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])
