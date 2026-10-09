"""Mechanical CAD library (build123d): parts, primitives, fasteners, Raspberry Pi boards, export, assembly.

Names are re-exported lazily so ``import piforge.mech`` stays cheap; the CAD kernel (build123d/OCP)
is imported on first use of a geometry function. Units: mm, Z up.

Quick tour::

    from piforge.mech import PartSpec, rounded_box, clearance_hole, get_board, Assembly, export_part
    lid = PartSpec("lid", rounded_box(90, 62, 3, radius=3) - clearance_hole("M3", 3).moved(...))
    pi = get_board("rpi4b"); pi.port("ethernet").center
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, str] = {
    # part
    "PartSpec": "part", "KINDS": "part", "normalize_color": "part",
    # primitives
    "EPS": "primitives", "rounded_box": "primitives", "hollow_box": "primitives", "slot": "primitives",
    "vent_slots": "primitives", "hex_vents": "primitives", "text_solid": "primitives", "emboss": "primitives", "engrave": "primitives",
    "chamfer_bottom": "primitives",
    # fasteners
    "MetricSize": "fasteners", "METRIC": "fasteners", "get_size": "fasteners",
    "hole_compensation": "fasteners", "clearance_hole": "fasteners", "tap_hole": "fasteners",
    "insert_hole": "fasteners", "counterbore_hole": "fasteners", "countersink_hole": "fasteners",
    "nut_trap": "fasteners", "screw": "fasteners", "standoff": "fasteners",
    # boards
    "Port": "boards", "BoardModel": "boards", "BOARDS": "boards", "get_board": "boards",
    # export
    "to_trimesh": "export", "export_part": "export", "export_parts": "export", "export_glb": "export",
    # assembly
    "Joint": "assembly", "Node": "assembly", "Assembly": "assembly", "DuplicateIdError": "assembly",
    "to_location": "assembly", "location_to_matrix": "assembly", "matrix_to_location": "assembly",
    # modules, enclosure, gears, mechanisms (Task 3)
    "ModuleModel": "modules", "MODULES": "modules", "get_module": "modules",
    "Enclosure": "enclosure", "EnclosureSpec": "enclosure", "PanelItem": "enclosure", "VentSpec": "enclosure",
    "gear_geometry": "gears", "gear_profile": "gears", "spur_gear": "gears", "rack": "gears",
    "center_distance": "gears", "check_mesh": "gears",
    "snap_fit_cantilever": "mechanisms", "hinge": "mechanisms", "cable_clip": "mechanisms",
    "din_rail_clip": "mechanisms", "funnel": "mechanisms", "chute": "mechanisms", "bearing_seat": "mechanisms",
    "servo_mount": "mechanisms", "shaft_coupler": "mechanisms",
    # split-flap display modules (money counter)
    "SplitFlapSpec": "splitflap", "SplitFlapModule": "splitflap",
    "digit_artwork": "splitflap_art", "flap_with_inlay": "splitflap_art", "flap_art": "splitflap_art",
    "SplitFlapHousing": "splitflap_housing", "HousingSpec": "splitflap_housing",
    "HousingElectronics": "splitflap_housing",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'piforge.mech' has no attribute {name!r}")
    value = getattr(importlib.import_module(f"piforge.mech.{module}"), name)
    globals()[name] = value  # cache for the next lookup
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
