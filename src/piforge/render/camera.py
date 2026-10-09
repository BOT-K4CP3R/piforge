"""Orthographic cameras for headless renders: named views and ``(azimuth, elevation)`` pairs.

Conventions (Z up, millimetres, never mirrored):

* ``(azimuth_deg, elevation_deg)`` gives the direction from the scene *toward the camera*:
  ``toward = (cos el·cos az, cos el·sin az, sin el)``. Azimuth is measured in the XY plane,
  counter-clockwise seen from above, starting at +X (so 0° = camera on +X, −90° = camera on −Y);
  elevation is the angle above the XY plane (+90° looks straight down).
* Image right = ``(−sin az, cos az, 0)`` (always horizontal), image up = ``toward × right``.
  Hence ``right × up = toward``: a right-handed, non-mirrored picture from every direction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from piforge.core.errors import NotFoundError, ValidationError

_ISO_ELEVATION = math.degrees(math.atan(1.0 / math.sqrt(2.0)))  # 35.264°: direction (1, -1, 1)

VIEWS: dict[str, tuple[float, float]] = {
    # name: (azimuth_deg, elevation_deg) — see the module docstring for the convention.
    "iso": (-45.0, _ISO_ELEVATION),  # camera at (+1, -1, +1): front-right-top; +Z up, +X/+Y go right
    "front": (-90.0, 0.0),  # camera at -Y looking +Y; image right = +X, up = +Z
    "back": (90.0, 0.0),  # camera at +Y looking -Y; image right = -X, up = +Z
    "right": (0.0, 0.0),  # camera at +X looking -X; image right = +Y, up = +Z
    "left": (180.0, 0.0),  # camera at -X looking +X; image right = -Y, up = +Z
    "top": (-90.0, 90.0),  # camera at +Z looking down; image right = +X, up = +Y
    "bottom": (-90.0, -90.0),  # camera at -Z looking up; image right = +X, up = -Y
}

_FROM = {  # human-readable camera position for labels
    "iso": "from +X -Y +Z",
    "front": "from -Y",
    "back": "from +Y",
    "right": "from +X",
    "left": "from -X",
    "top": "from +Z",
    "bottom": "from -Z",
}


@dataclass(frozen=True)
class Camera:
    """An orthographic view: unit vectors of the image axes and the viewing direction (world)."""

    name: str
    azimuth: float
    elevation: float
    right: np.ndarray  # world direction drawn towards image +x
    up: np.ndarray  # world direction drawn towards the top of the image
    toward: np.ndarray  # from the scene toward the camera; larger depth = nearer

    @property
    def basis(self) -> np.ndarray:
        """3×3 matrix whose rows are ``right``, ``up``, ``toward`` (world → camera coordinates)."""
        return np.stack([self.right, self.up, self.toward])

    @property
    def axis_aligned(self) -> bool:
        """True for straight front/back/left/right/top/bottom views (lengths are true to scale)."""
        return bool(np.abs(self.toward).max() > 1.0 - 1e-9)

    @property
    def label(self) -> str:
        """Short caption such as ``"front (from -Y)"`` or ``"az -30° el 20°"``."""
        if self.name in _FROM:
            return f"{self.name} ({_FROM[self.name]})"
        return f"az {self.azimuth:g}° el {self.elevation:g}°"


def camera_for(view: str | tuple[float, float] | Camera) -> Camera:
    """Resolve a view name (case-insensitive, see :data:`VIEWS`) or ``(azimuth, elevation)``."""
    if isinstance(view, Camera):
        return view
    if isinstance(view, str):
        key = view.strip().lower()
        if key not in VIEWS:
            raise NotFoundError("view", view, VIEWS)
        az, el = VIEWS[key]
        return _make(key, az, el)
    try:
        az, el = (float(x) for x in view)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"view must be one of {sorted(VIEWS)} or an (azimuth_deg, elevation_deg) pair, got {view!r}."
        ) from exc
    if not (math.isfinite(az) and math.isfinite(el)) or not -90.0 <= el <= 90.0:
        raise ValidationError(f"Need a finite azimuth and an elevation in [-90, 90], got {view!r}.")
    return _make(f"az{az:g}_el{el:g}", az, el)


def _make(name: str, az_deg: float, el_deg: float) -> Camera:
    az, el = math.radians(az_deg), math.radians(el_deg)
    toward = np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)])
    right = np.array([-math.sin(az), math.cos(az), 0.0])
    toward[np.abs(toward) < 1e-12] = 0.0  # exact axis views: no 6e-17 noise
    right[np.abs(right) < 1e-12] = 0.0
    up = np.cross(toward, right)
    up[np.abs(up) < 1e-12] = 0.0
    return Camera(name, az_deg, el_deg, right, up / np.linalg.norm(up), toward)
