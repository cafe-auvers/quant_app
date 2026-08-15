"""Shared, dependency-neutral redaction of secrets/account numbers from
payloads before they are logged or persisted.

Extracted from :mod:`src.services.event_journal` (revision 3.2 of
``docs/kanban_production_readiness.md``, Workstream 2 review): core-layer
modules (:mod:`src.core.execution_order_record`,
:mod:`src.core.discovered_external_order`) and
:mod:`src.services.execution_command_repository` needed the same
redaction logic before persisting a raw broker payload, and importing a
*private* function from a services-layer module
(``event_journal._safe_payload``) crossed both a privacy boundary and the
project's own core-should-not-depend-on-services layering. This module has
no dependencies on the rest of the codebase, so it is safe for any layer
(core, services, UI) to import.

:mod:`src.services.event_journal` re-exports :func:`mask_account_number`
and :func:`scrub_sensitive_text` from here unchanged, so existing callers
of those two names are unaffected by the move.
"""
from __future__ import annotations

import datetime as dt
import re
from enum import Enum
from typing import Any

_SENSITIVE_KEY_PARTS = (
    "access_token",
    "authorization",
    "app_secret",
    "password",
    "passwd",
    "secret",
    "token",
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:access_token|authorization|app_secret|password|passwd|secret|token|"
    r"app_key|appkey)\b[\"']?\s*[:=]\s*[\"']?)([^\s\"'&,;}]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_ACCOUNT_RE = re.compile(r"(?<!\d)(\d{8}-?\d{2})(?!\d)")


def mask_account_number(account_no: Any) -> str:
    """Return a useful correlation label without persisting the full account."""
    text = str(account_no or "").strip()
    if not text:
        return ""
    compact = text.replace("-", "")
    if len(compact) <= 4:
        return "*" * len(compact)
    return f"{compact[:2]}{'*' * max(4, len(compact) - 4)}{compact[-2:]}"


def scrub_sensitive_text(value: Any, *, account_no: Any = "") -> str:
    """Remove credentials and account identifiers from unstructured text."""
    text = str(value or "")
    account_text = str(account_no or "").strip()
    if account_text:
        masked = mask_account_number(account_text)
        variants = {account_text, account_text.replace("-", "")}
        for variant in sorted(
            (item for item in variants if item), key=len, reverse=True
        ):
            text = text.replace(variant, masked)
    text = _ACCOUNT_RE.sub(lambda match: mask_account_number(match.group(1)), text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    return _SENSITIVE_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)


def redact_payload(
    value: Any,
    *,
    key: str = "",
    depth: int = 0,
    account_no: Any = "",
) -> Any:
    """Recursively redact secrets/account numbers from an arbitrary
    JSON-like structure (dict/list/tuple/set/scalar) before it is ever
    logged or persisted. Any dict key containing one of
    :data:`_SENSITIVE_KEY_PARTS` is fully replaced regardless of its
    value's type.
    """
    normalized_key = str(key).strip().lower()
    if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if depth > 6:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return scrub_sensitive_text(value, account_no=account_no)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            str(item_key): redact_payload(
                item,
                key=str(item_key),
                depth=depth + 1,
                account_no=account_no,
            )
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [
            redact_payload(item, depth=depth + 1, account_no=account_no)
            for item in value
        ]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return scrub_sensitive_text(value, account_no=account_no)


def hash_redacted_payload(payload: Any) -> str:
    """A stable integrity/dedup hash of an already-redacted payload --
    computed over the redacted form (never the raw one), so the hash
    itself can never leak anything the redaction removed.
    """
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
