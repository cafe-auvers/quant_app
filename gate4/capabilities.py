"""Strict execution-capability manifest required before Gate 4."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from activation_gates.evidence import valid_git_commit_sha, valid_sha256


REQUIRED_EXECUTION_CAPABILITIES = frozenset(
    {
        "EXTERNAL_CORRELATION_BEHAVIOR",
        "AMBIGUOUS_SUBMISSION_RECOVERY",
        "IMMEDIATE_BROKER_ORDER_ID",
        "BROKER_ORDER_ID_UNIQUENESS_SCOPE",
        "ORDER_HISTORY_LATENCY",
        "ORDER_HISTORY_COMPLETENESS",
        "SUBMIT_ACCEPTED_MUTATION",
        "CANCEL_ACCEPTED_MUTATION",
        "REPLACE_ACCEPTED_MUTATION",
        "RATE_LIMIT_PRE_ACCEPTANCE_SEMANTICS",
    }
)


@dataclass(frozen=True)
class VerifiedExecutionCapabilities:
    path: Path
    sha256: str
    commit_sha: str
    environment: str
    account_ref: str
    capability_ids: tuple[str, ...]


def _aware(value: object) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def load_verified_execution_capabilities(
    path: Path,
    *,
    expected_commit: str,
    expected_environment: str = "PROD",
) -> VerifiedExecutionCapabilities:
    resolved = Path(path).expanduser().resolve()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Gate-4 capability manifest root must be an object")
    commit = str(payload.get("commit_sha") or "").strip().lower()
    if not valid_git_commit_sha(commit) or commit != str(expected_commit).lower():
        raise ValueError("Gate-4 capability manifest does not match the exact commit")
    environment = str(payload.get("environment") or "").strip().upper()
    if environment != str(expected_environment).strip().upper():
        raise ValueError("Gate-4 capability manifest environment mismatch")
    account_ref = str(payload.get("account_ref") or "").strip().lower()
    if not valid_sha256(account_ref):
        raise ValueError("Gate-4 capability manifest requires a hashed account_ref")

    review = payload.get("review")
    if not isinstance(review, Mapping):
        raise ValueError("Gate-4 capability manifest requires independent review")
    author = str(review.get("author") or "").strip()
    reviewer = str(review.get("reviewer") or "").strip()
    reference = str(review.get("reference") or "").removeprefix("sha256:").lower()
    if not (
        review.get("status") == "APPROVED"
        and author
        and reviewer
        and author.casefold() != reviewer.casefold()
        and _aware(review.get("reviewed_at"))
        and valid_sha256(reference)
    ):
        raise ValueError("Gate-4 capability manifest review is incomplete")

    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list):
        raise ValueError("Gate-4 capability manifest capabilities must be a list")
    verified: set[str] = set()
    for item in capabilities:
        if not isinstance(item, Mapping):
            raise ValueError("Gate-4 capability entries must be objects")
        capability_id = str(item.get("capability_id") or "").strip().upper()
        if capability_id in verified:
            raise ValueError(f"Duplicate Gate-4 capability: {capability_id}")
        if capability_id not in REQUIRED_EXECUTION_CAPABILITIES:
            raise ValueError(f"Unknown Gate-4 capability: {capability_id}")
        if item.get("status") != "VERIFIED":
            raise ValueError(f"Gate-4 capability is not VERIFIED: {capability_id}")
        if not valid_sha256(item.get("evidence_sha256")):
            raise ValueError(f"Gate-4 capability lacks evidence digest: {capability_id}")
        if not str(item.get("finding") or "").strip():
            raise ValueError(f"Gate-4 capability lacks a finding: {capability_id}")
        verified.add(capability_id)
    missing = sorted(REQUIRED_EXECUTION_CAPABILITIES - verified)
    if missing:
        raise ValueError("Missing Gate-4 capabilities: " + ", ".join(missing))
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return VerifiedExecutionCapabilities(
        path=resolved,
        sha256=digest,
        commit_sha=commit,
        environment=environment,
        account_ref=account_ref,
        capability_ids=tuple(sorted(verified)),
    )


__all__ = [
    "REQUIRED_EXECUTION_CAPABILITIES",
    "VerifiedExecutionCapabilities",
    "load_verified_execution_capabilities",
]
