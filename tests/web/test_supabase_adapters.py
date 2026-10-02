from __future__ import annotations

import gzip
import hashlib
import json

import httpx
import pytest

from src.web.cloud_publication import (
    CloudPublicationError,
    CloudSnapshotPublisher,
    CloudSnapshotReader,
    CloudSnapshotUpdating,
)
from src.web.supabase import (
    SupabaseAccessDenied,
    SupabaseCloudSnapshotBackend,
    SupabaseConflict,
    SupabaseDrawingAdapter,
    SupabaseIdentityAdapter,
    SupabaseSettings,
)


def settings():
    return SupabaseSettings(
        url="https://example.supabase.co",
        publishable_key="publishable-test-key",
        allowed_user_id="11111111-1111-1111-1111-111111111111",
    )


def test_identity_adapter_verifies_with_auth_endpoint_and_allowlist():
    def handler(request):
        assert request.url.path == "/auth/v1/user"
        assert request.headers["authorization"] == "Bearer real-token"
        return httpx.Response(200, json={"id": settings().allowed_user_id})

    adapter = SupabaseIdentityAdapter(
        settings(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert adapter.verify_access_token("real-token")["id"] == settings().allowed_user_id


def test_identity_adapter_denies_wrong_user_without_decoding_jwt_locally():
    adapter = SupabaseIdentityAdapter(
        settings(),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"id": "wrong-user"})
            )
        ),
    )
    with pytest.raises(SupabaseAccessDenied, match="not allowlisted"):
        adapter.verify_access_token("opaque-token")


def test_drawing_adapter_turns_zero_row_cas_into_conflict():
    adapter = SupabaseDrawingAdapter(
        settings(),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=[])
            )
        ),
    )
    with pytest.raises(SupabaseConflict, match="Stale"):
        adapter.update("drawing-id", 3, {"end_price": 10}, "access-token")


def test_modern_secret_key_is_not_sent_as_a_bearer_token():
    def handler(request):
        assert request.headers["apikey"] == "sb_secret_test"
        assert "authorization" not in request.headers
        return httpx.Response(200, json=[])

    backend = SupabaseCloudSnapshotBackend(
        "https://example.supabase.co",
        "sb_secret_test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert backend.manifest("AAPL", "1D") is None


def test_legacy_service_role_key_keeps_bearer_compatibility():
    def handler(request):
        assert request.headers["apikey"] == "legacy-jwt-key"
        assert request.headers["authorization"] == "Bearer legacy-jwt-key"
        return httpx.Response(200, json=[])

    backend = SupabaseCloudSnapshotBackend(
        "https://example.supabase.co",
        "legacy-jwt-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert backend.manifest("AAPL", "1D") is None


class FakeCloudBackend:
    def __init__(self):
        self.current = b""
        self.stale_reads: list[bytes] = []
        self.row = None
        self.fail_upload = False

    def begin_publication(self, symbol, timeframe, metadata):
        revision = int((self.row or {}).get("revision", 0)) + 1
        self.row = {
            "symbol": symbol,
            "timeframe": timeframe,
            "revision": revision,
            "publication_state": "UPDATING",
            "checksum_sha256": "0" * 64,
            **metadata,
        }
        return revision

    def upload_current(self, symbol, timeframe, content, checksum):
        if self.fail_upload:
            raise RuntimeError("interrupted upload")
        assert hashlib.sha256(content).hexdigest() == checksum
        self.current = content

    def finish_publication(
        self, symbol, timeframe, revision, *, state, checksum, compressed_bytes
    ):
        if self.row["revision"] != revision or self.row["publication_state"] != "UPDATING":
            return False
        self.row.update(
            publication_state=state,
            checksum_sha256=checksum,
            compressed_bytes=compressed_bytes,
        )
        return True

    def manifest(self, symbol, timeframe):
        return dict(self.row) if self.row else None

    def download_current(self, symbol, timeframe, *, cache_bust):
        return self.stale_reads.pop(0) if self.stale_reads else self.current


def cloud_bytes(value):
    return gzip.compress(json.dumps(value).encode(), mtime=0)


def test_cloud_reader_retries_stale_overwrite_until_checksum_matches():
    backend = FakeCloudBackend()
    publisher = CloudSnapshotPublisher(backend)
    old = cloud_bytes({"symbol": "OLD"})
    backend.current = old
    new = cloud_bytes({"symbol": "AAPL"})
    backend.stale_reads = [old, old]
    publisher.publish("AAPL", "1D", new, {"source": "TEST"})
    backend.stale_reads = [old]
    assert CloudSnapshotReader(backend).read("AAPL", "1D")["symbol"] == "AAPL"


def test_interrupted_publication_never_becomes_ready():
    backend = FakeCloudBackend()
    backend.fail_upload = True
    with pytest.raises(RuntimeError, match="interrupted"):
        CloudSnapshotPublisher(backend).publish(
            "AAPL", "1D", cloud_bytes({"symbol": "AAPL"}), {"source": "TEST"}
        )
    assert backend.row["publication_state"] == "FAILED"
    with pytest.raises(CloudPublicationError, match="not READY"):
        CloudSnapshotReader(backend).read("AAPL", "1D")


def test_manifest_change_without_fresh_object_stays_updating():
    backend = FakeCloudBackend()
    content = cloud_bytes({"symbol": "AAPL"})
    backend.row = {
        "revision": 4,
        "publication_state": "READY",
        "checksum_sha256": hashlib.sha256(content).hexdigest(),
    }
    backend.current = cloud_bytes({"symbol": "STALE"})
    with pytest.raises(CloudSnapshotUpdating, match="stale bytes"):
        CloudSnapshotReader(backend, freshness_attempts=2).read("AAPL", "1D")
