"""Printer profiles and filament materials.

Values are typical for FDM printing with a 0.4 mm nozzle. Material numbers are representative
manufacturer datasheet values for *printed* specimens (XY direction) — printed parts are weaker
across layers, which ``z_strength_factor`` captures. Costs are in ``CURRENCY`` per kg (Polish
retail, 2025-2026 typical).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from piforge.core.errors import NotFoundError

CURRENCY = "PLN"


@dataclass(frozen=True)
class Material:
    """A filament material. Units: g/cm³, °C, MPa, W/(m·K), CURRENCY/kg."""

    name: str
    density_g_cm3: float
    glass_transition_c: float
    max_service_c: float  # sustained use temperature before creep/softening becomes a problem
    youngs_modulus_mpa: float  # printed, XY
    tensile_strength_mpa: float  # printed, XY
    z_strength_factor: float  # fraction of XY strength across layers
    thermal_conductivity: float
    cost_per_kg: float
    allowable_strain: float  # for snap-fits (one-time-ish assembly), fraction
    notes: str = ""


# src: typical values from Prusament/Bambu/Polymaker technical data sheets (TDS); conservative ends.
MATERIALS: dict[str, Material] = {
    m.name: m
    for m in [
        Material("PLA", 1.24, 60.0, 50.0, 3000.0, 50.0, 0.6, 0.13, 80.0, 0.02,
                 "Easy, stiff; softens ~55 °C — avoid hot enclosures and cars."),
        Material("PETG", 1.27, 80.0, 70.0, 2000.0, 45.0, 0.7, 0.20, 85.0, 0.03,
                 "Default for electronics enclosures: tougher and more heat resistant than PLA."),
        Material("ABS", 1.04, 105.0, 85.0, 2100.0, 40.0, 0.5, 0.17, 85.0, 0.025,
                 "Needs enclosure; warps; good heat resistance."),
        Material("ASA", 1.07, 100.0, 85.0, 2000.0, 42.0, 0.55, 0.17, 110.0, 0.025,
                 "Like ABS but UV resistant — outdoor parts."),
        Material("TPU95A", 1.21, -30.0, 80.0, 26.0, 35.0, 0.8, 0.20, 130.0, 0.5,
                 "Flexible: gaskets, bumpers, feet."),
        Material("PC", 1.20, 147.0, 115.0, 2300.0, 60.0, 0.6, 0.20, 160.0, 0.04,
                 "High temperature and toughness; hard to print."),
        Material("PA-CF", 1.17, 70.0, 120.0, 6000.0, 80.0, 0.5, 0.30, 300.0, 0.015,
                 "Nylon + carbon fibre: stiff, strong, needs hardened nozzle and dry filament."),
    ]
}


@dataclass(frozen=True)
class PrinterProfile:
    """An FDM printer + process profile. Lengths in mm, angles in degrees."""

    name: str
    build_x: float
    build_y: float
    build_z: float
    nozzle_d: float = 0.4
    layer_h: float = 0.2
    line_w: float = 0.45
    hole_compensation: float = 0.15  # added to designed hole diameters (holes print undersized)
    clearance_press: float = 0.10  # per side: press fit
    clearance_sliding: float = 0.25  # per side: parts slide together
    clearance_loose: float = 0.40  # per side: free/rotating fit
    max_overhang_deg: float = 45.0  # measured from vertical; steeper (more horizontal) needs support
    # src: typical unsupported bridge span for PLA/PETG with part cooling (slicer bridge tests,
    # Prusa/Bambu knowledge bases report 20-50 mm); kept conservative per printer below.
    max_bridge_mm: float = 20.0
    min_wall: float = 0.8  # two perimeters
    min_feature: float = 0.4
    speed_mm_s: float = 150.0  # effective average extrusion speed used for time estimates
    vendor: str = ""

    @property
    def build_volume(self) -> tuple[float, float, float]:
        return (self.build_x, self.build_y, self.build_z)

    def with_(self, **changes) -> "PrinterProfile":
        """Return a copy with some fields changed (e.g. ``nozzle_d=0.6``)."""
        return replace(self, **changes)


# src: manufacturer spec sheets (build volume). Speeds are realistic averages, not max speeds.
PRINTERS: dict[str, PrinterProfile] = {
    p.name: p
    for p in [
        PrinterProfile("generic", 220, 220, 250, speed_mm_s=80.0, vendor="any"),
        PrinterProfile("bambu_a1", 256, 256, 256, speed_mm_s=180.0, max_bridge_mm=30.0, vendor="Bambu Lab"),
        PrinterProfile("bambu_a1_mini", 180, 180, 180, speed_mm_s=180.0, max_bridge_mm=30.0, vendor="Bambu Lab"),
        PrinterProfile("bambu_p1s", 256, 256, 256, speed_mm_s=200.0, max_bridge_mm=30.0, vendor="Bambu Lab"),
        PrinterProfile("bambu_x1c", 256, 256, 256, speed_mm_s=200.0, max_bridge_mm=30.0, vendor="Bambu Lab"),
        PrinterProfile("prusa_mk4", 250, 210, 220, speed_mm_s=150.0, max_bridge_mm=25.0, vendor="Prusa Research"),
        PrinterProfile("prusa_core_one", 250, 220, 270, speed_mm_s=170.0, max_bridge_mm=25.0, vendor="Prusa Research"),
        PrinterProfile("prusa_mini", 180, 180, 180, speed_mm_s=100.0, vendor="Prusa Research"),
        PrinterProfile("creality_ender3", 220, 220, 250, speed_mm_s=60.0, max_bridge_mm=15.0, vendor="Creality"),
        PrinterProfile("creality_k1", 220, 220, 250, speed_mm_s=180.0, max_bridge_mm=25.0, vendor="Creality"),
    ]
}


def get_printer(name: "str | PrinterProfile") -> PrinterProfile:
    """Look up a printer profile by name (case-insensitive); profiles pass through unchanged."""
    if isinstance(name, PrinterProfile):
        return name
    key = str(name).strip().lower().replace(" ", "_").replace("-", "_")
    if key in PRINTERS:
        return PRINTERS[key]
    raise NotFoundError("printer", name, PRINTERS)


def get_material(name: "str | Material") -> Material:
    """Look up a material by name (case-insensitive, e.g. 'petg'); materials pass through."""
    if isinstance(name, Material):
        return name
    key = str(name).strip().upper().replace("_", "-")
    aliases = {"PLA+": "PLA", "TPU": "TPU95A", "NYLON-CF": "PA-CF", "PACF": "PA-CF"}
    key = aliases.get(key, key)
    if key in MATERIALS:
        return MATERIALS[key]
    raise NotFoundError("material", name, MATERIALS)
