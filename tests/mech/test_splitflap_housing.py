"""Closed split-flap housing (piforge.mech.splitflap_housing)."""

from __future__ import annotations

import numpy as np
import pytest

from piforge.core.errors import ValidationError


@pytest.fixture(scope="module")
def housing():
    from piforge.mech import SplitFlapHousing

    return SplitFlapHousing()


@pytest.fixture(scope="module")
def asm(housing):
    return housing.assembly(devices=[f"SF{i + 1}" for i in range(housing.n)])


# -- kernel-light layout ---------------------------------------------------------------------------------
def test_lazy_exports():
    import piforge.mech as mech

    assert mech.SplitFlapHousing.__name__ == "SplitFlapHousing"
    assert mech.HousingSpec().wall > 0 and mech.HousingElectronics().board == "rpizero2w"


def test_validation():
    from piforge.mech import HousingSpec, SplitFlapHousing

    with pytest.raises(ValidationError):
        SplitFlapHousing(n_digits=0)
    with pytest.raises(ValidationError):
        SplitFlapHousing(n_digits=8, comma_after=8)
    with pytest.raises(ValidationError):
        HousingSpec(comma_depth=2.5)


def test_layout(housing):
    h, m, s = housing, housing.module, housing.module.spec
    # windows: the visible flap face shrunk by a uniform border wider than the stop tabs' reach
    assert h.spec.window_border > s.stop_overlap
    assert h.window_w == pytest.approx(s.flap_width - 2 * h.spec.window_border)
    assert h.window_z[1] == pytest.approx(h.face_top - h.spec.window_border)
    assert h.window_z[0] == pytest.approx(h.face_bottom + h.spec.window_border)
    assert h.window_h > s.digit_height  # the whole digit shows
    # front panel in front of the falling flap's arc; shroud ends before the module frames
    assert h.y_panel < h.swing_y < h.face_y
    assert h.y_shroud_end < s.frame_front
    # comma between digit 6 and 7, centred between their windows
    assert h.module_x(5) < h.comma_x < h.module_x(6)
    # row centred, modules inside the side walls
    assert h.module_x(0) - m.left_ext > -h.x_in and h.module_x(7) + m.pitch - m.left_ext < h.x_in


def test_segments_fit_and_are_staggered(housing):
    h = housing
    lim = h.seg_limit
    for joints, lo, hi in ((h.front_joints, -h.x_out, h.x_out), (h.floor_joints, -h.x_out, h.x_out)):
        widths = np.diff([lo, *joints, hi])
        assert widths.max() <= lim
    # every front joint is far from every floor/top/back joint (the box is tied across them)
    assert all(abs(a - b) >= h.spec.stagger for a in h.front_joints for b in h.floor_joints)
    # front joints never cut a window or its shroud
    for j in h.front_joints:
        assert all(abs(j - h.module_x(i)) > h.shroud_x_out for i in range(h.n))


# -- CAD ---------------------------------------------------------------------------------------------------
@pytest.mark.slow
def test_parts_watertight_printable_and_fit_bed(housing):
    from piforge.fab.analyze import analyze_mesh
    from piforge.mech.export import to_trimesh

    names = {p.name for p in housing.parts}
    assert {"front_0", "side_left", "side_right", "floor_0", "top_0", "back_0", "splice", "pillar",
            "comma_inlay"} <= names
    for part in housing.parts:
        mesh = to_trimesh(part.shape)
        assert mesh.is_watertight, part.name
        res = analyze_mesh(mesh, housing.printer, part.material, name=part.name, rotation=part.print_rotation)
        bad = [f for f in res.report.findings if f.severity.name in ("ERROR", "WARNING")]
        assert not bad, (part.name, [str(f) for f in bad])
        assert not res.needs_supports, part.name
    assert housing.bed_check().ok and housing.bed_check().has("HOUSING.BED_FIT")


@pytest.mark.slow
def test_no_interference(housing, asm):
    ignore = [(f"m{i}_motor", f"m{i}_spool") for i in range(housing.n)]
    rep = asm.check_interference(ignore=ignore, min_volume=0.5)
    assert not rep.errors, [str(f) for f in rep.errors]
    ids = {n.id for n in asm.nodes}
    assert {"m0_driver", "board", "perfboard", "dc_jack", "comma_inlay", "pillar_0", "splice_0"} <= ids


@pytest.mark.slow
def test_swing_clear(housing):
    rep = housing.swing_check()
    assert rep.has("HOUSING.SWING_CLEAR"), rep.to_markdown()


@pytest.mark.slow
def test_windows_show_only_flap_faces_rays(housing, asm):
    rep = housing.visibility_check(asm)
    assert rep.has("HOUSING.WINDOW_CLEAN"), rep.to_markdown()
    # oblique views (8°): whatever shows beside the flaps is black (housing, frames, spools)
    from piforge.render.colors import parse_color

    color = {n.id: n.part.color for n in asm.nodes}
    for ang in ((8, 0), (-8, 0), (0, 8), (0, -8)):
        for i, cnt in housing.window_visibility(asm, step=2.0, direction=ang).items():
            for nid in cnt:
                if nid is None or nid == "<seam>" or "flap_" in nid:
                    continue
                r, g, b, _ = parse_color(color[nid])
                assert max(r, g, b) < 0.2, (ang, i, nid, color[nid])


@pytest.mark.slow
def test_windows_show_only_flap_faces_render(housing, asm):
    """Front orthographic render, flaps green, everything else red/blue: inside every window only
    green (flap) pixels, except the 0.4 mm split between the two flap halves."""
    from piforge.mech.export import to_trimesh
    from piforge.render import RenderItem, render

    items = []
    for n in asm.nodes:
        if n.id.startswith("back_"):
            continue
        col = "#00c000" if "flap_" in n.id else ("#c00000" if n.part.kind == "printed" else "#0000c0")
        items.append(RenderItem(to_trimesh(asm.world_shape(n.id)), color=col, edges=False))
    img = render(items, view="front", size=(3600, 900), margin=0.0, edges=False)
    a = np.asarray(img.convert("RGB")).astype(int)
    # front view: image x = world x, image up = world z; the housing's outline is the scene extent
    scale = float(img.info["piforge.px_per_mm"])
    bx = (-housing.x_out, housing.x_out)
    bz = (housing.z_min, housing.z_max)
    ox = (a.shape[1] - (bx[1] - bx[0]) * scale) / 2
    oz = (a.shape[0] - (bz[1] - bz[0]) * scale) / 2
    seam = housing.module.spec.seam / 2 + 1.0
    for i in range(housing.n):
        wx0, wx1, wz0, wz1 = housing.window_rect(i)
        c0 = int(ox + (wx0 - bx[0]) * scale) + 3
        c1 = int(ox + (wx1 - bx[0]) * scale) - 3
        r0 = int(oz + (bz[1] - wz1) * scale) + 3
        r1 = int(oz + (bz[1] - wz0) * scale) - 3
        win = a[r0:r1, c0:c1]
        rows_z = bz[1] - (np.arange(r0, r1) - oz) / scale
        keep = np.abs(rows_z - housing.axis_z) > seam
        px = win[keep].reshape(-1, 3)
        green = (px[:, 1] > px[:, 0] + 20) & (px[:, 1] > px[:, 2] + 20)
        assert green.mean() > 0.999, (i, float(green.mean()))
