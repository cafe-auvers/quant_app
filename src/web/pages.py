from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .api import WebServices
from .assets import ReleaseAssets
from .auth import SESSION_COOKIE


STATIC_DIR = Path(__file__).resolve().parent / "static"


def _page(source: str) -> HTMLResponse:
    return HTMLResponse(
        source,
        headers={"Cache-Control": "no-store"},
    )


def register_pages(app: FastAPI, services: WebServices) -> None:
    assets = ReleaseAssets(STATIC_DIR, STATIC_DIR.parents[1] / "ui" / "static" / "vendor")
    assets.register(app)
    pages = {
        name: assets.render_page(STATIC_DIR / f"{name}.html")
        for name in ("login", "dashboard")
    }

    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request):
        if request.query_params:
            return RedirectResponse("/login", status_code=303)
        if services.auth.session(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/", status_code=303)
        return _page(pages["login"])

    @app.get("/", include_in_schema=False)
    async def dashboard_page(request: Request):
        if not services.auth.session(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/login", status_code=303)
        return _page(pages["dashboard"])
