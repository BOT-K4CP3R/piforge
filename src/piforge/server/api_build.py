"""Rebuild endpoints: single-flight background rebuild + ``/ws/events`` notifications.

``/ws/events`` messages: ``{"op":"build","state":"started"|"done"|"failed","error"?}`` and, while
a build runs, ``{"op":"build_progress","message":str}``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.post("/api/build", status_code=202)
async def rebuild(request: Request) -> dict:
    """Start a background rebuild of the project (no-op while one is running)."""
    st = request.app.state.piforge
    started = await st.start_build()
    return {"started": started, "status": st.build.to_dict()}


@router.get("/api/build/status")
def build_status(request: Request) -> dict:
    """State of the last/current rebuild: idle | running | done | failed (+ error, log)."""
    return request.app.state.piforge.build.to_dict()


@router.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    """Push build notifications to the GUI."""
    st = ws.app.state.piforge
    st.event_clients.add(ws)
    try:
        await ws.accept()
        if st.build.state == "running":
            await ws.send_json({"op": "build", "state": "started"})
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        st.event_clients.discard(ws)
