"""Database timestamp helpers."""

import datetime as dt


def _utcnow_naive() -> dt.datetime:
    """Return a naive UTC timestamp for existing DB columns and comparisons."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
