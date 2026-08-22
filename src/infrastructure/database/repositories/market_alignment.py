"""Atomic SQL publication and indexed lookup for market-alignment snapshots."""

from __future__ import annotations

import datetime as dt
import json
from enum import Enum
from typing import Iterable, Mapping, Optional

from sqlalchemy import MetaData, and_, func, select
from sqlalchemy.engine import Engine

from src.core.market_alignment import ContextState, MarketAlignmentSnapshot
from src.infrastructure.database.schema import (
    _ensure_market_alignment_tables,
    _get_market_alignment_batches_table,
    _get_stock_market_alignment_daily_table,
)
from src.infrastructure.database.sql_helpers import _execute_bulk_upsert
from src.infrastructure.database.time_utils import _utcnow_naive


PUBLISHED_STATUS = "published"


class MarketAlignmentRepository:
    """Batch-first repository; charts only read rows with a published manifest."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _tables(self):
        snapshots, batches = self._table_definitions()
        _ensure_market_alignment_tables(self.engine)
        return snapshots, batches

    @staticmethod
    def _table_definitions():
        metadata = MetaData()
        snapshots = _get_stock_market_alignment_daily_table(metadata)
        batches = _get_market_alignment_batches_table(metadata)
        return snapshots, batches

    def is_batch_published(self, as_of_date: dt.date, feature_version: str) -> bool:
        _snapshots, batches = self._tables()
        stmt = select(batches.c.status).where(
            batches.c.as_of_date == as_of_date,
            batches.c.feature_version == feature_version,
        )
        with self.engine.connect() as conn:
            status = conn.execute(stmt).scalar_one_or_none()
        return status == PUBLISHED_STATUS

    def latest_published_date(self, feature_version: Optional[str] = None) -> Optional[dt.date]:
        _snapshots, batches = self._tables()
        stmt = select(func.max(batches.c.as_of_date)).where(
            batches.c.status == PUBLISHED_STATUS
        )
        if feature_version:
            stmt = stmt.where(batches.c.feature_version == feature_version)
        with self.engine.connect() as conn:
            return conn.execute(stmt).scalar_one_or_none()

    def publish_batch(
        self,
        snapshots: Iterable[MarketAlignmentSnapshot],
        *,
        input_fingerprint: str,
        stats: Mapping[str, object],
    ) -> int:
        values = tuple(snapshots)
        if not values:
            raise ValueError("A market-alignment batch cannot be empty")
        as_of_dates = {item.as_of_date for item in values}
        versions = {item.feature_version for item in values}
        if len(as_of_dates) != 1 or len(versions) != 1:
            raise ValueError("Every alignment row must share one date and feature version")

        snapshot_table, batch_table = self._tables()
        now = _utcnow_naive()
        records = [self._snapshot_record(item, now) for item in values]
        manifest = {
            "as_of_date": values[0].as_of_date,
            "feature_version": values[0].feature_version,
            "status": PUBLISHED_STATUS,
            "input_fingerprint": str(input_fingerprint),
            "symbol_count": len(records),
            "stats_json": json.dumps(dict(stats), sort_keys=True, separators=(",", ":")),
            "computed_at": self._naive_utc(values[0].calculated_at),
            "published_at": now,
            "updated_at": now,
        }
        with self.engine.begin() as conn:
            # One transaction is the publication boundary. A failed row chunk
            # or manifest write rolls the entire attempted replacement back.
            _execute_bulk_upsert(
                conn,
                snapshot_table,
                records,
                ("symbol", "as_of_date", "feature_version"),
                self.engine.dialect.name,
            )
            _execute_bulk_upsert(
                conn,
                batch_table,
                [manifest],
                ("as_of_date", "feature_version"),
                self.engine.dialect.name,
            )
        return len(records)

    def get_latest_market_alignment(
        self,
        symbol: str,
    ) -> Optional[MarketAlignmentSnapshot]:
        # Deliberately do not call schema guards here. Chart selection is a
        # read-only indexed query and must never run a migration.
        snapshots, batches = self._table_definitions()
        canonical = str(symbol or "").strip().upper()
        if not canonical:
            return None
        stmt = (
            select(snapshots)
            .join(
                batches,
                and_(
                    batches.c.as_of_date == snapshots.c.as_of_date,
                    batches.c.feature_version == snapshots.c.feature_version,
                    batches.c.status == PUBLISHED_STATUS,
                ),
            )
            .where(snapshots.c.symbol == canonical)
            .order_by(snapshots.c.as_of_date.desc(), snapshots.c.feature_version.desc())
            .limit(1)
        )
        with self.engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._snapshot_from_row(row) if row is not None else None

    @staticmethod
    def _snapshot_record(snapshot: MarketAlignmentSnapshot, now: dt.datetime) -> dict:
        return {
            "symbol": snapshot.symbol.strip().upper(),
            "as_of_date": snapshot.as_of_date,
            "feature_version": snapshot.feature_version,
            "market_rs": snapshot.market_rs,
            "market_rs_source": snapshot.market_rs_source,
            "industry_peer_rs": snapshot.industry_peer_rs,
            "peer_basis": snapshot.peer_basis,
            "peer_count": int(snapshot.peer_count),
            "peer_group_id": snapshot.peer_group_id,
            "peer_group_name": snapshot.peer_group_name,
            "leadership_score": snapshot.leadership_score,
            "leadership_label": snapshot.leadership_label,
            "market_state": snapshot.market_state.value,
            "market_conditions_passed": snapshot.market_conditions_passed,
            "market_cap": snapshot.market_cap,
            "market_cap_as_of_date": snapshot.market_cap_as_of_date,
            "segment_name": snapshot.segment_name,
            "segment_proxy": snapshot.segment_proxy,
            "segment_state": snapshot.segment_state.value,
            "segment_conditions_passed": snapshot.segment_conditions_passed,
            "sector_name": snapshot.sector_name,
            "sector_proxy": snapshot.sector_proxy,
            "sector_state": snapshot.sector_state.value,
            "sector_conditions_passed": snapshot.sector_conditions_passed,
            "industry_name": snapshot.industry_name,
            "industry_proxy_or_index": snapshot.industry_proxy_or_index,
            "industry_state": snapshot.industry_state.value,
            "industry_conditions_passed": snapshot.industry_conditions_passed,
            "context_points": snapshot.context_points,
            "context_available_components": int(snapshot.context_available_components),
            "context_label": snapshot.context_label,
            "is_provisional": bool(snapshot.is_provisional),
            "classification_source": snapshot.classification_source,
            "calculation_details_json": json.dumps(
                dict(snapshot.calculation_details),
                sort_keys=True,
                separators=(",", ":"),
                default=MarketAlignmentRepository._json_default,
            ),
            "calculated_at": MarketAlignmentRepository._naive_utc(
                snapshot.calculated_at
            ),
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _snapshot_from_row(row: Mapping[str, object]) -> MarketAlignmentSnapshot:
        try:
            details = json.loads(str(row.get("calculation_details_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            details = {}
        calculated_at = row["calculated_at"]
        if isinstance(calculated_at, dt.datetime) and calculated_at.tzinfo is None:
            calculated_at = calculated_at.replace(tzinfo=dt.timezone.utc)
        return MarketAlignmentSnapshot(
            symbol=str(row["symbol"]),
            as_of_date=row["as_of_date"],
            feature_version=str(row["feature_version"]),
            market_rs=row.get("market_rs"),
            market_rs_source=str(row.get("market_rs_source") or "unknown"),
            industry_peer_rs=row.get("industry_peer_rs"),
            peer_basis=str(row.get("peer_basis") or "unavailable"),
            peer_count=int(row.get("peer_count") or 0),
            peer_group_id=row.get("peer_group_id"),
            peer_group_name=row.get("peer_group_name"),
            leadership_score=row.get("leadership_score"),
            leadership_label=str(row.get("leadership_label") or "N/A"),
            market_state=ContextState(str(row.get("market_state") or "UNKNOWN")),
            market_conditions_passed=row.get("market_conditions_passed"),
            market_cap=row.get("market_cap"),
            market_cap_as_of_date=row.get("market_cap_as_of_date"),
            segment_name=row.get("segment_name"),
            segment_proxy=row.get("segment_proxy"),
            segment_state=ContextState(str(row.get("segment_state") or "UNKNOWN")),
            segment_conditions_passed=row.get("segment_conditions_passed"),
            sector_name=row.get("sector_name"),
            sector_proxy=row.get("sector_proxy"),
            sector_state=ContextState(str(row.get("sector_state") or "UNKNOWN")),
            sector_conditions_passed=row.get("sector_conditions_passed"),
            industry_name=row.get("industry_name"),
            industry_proxy_or_index=row.get("industry_proxy_or_index"),
            industry_state=ContextState(str(row.get("industry_state") or "UNKNOWN")),
            industry_conditions_passed=row.get("industry_conditions_passed"),
            context_points=row.get("context_points"),
            context_available_components=int(
                row.get("context_available_components") or 0
            ),
            context_label=str(row.get("context_label") or "UNKNOWN"),
            is_provisional=bool(row.get("is_provisional")),
            classification_source=str(
                row.get("classification_source") or "unknown"
            ),
            calculation_details=details if isinstance(details, dict) else {},
            calculated_at=calculated_at,
        )

    @staticmethod
    def _naive_utc(value: dt.datetime) -> dt.datetime:
        if value.tzinfo is not None:
            return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return value

    @staticmethod
    def _json_default(value: object):
        if isinstance(value, (dt.date, dt.datetime)):
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        raise TypeError(f"Unsupported alignment detail type: {type(value).__name__}")
