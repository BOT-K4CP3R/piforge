"""Cited data for the enclosure thermal model (``piforge.analysis.thermal``).

Physical constants, empirical coefficients, model policy values and the per-board Raspberry Pi
thermal table. Every number carries a ``# src:`` comment; ``unverified`` marks rules of thumb.
Import these through ``piforge.analysis.thermal``, which re-exports the public names.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from piforge.core.errors import NotFoundError, ValidationError

# -- physical constants --------------------------------------------------------------------------
KELVIN = 273.15  # src: SI definition, 0 °C in K
SIGMA = 5.670374419e-8  # src: CODATA 2018, Stefan–Boltzmann constant, W/(m²·K⁴)
G = 9.80665  # src: standard gravity (3rd CGPM 1901), m/s²
P_ATM = 101325.0  # src: ISO 2533 standard atmosphere, Pa
R_AIR = 287.05  # src: specific gas constant of dry air (8.314462 / 0.0289647), J/(kg·K)
CP_AIR = 1007.0  # src: Incropera & DeWitt, Heat and Mass Transfer, Table A.4 (air, 300 K), J/(kg·K)
K_AIR_300 = 0.0263  # src: Incropera Table A.4 (300 K), W/(m·K); ∝ T^0.885 fits 250–400 K within 1 %
NU_AIR_300 = 15.89e-6  # src: Incropera Table A.4 (300 K), m²/s; ∝ T^1.78 fits 250–400 K within 1 %
PR_AIR = 0.707  # src: Incropera Table A.4, air at 300 K (0.69–0.72 over 250–400 K)
CFM_TO_M3S = 0.028316846592 / 60.0  # src: exact, 1 ft³/min = 0.028316846592 m³ per 60 s
EPSILON = 0.9  # src: emissivity of plastics/non-metals 0.85–0.95 (Incropera Table A.11)
CD_VENT = 0.6  # src: sharp-edged opening discharge coefficient ≈ 0.61 (CIBSE Guide A, ISO 5167)
K_SUPPORT = 0.15  # src: Incropera Table A.3, plywood/softwood desk 0.12–0.17 W/(m·K)

# -- fans and openings ---------------------------------------------------------------------------
# src: unverified (rule of thumb: small DC fans in compact vented boxes deliver ~40–70 % of their
# free-air flow); upper bound on top of the fan/opening curve intersection (_fan_flow), which gives
# 48 % for the 30 mm reference fan through 2 × 400 mm² and reaches 50 % at ≈ 430 mm² per side.
FAN_DERATE = 0.5
# src: Sunon MF30100V1-1000U-A99 (30×30×10 mm, 5 V): 5.5 CFM free air, max static pressure
# 0.2 inH₂O = 49.8 Pa (RS listing). Reference fan for hints and the default fan curve; small
# 25–60 mm DC fans are typically 20–60 Pa — pass ThermalInputs.fan_static_pa for another fan.
FAN_30MM_CFM = 5.0
FAN_30MM_STATIC_PA = 49.8
FAN_INLET_SPEED = 1.5  # src: unverified (inlet air ≤ 1–2 m/s keeps the inlet drop to a few Pa)
# src: unverified (port cut-out clearances and lid seams of a printed box ≈ 10–50 mm²): floor on
# each opening seen by a fan, so a fan in a vent-less box still moves a little air through gaps.
LEAK_MM2 = 20.0

# -- model policy (project decisions, not physics) -----------------------------------------------
MARGIN_C = 5.0  # safety margin below max-service / glass-transition temperatures, K
DT_MAX = 500.0  # K; heat that cannot be shed below this is flagged THERMAL.OVERTEMP (clamped)
FAN_STARVED_FRACTION = 0.8  # THERMAL.FAN_STARVED below 80 % of the derated free-air flow

COOLING = ("bare", "heatsink", "fan")  # SoC cooling levels, worst first


# -- Raspberry Pi defaults -----------------------------------------------------------------------
@dataclass(frozen=True)
class PiThermal:
    """Thermal defaults of one Raspberry Pi board.

    ``rise_*_c``: SoC temperature above the surrounding air under a sustained all-core stress test
    (``load_w``) on an open bench — ``bare`` = no heatsink, ``heatsink`` = passive heatsink, ``fan``
    = forced air over a heatsinked SoC. Rises capped by throttling are lower bounds.
    """

    board: str
    idle_w: float  # whole-board power at idle, W
    load_w: float  # whole-board power under an all-core stress test, W
    rise_bare_c: float
    rise_heatsink_c: float
    rise_fan_c: float
    # src: Raspberry Pi documentation, "Frequency management and thermal control": "When the core
    # temperature is between 80°C and 85°C, the Arm cores will be progressively throttled back. If
    # the temperature reaches 85°C, both the Arm cores and the GPU will be throttled back"; 85 °C is
    # the limit "on all models" (RPi blog 2023-10-04: all boards begin throttling at 80 °C).
    throttle_c: float = 80.0
    limit_c: float = 85.0
    soft_limit_c: float | None = None  # 3B+ only: 1.4 → 1.2 GHz step
    source: str = ""

    def soc_rise(self, cooling: str) -> float:
        """SoC rise above the internal air at ``load_w`` for a cooling level in ``COOLING``, K."""
        if cooling not in COOLING:
            raise ValidationError(f"cooling must be one of {', '.join(COOLING)}; got {cooling!r}")
        return getattr(self, f"rise_{cooling}_c")

    def to_dict(self) -> dict:
        return asdict(self)


PI_THERMAL: dict[str, PiThermal] = {p.board: p for p in (
    # src: CNX Software "Raspberry Pi 5 review – Part 2" (2023-11-05): 3.0 W idle headless, 8.8 W
    #   `stress -c 4`; 28 °C room: Active Cooler max 66.1 °C → fan 38 K; its heatsink with the fan
    #   unplugged reached 85 °C and throttled → heatsink ≥ 57 K. src: raspberrypi.com/news/heating-
    #   and-cooling-raspberry-pi-5 (2023-10-04): no cooling stays just above 85 °C (throttled), lab
    #   ambient not stated (22 °C assumed) → bare ≥ 63 K. (J. Geerling 2024: 2.4–3.3 W idle,
    #   8.9–9.8 W stress-ng.)
    PiThermal("rpi5", idle_w=3.0, load_w=8.8, rise_bare_c=63.0, rise_heatsink_c=57.0,
              rise_fan_c=38.0, source="RPi blog 2023-10-04; CNX Software 2023-11-05"),
    # src: pidramble.com power benchmarks (J. Geerling): idle 540 mA = 2.7 W, `stress --cpu 4`
    #   1280 mA = 6.4 W (RPi docs table: 0.6 A idle, 1.2 A stress with HDMI/USB attached).
    # src: J. Geerling "The best way to keep your cool running a Raspberry Pi 4" (2019-11-22), room
    #   23–24 °C, 10 min stress: bare 80 °C (throttled) → ≥ 57 K; heatsink 73 °C → 50 K; official
    #   case + 30 mm fan mod 57 °C → 34 K (includes the case air rise).
    PiThermal("rpi4b", idle_w=2.7, load_w=6.4, rise_bare_c=57.0, rise_heatsink_c=50.0,
              rise_fan_c=34.0, source="J. Geerling 2019-11-22 cooling test; pidramble.com power"),
    # src: pidramble.com: idle 350 mA = 1.9 W, `stress --cpu 4` 980 mA = 5.1 W.
    # src: datenreise.de "Raspberry Pi 3B+ and 3B in comparison": no case, ≈ 22 °C room, full load
    #   66 °C (at the 1.2 GHz soft limit) → bare 44 K.
    # src: unverified (heatsink = bare − 8 K, Geerling 2019: "heatsinks generally shave 5–10 °C";
    #   fan = 0.58 × bare, the measured Pi 4 fan-mod/bare ratio 33.5/56.5).
    # src: RPi docs "Frequency management and thermal control": 3B+ soft limit 60 °C by default
    #   (1.4 → 1.2 GHz, `temp_soft_limit`).
    PiThermal("rpi3bp", idle_w=1.9, load_w=5.1, rise_bare_c=44.0, rise_heatsink_c=36.0,
              rise_fan_c=25.0, soft_limit_c=60.0,
              source="datenreise.de 3B+ test; pidramble.com; heatsink/fan scaled (unverified)"),
    # src: Tom's Hardware "Raspberry Pi Zero 2 W Review" (2021): 280 mA idle, 580 mA under
    #   Stressberry (5 V); room not stated (23 °C assumed): bare 63.4 °C → 40 K, heatsink 56.9 °C
    #   → 34 K, Pimoroni Fan SHIM 39.2 °C → 16 K.
    PiThermal("rpizero2w", idle_w=1.4, load_w=2.9, rise_bare_c=40.0, rise_heatsink_c=34.0,
              rise_fan_c=16.0, source="Tom's Hardware Zero 2 W review (2021)"),
)}

_BOARD_ALIASES = {
    "pi5": "rpi5", "raspberrypi5": "rpi5", "rpi4": "rpi4b", "pi4": "rpi4b", "pi4b": "rpi4b",
    "raspberrypi4": "rpi4b", "raspberrypi4b": "rpi4b", "pi3bp": "rpi3bp", "rpi3bplus": "rpi3bp",
    "raspberrypi3bp": "rpi3bp", "zero2w": "rpizero2w", "pizero2w": "rpizero2w",
    "rpizero2": "rpizero2w", "raspberrypizero2w": "rpizero2w"}


def get_pi_thermal(board: str | PiThermal) -> PiThermal:
    """Look up the thermal defaults of a board ('rpi4b', 'Pi 5', 'rpi3b+', 'Zero 2 W', …)."""
    if isinstance(board, PiThermal):
        return board
    key = re.sub(r"[\s_\-.]", "", str(board).lower()).replace("+", "p")
    key = _BOARD_ALIASES.get(key, key)
    if key in PI_THERMAL:
        return PI_THERMAL[key]
    raise NotFoundError("board", board, PI_THERMAL)
