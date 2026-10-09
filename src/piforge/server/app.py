"""PiForge web server: REST + websockets over a project's build directory, and the browser GUI.

``create_app(project_dir)`` serves:

- ``/`` the GUI (``piforge/web/index.html``), ``/static/*`` the web dir, ``/build/*`` build files;
- ``GET /api/project | /api/scene | /api/report | /api/parts | /api/parts/{name}/overhang | /api/elec``;
- SPICE (:mod:`.api_spice`), digital twin (:mod:`.api_twin`), rebuild + events (:mod:`.api_build`).

Blocking work (SPICE runs, rebuilds, twin polling, mesh analysis) runs in worker threads.
"""

from __future__ import annotations

import logging
import socket
import threading
import webbrowser
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from piforge.core.errors import NotFoundError, PiForgeError
from piforge.core.report import jsonable
from piforge.server import api_build, api_spice, api_twin
from piforge.server.state import AppState

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class NoCacheStaticFiles(StaticFiles):
    """Static files that the browser must revalidate (rebuilds replace files in place)."""

    async def get_response(self, path: str, scope) -> Response:  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _json(data: object) -> JSONResponse:
    return JSONResponse(jsonable(data))


def create_app(project_dir: Path, *, build_dir: Path | None = None, build_on_start: bool = False) -> FastAPI:
    """Build the FastAPI app for ``project_dir`` (build output in ``build_dir``, default ``<project>/build``)."""
    project_dir = Path(project_dir).expanduser().resolve()
    build_dir = (Path(build_dir).expanduser() if build_dir else project_dir / "build").resolve()
    st = AppState(project_dir, build_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if build_on_start:
            await st.start_build()
        yield
        await st.twin.shutdown()

    app = FastAPI(title="PiForge", version="0.1.0", lifespan=lifespan)
    app.state.piforge = st

    @app.exception_handler(NotFoundError)
    async def _not_found(_request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api/project")
    def project() -> JSONResponse:
        """Manifest summary, finding counts, printer profile, rebuild status."""
        return _json(st.project_info())

    @app.get("/api/scene")
    def scene() -> JSONResponse:
        """``scene.json`` (spec §5.4) + ``version`` token; empty scene before the first build."""
        return _json(st.scene())

    @app.get("/api/report")
    def report() -> JSONResponse:
        """Merged build report (findings with ``source``)."""
        return _json(st.report())

    @app.get("/api/parts")
    def parts() -> JSONResponse:
        """Printed parts: analysis, files (+ ``urls``), status, scene node ids."""
        return _json(st.parts())

    @app.get("/api/parts/{name}/overhang")
    def overhang(name: str) -> JSONResponse:
        """Per-face overhang mask of the part's scene GLB in its print orientation."""
        try:
            return _json(st.overhang(name))
        except (ValueError, OSError) as exc:
            raise HTTPException(422, f"Cannot analyse the mesh of {name!r}: {exc}") from exc

    @app.get("/api/elec")
    def elec() -> JSONResponse:
        """BOM, wiring rows, 3D harness wires, cut list, pinout, config.txt, power, ERC, file URLs."""
        return _json(st.elec())

    app.include_router(api_spice.router)
    app.include_router(api_twin.router)
    app.include_router(api_build.router)
    app.mount("/static", NoCacheStaticFiles(directory=WEB_DIR), name="static")
    app.mount("/build", NoCacheStaticFiles(directory=build_dir, check_dir=False), name="build")
    return app


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def serve(project_dir: Path, *, port: int = 8765, open_browser: bool = True, host: str = "127.0.0.1",
          build_on_start: bool | None = None) -> None:
    """Run the GUI server for ``project_dir`` until Ctrl+C (builds first when there is no build)."""
    import uvicorn

    project_dir = Path(project_dir).expanduser().resolve()
    if not project_dir.is_dir():
        raise NotFoundError("project directory", str(project_dir), [])
    if not _port_free(host, port):
        raise PiForgeError(f"Port {port} on {host} is already in use; choose another with --port.")
    if build_on_start is None:
        build_on_start = not (project_dir / "build" / "manifest.json").is_file()
    app = create_app(project_dir, build_on_start=build_on_start)
    url = f"http://{host}:{port}/"
    log.info("PiForge GUI for %s at %s", project_dir, url)
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=host, port=port, log_level="info", ws_ping_interval=20.0)
