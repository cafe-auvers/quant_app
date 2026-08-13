"""Pure chart configuration models shared by controllers and renderers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping


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
