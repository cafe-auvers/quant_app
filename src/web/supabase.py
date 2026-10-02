from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


class SupabaseError(RuntimeError):
    pass


class SupabaseAccessDenied(SupabaseError):
    pass


class SupabaseConflict(SupabaseError):
    pass


def _api_headers(
    api_key: str,
    *,
    bearer_token: str = "",
    content_type: str = "",
) -> dict[str, str]:
    """Build headers for both modern opaque and legacy JWT API keys."""

    headers = {"apikey": api_key}
    if bearer_token and not bearer_token.startswith(("sb_publishable_", "sb_secret_")):
        headers["Authorization"] = f"Bearer {bearer_token}"
    if content_type:
        headers["Content-Type"] = content_type
    return headers


@dataclass(frozen=True)
class SupabaseSettings:
    url: str
    publishable_key: str
    allowed_user_id: str

    def validate(self) -> None:
        if not self.url.startswith("https://"):
            raise SupabaseError("Supabase URL must use HTTPS")
        if not self.publishable_key or not self.allowed_user_id:
            raise SupabaseError("Supabase publishable key and allowlisted user are required")


class SupabaseIdentityAdapter:
    """Verify access tokens through the supported Auth user endpoint."""

    def __init__(self, settings: SupabaseSettings, *, client: httpx.Client | None = None):
        settings.validate()
        self.settings = settings
        self.client = client or httpx.Client(timeout=8.0)

    def verify_access_token(self, access_token: str) -> dict[str, Any]:
        if not access_token or len(access_token) > 8192:
            raise SupabaseAccessDenied("Missing or malformed access token")
        response = self.client.get(
            f"{self.settings.url.rstrip('/')}/auth/v1/user",
            headers={
                "apikey": self.settings.publishable_key,
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        if response.status_code != 200:
            raise SupabaseAccessDenied("Supabase token verification failed")
        payload = response.json()
        if str(payload.get("id") or "") != self.settings.allowed_user_id:
            raise SupabaseAccessDenied("Supabase user is not allowlisted")
        return payload


class SupabaseChartObjectStore:
    """Private current-object storage; callers supply user or publisher credentials."""

    def __init__(self, url: str, api_key: str, *, bucket: str = "chart-cache", client: httpx.Client | None = None):
        if not url.startswith("https://") or not api_key:
            raise SupabaseError("HTTPS Supabase URL and API key are required")
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.bucket = bucket
        self.client = client or httpx.Client(timeout=20.0)

    @staticmethod
    def object_name(symbol: str, timeframe: str) -> str:
        safe_symbol = "".join(c for c in symbol.upper() if c.isalnum() or c in ".-")
        safe_timeframe = timeframe.upper()
        if not safe_symbol or safe_timeframe not in {"1D", "1H"}:
            raise SupabaseError("Invalid chart object identity")
        return f"current/{safe_symbol}/{safe_timeframe}.json.gz"

    def upload_current(self, symbol: str, timeframe: str, content: bytes, checksum: str) -> dict[str, Any]:
        if hashlib.sha256(content).hexdigest() != checksum:
            raise SupabaseError("Publisher checksum does not match supplied bytes")
        name = quote(self.object_name(symbol, timeframe), safe="/")
        response = self.client.post(
            f"{self.url}/storage/v1/object/{self.bucket}/{name}",
            headers={
                **_api_headers(
                    self.api_key,
                    bearer_token=self.api_key,
                    content_type="application/gzip",
                ),
                "x-upsert": "true",
                "x-content-sha256": checksum,
            },
            content=content,
        )
        if response.status_code not in {200, 201}:
            raise SupabaseError(f"Chart upload failed with HTTP {response.status_code}")
        return response.json()

    def download_current(
        self,
        symbol: str,
        timeframe: str,
        access_token: str,
        *,
        cache_bust: str = "",
    ) -> bytes:
        name = quote(self.object_name(symbol, timeframe), safe="/")
        response = self.client.get(
            f"{self.url}/storage/v1/object/authenticated/{self.bucket}/{name}",
            params={"v": cache_bust} if cache_bust else None,
            headers={
                **_api_headers(self.api_key, bearer_token=access_token),
                "Cache-Control": "no-cache",
            },
        )
        if response.status_code != 200:
            raise SupabaseError(f"Chart download failed with HTTP {response.status_code}")
        return response.content


class SupabaseCloudSnapshotBackend:
    """Service-key publisher backend for the manifest/object protocol.

    The service key belongs only in a server-side publisher configuration. It
    must never be passed to the browser or the ordinary web-process config.
    """

    def __init__(
        self,
        url: str,
        service_key: str,
        *,
        bucket: str = "chart-cache",
        client: httpx.Client | None = None,
    ) -> None:
        if not url.startswith("https://") or not service_key:
            raise SupabaseError("HTTPS Supabase URL and service key are required")
        self.url = url.rstrip("/")
        self.service_key = service_key
        self.client = client or httpx.Client(timeout=20.0)
        self.objects = SupabaseChartObjectStore(
            self.url,
            service_key,
            bucket=bucket,
            client=self.client,
        )

    @property
    def _headers(self) -> dict[str, str]:
        return _api_headers(
            self.service_key,
            bearer_token=self.service_key,
            content_type="application/json",
        )

    def begin_publication(
        self, symbol: str, timeframe: str, metadata: dict[str, Any]
    ) -> int:
        response = self.client.post(
            f"{self.url}/rest/v1/rpc/web_begin_chart_publication",
            headers=self._headers,
            json={
                "p_symbol": symbol,
                "p_timeframe": timeframe,
                "p_object_name": self.objects.object_name(symbol, timeframe),
                "p_source": str(metadata.get("source") or "UNKNOWN"),
                "p_adjustment_mode": str(
                    metadata.get("adjustment_mode") or "UNKNOWN"
                ),
                "p_session_policy": str(
                    metadata.get("session_policy") or "UNKNOWN"
                ),
            },
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"Manifest publication start failed with HTTP {response.status_code}"
            )
        return int(response.json())

    def upload_current(
        self, symbol: str, timeframe: str, content: bytes, checksum: str
    ) -> None:
        self.objects.upload_current(symbol, timeframe, content, checksum)

    def finish_publication(
        self,
        symbol: str,
        timeframe: str,
        revision: int,
        *,
        state: str,
        checksum: str,
        compressed_bytes: int,
    ) -> bool:
        response = self.client.post(
            f"{self.url}/rest/v1/rpc/web_finish_chart_publication",
            headers=self._headers,
            json={
                "p_symbol": symbol,
                "p_timeframe": timeframe,
                "p_revision": revision,
                "p_state": state,
                "p_checksum": checksum,
                "p_compressed_bytes": compressed_bytes,
            },
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"Manifest publication finish failed with HTTP {response.status_code}"
            )
        return bool(response.json())

    def manifest(self, symbol: str, timeframe: str) -> dict[str, Any] | None:
        response = self.client.get(
            f"{self.url}/rest/v1/web_chart_manifests",
            params={
                "symbol": f"eq.{symbol.upper()}",
                "timeframe": f"eq.{timeframe.upper()}",
                "limit": "1",
            },
            headers=self._headers,
        )
        if response.status_code != 200:
            raise SupabaseError(f"Manifest read failed with HTTP {response.status_code}")
        rows = response.json()
        return rows[0] if rows else None

    def download_current(
        self, symbol: str, timeframe: str, *, cache_bust: str
    ) -> bytes:
        return self.objects.download_current(
            symbol,
            timeframe,
            self.service_key,
            cache_bust=cache_bust,
        )


class SupabaseDrawingAdapter:
    """Caller-scoped ordinary drawing writes with optimistic revisions."""

    def __init__(self, settings: SupabaseSettings, *, client: httpx.Client | None = None):
        settings.validate()
        self.settings = settings
        self.client = client or httpx.Client(timeout=8.0)

    def _headers(self, access_token: str, *, return_rows: bool = False) -> dict[str, str]:
        headers = {
            "apikey": self.settings.publishable_key,
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        if return_rows:
            headers["Prefer"] = "return=representation"
        return headers

    def list_symbol(self, symbol: str, access_token: str) -> list[dict[str, Any]]:
        response = self.client.get(
            f"{self.settings.url}/rest/v1/web_drawings",
            params={"symbol": f"eq.{symbol.upper()}", "order": "updated_at.asc"},
            headers=self._headers(access_token),
        )
        if response.status_code != 200:
            raise SupabaseError(f"Drawing read failed with HTTP {response.status_code}")
        return list(response.json())

    def update(self, drawing_id: str, expected_revision: int, values: dict[str, Any], access_token: str) -> dict[str, Any]:
        payload = dict(values)
        payload["revision"] = expected_revision + 1
        response = self.client.patch(
            f"{self.settings.url}/rest/v1/web_drawings",
            params={
                "drawing_id": f"eq.{drawing_id}",
                "revision": f"eq.{expected_revision}",
                "deleted": "eq.false",
            },
            headers=self._headers(access_token, return_rows=True),
            json=payload,
        )
        if response.status_code not in {200, 204}:
            raise SupabaseError(f"Drawing update failed with HTTP {response.status_code}")
        rows = response.json() if response.content else []
        if len(rows) != 1:
            raise SupabaseConflict("Stale drawing revision or deleted drawing")
        return rows[0]
