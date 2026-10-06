from __future__ import annotations

import asyncio
import gzip
import json
import sqlite3

import pandas as pd

from src.web.cache import ChartBundleCache, ChartLoadCoordinator
from src.web.drawing_import import import_legacy_drawings
from src.web.market_data import (
    DemoMarketDataSource,
    MarketDataUnavailable,
    ReadOnlyMirrorMarketDataSource,
    _bundle_from_frame,
)
from src.web.store import WebStore


def test_demo_scanner_and_search_cover_non_scanner_symbol():
    source = DemoMarketDataSource()
    snapshot = source.scanner_snapshot(limit=5)
    assert len(snapshot["rows"]) == 5
    assert snapshot["source"] == "DEMO"
    assert all(row["symbol"] != "Q100" for row in snapshot["rows"])
    assert source.search("Q100")[0]["symbol"] == "Q100"


def test_daily_and_hourly_coverage_are_honest_and_sorted():
    source = DemoMarketDataSource()
    daily = source.chart_bundle("AAPL", "1D", daily_bars=750, hourly_months=6)
    hourly = source.chart_bundle("AAPL", "1H", daily_bars=750, hourly_months=6)
    assert len(daily["bars"]) == 750
    assert daily["coverage"]["bar_count"] == 750
    assert daily["coverage"]["unfinished_bar"] is None
    assert len(daily["indicators"]["ema50"]) == 750
    assert len(daily["indicators"]["relative_strength"]) == 750
    assert daily["indicators"]["relative_strength"][0]["value"] == 0.0
    assert len(daily["indicators"]["rs_sma50"]) == 750
    assert len(daily["indicators"]["rs_regime"]) == 750
    assert daily["context"]["adr_20"] > 0
    assert daily["context"]["return_1m"] is not None
    assert daily["context"]["return_3m"] is not None
    assert hourly["context"]["adr_20"] == daily["context"]["adr_20"]
    assert hourly["context"]["return_1m"] == daily["context"]["return_1m"]
    assert hourly["context"]["return_3m"] == daily["context"]["return_3m"]
    assert daily["market_alignment"]["leadership_label"] in {"STRONG", "MODERATE", "WEAK"}
    assert set(daily["market_alignment"]["states"]) == {"MKT", "SEG", "SEC", "IND"}
    assert daily["market_alignment"]["details"][-1]["title"] == "Metadata"
    assert any(event["status"] == "REPORTED" for event in daily["earnings"])
    assert any(event["status"] == "EXPECTED" for event in daily["earnings"])
    assert 700 <= len(hourly["bars"]) <= 1100
    hourly_times = [row["time"] for row in hourly["bars"]]
    assert hourly_times == sorted(set(hourly_times))
    start = pd.Timestamp(hourly["coverage"]["actual_start"])
    end = pd.Timestamp(hourly["coverage"]["actual_end"])
    assert 175 <= (end - start).days <= 190


def test_normalization_deduplicates_and_sorts_before_trim():
    index = pd.to_datetime(
        ["2026-01-03T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"]
    )
    frame = pd.DataFrame(
        {
            "Open": [3, 1, 4], "High": [4, 2, 5], "Low": [2, .5, 3],
            "Close": [3.5, 1.5, 4.5], "Volume": [30, 10, 40],
        },
        index=index,
    )
    bundle = _bundle_from_frame(
        symbol="TEST", timeframe="1D", frame=frame, source="TEST",
        requested_count=10, profile={"symbol": "TEST"},
    )
    assert [bar["time"] for bar in bundle["bars"]] == ["2026-01-01", "2026-01-03"]
    assert bundle["bars"][-1]["close"] == 4.5
    assert bundle["coverage"]["completeness"] == "PARTIAL"


def test_cache_detects_corruption_and_keeps_one_current_bundle(tmp_path):
    cache = ChartBundleCache(tmp_path, max_symbols=25)
    payload = DemoMarketDataSource().chart_bundle("AAPL", "1D", daily_bars=50, hourly_months=1)
    first = cache.write(payload)
    assert cache.read("AAPL", "1D").checksum == first.checksum
    artifact = cache.publication_artifact("AAPL", "1D")
    assert artifact is not None
    assert len(artifact.content) == artifact.metadata["compressed_bytes"]
    assert artifact.metadata["schema_version"] == 2
    assert artifact.metadata["payload_schema_version"] == 6
    assert len(artifact.metadata["compressed_checksum"]) == 64
    assert len(list(tmp_path.glob("AAPL.1D.json.gz"))) == 1
    path = tmp_path / "AAPL.1D.json.gz"
    path.write_bytes(gzip.compress(b'{"tampered":true}', mtime=0))
    assert cache.read("AAPL", "1D") is None


def test_cache_rejects_legacy_payload_without_relative_chart_schema(tmp_path):
    cache = ChartBundleCache(tmp_path, max_symbols=25)
    payload = DemoMarketDataSource().chart_bundle(
        "AAPL", "1D", daily_bars=50, hourly_months=1
    )
    payload["schema_version"] = 1
    cache.write(payload)
    assert cache.read("AAPL", "1D") is None
    assert cache.publication_artifact("AAPL", "1D") is None


def test_cache_never_reuses_a_bundle_from_a_different_data_source(tmp_path):
    cache = ChartBundleCache(tmp_path, max_symbols=25)
    payload = DemoMarketDataSource().chart_bundle(
        "AAPL", "1D", daily_bars=50, hourly_months=1
    )
    cache.write(payload)

    assert cache.read("AAPL", "1D", expected_source="DEMO") is not None
    assert (
        cache.read("AAPL", "1D", expected_source="LOCAL_SQLITE_MIRROR")
        is None
    )
    assert (
        cache.publication_artifact(
            "AAPL", "1D", expected_source="LOCAL_SQLITE_MIRROR"
        )
        is None
    )
    payload["coverage"]["source"] = "LOCAL_SQLITE_MIRROR:yfinance"
    cache.write(payload)
    assert (
        cache.read("AAPL", "1D", expected_source="LOCAL_SQLITE_MIRROR")
        is not None
    )


def test_cache_reloads_when_the_pc_mirror_revision_changes(tmp_path):
    cache = ChartBundleCache(tmp_path, max_symbols=25)
    payload = DemoMarketDataSource().chart_bundle(
        "AAPL", "1D", daily_bars=50, hourly_months=1
    )
    payload["coverage"]["source"] = "LOCAL_SQLITE_MIRROR"
    payload["coverage"]["source_revision"] = "mirror-r1"
    cache.write(payload)

    assert cache.read(
        "AAPL",
        "1D",
        expected_source="LOCAL_SQLITE_MIRROR",
        expected_source_revision="mirror-r1",
    ) is not None
    assert cache.read(
        "AAPL",
        "1D",
        expected_source="LOCAL_SQLITE_MIRROR",
        expected_source_revision="mirror-r2",
    ) is None


def test_compressed_chart_survives_mirror_update_during_preparation(tmp_path):
    class UpdatingSource(DemoMarketDataSource):
        def __init__(self):
            super().__init__()
            self.revision = 1
            self.loads = 0

        def cache_revision(self):
            return str(self.revision)

        def chart_bundle(self, *args, **kwargs):
            self.loads += 1
            payload = super().chart_bundle(*args, **kwargs)
            self.revision += 1
            return payload

    async def run():
        source = UpdatingSource()
        coordinator = ChartLoadCoordinator(source, ChartBundleCache(tmp_path), daily_bars=50, hourly_months=1)
        first, first_hit = await coordinator.get_compressed("AAPL", "1H")
        assert not first_hit
        assert json.loads(gzip.decompress(first.content))["coverage"]["source_revision"] == "1"
        second, second_hit = await coordinator.get_compressed("AAPL", "1H")
        assert not second_hit and source.loads == 2
        assert json.loads(gzip.decompress(second.content))["coverage"]["source_revision"] == "2"

    asyncio.run(run())


def test_coordinator_rebuilds_cache_when_generation_policy_changes(tmp_path):
    async def run():
        cache = ChartBundleCache(tmp_path, max_symbols=25)
        source = DemoMarketDataSource()
        first = ChartLoadCoordinator(
            source, cache, daily_bars=50, hourly_months=1
        )
        second = ChartLoadCoordinator(
            source, cache, daily_bars=60, hourly_months=1
        )

        first_bundle = await first.get("AAPL", "1D")
        second_bundle = await second.get("AAPL", "1D")

        assert len(first_bundle.payload["bars"]) == 50
        assert len(second_bundle.payload["bars"]) == 60
        assert second_bundle.cache_hit is False

    asyncio.run(run())


