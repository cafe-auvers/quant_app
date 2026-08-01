"""Regression tests for KIS token-cache safety and refresh serialization."""
from __future__ import annotations

import json
import threading
import time

import src.api.kis_account_snapshot_dual as kis_snapshot


def _config(cache_path):
    return kis_snapshot.KisConfig(
        environment=kis_snapshot.KisEnvironment.PROD,
        app_key="test-app-key",
        app_secret="test-app-secret",
        cano="12345678",
        account_product_code="01",
        base_url="https://kis.example",
        token_cache_path=cache_path,
        overseas_exchanges=("NASD",),
        overseas_currency="USD",
    )


def test_token_cache_write_is_atomic_and_applies_permission_restriction(tmp_path, monkeypatch):
    cache_path = tmp_path / "token.json"
    restricted_paths = []
    monkeypatch.setattr(
        kis_snapshot,
        "_restrict_file_to_current_user",
        lambda path: restricted_paths.append(path),
    )
    client = kis_snapshot.KisAccountClient(_config(cache_path))

    client._save_cached_token("secret-token", expires_in=3600)

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["access_token"] == "secret-token"
    assert cache_path in restricted_paths
    assert any(path.suffix == ".tmp" for path in restricted_paths)
    assert list(tmp_path.glob("*.tmp")) == []


def test_concurrent_cache_miss_requests_only_one_kis_token(tmp_path, monkeypatch):
    cache_path = tmp_path / "token.json"
    request_count = 0
    request_count_lock = threading.Lock()

    class _Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"access_token": "shared-token", "expires_in": 3600}

    def fake_request(_self, *_args, **_kwargs):
        nonlocal request_count
        with request_count_lock:
            request_count += 1
        # Give the other worker time to reach the lock while the first owns it.
        time.sleep(0.05)
        return _Response()

    monkeypatch.setattr(
        kis_snapshot.KisAccountClient,
        "_request_with_network_retry",
        fake_request,
    )
    monkeypatch.setattr(kis_snapshot, "_restrict_file_to_current_user", lambda _path: None)

    clients = [
        kis_snapshot.KisAccountClient(_config(cache_path)),
        kis_snapshot.KisAccountClient(_config(cache_path)),
    ]
    tokens = []
    errors = []

    def authenticate(client):
        try:
            tokens.append(client.authenticate())
        except Exception as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    workers = [threading.Thread(target=authenticate, args=(client,)) for client in clients]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert not errors
    assert tokens == ["shared-token", "shared-token"]
    assert request_count == 1
    assert json.loads(cache_path.read_text(encoding="utf-8"))["access_token"] == "shared-token"


def test_concurrent_forced_refresh_on_fresh_clients_requests_one_token(
    tmp_path, monkeypatch
):
    """A forced refresh must still share a token generated while waiting."""
    cache_path = tmp_path / "token.json"
    request_count = 0
    request_count_lock = threading.Lock()

    class _Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"access_token": "forced-shared-token", "expires_in": 3600}

    def fake_request(_self, *_args, **_kwargs):
        nonlocal request_count
        with request_count_lock:
            request_count += 1
        time.sleep(0.05)
        return _Response()

    monkeypatch.setattr(
        kis_snapshot.KisAccountClient,
        "_request_with_network_retry",
        fake_request,
    )
    monkeypatch.setattr(kis_snapshot, "_restrict_file_to_current_user", lambda _path: None)

    clients = [
        kis_snapshot.KisAccountClient(_config(cache_path)),
        kis_snapshot.KisAccountClient(_config(cache_path)),
    ]
    tokens = []
    errors = []

    def authenticate(client):
        try:
            tokens.append(client.authenticate(force_refresh=True))
        except Exception as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    workers = [threading.Thread(target=authenticate, args=(client,)) for client in clients]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert not errors
    assert tokens == ["forced-shared-token", "forced-shared-token"]
    assert request_count == 1
