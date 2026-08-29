"""Exact-release identity used by controlled-live readiness fences.

The production capability manifest certifies one exact Git checkout.  Keeping
this logic in one small module prevents startup scripts, runtime workers and
operator diagnostics from implementing subtly different SHA/digest checks.
No function in this module mutates Git, configuration or the manifest.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Optional

from src.utils.config import ROOT_DIR, get_env_value, resolve_repo_path


_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ReleaseIdentity:
    repository_head: str
    repository_clean: bool
    configured_commit_sha: str
    manifest_commit_sha: str
    configured_manifest_sha256: str
    actual_manifest_sha256: str
    manifest_review_status: str

    @property
    def release_id(self) -> str:
        """Stable cross-device identity for one approved deployment."""

        if self.issues:
            return ""
        return (
            f"{self.repository_head}:"
            f"{self.actual_manifest_sha256}"
        )

    @property
    def issues(self) -> tuple[str, ...]:
        issues: list[str] = []
        if not _SHA40_RE.fullmatch(self.repository_head):
            issues.append("repository HEAD is unavailable or not a full Git SHA")
        if not self.repository_clean:
            issues.append("repository checkout has uncommitted or untracked changes")
        if not _SHA40_RE.fullmatch(self.configured_commit_sha):
            issues.append("KIS_RUNTIME_COMMIT_SHA is not a full Git SHA")
        elif (
            _SHA40_RE.fullmatch(self.repository_head)
            and self.configured_commit_sha != self.repository_head
        ):
            issues.append("KIS_RUNTIME_COMMIT_SHA does not match repository HEAD")
        if not _SHA256_RE.fullmatch(self.configured_manifest_sha256):
            issues.append("KIS_CAPABILITY_MANIFEST_SHA256 is not a SHA-256 digest")
        elif self.actual_manifest_sha256 != self.configured_manifest_sha256:
            issues.append("reviewed capability-manifest digest does not match")
        if not _SHA40_RE.fullmatch(self.manifest_commit_sha):
            issues.append("capability manifest has no exact commit SHA")
        elif self.manifest_commit_sha != self.configured_commit_sha:
            issues.append("capability manifest commit does not match runtime commit")
        if self.manifest_review_status != "APPROVED":
            issues.append("capability manifest is not independently APPROVED")
        return tuple(issues)


def repository_head(*, root: Path = ROOT_DIR) -> str:
    """Return the exact checked-out commit without modifying the repository."""

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return str(completed.stdout or "").strip().lower()


def repository_is_clean(*, root: Path = ROOT_DIR) -> bool:
    """Return whether the checkout exactly represents its committed tree."""

    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return not str(completed.stdout or "").strip()


def _file_sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _load_manifest(path: Optional[Path]) -> dict:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def current_release_identity() -> ReleaseIdentity:
    configured_commit = str(
        get_env_value("KIS_RUNTIME_COMMIT_SHA", "") or ""
    ).strip().lower()
    configured_digest = str(
        get_env_value("KIS_CAPABILITY_MANIFEST_SHA256", "") or ""
    ).strip().lower()
    raw_manifest_path = str(
        get_env_value("KIS_CAPABILITY_MANIFEST_PATH", "") or ""
    ).strip()
    manifest_path = resolve_repo_path(raw_manifest_path) if raw_manifest_path else None
    manifest = _load_manifest(manifest_path)
    review = manifest.get("review")
    if not isinstance(review, dict):
        review = {}
    return ReleaseIdentity(
        repository_head=repository_head(),
        repository_clean=repository_is_clean(),
        configured_commit_sha=configured_commit,
        manifest_commit_sha=str(manifest.get("commit_sha") or "").strip().lower(),
        configured_manifest_sha256=configured_digest,
        actual_manifest_sha256=(
            _file_sha256(manifest_path) if manifest_path is not None else ""
        ),
        manifest_review_status=str(review.get("status") or "").strip().upper(),
    )


def require_approved_release_identity() -> ReleaseIdentity:
    identity = current_release_identity()
    if identity.issues:
        raise RuntimeError(
            "Controlled-live release identity is not approved: "
            + "; ".join(identity.issues)
        )
    return identity
