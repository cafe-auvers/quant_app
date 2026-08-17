"""Strict WS0 capability-manifest validation for the Gate-2 boundary."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping


TRADE_TIMESTAMP = "HDFSCNT0_TIMESTAMP_SEMANTICS"
QUOTE_TIMESTAMP = "HDFSASP0_TIMESTAMP_SEMANTICS"
TRADE_SEQUENCE = "HDFSCNT0_SEQUENCE_SEMANTICS"
QUOTE_SEQUENCE = "HDFSASP0_SEQUENCE_SEMANTICS"
EXECUTION_NOTICE = "EXECUTION_NOTICE_ENCRYPTION"
REQUIRED_CAPABILITIES = {
    TRADE_TIMESTAMP: "HDFSCNT0",
    QUOTE_TIMESTAMP: "HDFSASP0",
    TRADE_SEQUENCE: "HDFSCNT0",
    QUOTE_SEQUENCE: "HDFSASP0",
}
SEQUENCE_INTERPRETATIONS = {"MONOTONIC", "NO_USABLE_SEQUENCE"}
SEQUENCE_RESET_SEMANTICS = {"RESET_ON_RECONNECT", "CONTINUES_ACROSS_RECONNECT"}
TIMESTAMP_INTERPRETATION = "EXCHANGE_EVENT_TIME_AMERICA_NEW_YORK"
NOTICE_INTERPRETATION = "AES_CBC_ACK_KEY_IV_DECRYPTED_FIELD_MAP_VERIFIED"
REVIEW_METHODS = {
    "GITHUB_PR_REVIEW",
    "SIGNED_ATTESTATION",
    "PROCEDURAL_DUAL_CONTROL",
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class VerifiedCapabilityManifest:
    path: Path
    sha256: str
    commit_sha: str
    environment: str
    review_author: str
    reviewer: str
    reviewed_at: str
    review_method: str
    review_reference: str
    capabilities: dict[str, dict[str, str]]

    @property
    def confirmed_sequence_channels(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                item["tr_id"]
                for item in self.capabilities.values()
                if item["capability_id"] in {TRADE_SEQUENCE, QUOTE_SEQUENCE}
                and item["interpretation"] == "MONOTONIC"
            )
        )

    @property
    def sequence_field_by_channel(self) -> dict[str, str]:
        return {
            item["tr_id"]: item["sequence_field"]
            for item in self.capabilities.values()
            if item["capability_id"] in {TRADE_SEQUENCE, QUOTE_SEQUENCE}
            and item["interpretation"] == "MONOTONIC"
        }

    @property
    def sequence_reset_by_channel(self) -> dict[str, str]:
        return {
            item["tr_id"]: item["reset_semantics"]
            for item in self.capabilities.values()
            if item["capability_id"] in {TRADE_SEQUENCE, QUOTE_SEQUENCE}
            and item["interpretation"] == "MONOTONIC"
        }


def _read_json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"capability manifest is not readable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("capability manifest root must be an object")
    return value


def _parse_review(payload: Mapping) -> tuple[str, str, str, str, str]:
    review = payload.get("review")
    if not isinstance(review, dict) or review.get("status") != "APPROVED":
        raise ValueError("capability manifest requires review.status=APPROVED")
    author = str(review.get("author") or "").strip()
    reviewer = str(review.get("reviewer") or "").strip()
    reviewed_at = str(review.get("reviewed_at") or "").strip()
    method = str(review.get("method") or "").strip().upper()
    reference = str(review.get("reference") or "").strip()
    if not author or not reviewer or not reviewed_at or not reference:
        raise ValueError(
            "capability manifest review requires author, reviewer, reviewed_at, "
            "and reference"
        )
    if author.casefold() == reviewer.casefold():
        raise ValueError("capability manifest reviewer must differ from its author")
    if method not in REVIEW_METHODS:
        raise ValueError("capability manifest review method is unsupported")
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("capability manifest reviewed_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("capability manifest reviewed_at must include a timezone")
    return author, reviewer, reviewed_at, method, reference


def _timezone_aware_iso8601(value: object, *, field_name: str) -> str:
    rendered = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return rendered


def _evidence_path(manifest_path: Path, value: str) -> Path:
    relative = Path(str(value or ""))
    if not str(relative) or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("capability evidence_file must be a safe relative path")
    root = manifest_path.parent.resolve()
    resolved = (root / relative).resolve()
    if resolved.parent != root and root not in resolved.parents:
        raise ValueError("capability evidence_file escapes the manifest bundle")
    return resolved


def load_verified_capability_manifest(
    path: Path, *, expected_commit: str, expected_environment: str
) -> VerifiedCapabilityManifest:
    path = path.resolve()
    payload = _read_json_object(path)
    if payload.get("schema_version") != 1:
        raise ValueError("capability manifest schema_version must be 1")
    commit_sha = str(payload.get("commit_sha") or "").strip().lower()
    if commit_sha != str(expected_commit).lower():
        raise ValueError("capability manifest commit_sha does not match the soak commit")
    environment = str(payload.get("environment") or "").strip().upper()
    if environment != str(expected_environment).upper():
        raise ValueError("capability manifest environment does not match the soak")
    author, reviewer, reviewed_at, review_method, review_reference = _parse_review(
        payload
    )
    entries = payload.get("capabilities")
    if not isinstance(entries, list):
        raise ValueError("capability manifest capabilities must be a list")

    expected_notice_tr = "H0GSCNI0" if environment == "PROD" else "H0GSCNI9"
    expected_tr_ids = {**REQUIRED_CAPABILITIES, EXECUTION_NOTICE: expected_notice_tr}
    capabilities: dict[str, dict[str, str]] = {}
    for raw in entries:
        if not isinstance(raw, dict):
            raise ValueError("every capability entry must be an object")
        capability_id = str(raw.get("capability_id") or "").strip()
        if capability_id in capabilities:
            raise ValueError(f"duplicate capability_id: {capability_id}")
        if capability_id not in expected_tr_ids:
            raise ValueError(f"unsupported capability_id: {capability_id}")
        status = str(raw.get("status") or "").strip().upper()
        item_environment = str(raw.get("environment") or "").strip().upper()
        tr_id = str(raw.get("tr_id") or "").strip().upper()
        interpretation = str(raw.get("interpretation") or "").strip().upper()
        digest = str(raw.get("evidence_sha256") or "").strip().lower()
        if status != "VERIFIED":
            raise ValueError(f"{capability_id} status must be VERIFIED")
        if item_environment != environment:
            raise ValueError(f"{capability_id} environment mismatch")
        if tr_id != expected_tr_ids[capability_id]:
            raise ValueError(f"{capability_id} tr_id mismatch")
        if not SHA256_PATTERN.fullmatch(digest):
            raise ValueError(f"{capability_id} evidence_sha256 is invalid")
        evidence_path = _evidence_path(path, str(raw.get("evidence_file") or ""))
        if not evidence_path.is_file() or sha256_file(evidence_path) != digest:
            raise ValueError(f"{capability_id} evidence digest does not match its file")
        evidence_payload = _read_json_object(evidence_path)
        observed_at = str(evidence_payload.get("observed_at") or "").strip()
        if (
            evidence_payload.get("capability_id") != capability_id
            or str(evidence_payload.get("environment") or "").upper() != environment
            or str(evidence_payload.get("tr_id") or "").upper() != tr_id
            or str(evidence_payload.get("interpretation") or "").upper()
            != interpretation
            or not observed_at
            or not isinstance(evidence_payload.get("observations"), list)
            or not evidence_payload["observations"]
        ):
            raise ValueError(
                f"{capability_id} evidence lacks the required observed interpretation"
            )
        _timezone_aware_iso8601(
            observed_at, field_name=f"{capability_id} evidence observed_at"
        )
        if capability_id in {TRADE_SEQUENCE, QUOTE_SEQUENCE}:
            if interpretation not in SEQUENCE_INTERPRETATIONS:
                raise ValueError(f"{capability_id} has an unsupported interpretation")
            sequence_field = str(raw.get("sequence_field") or "").strip().upper()
            evidence_sequence_field = str(
                evidence_payload.get("sequence_field") or ""
            ).strip().upper()
            reset_semantics = str(raw.get("reset_semantics") or "").strip().upper()
            evidence_reset_semantics = str(
                evidence_payload.get("reset_semantics") or ""
            ).strip().upper()
            if interpretation == "MONOTONIC":
                if (
                    not sequence_field
                    or evidence_sequence_field != sequence_field
                    or reset_semantics not in SEQUENCE_RESET_SEMANTICS
                    or evidence_reset_semantics != reset_semantics
                ):
                    raise ValueError(
                        f"{capability_id} requires matching sequence field/reset evidence"
                    )
            elif (
                sequence_field
                or evidence_sequence_field
                or reset_semantics
                or evidence_reset_semantics
            ):
                raise ValueError(
                    f"{capability_id} cannot configure sequence behavior when none is usable"
                )
        elif capability_id in {TRADE_TIMESTAMP, QUOTE_TIMESTAMP}:
            if interpretation != TIMESTAMP_INTERPRETATION:
                raise ValueError(f"{capability_id} timestamp interpretation is unverified")
        elif interpretation != NOTICE_INTERPRETATION:
            raise ValueError("execution-notice interpretation is unverified")
        capabilities[capability_id] = {
            "capability_id": capability_id,
            "status": status,
            "environment": environment,
            "tr_id": tr_id,
            "interpretation": interpretation,
            "evidence_sha256": digest,
        }
        if capability_id in {TRADE_SEQUENCE, QUOTE_SEQUENCE}:
            capabilities[capability_id]["sequence_field"] = sequence_field
            capabilities[capability_id]["reset_semantics"] = reset_semantics

    missing = sorted(set(expected_tr_ids) - set(capabilities))
    if missing:
        raise ValueError(f"capability manifest is missing: {', '.join(missing)}")
    return VerifiedCapabilityManifest(
        path=path,
        sha256=sha256_file(path),
        commit_sha=commit_sha,
        environment=environment,
        review_author=author,
        reviewer=reviewer,
        reviewed_at=reviewed_at,
        review_method=review_method,
        review_reference=review_reference,
        capabilities=capabilities,
    )


def capability_snapshot_complete(
    capabilities: Mapping[str, Mapping[str, str]], *, environment: str
) -> bool:
    expected_notice = "H0GSCNI0" if environment.upper() == "PROD" else "H0GSCNI9"
    expected = {**REQUIRED_CAPABILITIES, EXECUTION_NOTICE: expected_notice}
    for capability_id, tr_id in expected.items():
        item = capabilities.get(capability_id)
        if not item:
            return False
        if (
            item.get("capability_id") != capability_id
            or item.get("status") != "VERIFIED"
            or item.get("environment") != environment.upper()
            or item.get("tr_id") != tr_id
            or not SHA256_PATTERN.fullmatch(str(item.get("evidence_sha256") or ""))
        ):
            return False
        interpretation = item.get("interpretation")
        if capability_id in {TRADE_SEQUENCE, QUOTE_SEQUENCE}:
            if interpretation not in SEQUENCE_INTERPRETATIONS:
                return False
            sequence_field = str(item.get("sequence_field") or "")
            reset_semantics = str(item.get("reset_semantics") or "")
            if (interpretation == "MONOTONIC") != bool(sequence_field):
                return False
            if interpretation == "MONOTONIC" and reset_semantics not in SEQUENCE_RESET_SEMANTICS:
                return False
            if interpretation == "NO_USABLE_SEQUENCE" and reset_semantics:
                return False
        elif capability_id in {TRADE_TIMESTAMP, QUOTE_TIMESTAMP}:
            if interpretation != TIMESTAMP_INTERPRETATION:
                return False
        elif interpretation != NOTICE_INTERPRETATION:
            return False
    return True