def test_coordinator_enforces_retention_and_preserves_pins(tmp_path):
    async def run():
        pinned = {"Q001"}
        coordinator = ChartLoadCoordinator(
            DemoMarketDataSource(),
            ChartBundleCache(tmp_path, max_symbols=25),
            daily_bars=50,
            hourly_months=1,
            pinned_symbols=lambda: pinned,
        )
        for index in range(1, 28):
            await coordinator.get(f"Q{index:03d}", "1D")

        symbols = {
            path.name.split(".", 1)[0]
            for path in tmp_path.glob("*.json.gz")
        }
        assert len(symbols) == 25
        assert "Q001" in symbols

    asyncio.run(run())


def test_coordinator_runtime_pins_preserve_connected_projection_symbols(tmp_path):
    async def run():
        coordinator = ChartLoadCoordinator(
            DemoMarketDataSource(),
            ChartBundleCache(tmp_path, max_symbols=25),
            daily_bars=50,
            hourly_months=1,
        )
        coordinator.pin_symbols(["Q001"])
        for index in range(1, 28):
            await coordinator.get(f"Q{index:03d}", "1D")

        symbols = {
            path.name.split(".", 1)[0]
            for path in tmp_path.glob("*.json.gz")
        }
        assert len(symbols) == 25
        assert "Q001" in symbols

    asyncio.run(run())


def test_cache_eviction_never_removes_pinned_symbol(tmp_path):
    cache = ChartBundleCache(tmp_path, max_symbols=25)
    source = DemoMarketDataSource()
    for index in range(1, 28):
        cache.write(source.chart_bundle(f"Q{index:03d}", "1D", daily_bars=50, hourly_months=1))
    removed = cache.evict(pinned_symbols={"Q001"})
    assert "Q001" not in removed
    assert (tmp_path / "Q001.1D.json.gz").exists()
    assert len({path.name.split('.')[0] for path in tmp_path.glob('*.json.gz')}) == 25


def test_concurrent_chart_load_is_deduplicated(tmp_path):
    class CountingSource(DemoMarketDataSource):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def chart_bundle(self, *args, **kwargs):
            self.calls += 1
            return super().chart_bundle(*args, **kwargs)

    async def run():
        source = CountingSource()
        coordinator = ChartLoadCoordinator(source, ChartBundleCache(tmp_path), daily_bars=50, hourly_months=1)
        values = await asyncio.gather(*[coordinator.get("AAPL", "1D") for _ in range(8)])
        assert source.calls == 1
        assert len({value.checksum for value in values}) == 1

    asyncio.run(run())


def test_compressed_cache_hit_skips_payload_decode(tmp_path):
    async def run():
        coordinator = ChartLoadCoordinator(
            DemoMarketDataSource(),
            ChartBundleCache(tmp_path),
            daily_bars=50,
            hourly_months=1,
        )
        first, first_hit = await coordinator.get_compressed("AAPL", "1D")
        second, second_hit = await coordinator.get_compressed("AAPL", "1D")
        assert first_hit is False
        assert second_hit is True
        assert second.content == first.content
        assert coordinator.request_count == 2

    asyncio.run(run())


def test_hourly_failure_does_not_erase_daily_cache(tmp_path):
    class DailyOnly(DemoMarketDataSource):
        def chart_bundle(self, symbol, timeframe, **kwargs):
            if timeframe == "1H":
                raise MarketDataUnavailable("hourly unavailable")
            return super().chart_bundle(symbol, timeframe, **kwargs)

    async def run():
        coordinator = ChartLoadCoordinator(DailyOnly(), ChartBundleCache(tmp_path), daily_bars=50, hourly_months=1)
        daily = await coordinator.get("AAPL", "1D")
        assert daily.payload["timeframe"] == "1D"
        try:
            await coordinator.get("AAPL", "1H")
        except MarketDataUnavailable:
            pass
        else:
            raise AssertionError("hourly load should fail")
        assert coordinator.cache.read("AAPL", "1D") is not None

    asyncio.run(run())


