"""Validate cloud archives and convert them to the existing restore format."""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime

from src.services.coordination_store_migration import (
    CoordinationStoreMigrationError, _canonical_json, _digest,
    coordination_metadata, validate_coordination_snapshot,
)
from src.services.schema_migration import _decode_backup_value, _encode_backup_value

MAX_COMPRESSED_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024


def cloud_archive_to_snapshot(content: bytes, checksum: str) -> dict:
    """Check both hashes, every table/column, and typed values before restore."""
    if len(content) > MAX_COMPRESSED_BYTES or hashlib.sha256(content).hexdigest() != checksum:
        raise CoordinationStoreMigrationError("Cloud archive checksum or size is invalid")
    import io
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
            decoded = stream.read(MAX_ARCHIVE_BYTES + 1)
        if len(decoded) > MAX_ARCHIVE_BYTES:
            raise CoordinationStoreMigrationError("Cloud archive expands beyond its budget")
        archive = json.loads(decoded)
        if archive.get("format") != "quant-cloud-coordination-backup-v1":
            raise CoordinationStoreMigrationError("Unsupported cloud archive format")
        raw = archive["payload_text"]
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != archive["payload_sha256"]:
            raise CoordinationStoreMigrationError("Cloud payload checksum is invalid")
        payload = json.loads(raw)
        created_at = str(archive["created_at"])
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        metadata = coordination_metadata()
        tables = payload["tables"]
        if set(tables) != set(metadata.tables):
            raise CoordinationStoreMigrationError("Cloud archive has an incomplete table set")
        for name, rows in tables.items():
            if not isinstance(rows, list) or any(set(row) != set(metadata.tables[name].c.keys()) for row in rows):
                raise CoordinationStoreMigrationError(f"Cloud columns differ for {name}")
            # Normalize timestamps to Python's canonical backup representation.
            tables[name] = sorted([
                {key: _encode_backup_value(_decode_backup_value(value)) for key, value in row.items()}
                for row in rows
            ], key=_canonical_json)
        snapshot = {"payload": payload, "created_at": created_at, "sha256": _digest(payload)}
        validate_coordination_snapshot(snapshot)
        return snapshot
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise CoordinationStoreMigrationError("Cloud archive is invalid") from exc
