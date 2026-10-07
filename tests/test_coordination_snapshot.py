from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from src.core.trade_card_state import TradeCardState
from src.services import coordination_snapshot as snapshots
from src.services import state_sync
from src.services import trade_card_repository as cards


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'shared.db'}")
    table = cards.ensure_trade_cards_table(engine)
    timestamp = dt.datetime(2026, 10, 7)
    records = []
    for index in range(300):
        card = TradeCardState(environment="PROD", account_no="1", symbol=f"S{index:03}")
        payload = card.to_dict()
        payload["name"] = "n" * max(0, 4200 - len(json.dumps(payload)))
        records.append(dict(
            environment="PROD", account_no="1", symbol=card.symbol,
            board_status=card.board_status.value, version=1,
            payload=json.dumps(payload), updated_at=timestamp,
        ))
    with engine.begin() as connection:
        connection.execute(table.insert(), records)
    transfers = []
    original = snapshots.coordination_read_connection

    class Connection:
        def __init__(self, target):
            self.target = target

        def __enter__(self):
            self.connection = original(self.target).__enter__()
            return self

        def __exit__(self, *arguments):
            return self.connection.__exit__(*arguments)

        def execute(self, statement):
            rows = self.connection.execute(statement).fetchall()
            transfers.append(rows)

            class Result:
                def fetchall(self):
                    return rows

            return Result()

    monkeypatch.setattr(snapshots, "coordination_read_connection", Connection)
    yield engine, table, transfers
    engine.dispose()


def test_unchanged_reads_return_no_rows_and_mutable_models_are_isolated(store):
    engine, table, transfers = store
    first = cards.list_trade_cards(engine, environment="PROD", raise_on_error=True)
    assert len(transfers[-1]) == 300
    first[0].name = "unsaved local mutation"
    first[0].version = 500
    for _ in range(20):
        current = cards.list_trade_cards(engine, environment="PROD", raise_on_error=True)
        assert len(current) == 300 and current[0].version == 1
        assert current[0].name != "unsaved local mutation"
        assert transfers[-1] == []


def test_single_card_reads_remain_fresh_without_retransferring_payloads(store):
    engine, table, transfers = store
    first = cards.get_trade_card(engine, "PROD", "1", "S000", raise_on_error=True)
    first.name = "uncommitted caller edit"
    for _ in range(10):
        current = cards.get_trade_card(engine, "PROD", "1", "S000", raise_on_error=True)
        assert current.name != "uncommitted caller edit"
        assert transfers[-1] == []
    peer = create_engine(engine.url)
    with peer.begin() as connection:
        connection.execute(table.update().where(table.c.symbol == "S000").values(version=2))
    assert cards.get_trade_card(engine, "PROD", "1", "S000", raise_on_error=True).version == 2
    assert len(transfers[-1]) == 1
    with peer.begin() as connection:
        connection.execute(table.delete().where(table.c.symbol == "S000"))
    assert cards.get_trade_card(engine, "PROD", "1", "S000", raise_on_error=True) is None
    assert len(transfers[-1]) == 1 and transfers[-1][0].version is None
    assert cards.get_trade_card(engine, "PROD", "2", "S001", raise_on_error=True) is None
    assert cards.get_trade_card(engine, "PROD", "1", "S001", raise_on_error=True).symbol == "S001"
    peer.dispose()


def test_single_card_failed_verification_never_returns_cached_success(store, monkeypatch):
    engine, _, _ = store
    assert cards.get_trade_card(engine, "PROD", "1", "S000", raise_on_error=True)

    def unavailable(_engine):
        raise OperationalError("SELECT", {}, RuntimeError("unavailable"))

    monkeypatch.setattr(snapshots, "coordination_read_connection", unavailable)
    with pytest.raises(OperationalError):
        cards.get_trade_card(engine, "PROD", "1", "S000", raise_on_error=True)


def test_external_updates_inserts_deletes_and_scope_changes_are_immediate(store):
    engine, table, transfers = store
    cards.list_trade_cards(engine, environment="PROD", raise_on_error=True)
    # Another engine represents another process; no local invalidation pulse.
    peer = create_engine(engine.url)
    with peer.begin() as connection:
        connection.execute(table.update().where(table.c.symbol == "S000").values(version=2))
        connection.execute(table.delete().where(table.c.symbol == "S001"))
        connection.execute(table.update().where(table.c.symbol == "S002").values(account_no="2", version=2))
    current = cards.list_trade_cards(engine, environment="PROD", raise_on_error=True)
    assert len(current) == 299
    assert {card.symbol: card.version for card in current}["S000"] == 2
    assert "S001" not in {card.symbol for card in current}
    assert len(transfers[-1]) == 4  # two changed identities and two removals
    scoped = cards.list_trade_cards(engine, environment="PROD", account_no="2", raise_on_error=True)
    assert [card.symbol for card in scoped] == ["S002"]
    with peer.begin() as connection:
        connection.execute(table.update().where(table.c.symbol == "S002").values(account_no="1", version=3))
    assert cards.list_trade_cards(engine, environment="PROD", account_no="2", raise_on_error=True) == []
    assert len(transfers[-1]) == 1 and transfers[-1][0].version is None
    peer.dispose()


