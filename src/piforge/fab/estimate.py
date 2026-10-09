"""Mass, filament, cost and time estimates for an FDM print — no slicer needed.

The model is deliberately simple (expect ±20 % against a real slicer):

* **shell** — every surface gets a solid skin. Measured along the face normal it is the larger of
  ``perimeters × line_w`` (walls, laid horizontally) and ``top_bottom_layers × layer_h`` (skins,
  stacked vertically), each projected onto the normal. The shell is capped at the solid volume, so
  thin parts come out fully solid.
* **infill** — the remaining interior volume × ``infill``.
* **mass/filament/cost** — extruded volume × density, over the 1.75 mm filament cross-section,
  × ``Material.cost_per_kg``.
* **time** — extruded volume / (``speed_mm_s`` × ``line_w`` × ``layer_h``) plus a fixed overhead
  per layer for travel, retraction and the Z move.

The mesh is assumed to be in print orientation (Z up, see :func:`piforge.fab.meshutil.place_on_bed`).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import trimesh

from piforge.core.errors import ValidationError
from piforge.fab.profiles import Material, PrinterProfile, get_material, get_printer

logger = logging.getLogger(__name__)

# src: 1.75 mm is the filament standard of every printer in profiles.PRINTERS (vendor spec sheets).
FILAMENT_DIAMETER_MM = 1.75
# src: heuristic — a layer change (Z move, retraction, travel to the next island) costs ~1-3 s.
LAYER_CHANGE_S = 2.0


@dataclass
class PrintEstimate:
    """Rough print estimate.

    Units: g, m of filament, ``piforge.fab.profiles.CURRENCY``, hours, mm³. ``shell_volume_mm3``
    and ``infill_volume_mm3`` are *extruded* volumes (the infill one already × infill fraction).
    """

    mass_g: float
    filament_m: float
    cost: float
    time_h: float
    shell_volume_mm3: float
    infill_volume_mm3: float


def _require_mesh(mesh: Any) -> trimesh.Trimesh:
    """Return ``mesh`` if it is a non-empty, finite ``trimesh.Trimesh``; else raise ValidationError.

    Package-private: the other fab modules validate their mesh arguments with it too.
    """
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValidationError(
            f"Expected a trimesh.Trimesh, got {type(mesh).__name__}. "
            "Load files with trimesh.load(path, force='mesh')."
        )
    if len(mesh.faces) == 0:
        raise ValidationError("Mesh has no faces.")
    if not np.isfinite(mesh.vertices).all():
        raise ValidationError("Mesh has non-finite (NaN/inf) vertex coordinates; repair the export.")
    return mesh


def _check_count(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < 0:
        raise ValidationError(f"{name} must be a whole number ≥ 0, got {value!r}")
    return int(value)


def _check_fraction(name: str, value: Any) -> float:
    try:
        frac = float(value)
    except (TypeError, ValueError):
        frac = math.nan
    if not 0.0 <= frac <= 1.0:  # also rejects NaN
        raise ValidationError(f"{name} must be a fraction in [0, 1], got {value!r}")
    return frac


def estimate_print(
    mesh: trimesh.Trimesh,
    printer: str | PrinterProfile = "generic",
    material: str | Material = "PLA",
    *,
    infill: float = 0.15,
    perimeters: int = 2,
    top_bottom_layers: int = 4,
) -> PrintEstimate:
    """Estimate mass, filament length, cost and time of printing ``mesh``.

    ``infill`` is the sparse-infill fraction (0 = hollow shell, 1 = solid). Perimeter and
    skin counts default to common slicer defaults (2 perimeters, 4 top/bottom layers).
    """
    mesh = _require_mesh(mesh)
    prof = get_printer(printer)
    mat = get_material(material)
    infill = _check_fraction("infill", infill)
    perimeters = _check_count("perimeters", perimeters)
    top_bottom_layers = _check_count("top_bottom_layers", top_bottom_layers)

    nz = np.abs(mesh.face_normals[:, 2])
    horizontal = np.sqrt(np.clip(1.0 - nz**2, 0.0, 1.0))
    t_wall = perimeters * prof.line_w
    t_skin = top_bottom_layers * prof.layer_h
    shell_thickness = np.maximum(t_skin * nz, t_wall * horizontal)
    volume = abs(float(mesh.volume))
    shell = min(float(np.dot(mesh.area_faces, shell_thickness)), volume)
    infill_volume = (volume - shell) * infill
    extruded = shell + infill_volume

    mass_g = extruded / 1000.0 * mat.density_g_cm3
    filament_m = extruded / (math.pi * (FILAMENT_DIAMETER_MM / 2.0) ** 2) / 1000.0
    cost = mass_g / 1000.0 * mat.cost_per_kg
    height = float(mesh.extents[2])
    layers = math.ceil(height / prof.layer_h - 1e-9) if height > 0 else 0
    flow_mm3_s = prof.speed_mm_s * prof.line_w * prof.layer_h
    time_s = extruded / flow_mm3_s + layers * LAYER_CHANGE_S
    logger.debug("estimate: %.0f mm³ extruded, %d layers, %.0f s", extruded, layers, time_s)
    return PrintEstimate(
        mass_g=mass_g,
        filament_m=filament_m,
        cost=cost,
        time_h=time_s / 3600.0,
        shell_volume_mm3=shell,
        infill_volume_mm3=infill_volume,
    )
