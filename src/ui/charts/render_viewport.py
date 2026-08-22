"""Viewport sizing helpers for local chart rendering."""

from __future__ import annotations

import pandas as pd


def default_visible_bar_count(chart_history: pd.DataFrame, *, uses_intraday_time: bool) -> int:
    """Show 81 daily sessions while counting their actual intraday bars."""

    default_visible_sessions = 81
    if not uses_intraday_time:
        return min(default_visible_sessions, len(chart_history))
    session_labels = [
        pd.Timestamp(timestamp).strftime("%Y-%m-%d")
        for timestamp in chart_history.index
    ]
    recent_sessions = list(dict.fromkeys(session_labels))[-default_visible_sessions:]
    first_visible_session = recent_sessions[0] if recent_sessions else ""
    return sum(label >= first_visible_session for label in session_labels)
