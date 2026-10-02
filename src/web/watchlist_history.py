from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import PROJECT_ROOT, WebConfig
from .store import ValidationError


SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")


def _date(value: object, name: str) -> dt.date:
    text = str(value or "").strip()
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{name} must be a YYYY-MM-DD date") from exc
    if parsed.isoformat() != text:
        raise ValidationError(f"{name} must be a YYYY-MM-DD date")
    return parsed


def _validated_range(start_date: object, end_date: object) -> tuple[dt.date, dt.date]:
    start_day = _date(start_date, "start_date")
    end_day = _date(end_date, "end_date")
    if end_day < start_day:
        raise ValidationError("end_date must be on or after start_date")
    if (end_day - start_day).days > 3660:
        raise ValidationError("Watchlist history range cannot exceed 10 years")
    return start_day, end_day


def _linked_main_worktree() -> Path | None:
    """Resolve the main worktree when this web build runs from a git worktree."""

    git_file = PROJECT_ROOT / ".git"
    if not git_file.is_file():
        return None
    try:
        marker = git_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not marker.lower().startswith("gitdir:"):
        return None
    git_dir = Path(marker.split(":", 1)[1].strip()).expanduser()
    if not git_dir.is_absolute():
        git_dir = (PROJECT_ROOT / git_dir).resolve()
    for parent in git_dir.parents:
        if parent.name == ".git":
            return parent.parent
    return None


def resolve_watchlist_history_path(config: WebConfig) -> Path | None:
    """Find the read-only desktop watchlist archive without copying its state."""

    configured = str(config.watchlist_history_path or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        return candidate.resolve()

    candidates = [PROJECT_ROOT / "data" / "watchlist.json"]
    linked_root = _linked_main_worktree()
    if linked_root is not None and linked_root != PROJECT_ROOT:
        candidates.append(linked_root / "data" / "watchlist.json")
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _clean_symbol(value: object) -> str:
    symbol = str(value or "").strip().upper()
    return symbol if SYMBOL_PATTERN.fullmatch(symbol) else ""


@dataclass(frozen=True)
class WatchlistHistorySource:
    """Read session-dated watchlists written by the desktop dashboard."""

    path: Path | None

    @classmethod
    def from_config(cls, config: WebConfig) -> "WatchlistHistorySource":
        return cls(resolve_watchlist_history_path(config))

    @property
    def available(self) -> bool:
        return bool(self.path and self.path.is_file())

    def list_range(
        self, start_date: object, end_date: object
    ) -> list[dict[str, Any]]:
        start_day, end_day = _validated_range(start_date, end_date)
        if not self.available or self.path is None:
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict):
            return []

        sessions = payload.get("sessions")
        if isinstance(sessions, dict):
            return self._session_rows(sessions, start_day, end_day)
        return self._legacy_rows(payload.get("items"), start_day, end_day)

    @staticmethod
    def _session_rows(
        sessions: Mapping[str, Any], start_day: dt.date, end_day: dt.date
    ) -> list[dict[str, Any]]:
        symbols: dict[str, dict[str, Any]] = {}
        for raw_date, snapshot in sessions.items():
            try:
                session_day = dt.date.fromisoformat(str(raw_date))
            except ValueError:
                continue
            if not start_day <= session_day <= end_day:
                continue
            raw_items = snapshot.get("items", []) if isinstance(snapshot, dict) else snapshot
            if not isinstance(raw_items, list):
                continue
            seen_this_session: set[str] = set()
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    continue
                symbol = _clean_symbol(raw_item.get("symbol"))
                if not symbol or symbol in seen_this_session:
                    continue
                seen_this_session.add(symbol)
                session_text = session_day.isoformat()
                row = symbols.setdefault(
                    symbol,
                    {
                        "symbol": symbol,
                        "name": "",
                        "first_seen_date": session_text,
                        "last_seen_date": session_text,
                        "watchlist_sessions": 0,
                    },
                )
                row["first_seen_date"] = min(row["first_seen_date"], session_text)
                row["last_seen_date"] = max(row["last_seen_date"], session_text)
                row["watchlist_sessions"] += 1
                name = str(raw_item.get("name") or "").strip()
                if name:
                    row["name"] = name[:240]
        return _sort_rows(symbols.values())

    @staticmethod
    def _legacy_rows(
        raw_items: object, start_day: dt.date, end_day: dt.date
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_items, list):
            return []
        symbols: dict[str, dict[str, Any]] = {}
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            symbol = _clean_symbol(raw_item.get("symbol"))
            try:
                added_day = dt.datetime.fromisoformat(
                    str(raw_item.get("added_date") or "").replace("Z", "+00:00")
                ).date()
            except ValueError:
                continue
            if not symbol or not start_day <= added_day <= end_day:
                continue
            symbols[symbol] = {
                "symbol": symbol,
                "name": str(raw_item.get("name") or "").strip()[:240],
                "first_seen_date": added_day.isoformat(),
                "last_seen_date": added_day.isoformat(),
                "watchlist_sessions": 1,
            }
        return _sort_rows(symbols.values())


def _sort_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            -int(str(row.get("last_seen_date", "0000-00-00")).replace("-", "")),
            -int(str(row.get("first_seen_date", "0000-00-00")).replace("-", "")),
            str(row.get("symbol", "")),
        ),
    )


def merge_watchlist_history(
    *row_groups: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Union canonical snapshots and web-local audit rows by symbol."""

    merged: dict[str, dict[str, Any]] = {}
    for rows in row_groups:
        for source in rows:
            symbol = _clean_symbol(source.get("symbol"))
            first_seen = str(source.get("first_seen_date") or "")
            last_seen = str(source.get("last_seen_date") or "")
            if not symbol or not first_seen or not last_seen:
                continue
            row = merged.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "name": "",
                    "first_seen_date": first_seen,
                    "last_seen_date": last_seen,
                    "watchlist_sessions": 0,
                    "watchlist_periods": 0,
                },
            )
            row["first_seen_date"] = min(row["first_seen_date"], first_seen)
            row["last_seen_date"] = max(row["last_seen_date"], last_seen)
            row["watchlist_sessions"] += int(source.get("watchlist_sessions") or 0)
            row["watchlist_periods"] += int(source.get("watchlist_periods") or 0)
            name = str(source.get("name") or "").strip()
            if name:
                row["name"] = name[:240]
    return _sort_rows(merged.values())
