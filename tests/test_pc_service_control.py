"""Tests for guarded laptop-to-PC service controls."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.services import pc_remote_control
from src.ui.workers import PcRemoteServiceStartWorker


def test_start_pc_service_rejects_unknown_service_without_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pc_remote_control.subprocess,
        "run",
        lambda *_args, **_kwargs: calls.append(True),
    )

    result = pc_remote_control.start_pc_service("database")

    assert result.success is False
    assert "listener" in result.message
    assert calls == []


def test_start_pc_service_parses_guarded_main_result(monkeypatch, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    helper = scripts / "start_pc_service_remote.ps1"
    helper.write_text("# test helper", encoding="utf-8")
    payload = {
        "success": False,
        "service": "main",
        "message": "PC main.py start refused during qualification.",
        "listener_running": True,
        "main_running": False,
        "qualification_guard_active": True,
    }
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps(payload) + "\n",
            stderr="",
        )

    monkeypatch.setattr(pc_remote_control, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(pc_remote_control.os, "name", "nt")
    monkeypatch.setattr(pc_remote_control.subprocess, "run", fake_run)

    result = pc_remote_control.start_pc_service("main", timeout=12)

    assert result.success is False
    assert result.qualification_guard_active is True
    assert result.listener_running is True
    assert observed["command"][-2:] == ["-Service", "Main"]
    assert observed["kwargs"]["timeout"] == 12
    assert "shell" not in observed["kwargs"]


def test_start_pc_service_reports_unparseable_helper_output(monkeypatch, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "start_pc_service_remote.ps1").write_text(
        "# test helper", encoding="utf-8"
    )
    monkeypatch.setattr(pc_remote_control, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(pc_remote_control.os, "name", "nt")
    monkeypatch.setattr(
        pc_remote_control.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="not json\n",
            stderr="WinRM unavailable",
        ),
    )

    result = pc_remote_control.start_pc_service("listener")

    assert result.success is False
    assert result.message == "WinRM unavailable"


def test_pc_remote_service_worker_emits_service_result(monkeypatch):
    expected = pc_remote_control.PcServiceStartResult(
        True,
        "listener",
        "started",
        listener_running=True,
    )
    monkeypatch.setattr(
        pc_remote_control,
        "start_pc_service",
        lambda _service: expected,
    )
    observed = []
    worker = PcRemoteServiceStartWorker("listener")
    worker.finished_start.connect(observed.append)

    worker.run()

    assert observed == [expected]
