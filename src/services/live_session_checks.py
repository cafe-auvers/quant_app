"""Passive live-session diagnostics; never an activation-gate certificate.

Only detached inputs enter the bounded background queue. The collector has
no production database, broker, transport, or lease reference. Fault injection
and formal gate closure remain the responsibility of the qualification runners.
"""
from __future__ import annotations

import copy
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import queue
import threading
import time
from typing import Any

from src.services.realtime_market_data import RealtimeMarketDataService
from src.utils.config import ROOT_DIR, get_env_value

logger = logging.getLogger(__name__)
_observer = None
_lock = threading.Lock()


def checks_enabled() -> bool:
    return str(get_env_value("LIVE_SESSION_CHECKS_ENABLED", "false")).lower() in {
        "true", "1", "yes", "on"
    }


def prepare_card_snapshot(cards):
    """Capture pre-decision card inputs without sharing mutable state."""
    if not checks_enabled():
        return ()
    try:
        return tuple(copy.deepcopy(card.to_dict()) for card in cards)
    except Exception:
        logger.exception("Live-session card capture failed; trading continues")
        return ()


class SnapshotMarketData(RealtimeMarketDataService):
    """Detached read facade: it cannot drain, subscribe, or reconnect KIS."""

    def __init__(self):
        self.quotes = {}
        self.states = {}
        self.connected = False

    def latest_quote(self, symbol):
        return self.quotes.get(symbol.upper())

    def is_connected(self):
        return self.connected

    def is_symbol_feed_available(self, symbol, *, require_trade=True, require_quote=True):
        state = self.states.get(symbol.upper(), {})
        return bool(self.connected and
                    (not require_trade or state.get("trade_acked")) and
                    (not require_quote or state.get("quote_acked")) and
                    not state.get("last_error") and
                    not state.get("trade_error") and not state.get("quote_error") and
                    not state.get("trade_configuration_error") and
                    not state.get("quote_configuration_error") and
                    not state.get("trade_rejected_due_to_capacity") and
                    not state.get("quote_rejected_due_to_capacity"))

    def is_symbol_execution_ready(self, symbol, *, require_trade=True,
                                  require_quote=True, now=None):
        from src.core import execution_config
        quote = self.latest_quote(symbol)
        state = self.states.get(symbol.upper(), {})
        reference = now or datetime.now(timezone.utc)
        if not self.is_symbol_feed_available(symbol, require_trade=require_trade,
                                             require_quote=require_quote):
            return False
        for needed, key in ((require_trade, "last_trade_event_at"),
                            (require_quote, "last_quote_event_at")):
            stamp = state.get(key)
            if needed and (stamp is None or not
                           0 <= (reference - stamp).total_seconds() <=
                           execution_config.BROKER_EVENT_STALE_SECONDS):
                return False
        return bool(quote and quote.is_execution_fresh(now=reference))

    def entry_quote_ready(self, symbol, *, now=None):
        quote = self.latest_quote(symbol)
        return bool(self.is_symbol_execution_ready(symbol, now=now) and
                    quote and quote.ask and quote.ask > 0 and quote.last_price > 0)

    def is_symbol_trading_halted(self, symbol):
        return bool(self.states.get(symbol.upper(), {}).get("trading_halted"))


