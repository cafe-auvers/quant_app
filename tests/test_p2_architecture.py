import ast
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
P2_SOURCE_ROOTS = (
    ROOT / "src" / "infrastructure" / "database",
    ROOT / "src" / "ui" / "buylist",
    ROOT / "src" / "ui" / "charts",
)


def _p2_source_files():
    for root in P2_SOURCE_ROOTS:
        yield from root.rglob("*.py")


def test_database_facades_are_static_and_point_to_focused_owners(monkeypatch):
    import src.infrastructure.database as database
    import src.utils.db_loader as legacy
    from src.infrastructure.database import (engine, mirror_copy, refresh,
                                             schema)
    from src.infrastructure.database.repositories import market_bars, scanner
    from src.infrastructure.database.settings import \
        CACHE_QUERY_SYMBOL_CHUNK_SIZE

    assert legacy is not database
    assert legacy.validate_mysql_identifier is database.validate_mysql_identifier
    assert database.validate_mysql_identifier.__module__ == engine.__name__
    assert database.sync_local_mirror_from_pc.__module__ == mirror_copy.__name__
    assert database._get_price_history_table.__module__ == schema.__name__
    assert database.save_symbol_history_to_db.__module__ == market_bars.__name__
    assert database.refresh_universe_history_to_db.__module__ == refresh.__name__
    assert database.load_scanner_metrics_from_db.__module__ == scanner.__name__

    monkeypatch.setattr(legacy, "CACHE_QUERY_SYMBOL_CHUNK_SIZE", 7)
    assert market_bars.CACHE_QUERY_SYMBOL_CHUNK_SIZE == CACHE_QUERY_SYMBOL_CHUNK_SIZE


def test_buylist_compatibility_module_exports_the_static_composite():
    import src.ui.buylist as buylist
    import src.ui.mixins.buylist_mixin as legacy

    assert legacy is not buylist
    assert legacy.BuylistMixin is buylist.BuylistMixin
    assert "BuylistViewMixin" in buylist.BuylistMixin._build_buylist_tab.__qualname__
    assert "BuylistActionsMixin" in buylist.BuylistMixin._open_orders_for_buylist_item.__qualname__
    assert "BuylistMonitoringMixin" in buylist.BuylistMixin._run_buylist_monitor_cycle.__qualname__
    assert "BuylistOrdersMixin" in buylist.BuylistMixin._submit_kis_buy_order.__qualname__


def test_core_exit_policy_owns_buylist_decisions():
    from src.core import exit_policy
    from src.ui.buylist.controller import US_MARKET_ZONE, BuylistController

    assert BuylistController.partial_exit_quantity is exit_policy.partial_exit_quantity
    assert BuylistController.momentum_exit_signal is exit_policy.momentum_exit_signal
    assert BuylistController.partial_exit_quantity(10) == 3
    assert BuylistController.partial_exit_quantity(1) == 1
    assert BuylistController.partial_exit_quantity(0) == 0

    item = SimpleNamespace(_latest_daily_close=90.0, _ema10=95.0, _ema20=85.0)
    assert BuylistController.momentum_exit_signal(item) == "10 EMA"

    before_close = dt.datetime(2026, 8, 13, 15, 59, tzinfo=US_MARKET_ZONE)
    rows = [
        (dt.date(2026, 8, 12), 100.0),
        (dt.date(2026, 8, 13), 101.0),
    ]
    assert BuylistController.completed_daily_close_rows(rows, before_close) == [
        (dt.date(2026, 8, 12), 100.0)
    ]


def test_chart_compatibility_modules_export_static_composites_and_models():
    import src.ui.charts.controller as controller
    import src.ui.charts.renderer as renderer
    import src.ui.mixins.charts_controller_mixin as legacy_controller
    import src.ui.mixins.charts_render_mixin as legacy_renderer
    from src.ui.charts.models import normalize_chart_options

    assert legacy_controller is not controller
    assert legacy_renderer is not renderer
    assert legacy_controller.ChartsControllerMixin is controller.ChartsControllerMixin
    assert legacy_renderer.ChartsRenderMixin is renderer.ChartsRenderMixin
    assert normalize_chart_options({"show_rs": False, "timeframe": "1H"}) == {
        "show_volume": True,
        "show_rs": False,
        "show_ema": True,
        "show_adr": True,
        "show_growth_1m": True,
        "show_growth_3m": True,
        "show_growth_6m": False,
        "show_stock_profile_watermark": True,
        "show_earnings_events": True,
        "show_earnings_line": True,
        "earnings_horizon_days": 14,
        "timeframe": "1H",
    }


