"""Vendored chart-script asset loading."""

import html
from functools import lru_cache
from pathlib import Path

_LIGHTWEIGHT_CHARTS_VERSION = "4.2.3"
_LIGHTWEIGHT_CHARTS_CDN_URL = (
    f"https://unpkg.com/lightweight-charts@{_LIGHTWEIGHT_CHARTS_VERSION}/dist/"
    "lightweight-charts.standalone.production.js"
)
_LIGHTWEIGHT_CHARTS_VENDOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "static" / "vendor" / "lightweight-charts.standalone.production.js"
)


@lru_cache(maxsize=1)
def _lightweight_charts_script_tag() -> str:
    """Reference the vendored library through a stable local URL.

    Keeping the 160-KiB production bundle outside each generated HTML page
    lets Chromium reuse its resource and compiled-script caches while users
    move through symbols. The CDN remains a last-resort fallback for an
    incomplete checkout.
    """
    if _LIGHTWEIGHT_CHARTS_VENDOR_PATH.is_file():
        source_url = html.escape(
            _LIGHTWEIGHT_CHARTS_VENDOR_PATH.as_uri(), quote=True
        )
        return (
            f'<script src="{source_url}" '
            f'data-lightweight-charts-version="{_LIGHTWEIGHT_CHARTS_VERSION}"></script>'
        )
    return f'<script src="{_LIGHTWEIGHT_CHARTS_CDN_URL}"></script>'


def lightweight_charts_base_path() -> Path:
    """Return the local base directory used by QWebEngine ``setHtml``."""

    return _LIGHTWEIGHT_CHARTS_VENDOR_PATH.parent