class LiveSessionChecks:
    def __init__(self, output_dir: Path, commit_sha: str, session_date: str,
                 *, queue_size: int = 128, start_thread: bool = True):
        self.output_dir = Path(output_dir).resolve()
        if self.output_dir == ROOT_DIR.resolve() or ROOT_DIR.resolve() in self.output_dir.parents:
            raise ValueError("Live-session evidence must be outside the repository")
        self.commit_sha = commit_sha
        self.session_date = session_date
        self.queue = queue.Queue(maxsize=queue_size)
        self.dropped = 0
        self.errors = Counter()
        self.event_counts = Counter()
        self.quote_count = 0
        self.regular_quote_count = 0
        self.last_metrics = {}
        self.last_cycle_at = None
        self.started_at = datetime.now(timezone.utc)
        self.market = SnapshotMarketData()
        self.shadow = None
        self._versions = {}
        self._stop = threading.Event()
        self._last_report = 0.0
        self.finished = False
        if start_thread:
            threading.Thread(target=self._run, name="PassiveLiveSessionChecks",
                             daemon=True).start()

    def enqueue(self, event_type, payload):
        if self.finished:
            return
        try:
            self.queue.put_nowait((event_type, payload))
        except queue.Full:
            self.dropped += 1

    def _initialize(self):
        from activation_gates.journal import AppendOnlyEvidenceJournal
        from gate2.reporting import _session_bounds
        from gate3.collector import Gate3EvidenceCollector
        from datetime import date
        self.session_open, self.session_close = _session_bounds(date.fromisoformat(self.session_date))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.journal = AppendOnlyEvidenceJournal(
            self.output_dir / "live_checks.evidence.jsonl",
            gate="PASSIVE_LIVE_SESSION_DIAGNOSTICS", commit_sha=self.commit_sha,
            allowed_event_types=("COLLECTOR_STARTED", "FEED_SAMPLE", "LIVE_EVENT",
                                 "COLLECTOR_ERROR", "COLLECTOR_ENDED"))
        self.collector = Gate3EvidenceCollector(
            journal_path=self.output_dir / "shadow.evidence.jsonl",
            shadow_store_path=self.output_dir / "decisions.shadow.jsonl",
            commit_sha=self.commit_sha, production_paths=(ROOT_DIR / "data",))
        self.journal.append("COLLECTOR_STARTED", {
            "session_date": self.session_date, "started_at": self.started_at.isoformat(),
            "mode": "PASSIVE_LIVE_DIAGNOSTICS", "formal_gate_pass": False,
            "session_open": self.session_open.isoformat(),
            "session_close": self.session_close.isoformat(),
            "production_trading_policy_changed_by_collector": False,
            "collector_broker_and_database_access": False})
        self.collector.record("SESSION_STARTED", session_date=self.session_date,
                              session_open=self.session_open.isoformat(),
                              session_close=self.session_close.isoformat(),
                              started_at=self.started_at.isoformat(),
                              collection_mode="PASSIVE_LIVE_DIAGNOSTICS")

    def _cycle(self, payload):
        from gate3.runner import ProductionShadowDecisionRuntime
        from src.core.trade_card_state import TradeCardState
        quotes, snapshots, feed, equity = payload
        self.last_cycle_at = feed["captured_at"]
        self.market.connected = feed["connected"]
        self.market.states = feed["states"]
        self.market.quotes = feed["latest_quotes"]
        cards = [TradeCardState.from_dict(item) for item in snapshots]
        if self.shadow is None and cards and equity > 0:
            self.shadow = ProductionShadowDecisionRuntime(
                collector=self.collector, market_data=self.market, cards=cards,
                isolated_database_path=self.output_dir / "shadow.sqlite3",
                account_equity=equity)
        if self.shadow is not None:
            # Reflect real operator edits and broker truth, never invent fills.
            retained = {card.card_key: card for card in self.shadow.cards}
            for card in cards:
                if self._versions.get(card.card_key) != card.version:
                    retained[card.card_key] = card
                    self._versions[card.card_key] = card.version
            current = {card.card_key for card in cards}
            self.shadow.cards[:] = [card for key, card in retained.items() if key in current]
            self.shadow.evaluate(quotes)
        self.quote_count += len(quotes)
        self.regular_quote_count += sum(quote.regular_session for quote in quotes)
        self.last_equity = equity
        if "metrics" in feed:
            self.last_metrics = feed["metrics"]
            self.journal.append("FEED_SAMPLE", self.last_metrics)

    def _process(self, kind, payload):
        if kind == "CYCLE":
            self._cycle(payload)
        else:
            self.event_counts[kind] += 1
            self.journal.append("LIVE_EVENT", {"event_type": kind, **payload})

    def _write_report(self, *, ended=False):
        now = datetime.now(timezone.utc)
        report = {
            "schema_version": 1, "commit_sha": self.commit_sha,
            "session_date": self.session_date, "updated_at": now.isoformat(),
            "started_at": self.started_at.isoformat(),
            "session_open": self.session_open.isoformat(),
            "session_close": self.session_close.isoformat(),
            "collector_state": "ENDED" if ended else "RUNNING",
            "mode": "PASSIVE_LIVE_DIAGNOSTICS", "formal_gate_result": "NOT_CERTIFIED",
            "dropped_batches": self.dropped, "collector_errors": dict(self.errors),
            "queue_depth": self.queue.qsize(),
            "last_cycle_at": self.last_cycle_at,
            "full_regular_session_observed": bool(
                ended and self.started_at <= self.session_open and
                self.last_cycle_at and self.last_cycle_at >= self.session_close and
                self.regular_quote_count and not self.dropped and not self.errors and
                self.queue.empty()),
            "gate2": {"feed_samples": self.last_metrics,
                      "latency_scope": "runtime cumulative; includes premarket",
                      "accepted_quote_count": self.quote_count,
                      "regular_quote_count": self.regular_quote_count,
                      "missing_qualification": ["read-only full-session run",
                                                "forced reconnect and silent-channel probes"]},
            "gate3": {"shadow_runtime_active": self.shadow is not None,
                      "shadow_card_count": len(self.shadow.cards) if self.shadow else 0,
                      "shadow_equity_usd": getattr(self, "last_equity", 0),
                      "shadow_equity_mode": "minimum observed account equity",
                      "shadow_decisions": self.collector.shadow_store.audit().event_counts,
                      "broker_access": False, "production_database_access": False,
                      "missing_qualification": ["complete branch and fence replay", "reviewed Gate-2 chain"]},
            "gate4": {"actual_runtime_event_counts": dict(self.event_counts),
                      "missing_qualification": ["reviewed Gate-3 chain", "three supervised session dates",
                                                "complete trade lifecycle and disarm probe"]},
        }
        path = self.output_dir / "checks_report.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _run(self):
        try:
            self._initialize()
            while not self._stop.is_set():
                try:
                    kind, payload = self.queue.get(timeout=0.5)
                except queue.Empty:
                    pass
                else:
                    try:
                        self._process(kind, payload)
                    except Exception as exc:
                        self.errors[type(exc).__name__] += 1
                        self.journal.append("COLLECTOR_ERROR", {"error_type": type(exc).__name__})
                    finally:
                        self.queue.task_done()
                if time.monotonic() - self._last_report >= 10:
                    self._write_report()
                    self._last_report = time.monotonic()
                if datetime.now(timezone.utc) >= self.session_close + timedelta(seconds=10):
                    break
            self.finished = True
            while not self.queue.empty():
                kind, payload = self.queue.get_nowait()
                try:
                    self._process(kind, payload)
                except Exception as exc:
                    self.errors[type(exc).__name__] += 1
                finally:
                    self.queue.task_done()
            self.collector.record("SESSION_ENDED", session_date=self.session_date,
                                  ended_at=datetime.now(timezone.utc).isoformat(),
                                  counters={"broker_mutation_attempt_count": 0,
                                            "fake_broker_ack_count": 0, "fake_fill_count": 0,
                                            "production_ledger_write_count": 0,
                                            "runtime_error_count": sum(self.errors.values()),
                                            "observer_dropped_batch_count": self.dropped})
            self.journal.append("COLLECTOR_ENDED", {"session_date": self.session_date,
                                "dropped_batches": self.dropped, "errors": dict(self.errors)})
            self._write_report(ended=True)
        except Exception:
            self.finished = True
            logger.exception("Live-session collector stopped; trading continues")
        finally:
            if self.shadow is not None:
                self.shadow.close()


