from __future__ import annotations

import hashlib
import re

from fastapi.testclient import TestClient

from src.web.application import create_web_app
from src.web.assets import ReleaseAssets
from src.web.config import WebConfig

from .conftest import login


def test_rendered_pages_bypass_legacy_worker_and_serve_matching_asset_bytes(tmp_path):
    app, services = create_web_app(WebConfig(data_dir=tmp_path / "web"))
    services.auth.bootstrap_user("owner", "correct horse battery staple")
    with TestClient(app, base_url="http://localhost:8080") as client:
        pages = [client.get("/login")]
        login(client)
        pages.append(client.get("/"))
        for page in pages:
            assert "no-store" in page.headers["cache-control"]
            assert "/live-static/" not in page.text
            assert "/live-vendor/" not in page.text
            urls = re.findall(r'(?:href|src)="(/release-static/[^\"]+)"', page.text)
            assert len(urls) >= 2
            for url in urls:
                response = client.get(url)
                assert response.status_code == 200
                assert "immutable" in response.headers["cache-control"]
                assert hashlib.sha256(response.content).hexdigest()[:16] == url.split("/")[2]
        assert client.get("/release-static/invalid/app.js").status_code == 404
        assert client.get("/release-static/invalid/config.json").status_code == 404


def test_changed_asset_gets_new_path_and_previous_release_keeps_its_bytes(tmp_path):
    static = tmp_path / "static"
    vendor = tmp_path / "vendor"
    static.mkdir()
    vendor.mkdir()
    for name in ("app.css", "app.js", "login.js"):
        (static / name).write_text("old release", encoding="utf-8")
    (vendor / "lightweight-charts.standalone.production.js").write_text("vendor", encoding="utf-8")
    page = static / "dashboard.html"
    page.write_text('<script src="/live-static/app.js?v=91"></script>', encoding="utf-8")
    previous = ReleaseAssets(static, vendor)
    old_html = previous.render_page(page)
    (static / "app.js").write_text("new release", encoding="utf-8")
    current = ReleaseAssets(static, vendor)
    assert current.render_page(page) != old_html
    assert previous.assets["app.js"][1] == b"old release"
