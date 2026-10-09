"""Layering rule: light subsystems must not import the OpenCascade CAD kernel.

Importing build123d/OCP costs seconds and hundreds of MB; electronics, SPICE, the digital twin,
fabrication analysis and rendering must stay fast and usable without it.
"""

import subprocess
import sys

import pytest

LIGHT_PACKAGES = [
    "piforge",
    "piforge.core",
    "piforge.fab",
    "piforge.render",
    "piforge.elec",
    "piforge.spice",
    "piforge.twin",
]


@pytest.mark.parametrize("mod", LIGHT_PACKAGES)
def test_light_package_does_not_import_cad_kernel(mod):
    code = (
        f"import importlib, pkgutil, sys\n"
        f"m = importlib.import_module({mod!r})\n"
        f"for info in pkgutil.walk_packages(getattr(m, '__path__', []), prefix={mod!r} + '.'):\n"
        f"    if '.shims' in info.name or info.name.endswith('.runner'):\n"
        f"        continue\n"
        f"    importlib.import_module(info.name)\n"
        f"bad = [k for k in ('build123d', 'OCP') if k in sys.modules]\n"
        f"assert not bad, bad\n"
    )
    if mod == "piforge":  # top-level: only check the package itself, not every subpackage
        code = "import piforge, sys; assert 'build123d' not in sys.modules and 'OCP' not in sys.modules"
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stderr[-2000:]
