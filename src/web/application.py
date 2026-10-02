from __future__ import annotations

import asyncio
import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .api import WebServices, build_services, register_api_routes
from .auth import SESSION_COOKIE
from .config import WebConfig, load_web_config
from .pages import register_pages


STATIC_DIR = Path(__file__).resolve().parent / "static"
VENDOR_DIR = Path(__file__).resolve().parents[1] / "ui" / "static" / "vendor"


def create_web_app(config: WebConfig) -> tuple[FastAPI, WebServices]:
    """Build a plain FastAPI/static application with no UI hydration runtime."""

    services = build_services(config)
    app = FastAPI(
        title="Quant Web Localhost",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    register_api_routes(app, services)
    app.mount("/web-static", StaticFiles(directory=STATIC_DIR), name="web-static")
    app.mount("/vendor", StaticFiles(directory=VENDOR_DIR), name="vendor")
    # Live assets use paths the retired service worker never intercepted. This
    # prevents an old cache from serving a previous chart renderer indefinitely.
    app.mount("/live-static", StaticFiles(directory=STATIC_DIR), name="live-static")
    app.mount("/live-vendor", StaticFiles(directory=VENDOR_DIR), name="live-vendor")

    @app.websocket("/live-updates")
    async def live_updates(websocket: WebSocket) -> None:
        session = await asyncio.to_thread(
            services.auth.session, websocket.cookies.get(SESSION_COOKIE)
        )
        if session is None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = services.live_updates.subscribe()
        try:
            await websocket.send_json({"kind": "ready"})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                except TimeoutError:
                    event = {"kind": "ping"}
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            services.live_updates.unsubscribe(queue)

    @app.websocket("/_nicegui_ws/socket.io/")
    async def migrate_retired_nicegui_tab(websocket: WebSocket) -> None:
        """Move already-open NiceGUI tabs to the static shell exactly once.

        Socket.IO uses a tiny Engine.IO framing protocol. Supporting only the
        opening packets is enough to deliver the native ``open`` event. This
        prevents retired tabs from hammering the server with reconnects.
        """

        await websocket.accept()
        await websocket.send_text(
            "0"
            + json.dumps(
                {
                    "sid": "retired-ui-migration",
                    "upgrades": [],
                    "pingInterval": 60_000,
                    "pingTimeout": 20_000,
                    "maxPayload": 1_000_000,
                },
                separators=(",", ":"),
            )
        )
        try:
            while True:
                packet = await websocket.receive_text()
                if packet == "2":
                    await websocket.send_text("3")
                elif packet.startswith("40"):
                    await websocket.send_text('40{"sid":"retired-ui-migration"}')
                    await websocket.send_text(
                        '42["open",{"path":"/reset-ui","new_tab":false}]'
                    )
                    await asyncio.sleep(1)
                    await websocket.close(code=1000)
                    return
        except WebSocketDisconnect:
            return

    @app.get("/service-worker.js", include_in_schema=False)
    async def service_worker() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "service_worker.js",
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-cache",
                "Service-Worker-Allowed": "/",
            },
        )

    @app.get("/reset-ui", include_in_schema=False)
    async def reset_retired_ui() -> HTMLResponse:
        """Clear the retired offline shell without removing login cookies."""

        return HTMLResponse(
            """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Refreshing Quant Web</title><style>html,body{margin:0;min-height:100%;background:#07110f;color:#d7e5df;font:16px system-ui}body{display:grid;place-items:center}</style></head>
<body><p>Refreshing dashboard…</p><script>
(async()=>{try{if('serviceWorker' in navigator){const registrations=await navigator.serviceWorker.getRegistrations();await Promise.all(registrations.map(item=>item.unregister()));}if('caches' in window){const keys=await caches.keys();await Promise.all(keys.map(item=>caches.delete(item)));}}finally{window.location.replace('/?lite=70');}})();
</script></body></html>""",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            },
        )

    register_pages(app, services)
    app.state.web_services = services
    return app, services


def run(config: WebConfig | None = None) -> None:
    config = config or load_web_config()
    app, _services = create_web_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        reload=False,
        access_log=False,
        proxy_headers=False,
        log_level="warning",
    )
