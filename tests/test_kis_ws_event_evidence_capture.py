"""Safety checks for the credentialed read-only WS evidence collector."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import capture_kis_ws_event_evidence as collector


def test_git_snapshot_rejects_dirty_checkout(monkeypatch):
    responses = iter(("a" * 40 + "\n", " M tests/example.py\n"))

    def fake_run(*args, **kwargs):
        return SimpleNamespace(stdout=next(responses))

    monkeypatch.setattr(collector.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="clean exact commit"):
        collector._git_snapshot()


def test_git_snapshot_returns_clean_exact_head(monkeypatch):
    responses = iter(("b" * 40 + "\n", ""))

    def fake_run(*args, **kwargs):
        return SimpleNamespace(stdout=next(responses))

    monkeypatch.setattr(collector.subprocess, "run", fake_run)

    assert collector._git_snapshot() == "b" * 40
