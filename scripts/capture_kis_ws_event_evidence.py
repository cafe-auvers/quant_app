"""Capture public KIS trade/quote frames for capability review.

This is a deliberately narrow qualification utility.  It issues a WebSocket
approval key, subscribes to HDFSCNT0/HDFSASP0 for one already-verified symbol
key, records public market-data frames, and exits.  It does not load the
production capability manifest because the purpose of this command is to
produce the raw evidence from which that manifest is reviewed.

It never constructs a broker, never calls an order endpoint, never subscribes
to H0GSCNI0/H0GSCNI9, and never writes credentials/approval keys to disk.
Output must be outside the repository.

Example::

    python scripts/capture_kis_ws_event_evidence.py ^
      --confirm-read-only ^
      --symbol AAPL ^
      --frames-per-channel 20 ^
      --output C:\\quant_evidence\\aapl_ws_frames.json
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_repo_env() -> None:
    from src.utils.config import install_repository_configuration

    install_repository_configuration()


_load_repo_env()

from src.api.kis_websocket import (  # noqa: E402
    KisWebSocketClient,
    KisWsDataFrame,
    KisWsSubscription,
    KisWsSystemFrame,
)
from src.api.kis_ws_auth import KisWsApprovalKeyProvider  # noqa: E402
from src.services.kis_realtime_market_data import (  # noqa: E402
    QUOTE_COLUMNS,
    QUOTE_TR_ID,
    TRADE_COLUMNS,
    TRADE_TR_ID,
)
from src.services.kis_ws_symbol_keys import KisWsSymbolKeyStore  # noqa: E402


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _git_snapshot() -> str:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise RuntimeError(
            "evidence capture requires a clean exact commit; commit or discard local changes first"
        )
    return head


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_external_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        pass
    else:
        raise RuntimeError("evidence output must remain outside the repository")
    if resolved.exists():
        raise RuntimeError(f"evidence output already exists: {resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _symbol_keys() -> dict[str, str]:
    snapshot = KisWsSymbolKeyStore().snapshot()
    if snapshot.last_error:
        raise RuntimeError(snapshot.last_error)
    return dict(snapshot.keys)


def _split_records(frame: KisWsDataFrame, columns: Sequence[str]) -> list[dict[str, str]]:
    values = frame.payload.split("^")
    width = len(columns)
    expected = int(frame.record_count) * width
    if len(values) != expected:
        raise RuntimeError(
            f"{frame.tr_id} payload width mismatch: values={len(values)} expected={expected}"
        )
    return [
        dict(zip(columns, values[offset : offset + width]))
        for offset in range(0, expected, width)
    ]


def _numeric(value: str) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _field_observations(records: Sequence[Mapping[str, str]]) -> dict[str, dict[str, Any]]:
    """Describe facts useful to a reviewer without assigning semantics."""

    if not records:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for field in records[0]:
        values = [str(record.get(field, "")) for record in records]
        numeric = [_numeric(value) for value in values]
        numeric_values = [value for value in numeric if value is not None]
        item: dict[str, Any] = {
            "sample_count": len(values),
            "nonempty_count": sum(bool(value) for value in values),
            "distinct_count": len(set(values)),
            "first": values[0],
            "last": values[-1],
        }
        if len(numeric_values) == len(values) and numeric_values:
            item.update(
                {
                    "numeric": True,
                    "nondecreasing_in_capture_order": all(
                        current >= previous
                        for previous, current in zip(
                            numeric_values, numeric_values[1:]
                        )
                    ),
                    "strict_increase_count": sum(
                        current > previous
                        for previous, current in zip(
                            numeric_values, numeric_values[1:]
                        )
                    ),
                }
            )
        else:
            item["numeric"] = False
        result[field] = item
    return result


def capture(
    *,
    symbol: str,
    output: Path,
    frames_per_channel: int,
    timeout_seconds: float,
    reconnect_after_seconds: float | None = None,
) -> dict[str, Any]:
    commit_sha = _git_snapshot()
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    keys = _symbol_keys()
    tr_key = keys.get(symbol, "")
    if not tr_key:
        raise RuntimeError(
            f"no already-verified KIS WebSocket key is configured for {symbol}"
        )

    required_env = (
        "KIS_PROD_BASE_URL",
        "KIS_PROD_APP_KEY",
        "KIS_PROD_APP_SECRET",
        "KIS_PROD_WS_URL",
    )
    missing = [name for name in required_env if not str(os.getenv(name, "")).strip()]
    if missing:
        raise RuntimeError("missing configuration: " + ", ".join(missing))

    target = max(1, int(frames_per_channel))
    timeout = max(5.0, float(timeout_seconds))
    lock = threading.Lock()
    done = threading.Event()
    fatal = threading.Event()
    connection_events: list[dict[str, Any]] = []
    acknowledgements: list[dict[str, Any]] = []
    protocol_operations: list[dict[str, Any]] = []
    frames: dict[str, list[dict[str, Any]]] = {
        TRADE_TR_ID: [],
        QUOTE_TR_ID: [],
    }
    records: dict[str, list[dict[str, str]]] = {
        TRADE_TR_ID: [],
        QUOTE_TR_ID: [],
    }
    errors: list[str] = []
    current_generation = 0
    initial_generation = 0
    latest_operation_generation: dict[tuple[str, str], int] = {}
    reconnect_requested_at: datetime | None = None
    reconnect_reacked_at: datetime | None = None
    reconnect_recovery_seconds: float | None = None
    post_reconnect_data_channels: set[str] = set()

    reconnect_offset = (
        None
        if reconnect_after_seconds is None
        else max(1.0, float(reconnect_after_seconds))
    )

    def capture_complete() -> bool:
        frames_complete = all(
            len(frames[tr_id]) >= target for tr_id in (TRADE_TR_ID, QUOTE_TR_ID)
        )
        if not frames_complete:
            return False
        if reconnect_offset is None:
            return True
        return bool(
            reconnect_requested_at is not None
            and reconnect_reacked_at is not None
            and post_reconnect_data_channels == {TRADE_TR_ID, QUOTE_TR_ID}
        )

    def sensitive_value_audit(_value: str) -> None:
        # Positive proof that approval issuance occurred, without retaining the
        # issued secret anywhere in the evidence object.
        return None

    provider = KisWsApprovalKeyProvider(
        base_url=os.getenv("KIS_PROD_BASE_URL", ""),
        app_key=os.getenv("KIS_PROD_APP_KEY", ""),
        app_secret=os.getenv("KIS_PROD_APP_SECRET", ""),
        ttl_seconds=float(os.getenv("KIS_WS_APPROVAL_KEY_TTL_SECONDS", "82800") or 82800),
        max_retries=1,
        # This true value authorizes only the already-proven approval/subscription
        # transport mechanics inside this dedicated read-only collector. It does
        # not set KIS_WS_PROTOCOL_VERIFIED and is never used by production
        # composition. The output itself remains unreviewed evidence.
        protocol_verified=True,
        sensitive_value_audit=sensitive_value_audit,
        critical_alert=lambda message: (errors.append(str(message)), fatal.set()),
    )
    client = KisWebSocketClient(
        url=os.getenv("KIS_PROD_WS_URL", ""),
        approval_keys=provider,
        reconnect_initial_seconds=1.0,
        reconnect_max_seconds=5.0,
        reconnect_jitter_seconds=0.0,
        critical_alert=lambda message: (errors.append(str(message)), fatal.set()),
    )

    def on_connection(connected: bool, reason: str, generation: int) -> None:
        nonlocal current_generation, initial_generation
        with lock:
            current_generation = int(generation)
            if connected and initial_generation <= 0:
                initial_generation = current_generation
            connection_events.append(
                {
                    "connected": bool(connected),
                    "generation": int(generation),
                    "observed_at": _iso(datetime.now(timezone.utc)),
                    "reason_present": bool(reason),
                    "reason_sha256": (
                        hashlib.sha256(str(reason).encode("utf-8")).hexdigest()
                        if reason
                        else ""
                    ),
                }
            )

    def on_operation(operation) -> None:
        with lock:
            generation = int(operation.generation)
            latest_operation_generation[
                (operation.tr_id, operation.tr_key)
            ] = generation
            protocol_operations.append(
                {
                    "generation": generation,
                    "action": operation.action,
                    "tr_id": operation.tr_id,
                    "tr_key": operation.tr_key,
                    "sent_at": _iso(operation.sent_at),
                }
            )

    def on_ack(frame: KisWsSystemFrame) -> None:
        nonlocal reconnect_reacked_at, reconnect_recovery_seconds
        if frame.tr_id not in {TRADE_TR_ID, QUOTE_TR_ID}:
            return
        with lock:
            generation = latest_operation_generation.get(
                (frame.tr_id, frame.tr_key), current_generation
            )
            observed_at = datetime.now(timezone.utc)
            acknowledgements.append(
                {
                    "tr_id": frame.tr_id,
                    "tr_key": frame.tr_key,
                    "generation": generation,
                    "accepted": bool(frame.accepted),
                    "message": frame.message,
                    "observed_at": _iso(observed_at),
                }
            )
            if not frame.accepted:
                errors.append(
                    f"subscription rejected for {frame.tr_id}: {frame.message}"
                )
                fatal.set()
            elif (
                reconnect_requested_at is not None
                and generation > initial_generation
            ):
                reacked_channels = {
                    item["tr_id"]
                    for item in acknowledgements
                    if item.get("accepted") is True
                    and int(item.get("generation") or 0) == generation
                }
                if reacked_channels == {TRADE_TR_ID, QUOTE_TR_ID}:
                    reconnect_reacked_at = observed_at
                    reconnect_recovery_seconds = (
                        observed_at - reconnect_requested_at
                    ).total_seconds()
                    if capture_complete():
                        done.set()

    def on_data(frame: KisWsDataFrame) -> None:
        columns = (
            TRADE_COLUMNS
            if frame.tr_id == TRADE_TR_ID
            else QUOTE_COLUMNS
            if frame.tr_id == QUOTE_TR_ID
            else None
        )
        if columns is None:
            return
        try:
            parsed = _split_records(frame, columns)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"{frame.tr_id} parse failure: {exc}")
            fatal.set()
            return
        with lock:
            is_post_reconnect = bool(
                reconnect_requested_at is not None
                and current_generation > initial_generation
            )
            already_has_post_reconnect_frame = any(
                int(item.get("generation") or 0) > initial_generation
                for item in frames[frame.tr_id]
            )
            if len(frames[frame.tr_id]) < target or (
                is_post_reconnect and not already_has_post_reconnect_frame
            ):
                frames[frame.tr_id].append(
                    {
                        "generation": current_generation,
                        "received_at": _iso(frame.received_at),
                        "record_count": frame.record_count,
                        "payload_fingerprint": frame.payload_fingerprint,
                        "records": parsed,
                    }
                )
                records[frame.tr_id].extend(parsed)
            if is_post_reconnect:
                post_reconnect_data_channels.add(frame.tr_id)
            if capture_complete():
                done.set()

    client.on_connection(on_connection)
    client.on_operation(on_operation)
    client.on_ack(on_ack)
    client.on_data(on_data)
    client.subscribe(
        [
            KisWsSubscription(TRADE_TR_ID, tr_key, symbol, "TRADE"),
            KisWsSubscription(QUOTE_TR_ID, tr_key, symbol, "QUOTE"),
        ]
    )

    started_at = datetime.now(timezone.utc)
    client.start()
    started_monotonic = time.monotonic()
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and not done.is_set() and not fatal.is_set():
            should_reconnect = False
            with lock:
                elapsed = time.monotonic() - started_monotonic
                initial_acks = {
                    item["tr_id"]
                    for item in acknowledgements
                    if item.get("accepted") is True
                    and int(item.get("generation") or 0) == initial_generation
                }
                healthy_initial_session = bool(
                    initial_generation > 0
                    and initial_acks == {TRADE_TR_ID, QUOTE_TR_ID}
                    and all(frames[tr_id] for tr_id in (TRADE_TR_ID, QUOTE_TR_ID))
                )
                if (
                    reconnect_offset is not None
                    and reconnect_requested_at is None
                    and elapsed >= reconnect_offset
                    and healthy_initial_session
                ):
                    reconnect_requested_at = datetime.now(timezone.utc)
                    should_reconnect = True
            if should_reconnect:
                loop = getattr(client, "_loop", None)
                if loop is None:
                    with lock:
                        errors.append(
                            "forced reconnect requested before the event loop was ready"
                        )
                    fatal.set()
                    continue
                asyncio.run_coroutine_threadsafe(client.reconnect(), loop)
            time.sleep(0.1)
    finally:
        client.stop()
    ended_at = datetime.now(timezone.utc)

    with lock:
        acked = {
            item["tr_id"]
            for item in acknowledgements
            if item.get("accepted") is True
        }
        counts = {tr_id: len(items) for tr_id, items in frames.items()}
        evidence = {
            "schema_version": 1,
            "evidence_kind": "KIS_WS_PUBLIC_EVENT_CAPTURE",
            "qualification_only": True,
            "review_status": "UNREVIEWED",
            "commit_sha": commit_sha,
            "environment": "PROD",
            "symbol": symbol,
            "tr_key": tr_key,
            "tr_ids": [TRADE_TR_ID, QUOTE_TR_ID],
            "started_at": _iso(started_at),
            "ended_at": _iso(ended_at),
            "broker_mutations": 0,
            "execution_notice_subscribed": False,
            "credentials_persisted": False,
            "approval_key_persisted": False,
            "connection_events": list(connection_events),
            "subscription_acknowledgements": list(acknowledgements),
            "protocol_operations": list(protocol_operations),
            "forced_reconnect": {
                "configured": reconnect_offset is not None,
                "requested_at": (
                    _iso(reconnect_requested_at)
                    if reconnect_requested_at is not None
                    else None
                ),
                "reacked_at": (
                    _iso(reconnect_reacked_at)
                    if reconnect_reacked_at is not None
                    else None
                ),
                "recovery_seconds": reconnect_recovery_seconds,
                "post_reconnect_data_channels": sorted(
                    post_reconnect_data_channels
                ),
            },
            "frame_counts": counts,
            "frames": {tr_id: list(items) for tr_id, items in frames.items()},
            "field_observations": {
                tr_id: _field_observations(records[tr_id])
                for tr_id in (TRADE_TR_ID, QUOTE_TR_ID)
            },
            "errors": list(errors),
        }

    missing_acks = sorted({TRADE_TR_ID, QUOTE_TR_ID} - acked)
    incomplete = [tr_id for tr_id, count in counts.items() if count < target]
    if missing_acks:
        evidence["errors"].append(
            "missing accepted subscription ACK(s): " + ", ".join(missing_acks)
        )
    if incomplete:
        evidence["errors"].append(
            "capture timed out before target frame count for: "
            + ", ".join(incomplete)
        )
    if reconnect_offset is not None:
        if reconnect_requested_at is None:
            evidence["errors"].append("forced reconnect was not injected")
        elif reconnect_reacked_at is None:
            evidence["errors"].append(
                "forced reconnect did not re-ACK both subscriptions"
            )
        if post_reconnect_data_channels != {TRADE_TR_ID, QUOTE_TR_ID}:
            evidence["errors"].append(
                "post-reconnect data did not resume on both channels"
            )

    output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return evidence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture read-only regular-session KIS trade/quote evidence"
    )
    parser.add_argument("--confirm-read-only", action="store_true")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--frames-per-channel", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--reconnect-after-seconds",
        type=float,
        help="inject one read-only transport reconnect after healthy ACK/data flow",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if not args.confirm_read_only:
        parser.error("--confirm-read-only is required")
    output = _require_external_output(args.output)
    evidence = capture(
        symbol=args.symbol,
        output=output,
        frames_per_channel=args.frames_per_channel,
        timeout_seconds=args.timeout_seconds,
        reconnect_after_seconds=args.reconnect_after_seconds,
    )
    digest = _sha256(output)
    print(f"Evidence: {output}")
    print(f"SHA-256: {digest}")
    print(
        "Frames: "
        + ", ".join(
            f"{tr_id}={count}"
            for tr_id, count in evidence["frame_counts"].items()
        )
    )
    if evidence["errors"]:
        for error in evidence["errors"]:
            print(f"ERROR: {error}")
        return 1
    print(
        "Capture complete. This file is UNREVIEWED evidence; review timestamp "
        "and sequence semantics before building the production capability manifest."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
