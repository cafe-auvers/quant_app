"""Read-only KIS retry classification and backoff tests."""

from types import SimpleNamespace

import pytest

from src.api import kis_account_snapshot_dual as kis_snapshot


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = {}
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def _client() -> kis_snapshot.KisAccountClient:
    config = SimpleNamespace(
        base_url="https://kis.example",
        cano="12345678",
        account_product_code="01",
        app_key="app-key",
        app_secret="app-secret",
    )
    client = kis_snapshot.KisAccountClient(config)
    client.access_token = "access-token"
    return client


def test_gateway_routing_error_is_classified_as_transient() -> None:
    response = _Response(
        500,
        {
            "rt_cd": "1",
            "msg_cd": "EGW00300",
            "msg1": "Gateway routing error",
        },
    )

    with pytest.raises(kis_snapshot.KisTransientApiError, match="EGW00300"):
        kis_snapshot.KisAccountClient._parse_response(
            response,
            endpoint="/uapi/domestic-stock/v1/trading/inquire-balance",
        )


def test_read_retries_gateway_routing_error_then_returns_success(monkeypatch) -> None:
    client = _client()
    responses = [
        _Response(
            500,
            {
                "rt_cd": "1",
                "msg_cd": "EGW00300",
                "msg1": "Gateway routing error",
            },
        ),
        _Response(200, {"rt_cd": "0", "output1": []}),
    ]
    calls = []
    sleeps = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(client, "_request_with_network_retry", request)
    monkeypatch.setattr(kis_snapshot.time, "sleep", sleeps.append)

    data, _headers = client._get_with_headers_inner(
        "/uapi/domestic-stock/v1/trading/inquire-balance",
        tr_id="TTTC8434R",
        params={},
    )

    assert data == {"rt_cd": "0", "output1": []}
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_non_transient_client_error_is_not_retried(monkeypatch) -> None:
    client = _client()
    response = _Response(
        400,
        {"rt_cd": "1", "msg_cd": "BAD_REQUEST", "msg1": "invalid request"},
    )
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return response

    monkeypatch.setattr(client, "_request_with_network_retry", request)

    with pytest.raises(kis_snapshot.KisApiError, match="BAD_REQUEST"):
        client._get_with_headers_inner("/balance", tr_id="TEST", params={})

    assert len(calls) == 1