def configured_observer():
    global _observer
    if not checks_enabled():
        return None
    with _lock:
        if _observer is None:
            _observer = LiveSessionChecks(
                Path(get_env_value("LIVE_SESSION_EVIDENCE_DIR", "")),
                str(get_env_value("KIS_RUNTIME_COMMIT_SHA", "")),
                str(get_env_value("LIVE_SESSION_DATE", "")))
        return _observer


def observe_cycle(service, quotes, snapshots, equity_provider):
    """Nonblocking handoff after production has finished its decision cycle."""
    try:
        observer = configured_observer()
        if observer is None or observer.finished:
            return
        symbols = {item["symbol"] for item in snapshots}
        feed = {"captured_at": datetime.now(timezone.utc),
                "connected": service.is_connected(),
                "states": {symbol: asdict(service.symbol_state(symbol)) for symbol in symbols},
                "latest_quotes": {symbol: service.latest_quote(symbol) for symbol in symbols}}
        if time.monotonic() - getattr(observer, "_last_sample", 0) >= 10:
            feed["metrics"] = {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "health": asdict(service.health_metrics()),
                "protocol": asdict(service.protocol_metrics_snapshot()),
                "capacity": asdict(service.subscription_capacity_snapshot())}
            observer._last_sample = time.monotonic()
        accounts = {(item["environment"], item["account_no"]) for item in snapshots}
        equity = min((float(equity_provider(*account)) for account in accounts), default=0)
        observer.enqueue("CYCLE", (tuple(quotes), snapshots, feed, equity))
    except Exception as exc:
        if _observer is not None:
            key = "input_capture:" + type(exc).__name__
            _observer.errors[key] += 1
            if _observer.errors[key] == 1:
                logger.exception("Live-session observation failed; trading continues")
        else:
            logger.exception("Live-session observation failed; trading continues")


def observe_execution_event(event_type: str, payload: dict[str, Any]):
    try:
        observer = configured_observer()
        if observer is not None:
            observer.enqueue(event_type, copy.deepcopy(payload))
    except Exception:
        logger.exception("Live-session execution capture failed; trading continues")
