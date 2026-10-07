"""Minimum activity measured only from completed opening-session bars."""
from __future__ import annotations

import math

import pandas as pd

from src.risk.orb_position import OrbSettings, get_orb_settings


def opening_volume_totals(
    bars: pd.DataFrame,
    local_index: pd.DatetimeIndex,
    session_start: pd.Timestamp,
) -> tuple[float | None, int]:
    """Sum volume since 09:30, excluding the unfinished bar and capping at 10:00.

    Empty minutes still count in elapsed time. Both minute and aggregated
    opening bars work because their volume is already summed by the provider.
    """
    elapsed = (local_index.max() - session_start).total_seconds() / 60
    minutes = min(30, max(0, int(elapsed)))
    if minutes == 0 or "Volume" not in bars.columns:
        return None, minutes
    end = session_start + pd.Timedelta(minutes=minutes)
    mask = (local_index >= session_start) & (local_index < end)
    if local_index[mask].has_duplicates:
        return None, minutes
    volumes = pd.to_numeric(bars.loc[mask, "Volume"], errors="coerce")
    if volumes.isna().any() or not all(math.isfinite(value) and value >= 0 for value in volumes):
        return None, minutes
    total = float(volumes.sum())
    return (total if math.isfinite(total) else None), minutes


def opening_liquidity_rejection(
    volume: object, minutes: object, *, settings: OrbSettings | None = None,
) -> str:
    """Check opening volume against shared settings or an explicit snapshot."""
    minimum = (settings or get_orb_settings()).opening_min_shares_per_minute
    if minimum == 0:
        return ""
    try:
        total = float(volume)
        duration = float(minutes)
    except (TypeError, ValueError, OverflowError):
        return "Opening liquidity unavailable: waiting for opening-session volume"
    if (
        not math.isfinite(total)
        or total < 0
        or not math.isfinite(duration)
        or not 1 <= duration <= 30
        or not duration.is_integer()
    ):
        return "Opening liquidity unavailable: waiting for opening-session volume"
    average = total / duration
    if average < minimum:
        return (
            f"Low opening liquidity: {average:,.2f} shares/min "
            f"< {minimum:g} required ({total:,.0f} shares / {duration:g} min)"
        )
    return ""
