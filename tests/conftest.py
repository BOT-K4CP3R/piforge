"""Shared pytest fixtures for PiForge."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Keep matplotlib headless and its cache out of the iCloud tree.
os.environ.setdefault("MPLBACKEND", "Agg")


@pytest.fixture
def spaced_tmp(tmp_path: Path) -> Path:
    """A temporary directory whose path contains spaces, a tilde and non-ASCII characters."""
    d = tmp_path / "dir with spaces ~ż"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(scope="session")
def ngspice_path() -> str:
    """Absolute path to the ngspice binary; skips the test when it is not installed."""
    p = shutil.which("ngspice")
    if p is None and Path("/opt/homebrew/bin/ngspice").exists():
        p = "/opt/homebrew/bin/ngspice"
    if p is None:
        pytest.skip("ngspice not installed (brew install ngspice)")
    return p


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT
