"""Renew an already-ON session switch on the operator's pinned PC release.

Run from the operations directory against the existing approved checkout.
An explicit OFF, release change, unavailable database, or unready executor is
never overridden. This command does not submit orders or claim ownership.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import platform
import sys

from sqlalchemy import text


def renew_enabled_session(engine, *, release: str, hostname: str, session: date,
                          now: datetime, dry_run: bool = False) -> str:
    """Read, fence and audit renewal in one transaction, serialized with OFF."""
    with engine.begin() as conn:
        lock = " FOR UPDATE" if engine.dialect.name != "sqlite" else ""
        row = conn.execute(text(
            "SELECT payload, revision FROM app_state_sync "
            "WHERE state_key = '__live_trading_control__'" + lock
        )).mappings().first()
        if row is None:
            return "NO_SAVED_ON_CHOICE"
        payload = json.loads(row['payload'])
        if payload.get('enabled') is not True:
            return "OFF_PRESERVED"
        if payload.get('runtime_commit_sha') != release:
            return "RELEASE_CHANGE_REQUIRES_OPERATOR"
        if payload.get('persist_across_sessions') is True:
            return "ALREADY_PERSISTENT"
        armed = date.fromisoformat(payload.get('session_date', ''))
        if armed == session:
            return "ALREADY_CURRENT"
        if armed > session:
            return "FUTURE_SESSION_PRESERVED"
        owner_row = conn.execute(text(
            "SELECT payload FROM app_state_sync WHERE state_key = '__main_device__'"
        )).scalar_one_or_none()
        owner = json.loads(owner_row) if owner_row else {}
        if str(owner.get('hostname', '')).lower() != hostname.lower():
            return "WAITING_FOR_PC_OWNERSHIP"
        runtime = conn.execute(text(
            "SELECT state, details_json, updated_at FROM runtime_device_state "
            "WHERE device_id = :device"
        ), {'device': owner.get('device_id', '')}).mappings().first()
        if runtime is None:
            return "WAITING_FOR_EXECUTOR"
        details = json.loads(runtime['details_json'] or '{}')
        observed = datetime.fromisoformat(str(runtime['updated_at'])).replace(tzinfo=timezone.utc)
        age = (now.astimezone(timezone.utc) - observed).total_seconds()
        if not (0 <= age <= 90 and runtime['state'] == 'ACTIVE'
                and details.get('repository_commit_sha') == release
                and all(details.get(key) is True for key in (
                    'main_py_alive', 'db_connected', 'kis_ready', 'executor_ready',
                    'order_reconciliation_ready', 'command_consumer_ready'))):
            return "WAITING_FOR_READY_EXECUTOR"
        if dry_run:
            return "WOULD_RENEW_SAVED_ON_CHOICE"
        payload['session_date'] = session.isoformat()
        revision = int(row['revision']) + 1
        result = conn.execute(text(
            "UPDATE app_state_sync SET payload=:payload, revision=:revision, "
            "updated_at=:now, updated_by_host=:host, updated_by_device=:device "
            "WHERE state_key='__live_trading_control__' AND revision=:previous"
        ), dict(payload=json.dumps(payload, separators=(',', ':')), revision=revision,
                now=now.astimezone(timezone.utc).replace(tzinfo=None),
                host=hostname, device=owner['device_id'], previous=row['revision']))
        if result.rowcount != 1:
            raise RuntimeError('Live switch changed during renewal')
        conn.execute(text(
            "INSERT INTO live_trading_control_audit "
            "(revision, previous_enabled, enabled, updated_by_host, updated_by_device, updated_at) "
            "VALUES (:revision, 1, 1, :host, :device, :now)"
        ), dict(revision=revision, host=hostname, device=owner['device_id'],
                now=now.astimezone(timezone.utc).replace(tzinfo=None)))
        return "SAVED_ON_CHOICE_RENEWED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository-root', type=Path, required=True)
    parser.add_argument('--expected-release', required=True)
    parser.add_argument('--expected-manifest-sha256', required=True)
    parser.add_argument('--expected-hostname', required=True)
    parser.add_argument('--log-file', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    engine = None
    try:
        if platform.node().lower() != args.expected_hostname.lower():
            raise RuntimeError('Automatic resume is restricted to the configured PC')
        sys.path.insert(0, str(args.repository_root.resolve(strict=True)))
        from src.core.release_identity import require_approved_release_identity
        from src.services.trading_state import is_trading_locked_disabled
        from src.infrastructure.database.coordination_engine import init_coordination_engine
        from src.utils.market_calendar import current_or_next_nyse_session_date

        identity = require_approved_release_identity()
        if (identity.repository_head != args.expected_release
                or identity.actual_manifest_sha256 != args.expected_manifest_sha256):
            raise RuntimeError('Automatic resume release pin no longer matches')
        if is_trading_locked_disabled():
            status = 'ADMINISTRATIVE_OFF_PRESERVED'
        else:
            engine = init_coordination_engine(ensure_schema=False, raise_on_error=True)
            if engine is None:
                raise RuntimeError('Coordination database unavailable')
            status = renew_enabled_session(
                engine, release=args.expected_release, hostname=args.expected_hostname,
                session=current_or_next_nyse_session_date(),
                now=datetime.now(timezone.utc), dry_run=args.dry_run)
        code = 0
    except Exception as exc:
        # Do not include database connection strings or provider secrets.
        status = f'RESUME_BLOCKED:{type(exc).__name__}'
        code = 1
    finally:
        if engine is not None:
            engine.dispose()
    line = json.dumps(dict(checked_at=datetime.now(timezone.utc).isoformat(),
                           status=status, dry_run=args.dry_run))
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    with args.log_file.open('a', encoding='utf-8') as handle:
        handle.write(line + '\n')
    print(line)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
