"""Pin anchors: where a wire attaches to a part (kernel-free; used by boards, modules and the harness).

A :class:`PinAnchor` is one electrical contact of a part in the PART frame: ``pos`` is the contact
point a wire leaves from (top of a header pin, socket mouth, solder pad, lead tip, terminal lug),
``dir`` the unit exit direction (the way a plug's wire leaves). ``pin`` is the electrical pin name
of the circuit part the anchor belongs to (``GPIO10``, ``IN1``, ``VCC``); ``number`` the physical
pin number when the part has one (the Pi header's 1…40 — matched against ``Pin.number`` first).

Contact kinds and the straight stub a wire keeps before it may turn (``STUB_MM``):

* ``dupont``  2.54 mm female housing on a male header pin (14 mm housing, as ``PLUG_HEADER``)
* ``jst_xh``  JST XH plug in a vertical socket (housing ≈ 9.8 mm above the socket)
* ``solder``  wire soldered to a pad / lead tip (heat-shrink sleeve)
* ``lead``    the part's own lead leaves its body here (``lead_mm`` = factory lead length)
* ``lug``     solder lug of a panel part (DC jack, switch)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from piforge.core.errors import ValidationError

Vec3 = tuple[float, float, float]

ANCHOR_KINDS = ("dupont", "jst_xh", "solder", "lead", "lug")
STUB_MM = {
    "dupont": 14.0,  # src: est — 2.54 mm DuPont female crimp housing ≈ 14 mm (same as boards.PLUG_HEADER depth)
    "jst_xh": 9.8,   # src: JST XH datasheet — XHP-n plug housing height 9.8 mm
    "solder": 4.0,   # src: est — solder joint + heat-shrink sleeve before the first bend
    "lead": 3.0,     # src: est — straight lead before the first bend at the motor's cable gland
    "lug": 5.0,      # src: est — solder lug + heat-shrink
}


@dataclass(frozen=True)
class PinAnchor:
    """One contact of a part (part frame, mm); see the module docstring.

    ``peer`` (optional) names the circuit ref the wire leaving this contact should go to — used on
    hub boards (a perfboard 5 V pad per driver). ``color``/``gauge_awg``/``lead_mm``/``cable`` mark a
    part's own factory lead (28BYJ-48: fixed colours, 230 mm, cable name). ``fan`` is the direction
    in which a second, third… wire on the same contact is offset (default: a perpendicular of
    ``dir``). ``id`` overrides the connector id (default ``"<ref>.<number or pin>"``).
    """

    pin: str
    pos: Vec3
    dir: Vec3 = (0.0, 0.0, 1.0)
    number: str = ""
    label: str = ""
    kind: str = "dupont"
    peer: str | None = None
    id: str | None = None
    color: str | None = None
    gauge_awg: int | None = None
    lead_mm: float | None = None
    cable: str | None = None
    fan: Vec3 | None = None

    def __post_init__(self) -> None:
        if self.kind not in ANCHOR_KINDS:
            raise ValidationError(f"PinAnchor {self.pin!r}: kind {self.kind!r} is not one of {ANCHOR_KINDS}")
        pos = tuple(float(v) for v in self.pos)
        d = tuple(float(v) for v in self.dir)
        if len(pos) != 3 or len(d) != 3 or not all(math.isfinite(v) for v in pos + d):
            raise ValidationError(f"PinAnchor {self.pin!r}: pos and dir must be three finite numbers")
        n = math.sqrt(sum(v * v for v in d))
        if n < 1e-9:
            raise ValidationError(f"PinAnchor {self.pin!r}: dir must not be zero")
        object.__setattr__(self, "pos", pos)
        object.__setattr__(self, "dir", tuple(v / n for v in d))
        object.__setattr__(self, "number", str(self.number))
        if self.fan is not None:
            f = tuple(float(v) for v in self.fan)
            fn = math.sqrt(sum(v * v for v in f))
            object.__setattr__(self, "fan", tuple(v / fn for v in f))

    @property
    def stub(self) -> float:
        """Straight length (mm) a wire keeps along ``dir`` from the contact (housing / sleeve)."""
        return STUB_MM[self.kind]

    def transformed(self, matrix: Any) -> "PinAnchor":
        """The anchor moved by a 4×4 rigid transform (numpy-like, row-major)."""
        def apply(v: Vec3, w: float) -> Vec3:
            return tuple(float(sum(matrix[r][c] * (v[c] if c < 3 else w) for c in range(4))) for r in range(3))

        fan = apply(self.fan, 0.0) if self.fan is not None else None
        return replace(self, pos=apply(self.pos, 1.0), dir=apply(self.dir, 0.0), fan=fan)

    def to_dict(self) -> dict:
        return {"pin": self.pin, "number": self.number, "label": self.label, "kind": self.kind,
                "pos": list(self.pos), "dir": list(self.dir)}


def anchor_from_record(rec: tuple | dict) -> PinAnchor:
    """Build a :class:`PinAnchor` from a module-data record ``(pin, number, pos, dir, kind[, extras])``."""
    if isinstance(rec, PinAnchor):
        return rec
    if isinstance(rec, dict):
        return PinAnchor(**rec)
    pin, number, pos, d, kind, *rest = rec
    extra = rest[0] if rest else {}
    return PinAnchor(pin=pin, number=number, pos=pos, dir=d, kind=kind, **extra)
