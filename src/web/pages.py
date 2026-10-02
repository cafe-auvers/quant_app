from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse

from .api import WebServices
from .auth import SESSION_COOKIE


STATIC_DIR = Path(__file__).resolve().parent / "static"


def _page(path: Path) -> FileResponse:
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def register_pages(app: FastAPI, services: WebServices) -> None:
    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request):
        if request.query_params:
            return RedirectResponse("/login", status_code=303)
        if services.auth.session(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/", status_code=303)
        return _page(STATIC_DIR / "login.html")

    @app.get("/", include_in_schema=False)
    async def dashboard_page(request: Request):
        if not services.auth.session(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/login", status_code=303)
        return _page(STATIC_DIR / "dashboard.html")
