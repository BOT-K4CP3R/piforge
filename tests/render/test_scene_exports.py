"""scene_items/glb_meshes live in piforge.render, pinout_markdown in piforge.elec (build re-exports them)."""


def test_moved_helpers_are_exported_and_reexported():
    import piforge.build as build
    import piforge.elec as elec
    import piforge.render as render

    assert build.scene_items is render.scene_items and build.glb_meshes is render.glb_meshes
    assert build.pinout_markdown is elec.pinout_markdown
