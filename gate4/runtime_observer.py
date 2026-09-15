"""Opt-in bridge from the controlled-live runtime to Gate-4 evidence."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from gate4.collector import Gate4EvidenceCollector
from src.core.exit_policy import market_session_date
from src.utils.config import get_env_value, resolve_repo_path


ROOT = Path(__file__).resolve().parents[1]


_lock = threading.RLock()
_collector: Gate4EvidenceCollector | None = None


def _enabled() -> bool:
    return str(get_env_value("GATE4_QUALIFICATION_ENABLED", "false") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def configured_collector() -> Gate4EvidenceCollector | None:
    global _collector
    if not _enabled():
        return None
    with _lock:
        if _collector is not None:
            return _collector
        path_value = str(get_env_value("GATE4_EVIDENCE_JOURNAL_PATH", "") or "").strip()
        commit = str(get_env_value("KIS_RUNTIME_COMMIT_SHA", "") or "").strip().lower()
        if not path_value or not commit:
            raise RuntimeError(
                "Gate-4 qualification requires GATE4_EVIDENCE_JOURNAL_PATH and "
                "KIS_RUNTIME_COMMIT_SHA"
            )
        path = resolve_repo_path(Path(path_value)).resolve()
        try:
            path.relative_to(ROOT.resolve())
        except ValueError:
            pass
        else:
            raise RuntimeError("Gate-4 evidence journal must remain outside the repository")
        _collector = Gate4EvidenceCollector(journal_path=path, commit_sha=commit)
        return _collector


def observe_gate4_event(event_type: str, **payload: Any) -> None:
    """Durably append an event, or do nothing when qualification is disabled."""
    collector = configured_collector()
    if collector is None:
        return
    session_date = str(
        payload.setdefault("session_date", market_session_date().isoformat())
    )
    dated = [
        event
        for event in collector.journal.read_all()
        if event.payload.get("session_date") == session_date
    ]
    starts = sum(event.event_type == "SESSION_STARTED" for event in dated)
    ends = sum(event.event_type == "SESSION_ENDED" for event in dated)
    if event_type == "SESSION_STARTED":
        raise RuntimeError("Gate-4 sessions must be started by the supervised-session CLI")
    if starts != 1 or ends:
        raise RuntimeError(
            "Gate-4 qualification event rejected: start one supervised, disarmed "
            "session with manage_gate4_session.py before activating the runtime"
        )
    collector.record(event_type, **payload)


def reset_runtime_observer_for_tests() -> None:
    global _collector
    with _lock:
        _collector = None


__all__ = [
    "configured_collector",
    "observe_gate4_event",
    "reset_runtime_observer_for_tests",
]
