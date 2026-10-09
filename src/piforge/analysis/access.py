"""Port access: can a plug / card / finger reach each board connector past the obstacles?

Card slots and buttons (:data:`FINGERTIP` kinds) are operated by a fingertip, not a plug:
:func:`finger_gap` measures how close a fingertip gets to the mouth past the obstacles.

A port's *plug envelope* is the box a typical mating plug occupies outside the connector mouth
(:attr:`piforge.mech.boards.Port.plug` = width, height, depth). It starts at the mouth and runs
``depth`` mm outwards along the port direction. Any solid overlap with an obstacle (enclosure
wall, lid, panel module…) means the plug cannot be inserted.

Uses the OCC kernel (build123d/OCP), allowed in ``piforge.analysis``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from build123d import Align, Box, Location, Part
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps

from piforge.core.errors import PiForgeError, ValidationError
from piforge.core.report import Report, Severity
from piforge.mech.boards import BoardModel, Port, get_board
from piforge.mech.enclosure import FINGER_REACH, FINGERTIP
from piforge.mech.primitives import _as_part

MIN_VOLUME = 0.01  # mm³ — overlaps below this are numerical noise / touching faces


def plug_envelope(board: BoardModel, port: Port, board_loc: Location, *, gap: float = 0.0) -> Part:
    """Box occupied by the mating plug of ``port`` (enclosure frame given by ``board_loc``).

    Width runs along the board edge (along X for top ports), height along Z (along Y for top
    ports), depth outwards from the mouth. ``gap`` grows width and height by ``gap`` per side
    (required free space around the plug).
    """
    if gap < 0:
        raise ValidationError("gap must be >= 0 mm")
    w, h, depth = port.plug
    w, h = w + 2 * gap, h + 2 * gap
    dx, dy, dz = port.direction
    cx, cy, cz = port.center
    if abs(dz) > 0.5:  # top/bottom port: w along X, h along Y, depth along ±Z
        size = (w, h, depth)
        center = (cx, cy, cz + dz * depth / 2)
    elif abs(dx) > 0.5:  # ±x edge: width along Y
        size = (depth, w, h)
        center = (cx + dx * depth / 2, cy, cz)
    else:  # ±y edge: width along X
        size = (w, depth, h)
        center = (cx, cy + dy * depth / 2, cz)
    env = Box(*size, align=(Align.CENTER, Align.CENTER, Align.CENTER)).moved(Location(center))
    out = env.moved(board_loc)
    return _as_part(out)


def _axis_box(port: Port, size_wh: tuple[float, float], near: float, far: float) -> Box:
    """Box across the port axis (w along the edge, h along Z; X/Y for top ports) spanning
    ``near``..``far`` mm outwards from the mouth (board frame)."""
    w, h = size_wh
    dx, dy, dz = port.direction
    cx, cy, cz = port.center
    mid, length = (near + far) / 2, far - near
    if abs(dz) > 0.5:
        size, center = (w, h, length), (cx, cy, cz + dz * mid)
    elif abs(dx) > 0.5:
        size, center = (length, w, h), (cx + dx * mid, cy, cz)
    else:
        size, center = (w, length, h), (cx, cy + dy * mid, cz)
    return Box(*size, align=(Align.CENTER, Align.CENTER, Align.CENTER)).moved(Location(center))


def finger_envelope(board: BoardModel, port: Port, board_loc: Location, *, offset: float = FINGER_REACH,
                    size: tuple[float, float, float] | None = None) -> Part:
    """Fingertip that operates a card slot / button: a box (w × h, ``depth`` long) on the port
    axis whose front face is ``offset`` mm outside the mouth. ``size`` defaults to
    :data:`FINGERTIP` for the port kind (microSD / button), else (14, 8, 20)."""
    w, h, depth = size or FINGERTIP.get(port.kind, FINGERTIP["button"])
    return _as_part(_axis_box(port, (w, h), offset, offset + depth).moved(board_loc))


def finger_gap(board: "BoardModel | str", port: "Port | str", board_loc: Location, obstacles: Sequence[Any], *,
               max_gap: float = 20.0, tol: float = 0.05) -> float:
    """How close (mm from the mouth, ≥ 0) a fingertip gets to ``port`` before ``obstacles`` stop it.

    0 means the finger reaches the mouth; a solid wall in front gives the distance from the mouth
    to the wall's outer face. Bisection to ``tol``; returns ``max_gap`` if blocked even there.
    """
    board = get_board(board)
    port = board.port(port) if isinstance(port, str) else port
    named = _named(obstacles)
    boxes = [(s, s.bounding_box()) for _n, s in named]

    def clear(off: float) -> bool:
        env = finger_envelope(board, port, board_loc, offset=off)
        ebb = env.bounding_box()
        return not any(_overlap(ebb, bb) and _common_volume(env, s) > MIN_VOLUME for s, bb in boxes)

    if clear(0.0):
        return 0.0
    if not clear(max_gap):
        return float(max_gap)
    lo, hi = 0.0, float(max_gap)
    while hi - lo > tol:
        mid = (lo + hi) / 2
        if clear(mid):
            hi = mid
        else:
            lo = mid
    return hi


def _common_volume(a: Any, b: Any) -> float:
    op = BRepAlgoAPI_Common(a.wrapped, b.wrapped)
    if not op.IsDone():
        raise PiForgeError("boolean common failed")
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(op.Shape(), props)
    return abs(props.Mass())


def _named(obstacles: Iterable[Any]) -> list[tuple[str, Any]]:
    out = []
    for i, ob in enumerate(obstacles):
        if isinstance(ob, tuple) and len(ob) == 2 and isinstance(ob[0], str):
            name, shape = ob
        elif hasattr(ob, "shape") and hasattr(ob, "name") and hasattr(ob, "kind"):  # PartSpec
            name, shape = ob.name, ob.shape
        else:
            name, shape = getattr(ob, "label", "") or f"obstacle{i}", ob
        if not hasattr(shape, "bounding_box"):
            raise ValidationError(f"obstacle {name!r} is not a build123d shape")
        out.append((str(name), shape))
    return out


def _overlap(a, b, tol: float = 1e-6) -> bool:
    return (a.min.X < b.max.X - tol and b.min.X < a.max.X - tol and a.min.Y < b.max.Y - tol
            and b.min.Y < a.max.Y - tol and a.min.Z < b.max.Z - tol and b.min.Z < a.max.Z - tol)


def check_port_access(board: "BoardModel | str", board_loc: Location, obstacles: Sequence[Any],
                      ports: Iterable[str] | None = None, *, gap: float = 0.0) -> Report:
    """Check every port's plug envelope against ``obstacles`` (same frame as ``board_loc``).

    ``obstacles``: build123d shapes, ``(name, shape)`` pairs or PartSpecs. ``ports``: names to
    check (default: all horizontal edge ports). Emits ``ACCESS.PORT_BLOCKED`` (ERROR; data port,
    obstacle, volume_mm3) per blocked port/obstacle pair and ``ACCESS.PORT_OK`` (INFO) per free port.
    """
    board = get_board(board)
    selected = list(board.edge_ports()) if ports is None else [board.port(p) for p in ports]
    named = _named(obstacles)
    boxes = [(n, s, s.bounding_box()) for n, s in named]
    rep = Report(title=f"port access: {board.key}")
    for port in selected:
        env = plug_envelope(board, port, board_loc, gap=gap)
        ebb = env.bounding_box()
        blocked = False
        for name, shape, bb in boxes:
            if not _overlap(ebb, bb):
                continue
            try:
                vol = _common_volume(env, shape)
            except Exception as exc:  # noqa: BLE001 - a kernel failure must not abort the check
                rep.add("ACCESS.CHECK_FAILED", Severity.WARNING,
                        f"Could not intersect the {port.name} plug envelope with {name}: {exc}",
                        f"port:{port.name}", port=port.name, obstacle=name)
                continue
            if vol > MIN_VOLUME:
                blocked = True
                rep.add("ACCESS.PORT_BLOCKED", Severity.ERROR,
                        f"Port {port.name} ({port.kind}): a plug ({port.plug[0]:g} × {port.plug[1]:g} mm, "
                        f"{port.plug[2]:g} mm deep) hits {name} ({vol:.1f} mm³).", f"port:{port.name}",
                        hint="Add a cutout for this port (enclosure ports=…), enlarge it, or move the obstacle.",
                        port=port.name, obstacle=name, volume_mm3=round(vol, 4))
        if not blocked:
            rep.add("ACCESS.PORT_OK", Severity.INFO,
                    f"Port {port.name} ({port.kind}) is reachable: a {port.plug[0]:g} × {port.plug[1]:g} mm "
                    f"plug fits.", f"port:{port.name}", port=port.name)
    return rep
