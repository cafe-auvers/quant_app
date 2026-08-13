"""Vendored chart-script asset loading."""

from functools import lru_cache
from pathlib import Path

_LIGHTWEIGHT_CHARTS_CDN_URL = (
    "https://unpkg.com/lightweight-charts@4.2.3/dist/"
    "lightweight-charts.standalone.production.js"
)
_LIGHTWEIGHT_CHARTS_VENDOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "static" / "vendor" / "lightweight-charts.standalone.production.js"
)


@lru_cache(maxsize=1)
def _lightweight_charts_script_tag() -> str:
    """Inline the vendored Lightweight Charts library so every chart
    reload/redraw doesn't re-fetch it from a CDN. Falls back to the CDN tag
    if the vendored file is ever missing (e.g. fresh checkout before assets
    are restored), so the chart still works either way.
    """
    try:
        source = _LIGHTWEIGHT_CHARTS_VENDOR_PATH.read_text(encoding="utf-8")
    except OSError:
        source = ""
    if source:
        return f"<script>{source}</script>"
    return f'<script src="{_LIGHTWEIGHT_CHARTS_CDN_URL}"></script>'