def test_rollback_never_enters_snapshot_and_failed_reads_never_use_cached_success(store, monkeypatch):
    engine, table, transfers = store
    cards.list_trade_cards(engine, raise_on_error=True)
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(table.update().where(table.c.symbol == "S000").values(version=2))
        transaction.rollback()
    assert cards.list_trade_cards(engine, raise_on_error=True)[0].version == 1
    assert transfers[-1] == []
    outage = OperationalError("SELECT", {}, RuntimeError("offline"))

    def unavailable(_engine):
        raise outage

    monkeypatch.setattr(snapshots, "coordination_read_connection", unavailable)
    with pytest.raises(OperationalError):
        cards.list_trade_cards(engine, raise_on_error=True)
    assert cards.list_trade_cards(engine) == []


def test_timestamp_check_observes_same_version_replacement_and_reset(store):
    engine, table, transfers = store
    cards.list_trade_cards(engine, raise_on_error=True)
    with engine.begin() as connection:
        connection.execute(table.update().where(table.c.symbol == "S000").values(updated_at=dt.datetime(2026, 10, 8)))
    cards.list_trade_cards(engine, raise_on_error=True)
    assert len(transfers[-1]) == 1
    cards.invalidate_trade_cards_table_cache(engine)
    cards.list_trade_cards(engine, raise_on_error=True)
    assert len(transfers[-1]) == 300


def test_concurrent_readers_share_snapshot_without_sharing_models(store):
    engine, table, transfers = store
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: cards.list_trade_cards(engine, raise_on_error=True), range(12)))
    assert sum(len(rows) for rows in transfers) == 300
    results[0][0].name = "modified"
    assert all(result[0].name != "modified" for result in results[1:])


def test_state_pull_rechecks_controls_and_only_transfers_changed_payloads(store):
    engine, _, transfers = store
    table = state_sync._ensure_state_sync_table(engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(
            state_key="settings", payload=json.dumps({"large": "x" * 47000}),
            revision=1, updated_at=dt.datetime(2026, 10, 7),
        ))
    initial = state_sync.pull_state(engine, "settings")
    initial.state.payload["large"] = "unsaved"
    assert state_sync.pull_state(engine, "settings").state.payload["large"] != "unsaved"
    assert transfers[-1] == []
    with engine.begin() as connection:
        connection.execute(table.update().where(table.c.state_key == "settings").values(payload='{"changed":true}', revision=2))
    assert state_sync.pull_state(engine, "settings").state.payload == {"changed": True}
    assert len(transfers[-1]) == 1
    with engine.begin() as connection:
        connection.execute(table.delete().where(table.c.state_key == "settings"))
    assert state_sync.pull_state(engine, "settings").status == state_sync.PULL_MISSING
    assert len(transfers[-1]) == 1


def test_monthly_egress_budget_includes_changes_and_protocol_allowance(store):
    engine, table, transfers = store
    cards.list_trade_cards(engine, environment="PROD", raise_on_error=True)
    warmup_bytes = sum(len(row.payload.encode()) for row in transfers[-1])
    transfers.clear()
    for version in range(2, 12):
        with engine.begin() as connection:
            connection.execute(table.update().where(table.c.symbol == "S000").values(version=version))
        cards.list_trade_cards(engine, environment="PROD", raise_on_error=True)
        cards.list_trade_cards(engine, environment="PROD", raise_on_error=True)
    changed_bytes = sum(len(row.payload.encode()) for rows in transfers for row in rows if row.payload)
    # Three consumers, one changed card per five seconds during 22 x 6.5h
    # sessions; conservative 2KB protocol/metadata per fresh poll, two polls
    # per change, hourly off-hours polls, daily cold starts, 300MB backups.
    changes_per_month = 22 * 6.5 * 3600 / 5
    projected_bytes = (
        3 * changes_per_month * (changed_bytes / 10 + 2 * 2048)
        + 3 * 31 * warmup_bytes
        + 3 * 31 * 24 * 2048
        + 300 * 1024 * 1024
    )
    assert projected_bytes < 4 * 1_000_000_000


def test_order_lists_keep_fresh_versions_and_existing_ordering(store):
    from src.core.execution_order_record import ExecutionOrderRecord
    from src.core.order_state import OrderIntent, OrderSide
    from src.services import execution_order_repository as orders

    engine, _, transfers = store
    for identity in ("Z-FIRST", "A-SECOND"):
        orders.record_execution_order(engine, ExecutionOrderRecord(
            environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
            intent=OrderIntent.ENTRY, client_order_id=identity, submitted_quantity=10,
        ))
    first = orders.list_execution_orders(engine, environment="PROD")
    assert [record.client_order_id for record in first] == ["Z-FIRST", "A-SECOND"]
    assert len(transfers[-1]) == 2
    assert len(orders.list_execution_orders(engine, environment="PROD")) == 2
    assert transfers[-1] == []
    record = first[0]
    record.remaining_quantity = 5
    orders.save_execution_order(engine, record, expected_version=record.version)
    current = orders.list_execution_orders(engine, environment="PROD")
    assert len(transfers[-1]) == 1
    assert current[0].remaining_quantity == 5 and current[0].version == 2
    newest = orders.list_execution_orders_for_card(engine, environment="PROD", account_no="1", symbol="AAPL")
    assert [record.client_order_id for record in newest] == ["A-SECOND", "Z-FIRST"]
