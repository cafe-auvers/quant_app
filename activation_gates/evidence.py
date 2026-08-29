"""Pure compatible-evidence-chain validation shared by Gates 3-5."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
import re
from typing import Any, Mapping

from src.utils.market_calendar import is_nyse_trading_day, next_nyse_trading_day


def canonical_report_sha256(report: Mapping[str, Any]) -> str:
    payload = json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def valid_sha256(value: object) -> bool:
    digest = str(value or "").strip().lower()
    return len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)


def valid_git_commit_sha(value: object) -> bool:
    """Accept complete SHA-1 or SHA-256 Git object identities, never prefixes."""

    commit = str(value or "").strip().lower()
    return len(commit) in {40, 64} and all(
        char in "0123456789abcdef" for char in commit
    )


def valid_aware_iso_datetime(value: object) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def evidence_integer(value: object) -> int | None:
    """Parse a JSON evidence counter without truncation or bool coercion."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    return None


def evidence_mapping(value: object) -> Mapping[str, Any]:
    """Return a mapping view, or an empty fail-closed view for bad evidence."""

    return value if isinstance(value, Mapping) else {}


def evidence_sequence(value: object) -> tuple[object, ...]:
    """Return a finite evidence sequence without treating text as a sequence."""

    return tuple(value) if isinstance(value, (list, tuple)) else ()


def validate_nyse_session_dates(
    values: object,
    *,
    expected_count: int,
    consecutive: bool,
    exact_count: bool = True,
) -> list[dict[str, str]]:
    """Validate ordered NYSE qualification-session identities."""

    property_name = (
        "five_consecutive_sessions" if consecutive else "supervised_session_count"
    )
    if not isinstance(values, (list, tuple)):
        return [violation(property_name, "session dates must be an ordered list")]
    raw_dates = [str(item or "").strip() for item in values]
    count_is_invalid = (
        len(raw_dates) != expected_count
        if exact_count
        else len(raw_dates) < expected_count
    )
    if count_is_invalid or len(set(raw_dates)) != len(raw_dates):
        qualifier = "exactly" if exact_count else "at least"
        return [
            violation(
                property_name,
                f"{qualifier} {expected_count} distinct session dates are required",
            )
        ]
    parsed: list[date] = []
    for raw in raw_dates:
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            return [violation(property_name, f"invalid ISO session date: {raw!r}")]
        if raw != day.isoformat() or not is_nyse_trading_day(day):
            return [
                violation(
                    property_name,
                    f"{raw!r} is not a canonical NYSE regular-session date",
                )
            ]
        parsed.append(day)
    if parsed != sorted(parsed):
        return [violation(property_name, "session dates must be chronological")]
    if consecutive:
        for previous, current in zip(parsed, parsed[1:]):
            expected = next_nyse_trading_day(previous + timedelta(days=1))
            if current != expected:
                return [
                    violation(
                        property_name,
                        f"{current.isoformat()} does not immediately follow "
                        f"{previous.isoformat()} on the NYSE calendar",
                    )
                ]
    return []


def validate_upstream_report(
    *,
    upstream_report: Mapping[str, Any],
    expected_gate: str,
    expected_digest: str,
    commit_sha: str,
) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    if not valid_git_commit_sha(commit_sha):
        violations.append(
            {
                "property": "exact_commit_identity",
                "detail": "current report commit is not a complete Git SHA",
            }
        )
    actual_digest = canonical_report_sha256(upstream_report)
    if str(upstream_report.get("gate") or "") != expected_gate:
        violations.append(
            {
                "property": "compatible_upstream_gate",
                "detail": f"expected {expected_gate} upstream report",
            }
        )
    if str(upstream_report.get("result") or "").upper() != "PASSED":
        violations.append(
            {
                "property": "compatible_upstream_gate",
                "detail": f"{expected_gate} upstream result is not PASSED",
            }
        )
    if str(upstream_report.get("commit_sha") or "").strip().lower() != str(
        commit_sha or ""
    ).strip().lower():
        violations.append(
            {
                "property": "compatible_upstream_gate",
                "detail": "upstream and current report commits differ",
            }
        )
    if not valid_sha256(expected_digest) or expected_digest.lower() != actual_digest:
        violations.append(
            {
                "property": "compatible_upstream_gate",
                "detail": "upstream report digest is missing or mismatched",
            }
        )
    return violations


def validate_independent_review(
    review: Mapping[str, Any],
) -> list[dict[str, str]]:
    author = str(review.get("author") or "").strip()
    reviewer = str(review.get("reviewer") or "").strip()
    status = str(review.get("status") or "").strip().upper()
    reviewed_at = str(review.get("reviewed_at") or "").strip()
    reference = str(review.get("reference") or "").strip()
    if (
        status == "APPROVED"
        and author
        and reviewer
        and author.casefold() != reviewer.casefold()
        and valid_aware_iso_datetime(reviewed_at)
        and valid_sha256(reference.removeprefix("sha256:"))
    ):
        return []
    return [
        {
            "property": "independent_review_approved",
            "detail": (
                "review requires APPROVED status, distinct non-blank author/reviewer, "
                "timezone-aware timestamp, and SHA-256 reference"
            ),
        }
    ]


def violation(property_name: str, detail: str) -> dict[str, str]:
    return {"property": property_name, "detail": detail}
