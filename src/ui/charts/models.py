"""Pure chart configuration models shared by controllers and renderers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Tuple


LOCAL_CHART_SHORTCUT_DEFAULTS = {
    "set_target": "T",
    "draw_line": "D",
    "erase_drawing": "E",
    "full_view": "F",
    "prev_symbol": "Up",
    "next_symbol": "Down",
    "pan_left": "Left",
    "pan_right": "Right",
}
LIGHTWEIGHT_CHART_SHORTCUT_DEFAULTS = {
    key: value
    for key, value in LOCAL_CHART_SHORTCUT_DEFAULTS.items()
    if key not in {"prev_symbol", "next_symbol"}
}


@dataclass(frozen=True)
class ChartRenderOptions:
    """Typed view of stable chart visibility defaults.

    Unknown options are retained because controller-specific values such as
    timeframe, visible-window state, and maximum bars also travel through the
    render-options mapping.
    """

    show_volume: bool = True
    show_rs: bool = True
    show_ema: bool = True
    show_adr: bool = True
    show_growth_1m: bool = True
    show_growth_3m: bool = True
    show_growth_6m: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls, options: Mapping[str, Any] | None = None
    ) -> "ChartRenderOptions":
        values = dict(options or {})
        known = {
            name: bool(values.pop(name))
            for name in (
                "show_volume",
                "show_rs",
                "show_ema",
                "show_adr",
                "show_growth_1m",
                "show_growth_3m",
                "show_growth_6m",
            )
            if name in values
        }
        return cls(**known, extra=values)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "show_volume": self.show_volume,
            "show_rs": self.show_rs,
            "show_ema": self.show_ema,
            "show_adr": self.show_adr,
            "show_growth_1m": self.show_growth_1m,
            "show_growth_3m": self.show_growth_3m,
            "show_growth_6m": self.show_growth_6m,
            **self.extra,
        }


def normalize_chart_options(
    options: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return the legacy dictionary shape from typed chart options."""
    return ChartRenderOptions.from_mapping(options).to_dict()


def normalize_chart_interaction_settings(
    settings: Mapping[str, Any] | None,
    *,
    renderer: str,
) -> Tuple[Dict[str, str], int]:
    """Return deterministic shortcuts and pan distance supplied by the UI."""
    defaults = (
        LIGHTWEIGHT_CHART_SHORTCUT_DEFAULTS
        if renderer == "lightweight"
        else LOCAL_CHART_SHORTCUT_DEFAULTS
    )
    raw_settings = dict(settings or {})
    raw_shortcuts = raw_settings.get("shortcuts", {})
    if not isinstance(raw_shortcuts, Mapping):
        raw_shortcuts = {}
    shortcuts = dict(defaults)
    for name in defaults:
        value = raw_shortcuts.get(name)
        if isinstance(value, str) and value.strip():
            shortcuts[name] = value.strip()
    try:
        pan_step_bars = max(1, int(raw_settings.get("chart_pan_step_bars", 1)))
    except (TypeError, ValueError, OverflowError):
        pan_step_bars = 1
    return shortcuts, pan_step_bars
