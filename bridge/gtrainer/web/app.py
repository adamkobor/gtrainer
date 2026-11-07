"""Local web UI: live meters, profile switching and editing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
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


def create_app(rt: Runtime) -> FastAPI:
    app = FastAPI(title="gtrainer")

    def bad(e: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(e))

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/meta")
    def meta() -> dict:
        return {
            "ride_buttons": list(RIDE_BUTTONS),
            "ps_buttons": list(PS_BUTTONS),
            "ps_axes": list(PS_AXES),
            "analog_sources": list(ANALOG_SOURCES),
            "sources": all_sources(),
        }

    @app.get("/api/profiles")
    def profiles() -> dict:
        return {"profiles": rt.store.names(), "active": rt.active_name}

    @app.get("/api/profiles/{name}")
    def get_profile(name: str) -> dict:
        try:
            return {
                "name": name,
                "yaml": rt.store.read_text(name),
                "doc": rt.store.read_doc(name),
                "resolved": rt.store.resolve(name),
            }
        except ProfileError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.put("/api/profiles/{name}")
    def put_profile_yaml(name: str, body: YamlBody) -> dict:
        try:
            rt.store.save_text(name, body.yaml)
        except ProfileError as e:
            raise bad(e) from e
        if name == rt.active_name:
            rt.reload_active()
        return {"ok": True}

    @app.put("/api/profiles/{name}/doc")
    def put_profile_doc(name: str, body: DocBody) -> dict:
        try:
            rt.store.save_doc(name, body.doc)
        except ProfileError as e:
            raise bad(e) from e
        if name == rt.active_name:
            rt.reload_active()
        return {"ok": True}

    @app.post("/api/profiles/{name}/validate")
    def validate_profile(name: str, body: YamlBody) -> dict:
        try:
            rt.store.validate_doc(rt.store.resolve(name, text=body.yaml))
        except ProfileError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    @app.post("/api/profiles/{name}/activate")
    def activate(name: str) -> dict:
        try:
            rt.activate(name)
        except ProfileError as e:
            raise bad(e) from e
        return {"ok": True, "active": name}

    @app.get("/api/rider")
    def get_rider() -> dict:
        return rt.rider.model_dump()

    @app.put("/api/rider")
    def put_rider(body: dict[str, Any]) -> dict:
        """Partial update: only the given fields change."""
        try:
            rider = RiderSettings.model_validate({**rt.rider.model_dump(), **body})
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        rt.set_rider(rider)
        return rt.rider.model_dump()

    @app.post("/api/sim")
    def sim_input(body: SimBody) -> dict:
        if not rt.sim:
            raise HTTPException(status_code=409, detail="not running in --sim mode")
        rt.set_sim_input(body.buttons, body.paddle_l, body.paddle_r)
        return {"ok": True}

    @app.post("/api/sim/gear")
    def sim_gear(body: SimGearBody) -> dict:
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