def test_chart_interaction_settings_are_explicit_and_deterministic():
    from src.ui.charts.models import normalize_chart_interaction_settings

    shortcuts, pan_step_bars = normalize_chart_interaction_settings(
        {
            "shortcuts": {
                "set_target": "Ctrl+T",
                "pan_left": "A",
                "unknown_action": "X",
            },
            "chart_pan_step_bars": "7",
        },
        renderer="local",
    )

    assert shortcuts["set_target"] == "Ctrl+T"
    assert shortcuts["pan_left"] == "A"
    assert shortcuts["draw_line"] == "D"
    assert "unknown_action" not in shortcuts
    assert pan_step_bars == 7
    assert normalize_chart_interaction_settings(
        {"shortcuts": "invalid", "chart_pan_step_bars": 0},
        renderer="lightweight",
    )[1] == 1


def test_p2_modules_use_no_wildcard_or_runtime_module_mutation():
    violations = []
    for path in _p2_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == "*" for alias in node.names
            ):
                violations.append(f"{path}: wildcard import")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if (
                    isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Name)
                    and node.func.value.func.id == "globals"
                    and node.func.attr == "update"
                ):
                    violations.append(f"{path}: globals().update")
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "__class__":
                        violations.append(f"{path}: __class__ assignment")
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Attribute)
                        and isinstance(target.value.value, ast.Name)
                        and target.value.value.id == "sys"
                        and target.value.attr == "modules"
                    ):
                        violations.append(f"{path}: sys.modules assignment")
    assert violations == []


def test_production_code_does_not_depend_on_legacy_database_facade():
    violations = []
    for path in (ROOT / "src").rglob("*.py"):
        if path == ROOT / "src" / "utils" / "db_loader.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "src.utils.db_loader":
                violations.append(str(path))
            if isinstance(node, ast.Import):
                if any(alias.name == "src.utils.db_loader" for alias in node.names):
                    violations.append(str(path))
    assert violations == []


def test_layer_dependencies_remain_one_way():
    violations = []
    rendering_files = list((ROOT / "src" / "ui" / "charts").glob("render*.py"))
    domain_files = [
        *list((ROOT / "src" / "core").rglob("*.py")),
        *list((ROOT / "src" / "risk").rglob("*.py")),
    ]
    infrastructure_files = list(
        (ROOT / "src" / "infrastructure" / "database").rglob("*.py")
    )

    forbidden_render_prefixes = (
        "src.api",
        "src.services.app_state",
        "src.services.order_ledger",
        "src.ui.workers",
        "src.utils.storage",
    )
    for path in rendering_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else ""
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            if module.startswith(forbidden_render_prefixes) or any(
                name.startswith(forbidden_render_prefixes) for name in names
            ):
                violations.append(f"{path}: renderer imports application workflow")

    for path in infrastructure_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else ""
            if module.startswith("src.ui"):
                violations.append(f"{path}: infrastructure imports UI")

    for path in domain_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else ""
            if module.startswith(("src.ui", "src.infrastructure.database")):
                violations.append(f"{path}: domain/risk imports implementation layer")

    assert violations == []


def test_large_p2_modules_are_split_behind_small_static_composites():
    chart_root = ROOT / "src" / "ui" / "charts"
    database_root = ROOT / "src" / "infrastructure" / "database"

    assert (chart_root / "controller.py").stat().st_size < 2_000
    assert (chart_root / "renderer.py").stat().st_size < 2_000
    assert max(
        path.stat().st_size
        for path in chart_root.glob("*.py")
        if path.name not in {"controller.py", "renderer.py"}
    ) < 80_000
    assert (database_root / "mirror.py").stat().st_size < 2_000
    assert (
        database_root / "repositories" / "market_data.py"
    ).stat().st_size < 2_000
    assert max(
        path.stat().st_size
        for path in database_root.glob("mirror_*.py")
    ) < 80_000
