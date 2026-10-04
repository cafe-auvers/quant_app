"""Serve a consistent release even when a phone has an older service worker."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response


class ReleaseAssets:
    def __init__(self, static_dir: Path, vendor_dir: Path) -> None:
        self.assets: dict[str, tuple[str, bytes, str]] = {}
        for name, path, media_type in (
            ("app.css", static_dir / "app.css", "text/css"),
            ("app.js", static_dir / "app.js", "application/javascript"),
            ("login.js", static_dir / "login.js", "application/javascript"),
            (
                "lightweight-charts.standalone.production.js",
                vendor_dir / "lightweight-charts.standalone.production.js",
                "application/javascript",
            ),
        ):
            body = path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()[:16]
            self.assets[name] = (digest, body, media_type)

    def render_page(self, path: Path) -> str:
        source = path.read_text(encoding="utf-8")
        for name, (digest, _body, _media_type) in self.assets.items():
            source = re.sub(
                r"/(?:live-static|live-vendor)/" + re.escape(name) + r"(?:\?[^\"']*)?",
                f"/release-static/{digest}/{name}",
                source,
            )
        return source

    def register(self, app: FastAPI) -> None:
        @app.get("/release-static/{digest}/{filename}", include_in_schema=False)
        async def release_asset(digest: str, filename: str) -> Response:
            asset = self.assets.get(filename)
            if asset is None or digest != asset[0]:
                raise HTTPException(status_code=404)
            return Response(
                asset[1],
                media_type=asset[2],
                headers={"Cache-Control": "public, max-age=31536000, immutable"},
            )
