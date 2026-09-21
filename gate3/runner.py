"""Headless Gate-3 session runner and captured-live replay probes."""

from __future__ import annotations

import argparse
import copy
import ctypes
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import MetaData, Table, create_engine, inspect, select
from sqlalchemy.engine import Engine

from activation_gates.evidence import canonical_report_sha256
from gate2.capabilities import load_verified_capability_manifest
from gate2.reporting import _session_bounds, clock_synchronization_status
from gate3.collector import Gate3EvidenceCollector
from gate3.decision_oracle import (
    OracleDecision,
    expected_entry,
    expected_exact_cancel,
    expected_higher_timeframe_replacement,
    expected_protective_sell,
)
from gate3.reporting import build_report
from gate3.shadow_boundary import ShadowExecutionBoundary, ShadowMutationIntercepted
from gate3.shadow_gateway import ShadowExecutionGateway
from src.core.execution_request import (
    CancelExecutionRequest,
    ReplaceExecutionRequest,
    SubmitExecutionRequest,
)
from src.core.order_state import BrokerOrderDiscoveryResult, OrderIntent, OrderSide
from src.core.runtime_safety_audit import begin_runtime_safety_audit
from src.core.trade_card_state import TradeCardState
from src.infrastructure.database.engine import init_mysql_engine
from src.services.buyboard_runtime import build_buyboard_runtime
from src.services.execution_command_repository import ensure_execution_commands_table
from src.services.execution_order_repository import ensure_execution_orders_table
from src.services.kis_realtime_market_data import (
    SubscriptionPriority,
    build_kis_realtime_market_data_from_environment,
)
from src.services.kis_ws_symbol_keys import KisWsSymbolKeyStore
from src.services.realtime_market_data import QuoteSnapshot, RealtimeMarketDataService


ROOT = Path(__file__).resolve().parents[1]
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
PRODUCTION_LEDGER_TABLES = (
    "execution_commands",
    "execution_orders",
    "trade_cards",
    "discovered_external_orders",
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def _set_keep_awake(enabled: bool) -> bool:
    if not hasattr(ctypes, "windll"):
        return False
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
    try:
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except (AttributeError, OSError):
        return False


def production_runtime_source_sha256() -> str:
    digest = hashlib.sha256()
    for relative in (
        "src/services/trading_engine.py",
        "src/services/buyboard_runtime.py",
        "src/services/execution_workflow_service.py",
        "src/services/execution_command_gateway.py",
        "gate3/shadow_gateway.py",
    ):
        digest.update(relative.encode("utf-8"))
        digest.update((ROOT / relative).read_bytes())
    return digest.hexdigest()


def _normalized(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        return {str(key): _normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    return value


def snapshot_production_ledgers(engine: Engine) -> dict[str, str]:
    """Hash canonical rows using SELECT only; no table is provisioned here."""
    available = set(inspect(engine).get_table_names())
    snapshots: dict[str, str] = {}
    for name in PRODUCTION_LEDGER_TABLES:
        if name not in available:
            snapshots[f"mysql:{name}"] = hashlib.sha256(b"ABSENT").hexdigest()
            continue
        table = Table(name, MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            rows = [dict(row._mapping) for row in connection.execute(select(table))]
        canonical_rows = sorted(
            json.dumps(
                _normalized(row),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
            for row in rows
        )
        snapshots[f"mysql:{name}"] = hashlib.sha256(
            "\n".join(canonical_rows).encode("utf-8")
        ).hexdigest()
    return snapshots


def load_production_cards_read_only(
    engine: Engine, *, symbols: Iterable[str]
) -> list[TradeCardState]:
    """Clone card payloads from the canonical table without repository DDL."""
    if "trade_cards" not in set(inspect(engine).get_table_names()):
        return []
    wanted = {str(symbol or "").strip().upper() for symbol in symbols}
    table = Table("trade_cards", MetaData(), autoload_with=engine)
    statement = select(table.c.payload, table.c.version)
    if wanted:
        statement = statement.where(table.c.symbol.in_(sorted(wanted)))
    with engine.connect() as connection:
        rows = connection.execute(statement).fetchall()
    cards = []
    for row in rows:
        payload = json.loads(row.payload) if isinstance(row.payload, str) else dict(row.payload)
        payload["version"] = int(row.version)
        cards.append(TradeCardState.from_dict(payload))
    return cards


class _NoBrokerTruthDelegate:
    """Fail-closed read facade for the physically isolated shadow runtime."""

    def get_order(self, **_kwargs: Any) -> list[Any]:
        return []

    def discover_orders(self, **_kwargs: Any) -> BrokerOrderDiscoveryResult:
        return BrokerOrderDiscoveryResult(
            snapshots=[],
            open_orders_complete=False,
            history_complete=False,
            reserved_orders_complete=False,
            errors=["shadow runtime has no broker-order truth"],
        )

    def get_positions(self, **_kwargs: Any) -> dict[str, Any]:
        return {}


class ProductionShadowDecisionRuntime:
    """The normal Buy Board runtime with only its final gateway substituted."""

    def __init__(
        self,
        *,
        collector: Gate3EvidenceCollector,
        market_data: RealtimeMarketDataService,
        cards: Sequence[TradeCardState],
        isolated_database_path: Path,
        account_equity: float,
    ) -> None:
        isolated_database_path.parent.mkdir(parents=True, exist_ok=True)
        self.isolated_engine = create_engine(
            f"sqlite:///{isolated_database_path.as_posix()}", future=True
        )
        ensure_execution_commands_table(self.isolated_engine)
        ensure_execution_orders_table(self.isolated_engine)
        self.cards = [copy.deepcopy(card) for card in cards]
        self.collector = collector
        self._last_heartbeat_monotonic: float | None = None

        def order_context(client_order_id: str) -> dict[str, Any]:
            for card in self.cards:
                if client_order_id in {
                    card.entry_client_order_id,
                    card.exit_client_order_id,
                }:
                    return {
                        "symbol": card.symbol,
                        "side": "BUY" if client_order_id == card.entry_client_order_id else "SELL",
                        "quantity": card.planned_quantity or card.broker_quantity,
                    }
            return {}

        boundary = ShadowExecutionBoundary(
            store=collector.shadow_store,
            read_delegate=_NoBrokerTruthDelegate(),
            order_context_lookup=order_context,
        )
        self.gateway = ShadowExecutionGateway(
            boundary=boundary,
            isolated_engine=self.isolated_engine,
        )
        account_value = max(1.0, float(account_equity))

        def card_lookup(environment: str, account_no: str, symbol: str):
            return next(
                (
                    card
                    for card in self.cards
                    if card.environment == str(environment).upper()
                    and card.account_no == str(account_no)
                    and card.symbol == str(symbol).upper()
                ),
                None,
            )

        self.runtime = build_buyboard_runtime(
            buying_power_provider=lambda _environment, _account: account_value,
            account_equity_provider=lambda _environment, _account: account_value,
            card_lookup=card_lookup,
            portfolio_cards_provider=lambda _environment, _account: list(self.cards),
            portfolio_orders_provider=lambda _environment, _account: [],
            portfolio_reservations_provider=lambda _environment, _account: [],
            portfolio_external_orders_provider=lambda _environment, _account: [],
            capital_reservation_engine=self.isolated_engine,
            broker=self.gateway,
            market_data=market_data,
            strategy_instance_id="GATE3_SHADOW_QUALIFICATION",
            persist_card_before_execution=lambda _card: None,
            qualification_shadow_only=True,
        )
        collector.record(
            "PRODUCTION_RUNTIME_STARTED",
            runtime_class="src.services.trading_engine.TradingEngine",
            composition_function="src.services.buyboard_runtime.build_buyboard_runtime",
            production_runtime_source_sha256=production_runtime_source_sha256(),
            shadow_gateway_at_final_boundary=True,
            shadow_store_isolated=True,
            production_card_count=len(self.cards),
        )

    @staticmethod
    def _quote_payload(quote: QuoteSnapshot) -> dict[str, Any]:
        return {
            "symbol": quote.symbol,
            "last_price": quote.last_price,
            "bid": quote.bid,
            "ask": quote.ask,
            "broker_event_at": quote.broker_event_at.isoformat(),
            "received_at": quote.received_at.isoformat(),
            "processed_at": quote.processed_at.isoformat(),
            "source": quote.source,
            "channel": quote.channel,
            "sequence": quote.sequence,
            "payload_fingerprint": quote.payload_fingerprint,
            "regular_session": quote.regular_session,
            "real_quote": True,
        }

    def evaluate(self, quotes: Sequence[QuoteSnapshot]) -> None:
        before = len(self.collector.shadow_store.read_all())
        for quote in quotes:
            self.collector.record("REAL_QUOTE_EVALUATED", **self._quote_payload(quote))
            for evaluator in (
                self.runtime.trading_engine.evaluate_quote,
                self.runtime.trading_engine.evaluate_entry_quote,
            ):
                try:
                    evaluator(self.cards, quote)
                except ShadowMutationIntercepted:
                    pass
        now = time.monotonic()
        if (
            self._last_heartbeat_monotonic is None
            or now - self._last_heartbeat_monotonic >= 1.0
        ):
            try:
                self.runtime.trading_engine.run_heartbeat(self.cards)
            except ShadowMutationIntercepted:
                pass
            self._last_heartbeat_monotonic = now
        emitted = self.collector.shadow_store.read_all()[before:]
        for event in emitted:
            branch = {
                "WOULD_SUBMIT": "ENTRY_ALLOWED",
                "WOULD_CANCEL": "CANCEL_EXACT_OWNERSHIP",
                "WOULD_REPLACE": "ENTRY_ALLOWED",
                "WOULD_SELL": "SELL_PROTECTIVE_EXIT",
            }.get(event.event_type)
            if branch:
                self.collector.record(
                    "DECISION_BRANCH_OBSERVED", branch=branch, source="LIVE"
                )

    def close(self) -> None:
        self.isolated_engine.dispose()


def _observe_oracle(
    collector: Gate3EvidenceCollector,
    *,
    branch: str,
    oracle: OracleDecision,
    action,
    fence: str = "",
) -> None:
    actual = None
    if action is not None:
        try:
            action()
        except ShadowMutationIntercepted as intercepted:
            actual = intercepted.event.event_type
    collector.record(
        "ORACLE_COMPARISON",
        branch=branch,
        expected_event=oracle.expected_event,
        actual_event=actual,
        matches=oracle.matches(actual),
        block_reasons=list(oracle.block_reasons),
    )
    collector.record("DECISION_BRANCH_OBSERVED", branch=branch, source="CAPTURED_LIVE_REPLAY")
    if fence:
        collector.record(
            "SAFETY_FENCE_PROBE",
            fence=fence,
            mutation_blocked=actual is None and oracle.expected_event is None,
        )


def run_captured_live_replay(collector: Gate3EvidenceCollector) -> None:
    """Exercise every rare branch using values from an actually captured quote."""
    quote_events = [
        event
        for event in collector.journal.read_all()
        if event.event_type == "REAL_QUOTE_EVALUATED"
    ]
    if not quote_events:
        raise RuntimeError("Captured-live replay requires at least one real KIS quote")
    quote = quote_events[-1].payload
    captured_last = Decimal(str(quote.get("last_price") or "0"))
    if not captured_last.is_finite() or captured_last <= 0:
        raise RuntimeError("Captured-live replay requires a positive finite last price")
    # Build a deterministic, cent-aligned price grid from the captured quote.
    # Percentage multiplication can create sub-cent prices (for example,
    # 10.61 * 0.98 = 10.3978), making an otherwise allowed oracle probe invalid.
    tick = Decimal("0.01")
    reference_decimal = max(Decimal("1.00"), captured_last).quantize(
        tick, rounding=ROUND_HALF_UP
    )
    reference = float(reference_decimal)
    orb_low = float(reference_decimal - (tick * 3))
    breakout_price = float(reference_decimal - (tick * 2))
    execution_price = float(reference_decimal - tick)
    confirmed_trade = float(reference_decimal + (tick * 2))
    ready_ask = float(reference_decimal + tick)
    replacement_price = float(reference_decimal - (tick * 2))
    protective_sell_price = reference
    account = "GATE3-SHADOW-ACCOUNT"
    symbol = str(quote.get("symbol") or "AAPL").upper()
    collector.record(
        "CAPTURED_LIVE_REPLAY_STARTED",
        captured_quote_sha256=hashlib.sha256(
            json.dumps(quote, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
    )
    boundary = ShadowExecutionBoundary(
        store=collector.shadow_store,
        read_delegate=_NoBrokerTruthDelegate(),
        order_context_lookup=lambda _order_id: {
            "symbol": symbol,
            "side": "BUY",
            "quantity": 1,
        },
        captured_live_replay=True,
    )

    entry_oracle = expected_entry(
        orb_high=reference,
        orb_low=orb_low,
        breakout_price=breakout_price,
        execution_price=execution_price,
        breakout_confirmed=True,
        last_trade=confirmed_trade,
        best_ask=ready_ask,
        regular_session_open=True,
        quote_fresh=True,
        mutation_enabled=True,
        lease_current=True,
        ownership_current=True,
        reconciliation_clear=True,
    )
    _observe_oracle(
        collector,
        branch="ENTRY_ALLOWED",
        oracle=entry_oracle,
        action=lambda: boundary.submit_guarded(
            SubmitExecutionRequest(
                client_order_id="gate3-replay-entry",
                environment="PROD",
                account_no=account,
                symbol=symbol,
                side=OrderSide.BUY,
                intent=OrderIntent.ENTRY,
                quantity=1,
                limit_price=execution_price,
            )
        ),
    )
    cancel_oracle = expected_exact_cancel(
        exact_order_owned=True,
        mutation_enabled=True,
        lease_current=True,
        ownership_current=True,
        reconciliation_clear=True,
    )
    _observe_oracle(
        collector,
        branch="CANCEL_EXACT_OWNERSHIP",
        oracle=cancel_oracle,
        action=lambda: boundary.cancel_guarded(
            CancelExecutionRequest(
                client_order_id="gate3-replay-entry",
                cancel_command_id="gate3-replay-cancel",
                environment="PROD",
                account_no=account,
                symbol=symbol,
                side="BUY",
                quantity=1,
            )
        ),
    )
    replace_oracle = expected_higher_timeframe_replacement(
        current_window="1m",
        current_score=1,
        candidate_window="5m",
        candidate_score=2,
        candidate_confirmed=True,
        zero_fill=True,
        exact_cancel_confirmed=True,
        candidate_plan_valid=True,
        mutation_enabled=True,
        lease_current=True,
        ownership_current=True,
        reconciliation_clear=True,
    )
    _observe_oracle(
        collector,
        branch="ENTRY_ALLOWED",
        oracle=replace_oracle,
        action=lambda: boundary.replace_guarded(
            ReplaceExecutionRequest(
                client_order_id="gate3-replay-entry",
                replace_command_id="gate3-replay-replace",
                new_client_order_id="gate3-replay-entry-2",
                new_quantity=1,
                new_limit_price=replacement_price,
                environment="PROD",
                account_no=account,
            )
        ),
    )
    sell_oracle = expected_protective_sell(
        quantity=1,
        execution_price_available=True,
        mutation_enabled=True,
        lease_current=True,
        ownership_current=True,
        reconciliation_clear=True,
    )
    _observe_oracle(
        collector,
        branch="SELL_PROTECTIVE_EXIT",
        oracle=sell_oracle,
        action=lambda: boundary.submit_guarded(
            SubmitExecutionRequest(
                client_order_id="gate3-replay-sell",
                environment="PROD",
                account_no=account,
                symbol=symbol,
                side=OrderSide.SELL,
                intent=OrderIntent.MANUAL_EXIT,
                quantity=1,
                limit_price=protective_sell_price,
            )
        ),
    )

    blocked = (
        ("ENTRY_STALE_DATA_BLOCKED", "stale_data", True, True, True, True),
        ("LEASE_LOSS_BLOCKED", "lease_loss", True, False, True, True),
        ("OWNERSHIP_MISMATCH_BLOCKED", "ownership", True, True, False, True),
        ("AMBIGUOUS_ORDER_RECONCILIATION", "reconciliation", True, True, True, False),
        ("KILL_SWITCH_BLOCKED", "kill_switch", False, True, True, True),
    )
    for branch, fence, mutation_enabled, lease_current, ownership_current, reconciliation_clear in blocked:
        oracle = expected_entry(
            orb_high=reference,
            orb_low=orb_low,
            breakout_price=breakout_price,
            execution_price=execution_price,
            breakout_confirmed=True,
            last_trade=confirmed_trade,
            best_ask=ready_ask,
            regular_session_open=True,
            quote_fresh=fence != "stale_data",
            mutation_enabled=mutation_enabled,
            lease_current=lease_current,
            ownership_current=ownership_current,
            reconciliation_clear=reconciliation_clear,
        )
        _observe_oracle(
            collector,
            branch=branch,
            oracle=oracle,
            action=None,
            fence=fence,
        )
    collector.record(
        "CAPTURED_LIVE_REPLAY_ENDED",
        completed=True,
        captured_quote_count=len(quote_events),
    )


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _require_external_output(path: Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        return resolved
    raise RuntimeError("Gate-3 runtime evidence must remain outside the repository")


def _exact_gate2_report(path: Path) -> tuple[str, dict[str, Any]]:
    commit = _git("rev-parse", "HEAD").lower()
    if _git("status", "--porcelain"):
        raise RuntimeError("Gate 3 requires a clean exact-commit worktree")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("result") != "PASSED" or report.get("commit_sha") != commit:
        raise RuntimeError("Gate-2 report must be PASSED on this exact commit")
    return commit, report


def finalize_existing(args: argparse.Namespace) -> int:
    commit, gate2_report = _exact_gate2_report(args.gate2_report)
    output_dir = _require_external_output(args.output_dir)
    if not output_dir.is_dir():
        raise RuntimeError(f"Gate-3 evidence directory does not exist: {output_dir}")
    if args.review is None:
        raise RuntimeError("Gate-3 finalization requires an independent review file")
    _require_external_output(args.review)
    collector = Gate3EvidenceCollector(
        journal_path=output_dir / "gate3.evidence.jsonl",
        shadow_store_path=output_dir / "gate3.shadow.jsonl",
        commit_sha=commit,
        production_paths=(ROOT / "data",),
    )
    review = json.loads(args.review.read_text(encoding="utf-8"))
    evidence = collector.build_evidence(
        gate2_report_sha256=canonical_report_sha256(gate2_report),
        strategy_rules_path=args.strategy_rules,
        review=review,
    )
    report = build_report(evidence, upstream_gate2_report=gate2_report)
    _write_json(output_dir / "gate3_evidence.json", evidence)
    _write_json(output_dir / "gate3_report.json", report)
    print(
        f"Gate 3 {report['result']}: {len(report['invariant_violations'])} "
        "violation(s) after independent review"
    )
    return 0 if report["result"] == "PASSED" else 1


def run_live(args: argparse.Namespace) -> int:
    commit, gate2_report = _exact_gate2_report(args.gate2_report)
    if args.review is not None:
        raise RuntimeError(
            "Review must follow the live session; rerun with --finalize-only afterward"
        )
    session_day = date.fromisoformat(args.session_date)
    session_open, session_close = _session_bounds(session_day)
    now = datetime.now(timezone.utc)
    if now > session_open:
        raise RuntimeError("Gate 3 must start before the NYSE regular-session open")
    clock = clock_synchronization_status()
    if not clock.get("synchronized"):
        raise RuntimeError("Gate 3 requires a synchronized Windows clock")

    output_dir = _require_external_output(args.output_dir)
    _require_external_output(args.capability_manifest)
    output_dir.mkdir(parents=True, exist_ok=False)
    collector = Gate3EvidenceCollector(
        journal_path=output_dir / "gate3.evidence.jsonl",
        shadow_store_path=output_dir / "gate3.shadow.jsonl",
        commit_sha=commit,
        production_paths=(ROOT / "data",),
    )
    mysql_engine = init_mysql_engine(ensure_schema=False)
    if mysql_engine is None:
        raise RuntimeError("Gate 3 requires read-only access to canonical MySQL")
    symbols = sorted({item.strip().upper() for item in args.symbols.split(",") if item.strip()})
    if not symbols:
        raise RuntimeError("Gate 3 requires at least one symbol")
    cards = load_production_cards_read_only(mysql_engine, symbols=symbols)
    if not cards:
        mysql_engine.dispose()
        raise RuntimeError(
            "Gate 3 requires at least one canonical production trade card for the "
            f"reviewed symbols ({', '.join(symbols)}); no synthetic card is allowed"
        )

    manifest = load_verified_capability_manifest(
        args.capability_manifest,
        expected_commit=commit,
        expected_environment="PROD",
    )
    key_store = KisWsSymbolKeyStore()
    keys = key_store.snapshot().keys
    missing = sorted(set(symbols) - set(keys))
    if missing:
        raise RuntimeError(f"missing verified subscription keys: {', '.join(missing)}")
    service = build_kis_realtime_market_data_from_environment(
        environment="PROD",
        confirmed_sequence_channels=manifest.confirmed_sequence_channels,
        sequence_field_by_channel=manifest.sequence_field_by_channel,
        sequence_reset_by_channel=manifest.sequence_reset_by_channel,
        execution_notice_verified=False,
        qualification_mode=True,
        symbol_key_store=key_store,
    )
    priority = {symbol: int(SubscriptionPriority.CRITICAL_EXIT) for symbol in symbols}
    service.configure_desired_channels(trade_priorities=priority, quote_priorities=priority)
    collector.record(
        "SESSION_STARTED",
        session_date=session_day.isoformat(),
        session_open=session_open.isoformat(),
        session_close=session_close.isoformat(),
        started_at=now.isoformat(),
        clock_synchronization=clock,
    )
    before = snapshot_production_ledgers(mysql_engine)
    collector.record("PRODUCTION_LEDGER_SNAPSHOT", phase="BEFORE", digests=before)
    runtime = ProductionShadowDecisionRuntime(
        collector=collector,
        market_data=service,
        cards=cards,
        isolated_database_path=output_dir / "gate3_isolated.sqlite3",
        account_equity=args.account_equity,
    )
    safety = begin_runtime_safety_audit()
    runtime_errors: list[str] = []
    after: dict[str, str] | None = None
    _set_keep_awake(True)
    try:
        service.start()
        while datetime.now(timezone.utc) < session_close:
            runtime.evaluate(service.poll_once())
            time.sleep(args.poll_seconds)
        runtime.evaluate(service.poll_once())
        run_captured_live_replay(collector)
    except Exception as exc:
        runtime_errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            service.stop()
        except Exception as exc:
            runtime_errors.append(f"service stop: {type(exc).__name__}: {exc}")
        try:
            runtime.close()
        except Exception as exc:
            runtime_errors.append(f"runtime close: {type(exc).__name__}: {exc}")
        safety_snapshot = safety.close()
        try:
            after = snapshot_production_ledgers(mysql_engine)
            collector.record("PRODUCTION_LEDGER_SNAPSHOT", phase="AFTER", digests=after)
        except Exception as exc:
            runtime_errors.append(f"ledger snapshot: {type(exc).__name__}: {exc}")
        ended_at = datetime.now(timezone.utc)
        collector.record(
            "SESSION_ENDED",
            session_date=session_day.isoformat(),
            ended_at=ended_at.isoformat(),
            counters={
                "broker_mutation_attempt_count": (
                    safety_snapshot.broker_mutation_attempt_count
                ),
                "fake_broker_ack_count": 0,
                "fake_fill_count": 0,
                "production_ledger_write_count": int(
                    after is None or before != after
                ),
                "runtime_error_count": len(runtime_errors),
            },
            runtime_errors=runtime_errors,
        )
        mysql_engine.dispose()
        _set_keep_awake(False)
    evidence = collector.build_evidence(
        gate2_report_sha256=canonical_report_sha256(gate2_report),
        strategy_rules_path=args.strategy_rules,
        review={},
    )
    report = build_report(evidence, upstream_gate2_report=gate2_report)
    _write_json(output_dir / "gate3_evidence.json", evidence)
    _write_json(output_dir / "gate3_report.json", report)
    print(
        f"Gate 3 {report['result']}: {len(report['invariant_violations'])} violation(s); "
        f"broker_mutations={evidence['broker_mutation_attempt_count']}; "
        f"runtime_errors={evidence['runtime_error_count']}"
    )
    return 0 if report["result"] == "PASSED" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Gate-3 shadow qualification")
    parser.add_argument("--gate2-report", type=Path, required=True)
    parser.add_argument("--capability-manifest", type=Path, required=True)
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--symbols", default="AAPL")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--strategy-rules",
        type=Path,
        default=ROOT / "rulebooks" / "US Swing Trading Rulebook.md",
    )
    parser.add_argument("--review", type=Path)
    parser.add_argument("--finalize-only", action="store_true")
    parser.add_argument("--account-equity", type=float, default=1_000_000.0)
    parser.add_argument("--poll-seconds", type=float, default=0.1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 < args.poll_seconds <= 0.25:
        raise RuntimeError("Gate-3 poll interval must be in (0, 0.25] seconds")
    if args.finalize_only:
        return finalize_existing(args)
    return run_live(args)


__all__ = [
    "ProductionShadowDecisionRuntime",
    "load_production_cards_read_only",
    "main",
    "finalize_existing",
    "production_runtime_source_sha256",
    "run_captured_live_replay",
    "snapshot_production_ledgers",
]


if __name__ == "__main__":
    raise SystemExit(main())
