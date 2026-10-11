"""Loopback-only historical research workspace, independent of the broker."""
from __future__ import annotations

from contextlib import asynccontextmanager
import csv
import datetime as dt
from io import StringIO
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.infrastructure.database.engine import init_mysql_engine
from src.infrastructure.database.repositories.breakout_replay import ReplayRepository
from src.services.breakout_replay import ReplayService
from src.services.replay_intraday import IntradayUnavailable

STATIC = Path(__file__).resolve().parent / "static" / "replay"
VENDOR = Path(__file__).resolve().parents[1] / "ui" / "static" / "vendor"


class ScanRequest(BaseModel):
    day: dt.date
    setup_name: str = Field(default="Setup 2", min_length=1, max_length=80)


class OpenRequest(BaseModel):
    scan_id: str
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,19}$")
    mode: Literal["backtesting", "historical"] = "backtesting"


class ViewRequest(BaseModel):
    mode: Literal["backtesting", "historical"]


class StepRequest(BaseModel):
    direction: int


class ReviewRequest(BaseModel):
    decision: str
    pattern: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=3000)
    outcome: str | None = None
    expected_day: dt.date | None = None


class DayRequest(BaseModel):
    day: dt.date


class PaperStudyRequest(DayRequest):
    request_id: UUID


class DrawingPoint(BaseModel):
    time: str | int
    price: float = Field(gt=0, allow_inf_nan=False)


class DrawingRequest(BaseModel):
    frame: str = "daily"
    kind: str
    points: list[DrawingPoint] = Field(min_length=1, max_length=2)
    day: dt.date | None = None
    visible_bars: int = Field(default=100, ge=1, le=100)
    source: str = "auto"
    interval_minutes: Literal[5, 30] = 5


class SnapshotRequest(BaseModel):
    data_url: str = Field(max_length=21_000_000)
    frame: str = "daily"
    day: dt.date | None = None
    visible_bars: int = Field(default=100, ge=1, le=100)
    source: str = "auto"
    interval_minutes: Literal[5, 30] = 5
    study_id: str | None = Field(default=None, max_length=36)


class PaperTradeRequest(BaseModel):
    request_id: UUID
    day: dt.date
    time: int
    action: Literal["buy", "partial_sell", "sell_all"]
    price: float = Field(gt=0, allow_inf_nan=False)
    stop_pct: float = Field(default=5, gt=0, lt=100, allow_inf_nan=False)
    percent: float = Field(default=50, gt=0, lt=100, allow_inf_nan=False)
    source: str = "auto"
    interval_minutes: Literal[5, 30] = 5
    study_id: str | None = Field(default=None, max_length=36)


def create_replay_app(service: ReplayService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        if app.state.replay is None:
            engine = init_mysql_engine(log_unavailable=False, ensure_schema=False)
            if engine is not None:
                try:
                    repository = ReplayRepository(engine)
                    for row in repository.rows(select(repository.scans).where(repository.scans.c.status == "running")):
                        repository.update_scan(row["id"], status="interrupted", payload={"message": "Previous scan was interrupted. Run it again to resume cached daily imports."})
                    app.state.replay = ReplayService(repository)
                except Exception:
                    engine.dispose()
        yield
        if app.state.replay:
            app.state.replay.pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="Breakout Lab", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.replay = service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.client and request.client.host not in ("127.0.0.1", "::1", "testclient"):
            return JSONResponse({"detail": "This workspace is available on localhost only"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
                return JSONResponse({"detail": "Use the workspace's own localhost tab"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site requests are not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(ValueError)
    async def bad_request(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(SQLAlchemyError)
    async def database_busy(_request, _exc):
        return JSONResponse({"detail": "The MySQL cache is temporarily busy or unavailable. Wait a moment and retry."}, status_code=503)

    def active():
        if app.state.replay is None:
            raise HTTPException(503, "MySQL is unavailable. Start MySQL and check the existing .env credentials, then restart the replay server.")
        return app.state.replay

    app.mount("/replay-static", StaticFiles(directory=STATIC), name="replay-static")
    app.mount("/vendor", StaticFiles(directory=VENDOR), name="vendor")

    @app.get("/", include_in_schema=False)
    @app.get("/deepdive", include_in_schema=False)
    def page():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status():
        return active().status()

    @app.get("/api/preparation")
    def preparation():
        cache = active().repo.prepared
        published = cache.job("published") or {}
        return {"progress": cache.job("progress"),
                "start_day": published.get("start_day"), "end_day": published.get("end_day")}

    @app.post("/api/scans")
    def scan(payload: ScanRequest):
        return active().start_scan(payload.day, payload.setup_name)

    @app.get("/api/scans/{scan_id}")
    def scan_results(scan_id: str):
        return active().scan_results(scan_id)

    @app.post("/api/sessions")
    def open_session(payload: OpenRequest):
        return active().open_session(payload.scan_id, payload.symbol, payload.mode)

    @app.get("/api/sessions/{session_id}")
    def session(session_id: str):
        return active().session_payload(session_id)

    @app.post("/api/sessions/{session_id}/mode")
    def session_mode(session_id: str, payload: ViewRequest):
        return active().set_mode(session_id, payload.mode)

    @app.post("/api/sessions/{session_id}/day")
    def study_day(session_id: str, payload: DayRequest):
        return active().select_day(session_id, payload.day)

    @app.post("/api/sessions/{session_id}/step")
    def step(session_id: str, payload: StepRequest):
        return active().step(session_id, payload.direction)

    @app.post("/api/sessions/{session_id}/review")
    def review(session_id: str, payload: ReviewRequest):
        return active().save_review(session_id, **payload.model_dump())

    @app.post("/api/sessions/{session_id}/expect-day")
    def expected_day(session_id: str, payload: DayRequest):
        return active().mark_expected_day(session_id, payload.day)

    @app.get("/api/sessions/{session_id}/intraday")
    def intraday(session_id: str, day: dt.date, source: str = "auto", visible_bars: int = 100,
            interval_minutes: Literal["5", "30"] = "5", study_id: str | None = None):
        return active().intraday(session_id, day, source, visible_bars, int(interval_minutes), study_id)

    @app.get("/api/sessions/{session_id}/paper")
    def paper_state(session_id: str, day: dt.date, source: str = "auto", time: int | None = None, study_id: str | None = None):
        return active().paper_state(session_id, day, source, time, study_id)

    @app.post("/api/sessions/{session_id}/paper/studies")
    def paper_study(session_id: str, payload: PaperStudyRequest):
        return active().create_paper_study(session_id, payload.day, payload.request_id)

    @app.post("/api/sessions/{session_id}/paper/trades")
    def paper_trade(session_id: str, payload: PaperTradeRequest):
        return active().paper_trade(session_id, **payload.model_dump())

    @app.post("/api/sessions/{session_id}/drawings")
    def drawing(session_id: str, payload: DrawingRequest):
        return active().add_drawing(session_id, **payload.model_dump())

    @app.delete("/api/sessions/{session_id}/drawings/{drawing_id}")
    def delete_drawing(session_id: str, drawing_id: str):
        return active().delete_drawing(session_id, drawing_id)

    @app.post("/api/sessions/{session_id}/snapshots")
    def snapshot(session_id: str, payload: SnapshotRequest):
        return active().save_snapshot(session_id, **payload.model_dump())

    @app.get("/api/snapshots/{snapshot_id}")
    def snapshot_file(snapshot_id: str):
        service = active()
        rows = service.repo.rows(select(service.repo.snapshots).where(service.repo.snapshots.c.id == snapshot_id))
        if not rows:
            raise HTTPException(404, "Snapshot not found")
        path = (service.snapshot_dir / rows[0]["path"]).resolve()
        if not path.is_relative_to(service.snapshot_dir.resolve()) or not path.is_file():
            raise HTTPException(404, "Snapshot file is missing")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/library")
    def library():
        # The explicit database view is hindsight research, separate from blind replay.
        service = active()
        rows = service.repo.review_rows()
        indexed = {(row["symbol"], row["day"]): row for row in rows}
        for row in rows:
            expected = indexed.get((row["symbol"], row["expected_day"])) if row["expected_day"] else None
            row["expected_outcome"] = expected["outcome"] if expected else None
        from src.core.breakout_replay import clean_json
        return clean_json({"reviews": rows, "snapshots": service.repo.rows(select(service.repo.snapshots.c.id,
            service.repo.snapshots.c.symbol, service.repo.snapshots.c.day, service.repo.snapshots.c.frame,
            service.repo.snapshots.c.path).order_by(service.repo.snapshots.c.created_at.desc()))})

    @app.get("/api/export.csv")
    def export():
        out = StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(["symbol", "session", "decision", "expected_session", "outcome", "pattern", "notes", "adr_20", "return_1m", "return_3m", "ti65", "blind_observation", "retrospective_revision"])
        for row in active().repo.review_rows():
            # Prevent spreadsheet formula execution in free-text journal fields.
            def cell(value):
                value = "" if value is None else str(value)
                return "'" + value if value.startswith(("=", "+", "-", "@")) else value
            writer.writerow([cell(row[name]) for name in ("symbol", "day", "decision", "expected_day", "outcome", "pattern", "notes")]
                + [row["metrics"].get(name) for name in ("adr_20", "return_1m", "return_3m", "ti65", "blind_observation", "retrospective_revision")])
        return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="breakout_learning_database.csv"'})

    @app.post("/api/provider/check")
    def provider_check():
        service = active()
        frame, source, cached = service.load_intraday_archive("AAPL", dt.date(2025, 1, 2))
        message = "Historical 5-minute data verified from the local archive; no provider request needed (AAPL, 2025-01-02)." if cached else "Historical 5-minute connection verified and cached (AAPL, 2025-01-02)."
        return {"source": source, "bars": len(frame), "cached": cached, "message": message}

    return app
