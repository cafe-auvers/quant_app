import ast
from pathlib import Path
from types import SimpleNamespace

from src.ui.mixins.dashboard_mixin import DashboardMixin


ROOT_DIR = Path(__file__).resolve().parents[1]


def test_retired_dashboard_summary_refresh_is_a_safe_noop():
    window = SimpleNamespace(
        sender=lambda: (_ for _ in ()).throw(
            AssertionError("dashboard refresh must not inspect QObject.sender()")
        )
    )

    DashboardMixin.update_dashboard_summary(window)

    assert not hasattr(window, "dashboard_summary_label")


def test_ui_code_does_not_call_qobject_sender():
    offenders = []
    for path in (ROOT_DIR / "src" / "ui").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sender"
            ):
                offenders.append(f"{path.relative_to(ROOT_DIR)}:{node.lineno}")

    assert offenders == []
