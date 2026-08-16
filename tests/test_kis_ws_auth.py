from __future__ import annotations

import datetime as dt

import pytest

from src.api.kis_ws_auth import (
    KisWsApprovalKeyProvider,
    KisWsAuthError,
    KisWsProtocolNotVerifiedError,
)


class _Response:
    def __init__(self, payload=None, error=None):
        self._payload = payload or {}
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _provider(session, **kwargs):
    now = dt.datetime(2026, 8, 16, 1, 0, tzinfo=dt.timezone.utc)
    return KisWsApprovalKeyProvider(
        base_url="https://openapi.example",
        app_key="app",
        app_secret="secret",
        ttl_seconds=3600,
        protocol_verified=True,
        session=session,
        clock=lambda: now,
        sleeper=lambda seconds: None,
        **kwargs,
    )


def test_approval_key_is_cached_until_refresh_margin():
    session = _Session([_Response({"approval_key": "APPROVAL"})])
    provider = _provider(session)

    assert provider.get().value == "APPROVAL"
    assert provider.get().value == "APPROVAL"
    assert len(session.calls) == 1
    assert session.calls[0][0].endswith("/oauth2/Approval")
    assert session.calls[0][1]["json"]["grant_type"] == "client_credentials"


def test_unverified_protocol_blocks_before_network_call():
    session = _Session([_Response({"approval_key": "MUST_NOT_BE_USED"})])
    provider = KisWsApprovalKeyProvider(
        base_url="https://openapi.example",
        app_key="app",
        app_secret="secret",
        ttl_seconds=3600,
        protocol_verified=False,
        session=session,
    )

    with pytest.raises(KisWsProtocolNotVerifiedError):
        provider.get()
    assert session.calls == []


def test_auth_failure_is_bounded_and_emits_critical_alert():
    session = _Session([RuntimeError("down"), RuntimeError("down")])
    alerts = []
    provider = _provider(session, max_retries=2, critical_alert=alerts.append)

    with pytest.raises(KisWsAuthError, match="bounded retry budget"):
        provider.get()
    assert len(session.calls) == 2
    assert len(alerts) == 1
