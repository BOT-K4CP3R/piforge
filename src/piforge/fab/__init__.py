"""3D-printing fabrication: printer/material profiles, printability analysis, estimates, slicing.

Profiles load eagerly (they are light). The mesh tools — :func:`analyze_mesh`,
:func:`best_orientation`, :func:`estimate_print` (trimesh/scipy, ~2 s to import) — and the slicer
bridge load on first use, so ``import piforge.fab`` stays cheap for code that only needs profiles.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from piforge.fab.profiles import (
    CURRENCY,
    MATERIALS,
    PRINTERS,
    Material,
    PrinterProfile,
    get_material,
    get_printer,
)

if TYPE_CHECKING:  # static analysers see the lazy names as regular imports
    from piforge.fab.analyze import (
        PrintAnalysis,
        analyze_mesh,
        bridge_face_mask,
        overhang_mask,
        wall_thickness_samples,
    )
    from piforge.fab.estimate import PrintEstimate, estimate_print
    from piforge.fab.meshutil import place_on_bed, rotation_matrix
    from piforge.fab.orient import OrientationCandidate, best_orientation, candidate_rotations
    from piforge.fab.slicer import (
        SliceResult,
        SlicerInfo,
        SlicerNotFoundError,
        find_slicer,
        parse_gcode_stats,
        slice_stl,
    )

_LAZY: dict[str, str] = {
    "PrintAnalysis": "piforge.fab.analyze",
    "analyze_mesh": "piforge.fab.analyze",
    "bridge_face_mask": "piforge.fab.analyze",
    "overhang_mask": "piforge.fab.analyze",
    "wall_thickness_samples": "piforge.fab.analyze",
    "OrientationCandidate": "piforge.fab.orient",
    "best_orientation": "piforge.fab.orient",
    "candidate_rotations": "piforge.fab.orient",
    "PrintEstimate": "piforge.fab.estimate",
    "estimate_print": "piforge.fab.estimate",
    "SliceResult": "piforge.fab.slicer",
    "SlicerInfo": "piforge.fab.slicer",
    "SlicerNotFoundError": "piforge.fab.slicer",
    "find_slicer": "piforge.fab.slicer",
    "parse_gcode_stats": "piforge.fab.slicer",
    "slice_stl": "piforge.fab.slicer",
    "place_on_bed": "piforge.fab.meshutil",
    "rotation_matrix": "piforge.fab.meshutil",
}


def __getattr__(name: str) -> Any:
    """Import lazily exported names on first access (PEP 562)."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value  # later lookups skip __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


__all__ = [
    "CURRENCY",
    "MATERIALS",
    "PRINTERS",
    "Material",
    "OrientationCandidate",
    "PrintAnalysis",
    "PrintEstimate",
    "PrinterProfile",
    "SliceResult",
    "SlicerInfo",
    "SlicerNotFoundError",
    "analyze_mesh",
    "best_orientation",
    "bridge_face_mask",
    "candidate_rotations",
    "estimate_print",
    "find_slicer",
    "get_material",
    "get_printer",
    "overhang_mask",
    "parse_gcode_stats",
    "place_on_bed",
    "rotation_matrix",
    "slice_stl",
    "wall_thickness_samples",
]
