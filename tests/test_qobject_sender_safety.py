import ast
from pathlib import Path
from types import SimpleNamespace

from src.ui.mixins.dashboard_mixin import DashboardMixin


ROOT_DIR = Path(__file__).resolve().parents[1]


class _Label:
    def __init__(self) -> None:
        self.value = ""

    def setText(self, value: str) -> None:
        self.value = value


def _dashboard_window():
    return SimpleNamespace(
        _cached_market_data_status="cached status",
        _format_market_data_status=lambda: "Unavailable",
        scanner_results=[],
        watchlist=SimpleNamespace(items=[]),
        trade_manager=SimpleNamespace(get_active_plans=lambda: []),
        db_enabled=False,
        dashboard_summary_label=_Label(),
        sender=lambda: (_ for _ in ()).throw(
            AssertionError("dashboard refresh must not inspect QObject.sender()")
        ),
    )


def test_dashboard_refresh_never_inspects_qobject_sender():
    window = _dashboard_window()

    DashboardMixin.update_dashboard_summary(window)

    assert window._cached_market_data_status == "cached status"
    assert "Scanner yielded 0 candidates." in window.dashboard_summary_label.value


def test_manual_dashboard_refresh_explicitly_invalidates_cached_status():
    calls = []
    window = SimpleNamespace(
        update_dashboard_summary=lambda **kwargs: calls.append(kwargs)
    )

    DashboardMixin._refresh_dashboard_summary_manually(window, False)

    assert calls == [{"force": True}]


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