def test_local_mirror_reads_existing_schema_without_modifying_it(tmp_path):
    path = tmp_path / "mirror.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE scanner_metrics (
                symbol TEXT, date TEXT, price_history_days INTEGER,
                price REAL, score REAL, rs_score_252 REAL, return_1m REAL,
                return_3m REAL, adr_20 REAL
            );
            CREATE TABLE scanner_metric_snapshots (
                snapshot_date TEXT, completed_at TEXT, metric_count INTEGER
            );
            CREATE TABLE stock_profiles (
                symbol TEXT, company_name TEXT, exchange TEXT,
                sector_name TEXT, industry_name TEXT
            );
            CREATE TABLE price_history (
                symbol TEXT, interval TEXT, date TEXT, open REAL, high REAL,
                low REAL, close REAL, adj_close REAL, volume REAL
            );
            CREATE TABLE hourly_price_history (
                symbol TEXT, timestamp TEXT, open REAL, high REAL, low REAL,
                close REAL, adj_close REAL, volume REAL, source TEXT
            );
            CREATE TABLE earnings_events (
                symbol TEXT, report_date TEXT, report_timing TEXT,
                event_status TEXT, eps_growth_status TEXT,
                is_date_estimated INTEGER, reported_eps REAL,
                estimated_eps REAL, eps_surprise_pct REAL,
                eps_yoy_growth_pct REAL
            );
            CREATE TABLE stock_market_alignment_daily (
                symbol TEXT, as_of_date TEXT, feature_version TEXT,
                market_rs REAL, market_rs_source TEXT, industry_peer_rs REAL,
                peer_basis TEXT, peer_count INTEGER, peer_group_name TEXT,
                leadership_score REAL, leadership_label TEXT,
                market_state TEXT, segment_name TEXT, segment_proxy TEXT,
                segment_state TEXT, sector_name TEXT, sector_proxy TEXT,
                sector_state TEXT, industry_name TEXT,
                industry_proxy_or_index TEXT, industry_state TEXT,
                context_label TEXT, is_provisional INTEGER,
                classification_source TEXT, calculation_details_json TEXT,
                calculated_at TEXT
            );
            CREATE TABLE market_alignment_batches (
                as_of_date TEXT, feature_version TEXT, status TEXT
            );
            INSERT INTO scanner_metrics VALUES ('AAPL','2026-09-30',1000,100,95,92,12,34,5.67);
            INSERT INTO scanner_metric_snapshots VALUES ('2026-09-30','2026-10-01',1);
            INSERT INTO stock_profiles VALUES ('AAPL','Apple Test','NASDAQ','Technology','Hardware');
            INSERT INTO price_history VALUES ('AAPL','1d','2026-09-29',99,102,98,101,101,1000000);
            INSERT INTO price_history VALUES ('AAPL','1d','2026-09-30',101,104,100,103,103,1200000);
            INSERT INTO price_history VALUES ('SPY','1d','2026-09-29',600,602,598,601,601,9000000);
            INSERT INTO price_history VALUES ('SPY','1d','2026-09-30',601,604,600,603,603,9200000);
            INSERT INTO hourly_price_history VALUES ('AAPL','2026-09-30T13:30:00+00:00',101,102,100,101.5,101.5,10000,'FIXTURE');
            INSERT INTO hourly_price_history VALUES ('SPY','2026-09-30T13:30:00+00:00',601,602,600,601.5,601.5,80000,'FIXTURE');
            INSERT INTO earnings_events VALUES ('AAPL','2026-09-30','AMC','REPORTED','NORMAL',0,0.80,1.00,-20.0,-12.0);
            INSERT INTO stock_market_alignment_daily VALUES (
                'AAPL','2026-09-30','1.0',88,'scanner_growth_rank_1m',72,
                'industry',18,'Hardware',82.5,'STRONG',
                'GREEN','Mega-Cap','MGK','YELLOW','Technology','XLK',
                'GREEN','Hardware','Demo index','RED','SUPPORTIVE',0,
                'fixture','{}','2026-10-01T01:00:00+00:00'
            );
            INSERT INTO market_alignment_batches VALUES ('2026-09-30','1.0','PUBLISHED');
            """
        )
    before = path.read_bytes()
    source = ReadOnlyMirrorMarketDataSource(path)
    assert source.scanner_snapshot(limit=1)["rows"][0]["symbol"] == "AAPL"
    assert source.search("Apple")[0]["company"] == "Apple Test"
    daily = source.chart_bundle("AAPL", "1D", daily_bars=750, hourly_months=6)
    hourly = source.chart_bundle("AAPL", "1H", daily_bars=750, hourly_months=6)
    assert daily["coverage"]["completeness"] == "PARTIAL"
    assert len(daily["indicators"]["relative_strength"]) == 2
    assert daily["indicators"]["relative_strength"][0]["value"] == 0.0
    assert daily["earnings"] == [
        {
            "date": "2026-09-30",
            "timing": "AMC",
            "status": "REPORTED",
            "growth_status": "NORMAL",
            "estimated": False,
            "reported_eps": 0.8,
            "estimated_eps": 1.0,
            "eps_surprise_pct": -20.0,
            "eps_yoy_growth_pct": -12.0,
        }
    ]
    assert daily["market_alignment"]["score"] == 83
    assert daily["market_alignment"]["leadership_label"] == "STRONG"
    assert daily["market_alignment"]["context_label"] == "SUPPORTIVE"
    assert daily["market_alignment"]["states"] == {
        "MKT": "GREEN", "SEG": "YELLOW", "SEC": "GREEN", "IND": "RED"
    }
    assert daily["market_alignment"]["stale"] is True
    assert hourly["market_alignment"] == daily["market_alignment"]
    assert hourly["coverage"]["source"].endswith("FIXTURE")
    assert hourly["context"]["adr_20"] == 5.67
    assert hourly["context"]["return_1m"] == 12.0
    assert hourly["context"]["return_3m"] == 34.0
    assert path.read_bytes() == before
    assert not path.with_name(path.name + "-wal").exists()


def test_local_mirror_scanner_applies_saved_setup_and_computed_ranking(tmp_path):
    path = tmp_path / "mirror.db"
    setups_path = tmp_path / "scanner_setups.json"
    setups_path.write_text(
        json.dumps(
            {
                "setups": {
                    "Momentum": {
                        "rules": [
                            {"attribute": "volume", "operator": ">=", "threshold": 100000},
                            {"attribute": "growth_rank_1m", "operator": ">=", "threshold": 95},
                        ]
                    },
                    "Strict": {
                        "rules": [
                            {"attribute": "growth_rank_1m", "operator": ">=", "threshold": 99},
                        ]
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE scanner_metrics (
                symbol TEXT, date TEXT, price_history_days INTEGER, price REAL,
                volume REAL, dollar_volume REAL, adr REAL, adr_20 REAL,
                growth_rank REAL, growth_rank_1m REAL, trend_intensity REAL,
                score REAL, rs_score_252 REAL, return_1m REAL
            );
            CREATE TABLE scanner_metric_snapshots (
                snapshot_date TEXT, completed_at TEXT, metric_count INTEGER
            );
            INSERT INTO scanner_metrics VALUES
                ('AAA','2026-10-01',300,10,50000,500000,3,3,99.9,99.9,99,0,90,25),
                ('ZZZ','2026-10-01',300,20,500000,10000000,7,7,99.5,99.5,98,0,95,20),
                ('MMM','2026-10-01',300,30,400000,12000000,4,4,97,97,96,0,85,15);
            INSERT INTO scanner_metric_snapshots VALUES ('2026-10-01','2026-10-02',3);
            """
        )

    source = ReadOnlyMirrorMarketDataSource(
        path, scanner_setups_path=setups_path
    )
    momentum = source.scanner_snapshot(limit=300, setup="Momentum")
    strict = source.scanner_snapshot(limit=300, setup="Strict")

    assert momentum["setup"] == "Momentum"
    assert momentum["available_setups"] == ["Momentum", "Strict"]
    assert momentum["total_matches"] == 2
    assert [row["symbol"] for row in momentum["rows"]] == ["ZZZ", "MMM"]
    assert momentum["rows"][0]["score"] > momentum["rows"][1]["score"]
    assert strict["total_matches"] == 2
    assert [row["symbol"] for row in strict["rows"]] == ["ZZZ", "AAA"]


def test_drawing_import_is_dry_run_stable_and_idempotent(tmp_path):
    source = tmp_path / "chart_drawings.json"
    source.write_text(
        json.dumps({"AAPL": [{
            "start_date": "2026-09-01T13:30:00Z", "start_price": 100,
            "end_date": "2026-09-02T13:30:00Z", "end_price": 110,
            "timeframe": "1H",
        }]}),
        encoding="utf-8",
    )
    store = WebStore(tmp_path / "state.db")
    dry = import_legacy_drawings(store, [source], dry_run=True)
    assert dry.imported == 1
    assert store.list_drawings("AAPL") == []
    applied = import_legacy_drawings(store, [source], dry_run=False)
    assert applied.imported == 1
    again = import_legacy_drawings(store, [source], dry_run=False)
    assert again.unchanged == 1
    assert len(store.list_drawings("AAPL")) == 1
