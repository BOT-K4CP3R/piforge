"""Headless PNG renders of meshes: a numpy z-buffer rasterizer, multi-view sheets and heat-maps.

No GPU/OpenGL: pure numpy + Pillow (trimesh meshes in), deterministic and fast enough for
~100k triangles. Orthographic, Z up; views ``iso`` (from +X −Y +Z), ``front`` (camera at −Y),
``back``, ``left``, ``right`` (camera at +X), ``top`` (camera at +Z), ``bottom`` or any
``(azimuth, elevation)`` pair — see :mod:`piforge.render.camera`.

Typical use::

    from piforge.render import RenderItem, heat_colors, render, render_views, save_png

    save_png(render(mesh, view="iso"), "build/part.png")
    save_png(render_views([RenderItem(base), RenderItem(lid, color="#e0a458")], title="Case"),
             "build/case_sheet.png")
    heat = render(RenderItem(mesh, face_colors=heat_colors(thickness, 0.0, 3.0)))
    save_png(add_colorbar(heat, 0.0, 3.0, label="wall [mm]"), "build/wall_heat.png")
"""

from piforge.render.camera import VIEWS, Camera, camera_for
from piforge.render.colors import DEFAULT_COLOR, PALETTE, heat_colors, parse_color
from piforge.render.raster import RenderItem, render
from piforge.render.scene import glb_meshes, scene_items
from piforge.render.views import add_colorbar, render_views, save_png

__all__ = [
    "DEFAULT_COLOR",
    "PALETTE",
    "VIEWS",
    "Camera",
    "RenderItem",
    "add_colorbar",
    "camera_for",
    "glb_meshes",
    "heat_colors",
    "parse_color",
    "render",
    "render_views",
    "save_png",
    "scene_items",
]
