"""Fail-closed pagination tests for broker position discovery."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.api import kis_account_snapshot_dual as kis_snapshot
from src.core.order_state import BrokerOrderDiscoveryResult
from src.services.handoff_reconciliation import run_post_claim_broker_reconciliation


def _client(*, exchanges=("NASD",)):
    config = SimpleNamespace(
        cano="12345678",
        account_product_code="01",
        domestic_balance_tr_id="TTTC8434R",
        overseas_balance_tr_id="TTTS3012R",
        overseas_exchanges=exchanges,
        overseas_currency="USD",
    )
    return kis_snapshot.KisAccountClient(config)


def _domestic_row(symbol: str, quantity: int = 1) -> dict:
    return {"pdno": symbol, "hldg_qty": str(quantity), "pchs_avg_pric": "100"}


def _overseas_row(symbol: str, quantity: int = 1) -> dict:
    return {
        "ovrs_pdno": symbol,
        "ovrs_cblc_qty": str(quantity),
        "pchs_avg_pric": "100",
    }


def test_domestic_balance_continuation_fetches_all_pages(monkeypatch):
    client = _client()
    responses = [
        (
            {
                "output1": [_domestic_row("005930")],
                "output2": [{}],
                "ctx_area_fk100": "next-fk",
                "ctx_area_nk100": "next-nk",
            },
            {"tr_cont": "F"},
        ),
        ({"output1": [_domestic_row("000660")], "output2": [{}]}, {}),
    ]
    calls = []

    def fake_get(endpoint, tr_id, params, tr_cont=""):
        calls.append((dict(params), tr_cont))
        return responses.pop(0)

    monkeypatch.setattr(client, "_get_with_headers", fake_get)
    monkeypatch.setattr(kis_snapshot.time, "sleep", lambda _seconds: None)

    result = client.get_domestic_balance()

    assert [holding["symbol"] for holding in result["holdings"]] == [
        "005930",
        "000660",
    ]
    assert calls[1][0]["CTX_AREA_FK100"] == "next-fk"
    assert calls[1][0]["CTX_AREA_NK100"] == "next-nk"
    assert calls[1][1] == "N"


def test_domestic_continuation_without_cursor_fails_closed(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client,
        "_get_with_headers",
        lambda *args, **kwargs: ({"output1": []}, {"tr_cont": "F"}),
    )

    with pytest.raises(kis_snapshot.KisApiError, match="without a cursor"):
        client.get_domestic_balance()


def test_overseas_continuation_without_cursor_fails_closed(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client,
        "_get_with_headers",
        lambda *args, **kwargs: ({"output1": []}, {"tr_cont": "M"}),
    )

    with pytest.raises(kis_snapshot.KisApiError, match="without a cursor"):
        client.get_overseas_balance()


def test_overseas_maximum_page_exhaustion_fails_closed(monkeypatch):
    client = _client()
    calls = []

    def fake_get(endpoint, tr_id, params, tr_cont=""):
        calls.append((dict(params), tr_cont))
        page_number = len(calls)
        return {
            "output1": [],
            "ctx_area_fk200": f"next-{page_number}",
            "ctx_area_nk200": f"next-{page_number}",
        }, {"tr_cont": "F"}

    monkeypatch.setattr(client, "_get_with_headers", fake_get)
    monkeypatch.setattr(kis_snapshot, "MAX_BALANCE_PAGES", 2)
    monkeypatch.setattr(kis_snapshot.time, "sleep", lambda _seconds: None)

    with pytest.raises(kis_snapshot.KisApiError, match="more than 2 pages"):
        client.get_overseas_balance()

    assert len(calls) == 2


def test_holding_only_on_page_two_blocks_handoff(monkeypatch):
    client = _client()
    responses = [
        (
            {
                "output1": [],
                "ctx_area_fk200": "next-fk",
                "ctx_area_nk200": "next-nk",
            },
            {"tr_cont": "F"},
        ),
        ({"output1": [_overseas_row("MSFT", 7)]}, {}),
    ]
    monkeypatch.setattr(
        client,
        "_get_with_headers",
        lambda *args, **kwargs: responses.pop(0),
    )
    monkeypatch.setattr(client, "_get", lambda *args, **kwargs: {"output2": []})
    monkeypatch.setattr(kis_snapshot.time, "sleep", lambda _seconds: None)
    positions = {
        "domestic": {"holdings": []},
        "overseas": client.get_overseas_balance(),
    }

    class Broker:
        def discover_orders(self, **kwargs):
            return BrokerOrderDiscoveryResult(
                open_orders_complete=True,
                history_complete=True,
                reserved_orders_complete=True,
            )

        def get_positions(self, **kwargs):
            return positions

    result = run_post_claim_broker_reconciliation(
        SimpleNamespace(items=[]),
        broker=Broker(),
        configured_account_numbers=["12345678-01"],
        event_recorder=lambda *args, **kwargs: None,
    )

    assert result.ok is False
    assert result.blocked_symbols == ["MSFT"]

