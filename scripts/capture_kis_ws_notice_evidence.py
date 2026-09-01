"""Capture redacted KIS execution-notice protocol evidence without trading.

The collector subscribes only to the production H0GSCNI0 notice channel for
the configured HTS identity.  It records whether KIS supplied an encryption
key/IV on the subscription ACK and, if an encrypted notice arrives, validates
the decrypted field count against KIS's pinned first-party sample.  Decrypted
account, order, name, symbol, price, and quantity values are never persisted.

This command does not construct a broker or call any mutation endpoint.  A
notice can only arrive because of account activity initiated elsewhere.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.capture_kis_ws_event_evidence import (
    _git_snapshot,
    _iso,
    _require_external_output,
    _sha256,
)
from src.api.kis_websocket import (
    KisWebSocketClient,
    KisWsDataFrame,
    KisWsSubscription,
    KisWsSystemFrame,
)
from src.api.kis_ws_auth import KisWsApprovalKeyProvider


NOTICE_TR_ID = "H0GSCNI0"
OFFICIAL_SAMPLE_REPOSITORY = "koreainvestment/open-trading-api"
OFFICIAL_SAMPLE_COMMIT = "b4e6249714418aa57833d1cbbbced39cbcc5b125"
OFFICIAL_SAMPLE_PATH = "examples_llm/overseas_stock/ccnl_notice/ccnl_notice.py"
NOTICE_COLUMNS = (
    "CUST_ID",
    "ACNT_NO",
    "ODER_NO",
    "OODER_NO",
    "SELN_BYOV_CLS",
    "RCTF_CLS",
    "ODER_KIND2",
    "STCK_SHRN_ISCD",
    "CNTG_QTY",
    "CNTG_UNPR",
    "STCK_CNTG_HOUR",
    "RFUS_YN",
    "CNTG_YN",
    "ACPT_YN",
    "BRNC_NO",
    "ODER_QTY",
    "ACNT_NAME",
    "CNTG_ISNM",
    "ODER_COND",
    "DEBT_GB",
    "DEBT_DATE",
    "START_TM",
    "END_TM",
    "TM_DIV_TP",
    "CNTG_UNPR12",
)


def _digits(value: object) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def _structural_fields(values: Sequence[str]) -> list[dict[str, Any]]:
    return [
        {
            "position": index,
            "column": column,
            "present": bool(value),
            "character_count": len(value),
            "numeric": bool(value) and value.replace(".", "", 1).isdigit(),
        }
        for index, (column, value) in enumerate(
            zip(NOTICE_COLUMNS, values), start=1
        )
    ]


def capture_notice(*, output: Path, timeout_seconds: float) -> dict[str, Any]:
    commit_sha = _git_snapshot()
    timeout = max(5.0, float(timeout_seconds))
    required_env = (
        "KIS_PROD_BASE_URL",
        "KIS_PROD_APP_KEY",
        "KIS_PROD_APP_SECRET",
        "KIS_PROD_WS_URL",
        "KIS_WS_HTS_ID",
    )
    missing = [name for name in required_env if not str(os.getenv(name, "")).strip()]
    if missing:
        raise RuntimeError("missing configuration: " + ", ".join(missing))

    lock = threading.Lock()
    done = threading.Event()
    fatal = threading.Event()
    errors: list[str] = []
    connection_events: list[dict[str, Any]] = []
    acknowledgement: dict[str, Any] = {}
    notice_observation: dict[str, Any] = {}

    provider = KisWsApprovalKeyProvider(
        base_url=os.getenv("KIS_PROD_BASE_URL", ""),
        app_key=os.getenv("KIS_PROD_APP_KEY", ""),
        app_secret=os.getenv("KIS_PROD_APP_SECRET", ""),
        ttl_seconds=float(
            os.getenv("KIS_WS_APPROVAL_KEY_TTL_SECONDS", "82800") or 82800
        ),
        max_retries=1,
        protocol_verified=True,
        sensitive_value_audit=lambda _value: None,
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
        with lock:
            connection_events.append(
                {
                    "connected": bool(connected),
                    "generation": int(generation),
                    "observed_at": _iso(datetime.now(timezone.utc)),
                    "reason_present": bool(reason),
                }
            )

    def on_ack(frame: KisWsSystemFrame) -> None:
        if frame.tr_id != NOTICE_TR_ID:
            return
        with lock:
            acknowledgement.update(
                {
                    "accepted": bool(frame.accepted),
                    "message_code": frame.message_code,
                    "encryption_flag": frame.encrypt,
                    "encryption_key_present": bool(frame.encryption_key),
                    "encryption_key_length": len(frame.encryption_key),
                    "encryption_iv_present": bool(frame.encryption_iv),
                    "encryption_iv_length": len(frame.encryption_iv),
                    "observed_at": _iso(datetime.now(timezone.utc)),
                }
            )
            if not frame.accepted:
                errors.append("execution-notice subscription was rejected")
                fatal.set()

    def on_data(frame: KisWsDataFrame) -> None:
        if frame.tr_id != NOTICE_TR_ID:
            return
        values = tuple(frame.payload.split("^"))
        configured_account = _digits(os.getenv("KIS_PROD_ACCOUNT_NO", ""))
        observed_account = _digits(values[1] if len(values) > 1 else "")
        with lock:
            notice_observation.update(
                {
                    "observed_at": _iso(frame.received_at),
                    "transport_encrypted": bool(frame.encrypted),
                    "decryption_succeeded": True,
                    "field_count": len(values),
                    "expected_field_count": len(NOTICE_COLUMNS),
                    "field_count_matches_official_sample": (
                        len(values) == len(NOTICE_COLUMNS)
                    ),
                    "configured_account_matches_field_2": bool(
                        configured_account
                        and observed_account
                        and (
                            configured_account == observed_account
                            or configured_account.endswith(observed_account)
                            or observed_account.endswith(configured_account)
                        )
                    ),
                    "structural_fields": _structural_fields(values),
                    "payload_fingerprint": frame.payload_fingerprint,
                    "decrypted_values_persisted": False,
                }
            )
            if len(values) != len(NOTICE_COLUMNS):
                errors.append(
                    "decrypted execution-notice field count does not match "
                    "the pinned official sample"
                )
            done.set()

    client.on_connection(on_connection)
    client.on_ack(on_ack)
    client.on_data(on_data)
    client.subscribe(
        [
            KisWsSubscription(
                NOTICE_TR_ID,
                os.getenv("KIS_WS_HTS_ID", "").strip(),
                channel="EXECUTION_NOTICE",
            )
        ]
    )

    started_at = datetime.now(timezone.utc)
    client.start()
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and not done.is_set() and not fatal.is_set():
            time.sleep(0.1)
    finally:
        client.stop()
    ended_at = datetime.now(timezone.utc)

    with lock:
        evidence = {
            "schema_version": 1,
            "evidence_kind": "KIS_WS_EXECUTION_NOTICE_CAPTURE",
            "qualification_only": True,
            "review_status": "UNREVIEWED",
            "commit_sha": commit_sha,
            "environment": "PROD",
            "tr_id": NOTICE_TR_ID,
            "started_at": _iso(started_at),
            "ended_at": _iso(ended_at),
            "broker_mutations": 0,
            "credentials_persisted": False,
            "decrypted_values_persisted": False,
            "official_sample": {
                "repository": OFFICIAL_SAMPLE_REPOSITORY,
                "commit_sha": OFFICIAL_SAMPLE_COMMIT,
                "path": OFFICIAL_SAMPLE_PATH,
                "columns": list(NOTICE_COLUMNS),
            },
            "connection_events": list(connection_events),
            "subscription_acknowledgement": dict(acknowledgement),
            "notice_observation": dict(notice_observation),
            "errors": list(errors),
        }

    ack = evidence["subscription_acknowledgement"]
    if not ack.get("accepted"):
        evidence["errors"].append("accepted execution-notice ACK was not observed")
    if not ack.get("encryption_key_present") or not ack.get("encryption_iv_present"):
        evidence["errors"].append("execution-notice ACK omitted encryption key or IV")
    if not evidence["notice_observation"]:
        evidence["errors"].append(
            "no live execution notice arrived during the observation window"
        )

    output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return evidence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture redacted KIS execution-notice protocol evidence"
    )
    parser.add_argument("--confirm-read-only", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.confirm_read_only:
        parser.error("--confirm-read-only is required")
    output = _require_external_output(args.output)
    evidence = capture_notice(output=output, timeout_seconds=args.timeout_seconds)
    print(f"Evidence: {output}")
    print(f"SHA-256: {_sha256(output)}")
    ack = evidence["subscription_acknowledgement"]
    print(
        "ACK: accepted="
        f"{bool(ack.get('accepted'))} key_present="
        f"{bool(ack.get('encryption_key_present'))} iv_present="
        f"{bool(ack.get('encryption_iv_present'))}"
    )
    print(
        "Notice: observed="
        f"{bool(evidence['notice_observation'])}; broker_mutations=0"
    )
    if evidence["errors"]:
        for error in evidence["errors"]:
            print(f"ERROR: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
