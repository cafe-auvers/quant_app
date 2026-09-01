from __future__ import annotations

import runpy
import sys
import types
from pathlib import Path

from src.utils import config


def test_gate2_entrypoint_installs_repository_configuration_before_reporting_import(
    monkeypatch,
):
    calls: list[str] = []

    monkeypatch.setattr(
        config,
        "install_repository_configuration",
        lambda: calls.append("install"),
    )

    fake_reporting = types.ModuleType("gate2.reporting")

    def reporting_attribute(name: str):
        if name != "main":
            raise AttributeError(name)
        assert calls == ["install"]
        return lambda argv=None: 0

    fake_reporting.__getattr__ = reporting_attribute  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gate2.reporting", fake_reporting)

    root = Path(__file__).resolve().parents[1]
    runpy.run_path(str(root / "scripts" / "run_gate2_soak.py"), run_name="gate2_test")

    assert calls == ["install"]
