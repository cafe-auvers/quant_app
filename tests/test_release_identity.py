from __future__ import annotations

import hashlib
import json

from src.core import release_identity


def test_exact_release_identity_requires_clean_matching_approved_manifest(
    tmp_path, monkeypatch
):
    commit_sha = "a" * 40
    manifest_path = tmp_path / "capability.json"
    manifest_path.write_text(
        json.dumps(
            {
                "commit_sha": commit_sha,
                "review": {"status": "APPROVED"},
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    values = {
        "KIS_RUNTIME_COMMIT_SHA": commit_sha,
        "KIS_CAPABILITY_MANIFEST_PATH": str(manifest_path),
        "KIS_CAPABILITY_MANIFEST_SHA256": digest,
    }
    monkeypatch.setattr(
        release_identity,
        "get_env_value",
        lambda key, default="": values.get(key, default),
    )
    monkeypatch.setattr(release_identity, "repository_head", lambda: commit_sha)
    monkeypatch.setattr(release_identity, "repository_is_clean", lambda: True)

    identity = release_identity.current_release_identity()

    assert identity.issues == ()
    assert identity.release_id == f"{commit_sha}:{digest}"


def test_exact_release_identity_rejects_dirty_or_mismatched_checkout(
    monkeypatch,
):
    identity = release_identity.ReleaseIdentity(
        repository_head="a" * 40,
        repository_clean=False,
        configured_commit_sha="b" * 40,
        manifest_commit_sha="b" * 40,
        configured_manifest_sha256="c" * 64,
        actual_manifest_sha256="d" * 64,
        manifest_review_status="PENDING",
    )

    assert identity.release_id == ""
    assert "uncommitted" in "; ".join(identity.issues)
    assert "does not match repository HEAD" in "; ".join(identity.issues)
    assert "digest does not match" in "; ".join(identity.issues)
    assert "not independently APPROVED" in "; ".join(identity.issues)
