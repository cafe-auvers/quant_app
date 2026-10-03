from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import anyio
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    AuthenticationError,
    LocalAuthService,
    LoginRateLimited,
)
from .cache import ChartBundleCache, ChartLoadCoordinator
from .canonical_planning import (
    CanonicalPlanningSource,
    CanonicalPlanningUnavailable,
    build_canonical_planning_source,
)
from .config import WebConfig, ensure_runtime_directories, load_web_config
from .connected_planning import (
    ConnectedPlanningService,
    ConnectedPlanningUnavailable,
)
from .connected_operator import (
    ConnectedOperatorService,
    ConnectedOperatorUnavailable,
)
from .market_data import MarketDataUnavailable, MarketDataSource, build_market_data_source
from .market_status import nyse_market_status
from .live_updates import LiveUpdateHub
from .store import ConflictError, SessionRecord, ValidationError, WebStore
from .watchlist_history import WatchlistHistorySource, merge_watchlist_history


class OriginHostMiddleware:
    """Validate Host for every request and Origin for mutations/WebSockets."""

    def __init__(self, app: ASGIApp, *, hosts: set[str], origins: set[str]):
        self.app = app
        self.hosts = {value.lower() for value in hosts}
        self.origins = {value.rstrip("/") for value in origins}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin1").lower(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        raw_host = headers.get("host", "").strip()
        if raw_host.startswith("[") and "]" in raw_host:
            host = raw_host[1 : raw_host.index("]")].lower()
        elif raw_host.count(":") == 1:
            host = raw_host.rsplit(":", 1)[0].lower()
        else:
            host = raw_host.lower()
        origin = headers.get("origin", "").rstrip("/")
        method = str(scope.get("method", "GET")).upper()
        host_ok = host in self.hosts
        origin_required = scope["type"] == "websocket" or method not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }
        origin_ok = not origin_required or origin in self.origins
        if host_ok and origin_ok:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(
            {"detail": "Untrusted Host or Origin"}, status_code=400
        )
        await response(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                            b"style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                            b"font-src 'self' data:; connect-src 'self' ws: wss:; "
                            b"object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
                        ),
                    ]
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


@dataclass
class WebServices:
    config: WebConfig
    store: WebStore
    auth: LocalAuthService
    market: MarketDataSource
    cache: ChartBundleCache
    charts: ChartLoadCoordinator
    watchlist_history: WatchlistHistorySource
    canonical_planning: CanonicalPlanningSource
    connected_planning: ConnectedPlanningService
    connected_operator: ConnectedOperatorService
    live_updates: LiveUpdateHub


def build_services(config: WebConfig) -> WebServices:
    ensure_runtime_directories(config)
    store = WebStore(config.database_path)
    scanner_setups_path = (
        config.resolved_pc_repository / "data" / "scanner_setups.json"
        if config.resolved_pc_repository is not None
        else None
    )
    market = build_market_data_source(
        config.resolved_local_mirror_path,
        scanner_setups_path=scanner_setups_path,
    )
    cache = ChartBundleCache(
        config.cache_dir, max_symbols=config.chart_cache_max_symbols
    )
    canonical_planning = build_canonical_planning_source(config)
    connected_planning = ConnectedPlanningService.from_config(
        config, canonical_planning
    )
    return WebServices(
        config=config,
        store=store,
        auth=LocalAuthService(store, session_hours=config.session_hours),
        market=market,
        cache=cache,
        charts=ChartLoadCoordinator(
            market,
            cache,
            daily_bars=config.daily_bars,
            hourly_months=config.hourly_months,
            pinned_symbols=store.pinned_symbols,
        ),
        watchlist_history=WatchlistHistorySource.from_config(config),
        canonical_planning=canonical_planning,
        connected_planning=connected_planning,
        connected_operator=ConnectedOperatorService(
            config, canonical_planning, connected_planning
        ),
        live_updates=LiveUpdateHub(),
    )


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class PlanningCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=32, max_length=40)
    operation: str = Field(min_length=3, max_length=40)
    expected_revision: int = Field(ge=0)
    breakout_price: float | None = Field(default=None, gt=0)


class DraftPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class BuyTodayOperatorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=32, max_length=40)
    expected_revision: int = Field(ge=1)
    enabled: bool


class BoardOperatorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=32, max_length=40)
    action: str = Field(min_length=3, max_length=40)
    symbol: str = Field(min_length=1, max_length=20)
    expected_revision: int = Field(ge=1)
    quantity: int | None = Field(default=None, ge=1)
    price: float | None = Field(default=None, gt=0)
    target_priority: int | None = None


class PublishTodayPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=32, max_length=40)


class OperatorControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(pattern="^(?i:pc|laptop|mobile)$")


class OrbSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    command_id: str = Field(min_length=32, max_length=40)
    expected_revision: int = Field(ge=0)
    capital_min_percent: float = Field(ge=0, le=100)
    capital_ideal_percent: float = Field(ge=0, le=100)
    capital_max_percent: float = Field(ge=0, le=100)
    stop_adr_min_percent: float = Field(ge=0, le=1000)
    stop_adr_ideal_percent: float = Field(ge=0, le=1000)
    stop_adr_max_percent: float = Field(ge=0, le=1000)


class DrawingCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=120)
    symbol: str = Field(min_length=1, max_length=20)
    start_date: str = Field(min_length=1, max_length=40)
    start_price: float = Field(gt=0)
    end_date: str = Field(min_length=1, max_length=40)
    end_price: float = Field(gt=0)
    timeframe: str = Field(pattern="^(1D|1H)$")


class DrawingUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    start_date: str = Field(min_length=1, max_length=40)
    start_price: float = Field(gt=0)
    end_date: str = Field(min_length=1, max_length=40)
    end_price: float = Field(gt=0)
    timeframe: str = Field(pattern="^(1D|1H)$")


class DrawingDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


def register_api_routes(app: FastAPI, services: WebServices) -> None:
    config = services.config

    def pin_rows(rows: list[dict[str, Any]]) -> None:
        services.charts.pin_symbols(
            [str(row.get("symbol") or "") for row in rows]
        )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        OriginHostMiddleware,
        hosts=set(config.trusted_hosts),
        origins=set(config.trusted_origins),
    )

    @app.exception_handler(ConflictError)
    async def conflict_handler(_request: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc), "current": exc.current, "state": "CONFLICT"},
            status_code=409,
        )

    @app.exception_handler(ValidationError)
    async def validation_handler(_request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(MarketDataUnavailable)
    async def market_handler(
        _request: Request, exc: MarketDataUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc), "state": "UNAVAILABLE"}, status_code=503
        )

    @app.exception_handler(ConnectedPlanningUnavailable)
    async def connected_planning_handler(
        _request: Request, exc: ConnectedPlanningUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc), "state": "UNAVAILABLE"}, status_code=503
        )

    @app.exception_handler(ConnectedOperatorUnavailable)
    async def connected_operator_handler(
        _request: Request, exc: ConnectedOperatorUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc), "state": "OPERATOR_UNAVAILABLE"},
            status_code=503,
        )

    def current_session(request: Request) -> SessionRecord:
        session = services.auth.session(request.cookies.get(SESSION_COOKIE))
        if session is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return session

    def csrf_session(
        request: Request,
        session: SessionRecord = Depends(current_session),
        x_csrf_token: str | None = Header(default=None),
    ) -> SessionRecord:
        try:
            services.auth.require_csrf(session, x_csrf_token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return session

    def announce_planning_change(
        kind: str,
        symbol: str = "",
        result: dict[str, Any] | None = None,
    ) -> None:
        card = (result or {}).get("card") or {}
        services.live_updates.publish(
            {
                "kind": kind,
                "symbol": str(symbol or card.get("symbol") or "").strip().upper(),
                "revision": card.get("version"),
            }
        )

    async def apply_watchlist_rollover(
        market_status: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        market = market_status or nyse_market_status()
        session_date = str(market["watchlist_session_date"])
        if config.mode != "SANDBOX":
            return market, {
                "rolled_over": False,
                "initialized": False,
                "previous_session_date": None,
                "session_date": session_date,
                "archived_symbols": 0,
            }
        rollover = await anyio.to_thread.run_sync(
            services.store.rollover_watchlist_session, session_date
        )
        return market, rollover

    async def canonical_connectivity() -> dict[str, Any]:
        if config.mode != "CONNECTED":
            return {
                "state": "DISABLED",
                "reason": "Sandbox mode",
                "revision": None,
                "executor": "UNKNOWN",
            }
        return await anyio.to_thread.run_sync(
            services.canonical_planning.connectivity
        )

    async def market_connectivity() -> dict[str, Any]:
        health = getattr(services.market, "health", None)
        if not callable(health):
            return {"state": "UNKNOWN", "source": config.data_mode}
        return await anyio.to_thread.run_sync(health)

    @app.get("/api/v1/auth/csrf", include_in_schema=False)
    async def login_csrf(response: Response) -> dict[str, str]:
        token = secrets.token_urlsafe(32)
        response.set_cookie(
            LOGIN_CSRF_COOKIE,
            token,
            httponly=False,
            secure=config.secure_cookie,
            samesite="strict",
            max_age=600,
            path="/",
        )
        return {"csrf_token": token}

    @app.post("/api/v1/auth/login", include_in_schema=False)
    async def login(
        payload: LoginRequest,
        request: Request,
        response: Response,
        x_csrf_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE)
        if (
            not cookie_token
            or not x_csrf_token
            or not secrets.compare_digest(cookie_token, x_csrf_token)
        ):
            raise HTTPException(status_code=403, detail="Invalid login CSRF token")
        remote_key = request.client.host if request.client else "unknown"
        try:
            issued = await anyio.to_thread.run_sync(
                lambda: services.auth.authenticate(
                    payload.username, payload.password, remote_key=remote_key
                )
            )
        except LoginRateLimited as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        response.set_cookie(
            SESSION_COOKIE,
            issued.token,
            httponly=True,
            secure=config.secure_cookie,
            samesite="strict",
            max_age=config.session_hours * 3600,
            path="/",
        )
        response.delete_cookie(LOGIN_CSRF_COOKIE, path="/")
        return {
            "authenticated": True,
            "username": payload.username.strip().lower(),
            "expires_at": issued.expires_at,
            "csrf_token": issued.csrf_token,
        }

    @app.get("/api/v1/session", include_in_schema=False)
    async def session(session: SessionRecord = Depends(current_session)) -> dict[str, Any]:
        market, rollover = await apply_watchlist_rollover()
        canonical = await canonical_connectivity()
        draft_revision = await anyio.to_thread.run_sync(
            lambda: services.store.buy_today_draft_revision(
                shared=config.mode != "SANDBOX"
            )
        )
        write_capabilities = services.connected_planning.capabilities()
        operator_capabilities = await anyio.to_thread.run_sync(
            services.connected_operator.authority
        )
        planning_writable = bool(
            config.mode == "SANDBOX" or write_capabilities.get("available")
        )
        return {
            "authenticated": True,
            "username": session.username,
            "expires_at": session.expires_at,
            "csrf_token": session.csrf_token,
            "mode": config.mode,
            "data_mode": config.data_mode,
            "market_status": market,
            "watchlist_rollover": rollover,
            "planning_revision": canonical.get("revision"),
            "buy_today_draft_revision": draft_revision,
            "planning_writable": planning_writable,
            "planning_operations": write_capabilities.get("operations", []),
            "operator": operator_capabilities,
            "sync_state": "NOT SYNCED TO EXECUTOR"
            if config.mode == "SANDBOX"
            else (
                "PC CANONICAL + GUARDED OPERATOR"
                if operator_capabilities.get("delegated")
                and operator_capabilities.get("operations")
                else "PC CANONICAL PASSIVE WRITES"
                if planning_writable and canonical.get("state") == "AVAILABLE"
                else "PC CANONICAL READ-ONLY"
                if canonical.get("state") == "AVAILABLE"
                else "PC CANONICAL UNAVAILABLE"
            ),
        }

    @app.post("/api/v1/auth/logout", include_in_schema=False)
    async def logout(
        request: Request,
        response: Response,
        _session: SessionRecord = Depends(csrf_session),
    ) -> dict[str, bool]:
        await anyio.to_thread.run_sync(
            services.auth.logout, request.cookies.get(SESSION_COOKIE)
        )
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"logged_out": True}

    @app.get("/api/v1/status")
    async def status(_session: SessionRecord = Depends(current_session)) -> dict[str, Any]:
        market, rollover = await apply_watchlist_rollover()
        market_data, canonical = await asyncio.gather(
            market_connectivity(),
            canonical_connectivity(),
        )
        draft_revision = await anyio.to_thread.run_sync(
            lambda: services.store.buy_today_draft_revision(
                shared=config.mode != "SANDBOX"
            )
        )
        market_state = str(market_data.get("state") or "UNKNOWN").upper()
        data_available = market_state in {"AVAILABLE", "DEMO"}
        write_capabilities = services.connected_planning.capabilities()
        operator_capabilities = await anyio.to_thread.run_sync(
            services.connected_operator.authority
        )
        return {
            "web_service": "HEALTHY",
            "browser_connection": "CONNECTED",
            "data_host": "AVAILABLE" if data_available else "UNAVAILABLE",
            "data_freshness": market_data.get("freshness", market_state),
            "market_data": market_data,
            "executor_health": canonical.get("executor", "UNKNOWN"),
            "executor_heartbeat_age_seconds": canonical.get(
                "executor_heartbeat_age_seconds"
            ),
            "market_status": market,
            "watchlist_rollover": rollover,
            "mode": config.mode,
            "data_mode": config.data_mode,
            "canonical_planning": canonical.get("state", "UNAVAILABLE"),
            "canonical_reason": canonical.get("reason", ""),
            "planning_revision": canonical.get("revision"),
            "buy_today_draft_revision": draft_revision,
            "planning_writable": bool(
                config.mode == "SANDBOX" or write_capabilities.get("available")
            ),
            "planning_operations": write_capabilities.get("operations", []),
            "operator": operator_capabilities,
            "canonical_writes": (
                "PLANNING + GUARDED OPERATOR"
                if operator_capabilities.get("delegated")
                and operator_capabilities.get("operations")
                else "PASSIVE PLANNING ONLY"
                if services.connected_planning.available
                else "OFF / READ ONLY"
            ),
            "supabase": "CONFIGURED"
            if config.supabase_enabled
            else "OPTIONAL / NOT CONFIGURED",
        }

    @app.get("/api/v1/scanner")
    async def scanner(
        limit: int = 300,
        setup: str | None = None,
        _session: SessionRecord = Depends(current_session),
    ) -> dict[str, Any]:
        limit = min(500, max(1, limit))
        return await anyio.to_thread.run_sync(
            lambda: services.market.scanner_snapshot(limit=limit, setup=setup)
        )

    @app.get("/api/v1/search")
    async def search(
        q: str,
        limit: int = 30,
        _session: SessionRecord = Depends(current_session),
    ) -> dict[str, Any]:
        if len(q) > 120:
            raise HTTPException(status_code=422, detail="Search is too long")
        results = await anyio.to_thread.run_sync(
            lambda: services.market.search(q, limit=min(50, max(1, limit)))
        )
        return {"query": q, "results": results, "source": config.data_mode}

    @app.get("/api/v1/charts/{symbol}/{timeframe}/status")
    async def chart_status(
        symbol: str,
        timeframe: str,
        _session: SessionRecord = Depends(current_session),
    ) -> dict[str, Any]:
        return services.charts.status(symbol, timeframe)

    @app.get("/api/v1/charts/{symbol}/{timeframe}")
    async def chart(
        symbol: str,
        timeframe: str,
        request: Request,
        background_tasks: BackgroundTasks,
        refresh: bool = False,
        _session: SessionRecord = Depends(current_session),
    ) -> Response:
        artifact, cache_hit = await services.charts.get_compressed(
            symbol, timeframe, refresh=refresh
        )
        background_tasks.add_task(services.store.touch_retained_symbol, symbol)
        compressed_checksum = str(artifact.metadata["compressed_checksum"])
        etag = f'"{compressed_checksum}"'
        headers = {
            "ETag": etag,
            "Cache-Control": "private, max-age=300",
            "Content-Encoding": "gzip",
            "Vary": "Accept-Encoding",
            "X-Chart-Cache": "HIT" if cache_hit else "MISS",
            "X-Compressed-Bytes": str(len(artifact.content)),
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(
            content=artifact.content,
            media_type="application/json",
            headers=headers,
            background=background_tasks,
        )

    @app.get("/api/v1/planning")
    async def plans(
        stage: str | None = None,
        _session: SessionRecord = Depends(current_session),
    ) -> dict[str, Any]:
        if config.mode != "SANDBOX":
            try:
                result = await anyio.to_thread.run_sync(
                    lambda: services.canonical_planning.list_plans(stage)
                )
            except CanonicalPlanningUnavailable as exc:
                return {
                    "rows": [],
                    "mode": "CONNECTED",
                    "revision": None,
                    "sync_state": f"CANONICAL PLANNING UNAVAILABLE: {exc}",
                }
            pin_rows(list(result.get("rows") or []))
            return {
                **result,
                "mode": "CONNECTED",
                "sync_state": "PC CANONICAL READ-ONLY",
            }
        await apply_watchlist_rollover()
        rows = await anyio.to_thread.run_sync(services.store.list_plans, stage)
        return {"rows": rows, "mode": "SANDBOX", "sync_state": "NOT SYNCED TO EXECUTOR"}

    @app.get("/api/v1/planning-history")
    async def planning_history(
        start_date: str,
        end_date: str,
        _session: SessionRecord = Depends(current_session),
    ) -> dict[str, Any]:
        await apply_watchlist_rollover()
        canonical_rows = await anyio.to_thread.run_sync(
            services.watchlist_history.list_range, start_date, end_date
        )
        local_rows = await anyio.to_thread.run_sync(
            services.store.list_watchlist_history, start_date, end_date
        )
        rows = merge_watchlist_history(canonical_rows, local_rows)
        return {
            "rows": rows,
            "start_date": start_date,
            "end_date": end_date,
            "mode": config.mode,
            "source": (
                "DATED WATCHLIST + LOCAL WEB AUDIT"
                if services.watchlist_history.available
                else "LOCAL WEB AUDIT"
            ),
            "sync_state": "READ ONLY",
        }

    @app.get("/api/v1/buy-today-drafts")
    async def buy_today_drafts(
        _session: SessionRecord = Depends(current_session),
    ) -> dict[str, Any]:
        if config.mode != "SANDBOX":
            try:
                planning, canonical_today, local_drafts = await asyncio.gather(
                    anyio.to_thread.run_sync(
                        services.canonical_planning.list_plans
                    ),
                    anyio.to_thread.run_sync(
                        services.canonical_planning.list_buy_today
                    ),
                    anyio.to_thread.run_sync(
                        services.store.list_shared_buy_today_drafts
                    ),
                )
            except CanonicalPlanningUnavailable as exc:
                return {
                    "rows": [],
                    "mode": "CONNECTED",
                    "revision": None,
                    "sync_state": f"CANONICAL BUY TODAY UNAVAILABLE: {exc}",
                    "draft_revision": await anyio.to_thread.run_sync(
                        lambda: services.store.buy_today_draft_revision(shared=True)
                    ),
                }
            planning_by_symbol = {
                row["symbol"]: row for row in planning.get("rows", [])
            }
            pin_rows(list(planning.get("rows") or []))
            pin_rows(list(canonical_today.get("rows") or []))
            canonical_symbols = {
                row["symbol"] for row in canonical_today.get("rows", [])
            }
            valid_drafts = []
            for draft in local_drafts:
                card = planning_by_symbol.get(draft["symbol"])
                if (
                    card is None
                    or draft["symbol"] in canonical_symbols
                    or not card.get("buylist_member")
                    or int(card.get("version") or 0)
                    != int(draft.get("card_version") or 0)
                ):
                    continue
                valid_drafts.append(
                    {
                        **draft,
                        "name": card.get("name") or draft["symbol"],
                        "breakout_price": card.get("breakout_price"),
                    }
                )
            return {
                "rows": [*canonical_today.get("rows", []), *valid_drafts],
                "revision": planning.get("revision"),
                "draft_revision": await anyio.to_thread.run_sync(
                    lambda: services.store.buy_today_draft_revision(shared=True)
                ),
                "mode": "CONNECTED",
                "executable": False,
                "sync_state": "CANONICAL BUY TODAY READ ONLY + SHARED WEB DRAFTS",
            }
        await apply_watchlist_rollover()
        rows = await anyio.to_thread.run_sync(services.store.list_buy_today_drafts)
        return {
            "rows": rows,
            "draft_revision": await anyio.to_thread.run_sync(
                services.store.buy_today_draft_revision
            ),
            "mode": "SANDBOX",
            "executable": False,
            "sync_state": "NOT SYNCED TO EXECUTOR",
        }

    @app.get("/api/v1/planning/{symbol}")
    async def plan(
        symbol: str,
        _session: SessionRecord = Depends(current_session),
    ) -> dict[str, Any]:
        if config.mode != "SANDBOX":
            try:
                result = await anyio.to_thread.run_sync(
                    lambda: services.canonical_planning.get_plan(
                        symbol,
                        include_inactive=services.connected_planning.available,
                    )
                )
            except CanonicalPlanningUnavailable as exc:
                return {
                    "card": None,
                    "revision": None,
                    "sync_state": f"CANONICAL PLANNING UNAVAILABLE: {exc}",
                }
            if result.get("card"):
                pin_rows([result["card"]])
            return {**result, "sync_state": "PC CANONICAL READ-ONLY"}
        await apply_watchlist_rollover()
        card = await anyio.to_thread.run_sync(services.store.get_plan, symbol)
        return {"card": card, "sync_state": "NOT SYNCED TO EXECUTOR"}

    @app.post("/api/v1/planning/{symbol}/commands")
    async def planning_command(
        symbol: str,
        payload: PlanningCommandRequest,
        session: SessionRecord = Depends(csrf_session),
    ) -> dict[str, Any]:
        if config.mode != "SANDBOX":
            if not config.canonical_planning_writes:
                raise HTTPException(
                    status_code=403,
                    detail="CONNECTED canonical planning writes are disabled",
                )
            command_payload = {
                "operation": payload.operation.strip().lower(),
                "symbol": str(symbol or "").strip().upper(),
                "expected_revision": int(payload.expected_revision),
                "breakout_price": payload.breakout_price,
            }
            replay = await anyio.to_thread.run_sync(
                lambda: services.store.connected_command_replay(
                    command_id=payload.command_id,
                    payload=command_payload,
                )
            )
            if replay is not None:
                return replay
            result = await anyio.to_thread.run_sync(
                lambda: services.connected_planning.apply(
                    command_id=payload.command_id,
                    operation=payload.operation,
                    symbol=symbol,
                    expected_revision=payload.expected_revision,
                    breakout_price=payload.breakout_price,
                )
            )
            recorded = await anyio.to_thread.run_sync(
                lambda: services.store.record_connected_command(
                    command_id=payload.command_id,
                    payload=command_payload,
                    response=result,
                    actor=session.username,
                )
            )
            announce_planning_change("planning", symbol, recorded)
            return recorded
        await apply_watchlist_rollover()
        result = await anyio.to_thread.run_sync(
            lambda: services.store.apply_planning_command(
                command_id=payload.command_id,
                operation=payload.operation,
                symbol=symbol,
                expected_revision=payload.expected_revision,
                actor=session.username,
                breakout_price=payload.breakout_price,
            )
        )
        announce_planning_change("planning", symbol, result)
        return result

    @app.post("/api/v1/planning/{symbol}/buy-today-preview")
    async def buy_today_preview(
        symbol: str,
        payload: DraftPreviewRequest,
        session: SessionRecord = Depends(csrf_session),
    ) -> dict[str, Any]:
        if config.mode != "SANDBOX":
            try:
                result = await anyio.to_thread.run_sync(
                    lambda: services.canonical_planning.get_plan(symbol, force=True)
                )
            except CanonicalPlanningUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            card = result.get("card")
            if card is None:
                raise ConflictError("Buy Today preview requires a Buylist card")
            preview = await anyio.to_thread.run_sync(
                services.store.save_shared_buy_today_preview,
                card,
                payload.expected_revision,
                session.username,
            )
            response = {
                "preview": preview,
                "published": False,
                "executable": False,
                "state": "SHARED WEB DRAFT / NOT EXECUTABLE",
            }
            announce_planning_change("buy_today", symbol)
            return response
        await apply_watchlist_rollover()
        preview = await anyio.to_thread.run_sync(
            services.store.save_buy_today_preview,
            symbol,
            payload.expected_revision,
            session.username,
        )
        announce_planning_change("buy_today", symbol)
        return {"preview": preview, "published": False, "executable": False}

    @app.delete("/api/v1/planning/{symbol}/buy-today-preview")
    async def cancel_buy_today_preview(
        symbol: str,
        payload: DraftPreviewRequest,
        session: SessionRecord = Depends(csrf_session),
    ) -> dict[str, Any]:
        if config.mode != "SANDBOX":
            try:
                result = await anyio.to_thread.run_sync(
                    lambda: services.canonical_planning.get_plan(symbol, force=True)
                )
            except CanonicalPlanningUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            card = result.get("card")
            if card is None:
                raise ConflictError("Buy Today cancellation requires a planning card")
            cancelled = await anyio.to_thread.run_sync(
                services.store.cancel_shared_buy_today_preview,
                card,
                payload.expected_revision,
                session.username,
            )
            response = {
                "cancelled": cancelled,
                "published": False,
                "executable": False,
                "state": "SHARED WEB DRAFT CANCELLED",
            }
            announce_planning_change("buy_today", symbol)
            return response
        await apply_watchlist_rollover()
        cancelled = await anyio.to_thread.run_sync(
            services.store.cancel_buy_today_preview,
            symbol,
            payload.expected_revision,
            session.username,
        )
        announce_planning_change("buy_today", symbol)
        return {"cancelled": cancelled, "published": False, "executable": False}

    @app.post("/api/v1/planning/{symbol}/activate-buy-today")
    async def activate_buy_today(
        symbol: str,
        payload: BuyTodayOperatorRequest | None = None,
        session: SessionRecord = Depends(csrf_session),
    ) -> dict[str, Any]:
        if config.mode == "SANDBOX" or not services.connected_operator.available:
            raise HTTPException(
                status_code=403,
                detail="Mobile Buy Today operator actions are disabled",
            )
        if payload is None:
            raise HTTPException(status_code=422, detail="Operator action body is required")
        command_payload = {
            "operation": (
                "activate_buy_today" if payload.enabled else "deactivate_buy_today"
            ),
            "symbol": str(symbol or "").strip().upper(),
            "expected_revision": int(payload.expected_revision),
            "enabled": bool(payload.enabled),
        }
        replay = await anyio.to_thread.run_sync(
            lambda: services.store.connected_command_replay(
                command_id=payload.command_id,
                payload=command_payload,
            )
        )
        if replay is not None:
            return replay
        result = await anyio.to_thread.run_sync(
            lambda: services.connected_operator.set_buy_today(
                command_id=payload.command_id,
                symbol=symbol,
                expected_revision=payload.expected_revision,
                enabled=payload.enabled,
            )
        )
        recorded = await anyio.to_thread.run_sync(
            lambda: services.store.record_connected_command(
                command_id=payload.command_id,
                payload=command_payload,
                response=result,
                actor=session.username,
            )
        )
        announce_planning_change("buy_today", symbol, recorded)
        return recorded

    @app.get("/api/v1/buyboard")
    async def buyboard(
        _session: SessionRecord = Depends(current_session),
    ) -> dict[str, Any]:
        if config.mode == "SANDBOX" or not services.canonical_planning.available:
            raise HTTPException(status_code=404, detail="Canonical Buy Board unavailable")
        result = await anyio.to_thread.run_sync(
            services.connected_operator.board_snapshot
        )
        pin_rows(list(result.get("rows") or []))
        return {
            **result,
            "columns": [
                "BUYLIST",
                "BUY_TODAY",
                "ENTRY_PENDING",
                "OPEN_POSITION",
                "PARTIAL_SELL",
                "SELL_ALL",
            ],
            "sync_state": "PC CANONICAL KANBAN",
        }

    @app.get("/api/v1/operator/control")
    async def operator_control(
        _session: SessionRecord = Depends(current_session),
    ) -> dict[str, Any]:
        if config.mode == "SANDBOX" or not services.connected_operator.available:
            raise HTTPException(status_code=404, detail="Operator Control unavailable")
        return await anyio.to_thread.run_sync(
            services.connected_operator.authority
        )

    @app.post("/api/v1/operator/control")
    async def change_operator_control(
        payload: OperatorControlRequest,
        _session: SessionRecord = Depends(csrf_session),
    ) -> dict[str, Any]:
        if config.mode == "SANDBOX" or not services.connected_operator.available:
            raise HTTPException(status_code=403, detail="Operator Control unavailable")
        result = await anyio.to_thread.run_sync(
            lambda: services.connected_operator.set_operator_control_target(
                payload.target
            )
        )
        announce_planning_change("operator_control")
        return result

    @app.get("/api/v1/operator/orb-settings")
    async def orb_settings(
        _session: SessionRecord = Depends(current_session),
    ) -> dict[str, Any]:
        if config.mode == "SANDBOX" or services.connected_operator.engine is None:
            raise HTTPException(status_code=404, detail="Shared ORB settings unavailable")
        return await anyio.to_thread.run_sync(
            services.connected_operator.orb_settings_snapshot
        )

    @app.put("/api/v1/operator/orb-settings")
    async def update_orb_settings(
        payload: OrbSettingsUpdateRequest,
        session: SessionRecord = Depends(csrf_session),
    ) -> dict[str, Any]:
        if config.mode == "SANDBOX" or not services.connected_operator.available:
            raise HTTPException(status_code=403, detail="Shared ORB settings are disabled")
        from src.risk.orb_position import OrbSettings

        settings_values = payload.model_dump(
            exclude={"command_id", "expected_revision"}
        )
        try:
            settings = OrbSettings(**settings_values)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        command_payload = {
            "operation": "update_orb_settings",
            "expected_revision": int(payload.expected_revision),
            **settings.to_dict(),
        }
        replay = await anyio.to_thread.run_sync(
            lambda: services.store.connected_command_replay(
                command_id=payload.command_id,
                payload=command_payload,
            )
        )
        if replay is not None:
            return replay
        result = await anyio.to_thread.run_sync(
            lambda: services.connected_operator.update_orb_settings(
                expected_revision=payload.expected_revision,
                settings=settings,
            )
        )
        recorded = await anyio.to_thread.run_sync(
            lambda: services.store.record_connected_command(
                command_id=payload.command_id,
                payload=command_payload,
                response=result,
                actor=session.username,
            )
        )
        announce_planning_change("orb_settings", result=recorded)
        return recorded

    @app.post("/api/v1/operator/board-actions")
    async def apply_board_action(
        payload: BoardOperatorRequest,
        session: SessionRecord = Depends(csrf_session),
    ) -> dict[str, Any]:
        if config.mode == "SANDBOX" or not services.connected_operator.available:
            raise HTTPException(status_code=403, detail="Mobile Buy Board actions are disabled")
        command_payload = payload.model_dump()
        command_payload["operation"] = command_payload.pop("action")
        command_payload["symbol"] = str(payload.symbol or "").strip().upper()
        replay = await anyio.to_thread.run_sync(
            lambda: services.store.connected_command_replay(
                command_id=payload.command_id,
                payload=command_payload,
            )
        )
        if replay is not None:
            return replay
        result = await anyio.to_thread.run_sync(
            lambda: services.connected_operator.apply_board_action(
                command_id=payload.command_id,
                action=payload.action,
                symbol=payload.symbol,
                expected_revision=payload.expected_revision,
                quantity=payload.quantity,
                price=payload.price,
                target_priority=payload.target_priority,
            )
        )
        recorded = await anyio.to_thread.run_sync(
            lambda: services.store.record_connected_command(
                command_id=payload.command_id,
                payload=command_payload,
                response=result,
                actor=session.username,
            )
        )
        announce_planning_change("board", payload.symbol, recorded)
        return recorded

    @app.post("/api/v1/operator/publish-today-plan")
    async def publish_today_plan(
        payload: PublishTodayPlanRequest,
        session: SessionRecord = Depends(csrf_session),
    ) -> dict[str, Any]:
        if config.mode == "SANDBOX" or not services.connected_operator.available:
            raise HTTPException(
                status_code=403,
                detail="Mobile plan publication is disabled",
            )
        command_payload = {
            "operation": "publish_today_plan",
            "symbol": "",
        }
        replay = await anyio.to_thread.run_sync(
            lambda: services.store.connected_command_replay(
                command_id=payload.command_id,
                payload=command_payload,
            )
        )
        if replay is not None:
            return replay
        result = await anyio.to_thread.run_sync(
            lambda: services.connected_operator.publish_today_plan(
                command_id=payload.command_id
            )
        )
        recorded = await anyio.to_thread.run_sync(
            lambda: services.store.record_connected_command(
                command_id=payload.command_id,
                payload=command_payload,
                response=result,
                actor=session.username,
            )
        )
        announce_planning_change("board", result=recorded)
        return recorded

    @app.get("/api/v1/operator/commands/{command_id}")
    async def operator_command_status(
        command_id: str,
        _session: SessionRecord = Depends(current_session),
    ) -> dict[str, Any]:
        if config.mode == "SANDBOX" or not services.connected_operator.available:
            raise HTTPException(status_code=404, detail="Operator command unavailable")
        return await anyio.to_thread.run_sync(
            lambda: services.connected_operator.command_status(command_id)
        )

    @app.get("/api/v1/drawings/{symbol}")
    async def drawings(
        symbol: str,
        _session: SessionRecord = Depends(current_session),
    ) -> dict[str, Any]:
        rows = await anyio.to_thread.run_sync(services.store.list_drawings, symbol)
        return {"rows": rows}

    @app.post("/api/v1/drawings")
    async def create_drawing(
        payload: DrawingCreateRequest,
        session: SessionRecord = Depends(csrf_session),
    ) -> dict[str, Any]:
        drawing = await anyio.to_thread.run_sync(
            lambda: services.store.create_drawing(
                payload.model_dump(), actor=session.username
            )
        )
        return {"drawing": drawing, "state": "SAVED LOCALLY"}

    @app.put("/api/v1/drawings/{drawing_id}")
    async def update_drawing(
        drawing_id: str,
        payload: DrawingUpdateRequest,
        session: SessionRecord = Depends(csrf_session),
    ) -> dict[str, Any]:
        drawing = await anyio.to_thread.run_sync(
            lambda: services.store.update_drawing(
                drawing_id, payload.model_dump(), actor=session.username
            )
        )
        return {"drawing": drawing, "state": "SAVED LOCALLY"}

    @app.delete("/api/v1/drawings/{drawing_id}")
    async def delete_drawing(
        drawing_id: str,
        payload: DrawingDeleteRequest,
        session: SessionRecord = Depends(csrf_session),
    ) -> dict[str, Any]:
        drawing = await anyio.to_thread.run_sync(
            lambda: services.store.delete_drawing(
                drawing_id,
                expected_revision=payload.expected_revision,
                actor=session.username,
            )
        )
        return {"drawing": drawing, "state": "DELETED"}

    async def denied_action(
        action: str, _session: SessionRecord = Depends(csrf_session)
    ) -> None:
        raise HTTPException(
            status_code=403,
            detail=f"{action} is forbidden in the localhost web milestone",
        )

    for prefix in ("execution", "orders", "ownership", "risk", "power"):
        app.add_api_route(
            f"/api/v1/{prefix}/{{action}}",
            denied_action,
            methods=["POST", "PUT", "PATCH", "DELETE"],
            include_in_schema=False,
            name=f"deny_{prefix}",
        )


def create_api_app(
    config: WebConfig | None = None, *, services: WebServices | None = None
) -> FastAPI:
    config = config or load_web_config()
    services = services or build_services(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            services.connected_planning.close()
            services.canonical_planning.close()

    app = FastAPI(
        title="Quant Web Localhost",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    register_api_routes(app, services)
    app.state.web_services = services
    return app
