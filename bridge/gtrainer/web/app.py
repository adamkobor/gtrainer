"""Local web UI: live meters, profile switching and editing.

Handlers are `async def` on purpose: they then run on the event loop, between
engine ticks, instead of in a worker thread that would race the 100 Hz engine.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from ..decode.zwift_ride import RIDE_BUTTONS
from ..mapping.profiles import ProfileError
from ..output.chiaki import PS_AXES, PS_BUTTONS
from ..runtime import Runtime
from ..settings import RiderSettings
from ..signals import ANALOG_SOURCES, all_sources

STATIC = Path(__file__).parent / "static"
PUSH_INTERVAL_S = 0.05
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname(netloc_or_url: str) -> str:
    """'127.0.0.1:8765' or 'http://[::1]:8765' -> '127.0.0.1' / '::1'."""
    if "//" not in netloc_or_url:
        netloc_or_url = "//" + netloc_or_url
    try:
        return (urlsplit(netloc_or_url).hostname or "").lower()
    except ValueError:
        return ""


class LocalOnlyMiddleware:
    """Rejects requests that don't come from the UI's own origin.

    - Host must be a loopback name (blocks DNS rebinding).
    - Origin, if sent, must be a loopback origin (blocks other web pages from
      POSTing to the API or reading the /ws feed; browsers always send Origin
      on cross-site POSTs and WebSocket handshakes).
    """

    def __init__(self, app, allowed_hosts: frozenset[str] = LOCAL_HOSTS) -> None:
        self.app = app
        self.allowed = allowed_hosts

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
            host_ok = _hostname(headers.get("host", "")) in self.allowed
            origin = headers.get("origin")
            origin_ok = origin is None or _hostname(origin) in self.allowed
            if not (host_ok and origin_ok):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await PlainTextResponse("forbidden: non-local origin", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class YamlBody(BaseModel):
    yaml: str


class DocBody(BaseModel):
    doc: dict[str, Any]


class SimBody(BaseModel):
    buttons: list[str] = []
    paddle_l: float = 0.0
    paddle_r: float = 0.0


class SimGearBody(BaseModel):
    gear: int | None = None


def create_app(rt: Runtime, allowed_hosts: frozenset[str] = LOCAL_HOSTS) -> FastAPI:
    app = FastAPI(title="gtrainer")
    app.add_middleware(LocalOnlyMiddleware, allowed_hosts=allowed_hosts)

    def bad(e: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(e))

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/meta")
    async def meta() -> dict:
        return {
            "ride_buttons": list(RIDE_BUTTONS),
            "ps_buttons": list(PS_BUTTONS),
            "ps_axes": list(PS_AXES),
            "analog_sources": list(ANALOG_SOURCES),
            "sources": all_sources(),
        }

    @app.get("/api/profiles")
    async def profiles() -> dict:
        return {"profiles": rt.store.names(), "active": rt.active_name}

    @app.get("/api/profiles/{name}")
    async def get_profile(name: str) -> dict:
        try:
            return {
                "name": name,
                "yaml": rt.store.read_text(name),
                "doc": rt.store.read_doc(name),
                "resolved": rt.store.resolve(name),
            }
        except ProfileError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    def saved(name: str, save) -> dict:
        try:
            save()
        except ProfileError as e:
            raise bad(e) from e
        if name == rt.active_name:
            rt.reload_active()
        return {"ok": True}

    @app.post("/api/profiles/{name}")
    async def create_profile(name: str, body: YamlBody) -> dict:
        """Like PUT, but refuses to replace an existing profile."""
        try:
            exists = rt.store.path(name).exists()
        except ProfileError as e:
            raise bad(e) from e
        if exists:
            raise HTTPException(status_code=409, detail=f"profile {name!r} already exists")
        return saved(name, lambda: rt.store.save_text(name, body.yaml))

    @app.put("/api/profiles/{name}")
    async def put_profile_yaml(name: str, body: YamlBody) -> dict:
        return saved(name, lambda: rt.store.save_text(name, body.yaml))

    @app.put("/api/profiles/{name}/doc")
    async def put_profile_doc(name: str, body: DocBody) -> dict:
        return saved(name, lambda: rt.store.save_doc(name, body.doc))

    @app.post("/api/profiles/{name}/validate")
    async def validate_profile(name: str, body: YamlBody) -> dict:
        try:
            rt.store.validate_doc(rt.store.resolve(name, text=body.yaml))
        except ProfileError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    @app.post("/api/profiles/{name}/activate")
    async def activate(name: str) -> dict:
        try:
            rt.activate(name)
        except ProfileError as e:
            raise bad(e) from e
        return {"ok": True, "active": name}

    @app.get("/api/rider")
    async def get_rider() -> dict:
        return rt.rider.model_dump()

    @app.put("/api/rider")
    async def put_rider(body: dict[str, Any]) -> dict:
        """Partial update: only the given fields change."""
        try:
            rider = RiderSettings.model_validate({**rt.rider.model_dump(), **body})
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        rt.set_rider(rider)
        return rt.rider.model_dump()

    @app.post("/api/sim")
    async def sim_input(body: SimBody) -> dict:
        if not rt.sim:
            raise HTTPException(status_code=409, detail="not running in --sim mode")
        rt.set_sim_input(body.buttons, body.paddle_l, body.paddle_r)
        return {"ok": True}

    @app.post("/api/sim/gear")
    async def sim_gear(body: SimGearBody) -> dict:
        if not rt.sim:
            raise HTTPException(status_code=409, detail="not running in --sim mode")
        rt.set_sim_gear(body.gear)
        return {"ok": True, "gear": rt.sim_gear}

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        try:
            while True:
                await socket.send_json(rt.status_json())
                await asyncio.sleep(PUSH_INTERVAL_S)
        except (WebSocketDisconnect, RuntimeError):
            pass

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
