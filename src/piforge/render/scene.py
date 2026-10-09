"""Render items from a built ``scene.json`` + GLBs (trimesh/numpy only; no CAD kernel)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from piforge.render.raster import RenderItem


def scene_items(build_dir: Path | str) -> list:
    """Render items for every node of ``<build_dir>/scene.json`` at its world pose, GLB leaf colours."""
    build_dir = Path(build_dir)
    scene = json.loads((build_dir / "scene.json").read_text(encoding="utf-8"))
    cache: dict[str, list] = {}
    items = []
    for node in scene.get("nodes") or []:
        rel = node.get("mesh")
        if not rel:
            continue
        world = np.asarray(node.get("world_matrix") or node["matrix"], dtype=float).reshape(4, 4, order="F")
        if rel not in cache:
            cache[rel] = glb_meshes(build_dir / rel, node.get("color") or "#9aa4b2")
        for mesh, color in cache[rel]:
            m = mesh.copy()
            m.apply_transform(world)
            items.append(RenderItem(m, color=color))
    return items


def glb_meshes(path: Path | str, default: str = "#9aa4b2") -> list[tuple[Any, str]]:
    """(mesh in the GLB's frame, hex colour) per geometry of a GLB (e.g. written by ``export_glb``)."""
    import trimesh

    sc = trimesh.load(str(path), force="scene")
    out = []
    for node_name in sc.graph.nodes_geometry:
        transform, geom_name = sc.graph[node_name]
        g = sc.geometry[geom_name].copy()
        g.apply_transform(transform)
        color = default
        base = getattr(getattr(g.visual, "material", None), "baseColorFactor", None)
        if base is not None:  # glTF baseColorFactor is LINEAR (uint8 or float) -> sRGB hex
            arr = np.asarray(base, dtype=float).ravel()[:3]
            lin = arr if np.asarray(base).dtype.kind == "f" else arr / 255.0
            lin = np.clip(lin, 0.0, 1.0)
            srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - 0.055)
            color = "#" + "".join(f"{int(round(float(v) * 255.0)):02x}" for v in np.clip(srgb, 0, 1))
        g.merge_vertices(merge_tex=True, merge_norm=True)  # undo the viewer's smooth-shading split
        out.append((g, color))
    return out
