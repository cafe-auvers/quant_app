import datetime as dt
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from src.core.execution_order_record import ExecutionOrderStatus
from src.core.order_state import OrderIntent, OrderSide
from src.core.trade_journal import fill_facts
from src.services.daily_journal import (
    approved_daily_publication, confirm_daily_journal, load_daily_journal,
    prepare_daily_journal_packet, save_daily_journal_draft,
)
from src.services.trade_journal import TradeJournalStore
from src.core.journal_charts import snapshot_bars

DAY = "2026-10-08"


@pytest.fixture
def day_store(tmp_path):
    store = TradeJournalStore(tmp_path / "journal")
    now = dt.datetime(2026, 10, 8, 20, 20, tzinfo=dt.timezone.utc)
    for identity, symbol, session, side in (
        ("buy", "BOUGHT", DAY, OrderSide.BUY),
        ("sell", "SOLD_ONLY", DAY, OrderSide.SELL),
        ("yesterday", "YESTERDAY", "2026-10-07", OrderSide.BUY),
    ):
        order = SimpleNamespace(
            environment="PROD", account_no="private-account", client_order_id=identity,
            symbol=symbol, side=side, intent=OrderIntent.ENTRY,
            filled_quantity=10, average_fill_price=100, market_session_date=session,
            submission_started_at=f"{session}T13:36:00+00:00", prepared_at=None,
            last_broker_seen_at=f"{session}T13:38:00+00:00", last_reconciled_at=None,
            status=ExecutionOrderStatus.FILLED,
        )
        facts = fill_facts(order, now)
        store.record_fill(facts, {"provenance": "order-linked snapshot", "fields": {"entry_orb_window": "5m"}}, now)
        if identity == "buy":
            frame = pd.DataFrame({"Open": [99], "High": [101], "Low": [98], "Close": [100], "Volume": [1000]},
                                 index=pd.to_datetime(["2026-10-08T13:30:00Z"]))
            trade = next(row for row in store.list_trades() if row["journal_id"] == facts["journal_id"])
            for interval in ("1d", "5m"):
                store.save_snapshot(trade, {
                    "stage": "end_of_day", "interval": interval,
                    "bars": snapshot_bars(frame, dt.date.fromisoformat(DAY), interval, now),
                    "source": "synthetic test", "capture_basis": "end-of-day review",
                    "captured_at": now.isoformat(), "bars_sha256": f"test-{interval}",
                    "fill_price_at_capture": 100,
                })
    return store


def test_day_packet_has_bought_stocks_charts_without_creating_a_journal(day_store):
    path = prepare_daily_journal_packet(day_store, DAY)
    evidence = json.loads((path / "trading_day.json").read_text(encoding="utf-8"))
    assert [row["symbol"] for row in evidence["buys"]] == ["BOUGHT"]
    assert [row["symbol"] for row in evidence["sells"]] == ["SOLD_ONLY"]
    assert {row["interval"] for row in evidence["charts"]} == {"1d", "5m"}
    assert all(row["symbol"] == "BOUGHT" for row in evidence["charts"])
    assert len(list(path.glob("chart-*"))) == 2
    assert load_daily_journal(day_store, DAY) is None
    assert "few adjectives" in (path / "writing_instructions.txt").read_text()


def test_saved_chat_draft_attaches_both_graphs_and_requires_confirmation(day_store):
    entry = save_daily_journal_draft(day_store, DAY, comments="waited then bought", draft="I waited for the entry, then bought BOUGHT.")
    assert entry["state"] == "AWAITING_CONFIRMATION"
    assert entry["approval"] is None
    saved = day_store.root / "days" / DAY
    assert "Daily" in (saved / "draft.md").read_text(encoding="utf-8")
    assert "5-minute" in (saved / "draft.md").read_text(encoding="utf-8")
    assert (saved / "journal.html").read_text(encoding="utf-8").count("<svg") == 2
    with pytest.raises(ValueError, match="not confirmed"):
        approved_daily_publication(day_store, DAY)


def test_confirmation_applies_to_exact_revision_and_destinations(day_store):
    entry = save_daily_journal_draft(day_store, DAY, comments="waited", draft="I waited today.")
    with pytest.raises(ValueError, match="destinations"):
        confirm_daily_journal(day_store, DAY, revision_id=entry["revision_id"], confirmation="Post this", destinations=[])
    confirmed = confirm_daily_journal(day_store, DAY, revision_id=entry["revision_id"], confirmation="Post this version on X", destinations=["X"])
    assert confirmed["state"] == "APPROVED"
    result = approved_daily_publication(day_store, DAY)
    assert result["draft"] == "I waited today."
    assert result["destinations"] == ["X"]


def test_editing_entry_clears_earlier_approval_and_preserves_revisions(day_store):
    first = save_daily_journal_draft(day_store, DAY, comments="waited", draft="I waited today.")
    confirm_daily_journal(day_store, DAY, revision_id=first["revision_id"], confirmation="Post it on X", destinations=["X"])
    second = save_daily_journal_draft(day_store, DAY, comments="waited then bought", draft="I waited, then bought.")
    assert second["state"] == "AWAITING_CONFIRMATION"
    assert second["approval"] is None
    assert len(list((day_store.root / "days" / DAY / "revisions").glob("*.json"))) == 2
    with pytest.raises(ValueError, match="changed"):
        confirm_daily_journal(day_store, DAY, revision_id=first["revision_id"], confirmation="Post it", destinations=["X"])


def test_updated_trade_facts_block_publication_of_previous_approval(day_store):
    first = save_daily_journal_draft(day_store, DAY, comments="bought", draft="I bought BOUGHT.")
    confirm_daily_journal(day_store, DAY, revision_id=first["revision_id"], confirmation="Post this on X", destinations=["X"])
    trade = next(row for row in day_store.list_trades() if row["symbol"] == "BOUGHT")
    trade["quantity"] = 20
    with day_store.connect() as conn:
        conn.execute("UPDATE trades SET payload=? WHERE journal_id=?", (json.dumps(trade), trade["journal_id"]))
    with pytest.raises(ValueError, match="charts changed"):
        approved_daily_publication(day_store, DAY)


def test_no_trade_day_can_have_a_journal_without_inventing_purchases(day_store):
    entry = save_daily_journal_draft(day_store, "2026-10-06", comments="I stayed out", draft="I stayed out today.")
    assert entry["evidence"]["buys"] == []
    assert entry["evidence"]["charts"] == []
    assert "I stayed out today." in (day_store.root / "days" / "2026-10-06" / "journal.html").read_text()
