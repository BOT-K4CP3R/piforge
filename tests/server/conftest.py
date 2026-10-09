"""Fixtures for the web server tests: a hand-made project + build directory (no build123d)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from .fixture_build import make_fixture_project


@pytest.fixture(scope="session")
def fixture_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The fixture project, generated once per session (read-only — copy it before mutating)."""
    root = tmp_path_factory.mktemp("server") / "gauge project ~ż"
    return make_fixture_project(root)


@pytest.fixture
def fixture_project(fixture_template: Path, tmp_path: Path) -> Path:
    """A private copy of the fixture project (path with spaces, ``~`` and non-ASCII)."""
    dst = tmp_path / "proj with spaces ~ż"
    shutil.copytree(fixture_template, dst)
    return dst


@pytest.fixture
def app(fixture_project: Path):
    """The FastAPI app serving the fixture project."""
    from piforge.server.app import create_app

    return create_app(fixture_project)


@pytest.fixture
def client(app) -> Iterator:
    """A ``TestClient`` with the app's lifespan running."""
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c
