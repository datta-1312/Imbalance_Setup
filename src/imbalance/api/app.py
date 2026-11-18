"""FastAPI backend (spec §8.7, §9): REST snapshot/detail/config endpoints, a
WebSocket stream of alerts, events and snapshots, the Kite login callback, and
the built React dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from .. import __version__
from ..config import ConfigManager, Params
from ..engine.types import _clean
from .params_meta import PARAM_META

log = logging.getLogger(__name__)


class AppState:
    """Holds the runtime (None while the daily start-up routine is running)."""

    def __init__(self, cfgm: ConfigManager, runtime=None, kite_rest=None):
        self.cfgm = cfgm
        self.runtime = runtime
        self.kite_rest = kite_rest
        self.status = "starting" if runtime is None else "running"
        self.message: str | None = None
        self.login_event = asyncio.Event()


class Toggle(BaseModel):
    value: bool


def jsonable(obj: Any) -> Any:
    return _clean(obj)


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="Imbalance Setup Screener", version=__version__)
    app.state.screener = state
    cfg = state.cfgm.config

    def runtime():
        if state.runtime is None:
            raise HTTPException(503, detail={"status": state.status, "message": state.message})
        return state.runtime

    def require_auth(request: Request) -> None:
        env = state.cfgm.config.api.auth_token_env
        token = os.environ.get(env) if env else None
        if not token:
            return
        got = request.headers.get("authorization", "")
        if not secrets.compare_digest(got, f"Bearer {token}"):
            raise HTTPException(401, detail="missing or wrong bearer token")

    # ------------------------------------------------------------------ read
    @app.get("/api/status")
    def status() -> dict:
        return {"status": state.status, "message": state.message, "version": __version__,
                "login_url": state.kite_rest.login_url() if state.kite_rest and state.status == "login_required"
                else None}

    @app.get("/api/health")
    def health(rt=Depends(runtime)) -> JSONResponse:
        return JSONResponse(jsonable(rt.health()))

    @app.get("/api/snapshot")
    def snapshot(rt=Depends(runtime)) -> JSONResponse:
        return JSONResponse(jsonable(rt.snapshot()))

    @app.get("/api/setups/{setup_id}")
    def setup(setup_id: str, rt=Depends(runtime)) -> JSONResponse:
        d = rt.engine.setup_detail(setup_id)
        if d is None:
            raise HTTPException(404, "unknown setup")
        d["events"] = [e for e in rt.recent_events if e.get("setup_id") == setup_id]
        return JSONResponse(jsonable(d))

    @app.get("/api/instruments")
    def instruments(rt=Depends(runtime)) -> JSONResponse:
        rows = []
        for tok, tr in sorted(rt.engine.trackers.items()):
            i = tr.inst
            rows.append({"token": tok, "symbol": i.symbol, "display": i.display, "kind": i.kind.value,
                         "underlying": i.underlying, "scannable": i.scannable, "reason": i.scan_block_reason,
                         "stale": tr.stale, "paused": tr.paused, "muted": tok in rt.dispatcher.muted,
                         "last": tr.last_price, "setup": tr.setup.state.value if tr.setup else None})
        return JSONResponse(jsonable(rows))

    @app.get("/api/instruments/{token}")
    def instrument(token: int, rt=Depends(runtime)) -> JSONResponse:
        d = rt.engine.instrument_detail(token)
        if d is None:
            raise HTTPException(404, "unknown instrument")
        d["muted"] = token in rt.dispatcher.muted
        d["events"] = [e for e in rt.recent_events if e.get("token") == token][-200:]
        return JSONResponse(jsonable(d))

    @app.get("/api/alerts")
    def alerts(limit: int = 100, rt=Depends(runtime)) -> JSONResponse:
        return JSONResponse(jsonable(list(rt.dispatcher.recent)[-limit:]))

    @app.get("/api/events")
    def events(limit: int = 200, setup_id: str | None = None, token: int | None = None,
               rt=Depends(runtime)) -> JSONResponse:
        rows = [e for e in rt.recent_events
                if (setup_id is None or e.get("setup_id") == setup_id) and (token is None or e.get("token") == token)]
        return JSONResponse(jsonable(rows[-limit:]))

    @app.get("/api/config")
    def get_config() -> JSONResponse:
        c = state.cfgm.config
        return JSONResponse(jsonable({
            "version": state.cfgm.version,
            "params": c.base_params().model_dump(),
            "defaults": c.params.model_dump(),
            "presets": c.presets,
            "active_preset": c.active_preset,
            "symbol_overrides": c.symbol_overrides,
            "meta": PARAM_META,
            "telegram_enabled": c.alerts.telegram.enabled,
        }))

    # ------------------------------------------------------------------ write
    @app.put("/api/config", dependencies=[Depends(require_auth)])
    async def put_config(request: Request) -> JSONResponse:
        body = await request.json()
        allowed = {"params", "active_preset", "symbol_overrides", "presets"}
        bad = set(body) - allowed
        if bad:
            raise HTTPException(400, f"cannot edit {sorted(bad)} from the dashboard")
        patch = dict(body)
        if "params" in patch:
            unknown = set(patch["params"]) - set(Params.model_fields)
            if unknown:
                raise HTTPException(400, f"unknown parameters: {sorted(unknown)}")
        try:
            state.cfgm.write_overlay(patch)
        except ValidationError as exc:
            raise HTTPException(400, json.loads(exc.json(include_url=False)))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return get_config()

    @app.post("/api/symbols/{token}/mute", dependencies=[Depends(require_auth)])
    def mute(token: int, body: Toggle, rt=Depends(runtime)) -> dict:
        if token not in rt.engine.trackers:
            raise HTTPException(404, "unknown instrument")
        (rt.dispatcher.muted.add if body.value else rt.dispatcher.muted.discard)(token)
        return {"token": token, "muted": body.value}

    @app.post("/api/symbols/{token}/pause", dependencies=[Depends(require_auth)])
    def pause(token: int, body: Toggle, rt=Depends(runtime)) -> dict:
        if not rt.engine.set_paused(token, body.value):
            raise HTTPException(404, "unknown instrument")
        return {"token": token, "paused": body.value}

    # ------------------------------------------------------------------ live stream
    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while state.runtime is None:
                await websocket.send_text(json.dumps({"topic": "status",
                                                      "data": {"status": state.status, "message": state.message}}))
                await asyncio.sleep(2.0)
            rt = state.runtime
            q = rt.bus.subscribe()
            try:
                await websocket.send_text(json.dumps({"topic": "snapshot", "data": jsonable(rt.snapshot())}))
                while True:
                    msg = await q.get()
                    await websocket.send_text(json.dumps(jsonable(msg)))
            finally:
                rt.bus.unsubscribe(q)
        except (WebSocketDisconnect, RuntimeError):
            return

    # ------------------------------------------------------------------ Kite login (daily)
    @app.get("/kite/login")
    def kite_login():
        if state.kite_rest is None:
            raise HTTPException(404, "kite adapter not configured")
        return RedirectResponse(state.kite_rest.login_url())

    @app.get("/kite/callback")
    async def kite_callback(request_token: str | None = None, status: str | None = None):
        if state.kite_rest is None:
            raise HTTPException(404, "kite adapter not configured")
        if status != "success" or not request_token:
            return HTMLResponse("<p>Kite login was not completed.</p>", status_code=400)
        try:
            data = await state.kite_rest.generate_session(request_token)
            state.kite_rest.save_session(data)
        except Exception as exc:
            log.error("kite session exchange failed: %s", exc)
            return HTMLResponse("<p>Kite session exchange failed; check the logs and try again.</p>",
                                status_code=502)
        state.login_event.set()
        return HTMLResponse("<p>Logged in to Kite. The screener is starting; you can close this tab.</p>")

    # ------------------------------------------------------------------ dashboard
    static = Path(cfg.api.static_dir)
    if (static / "index.html").exists():
        if (static / "assets").exists():
            app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            f = static / path
            if path and f.is_file() and static in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(static / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        def no_dashboard():
            return HTMLResponse("<h3>Imbalance Setup Screener API</h3><p>The dashboard is not built. Run "
                                "<code>cd dashboard &amp;&amp; npm ci &amp;&amp; npm run build</code>. "
                                "API docs: <a href='/docs'>/docs</a>.</p>")

    return app
