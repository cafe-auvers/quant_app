"""Static compatibility facade for focused market-data repositories."""

from .chart_indicators import (_chart_indicator_manifest_matches,
                               _get_chart_indicator_manifests,
                               _history_watermark_values,
                               calculate_chart_indicators,
                               calculate_chart_indicators_since,
                               get_chart_indicator_refresh_plan,
                               get_latest_chart_indicator_dates,
                               get_latest_chart_indicator_source_dates,
                               load_chart_indicators_from_db,
                               refresh_chart_indicators_for_symbol,
                               refresh_chart_indicators_to_db,
                               save_chart_indicators_batch_to_db,
                               save_chart_indicators_to_db)
from .market_bars import (delete_intraday_history_for_symbol,
                          get_latest_hourly_price_history_timestamp,
                          load_hourly_history_from_db,
                          load_intraday_history_from_db,
                          load_symbol_history_from_db,
                          load_universe_history_from_db,
                          prune_intraday_history, save_hourly_history_to_db,
                          save_intraday_history_to_db,
                          save_symbol_history_to_db,
                          save_universe_history_batch_to_db,
                          save_universe_hourly_history_batch_to_db)
from .market_watermarks import (get_latest_hourly_price_history_timestamps,
                                get_latest_price_history_date,
                                get_latest_price_history_dates,
                                get_price_history_watermarks)

__all__ = [name for name in globals() if not name.startswith("_")]
