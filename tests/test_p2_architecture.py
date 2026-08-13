import datetime as dt
from types import SimpleNamespace


def test_database_facade_exposes_focused_p2_modules(monkeypatch):
    import src.infrastructure.database as database
    import src.utils.db_loader as legacy
    from src.infrastructure.database import engine, mirror, refresh, schema
    from src.infrastructure.database.repositories import market_data, scanner

    assert legacy is database
    assert database.validate_mysql_identifier.__module__ == engine.__name__
    assert database.sync_local_mirror_from_pc.__module__ == mirror.__name__
    assert database._get_price_history_table.__module__ == schema.__name__
    assert database.save_symbol_history_to_db.__module__ == market_data.__name__
    assert database.refresh_universe_history_to_db.__module__ == refresh.__name__
    assert database.load_scanner_metrics_from_db.__module__ == scanner.__name__

    monkeypatch.setattr(legacy, "CACHE_QUERY_SYMBOL_CHUNK_SIZE", 7)
    assert market_data.CACHE_QUERY_SYMBOL_CHUNK_SIZE == 7
    assert scanner.CACHE_QUERY_SYMBOL_CHUNK_SIZE == 7


def test_buylist_legacy_import_resolves_to_decomposed_package():
    import src.ui.buylist as buylist
    import src.ui.mixins.buylist_mixin as legacy

    assert legacy is buylist
    assert "BuylistViewMixin" in buylist.BuylistMixin._build_buylist_tab.__qualname__
    assert "BuylistActionsMixin" in buylist.BuylistMixin._open_orders_for_buylist_item.__qualname__
    assert "BuylistMonitoringMixin" in buylist.BuylistMixin._run_buylist_monitor_cycle.__qualname__
    assert "BuylistOrdersMixin" in buylist.BuylistMixin._submit_kis_buy_order.__qualname__


def test_buylist_controller_owns_pure_exit_decisions():
    from src.ui.buylist.controller import BuylistController, US_MARKET_ZONE

    assert BuylistController.partial_exit_quantity(10) == 3
    assert BuylistController.partial_exit_quantity(1) == 1
    assert BuylistController.partial_exit_quantity(0) == 0

    item = SimpleNamespace(_latest_daily_close=90.0, _ema10=95.0, _ema20=85.0)
    assert BuylistController.momentum_exit_signal(item) == "10 EMA"

    before_close = dt.datetime(
        2026, 8, 13, 15, 59, tzinfo=US_MARKET_ZONE
    )
    rows = [
        (dt.date(2026, 8, 12), 100.0),
        (dt.date(2026, 8, 13), 101.0),
    ]
    assert BuylistController.completed_daily_close_rows(rows, before_close) == [
        (dt.date(2026, 8, 12), 100.0)
    ]


def test_chart_legacy_imports_resolve_to_p2_modules_and_models():
    import src.ui.charts.controller as controller
    import src.ui.charts.renderer as renderer
    import src.ui.mixins.charts_controller_mixin as legacy_controller
    import src.ui.mixins.charts_render_mixin as legacy_renderer
    from src.ui.charts.models import normalize_chart_options

    assert legacy_controller is controller
    assert legacy_renderer is renderer
    assert normalize_chart_options({"show_rs": False, "timeframe": "1H"}) == {
        "show_volume": True,
        "show_rs": False,
        "show_ema": True,
        "show_adr": True,
        "show_growth_1m": True,
        "show_growth_3m": True,
        "show_growth_6m": False,
        "timeframe": "1H",
    }
