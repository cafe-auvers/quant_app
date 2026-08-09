"""Encrypted offsite backup of `.env` secrets to Google Drive.

`.env` holds real secrets -- MySQL password, KIS trading keys, OpenAI key.
Unlike everything in `cloud_backup.py`, this is never written in plaintext
to the synced Drive folder. It's encrypted first with a key derived from a
passphrase the user chooses and types each time -- never stored anywhere,
not in `.env`, not alongside the backup, not in this repo. A compromised
Google account alone is therefore not enough to read the secrets back; the
passphrase is also required, and only the user holds it.

Algorithm: PBKDF2-HMAC-SHA256 (600,000 iterations, OWASP's 2023 minimum
recommendation) derives a 32-byte key from the passphrase and a random
16-byte per-backup salt; Fernet (AES-128-CBC + HMAC, from the `cryptography`
package) does the actual authenticated encryption. The salt isn't secret
and is stored next to the ciphertext -- salts never need to be, only the
passphrase does.

Deliberately not part of the automatic `cloud_backup.py` cycle: `.env`
changes rarely (only when credentials are rotated), and prompting for a
passphrase from a background thread on a timer is bad UX and arguably bad
security practice (repeated prompts train users to enter passwords without
thinking). This is a manual, explicit action every time.

Only the latest backup is kept (no daily history, unlike the JSON state
backups) -- there's no value in an old copy of rotated-out credentials.
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from src.services.cloud_backup import BACKUP_SUBDIR_NAME

logger = logging.getLogger(__name__)

SECRETS_DIRNAME = "secrets"
ENV_BACKUP_FILENAME = "env.enc"
SALT_FILENAME = "env.salt"
PBKDF2_ITERATIONS = 600_000
MIN_RECOMMENDED_PASSPHRASE_LENGTH = 12


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _atomic_write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_name(f".{dest.name}.tmp")
    tmp_dest.write_bytes(data)
    tmp_dest.replace(dest)


def _secrets_dir(backup_root: Path) -> Path:
    return Path(backup_root) / BACKUP_SUBDIR_NAME / SECRETS_DIRNAME


@dataclass
class EnvBackupResult:
    success: bool
    destination: Optional[Path] = None
    error: str = ""


def backup_env_file(env_path: Path, backup_root: Path, passphrase: str) -> EnvBackupResult:
    """Encrypt `env_path` with `passphrase` and write it into the Drive folder.

    A fresh random salt is generated every call, so backing up again after
    rotating the passphrase (or just periodically) never reuses key material.
    """
    env_path = Path(env_path)
    if not env_path.is_file():
        return EnvBackupResult(success=False, error=f"{env_path} does not exist.")
    if not passphrase:
        return EnvBackupResult(success=False, error="A passphrase is required.")

    secrets_dir = _secrets_dir(backup_root)
    try:
        salt = os.urandom(16)
        key = _derive_key(passphrase, salt)
        ciphertext = Fernet(key).encrypt(env_path.read_bytes())
        _atomic_write(secrets_dir / SALT_FILENAME, salt)
        _atomic_write(secrets_dir / ENV_BACKUP_FILENAME, ciphertext)
    except OSError as exc:
        logger.info("Env backup failed: %s", exc)
        return EnvBackupResult(success=False, error=str(exc))

    logger.info("Encrypted .env backup written to %s", secrets_dir)
    return EnvBackupResult(success=True, destination=secrets_dir / ENV_BACKUP_FILENAME)


def env_backup_exists(backup_root: Path) -> bool:
    secrets_dir = _secrets_dir(backup_root)
    return (secrets_dir / SALT_FILENAME).is_file() and (secrets_dir / ENV_BACKUP_FILENAME).is_file()


@dataclass
class EnvRestoreResult:
    success: bool
    restored_path: Optional[Path] = None
    preserved_original: Optional[Path] = None
    error: str = ""


def restore_env_file(
    backup_root: Path,
    target_path: Path,
    passphrase: str,
    *,
    preserve_existing: bool = True,
) -> EnvRestoreResult:
    """Decrypt the backed-up `.env` and write it to `target_path`.

    Wrong passphrase produces a clean, specific error (`InvalidToken`) rather
    than corrupted output -- Fernet authenticates the ciphertext, it can't
    silently decrypt to garbage. Any existing file at `target_path` is
    preserved first, same safety rule as the JSON restore.
    """
    secrets_dir = _secrets_dir(backup_root)
    salt_path = secrets_dir / SALT_FILENAME
    enc_path = secrets_dir / ENV_BACKUP_FILENAME
    if not salt_path.is_file() or not enc_path.is_file():
        return EnvRestoreResult(success=False, error=f"No encrypted .env backup found at {secrets_dir}")
    if not passphrase:
        return EnvRestoreResult(success=False, error="A passphrase is required.")

    try:
        salt = salt_path.read_bytes()
        key = _derive_key(passphrase, salt)
        plaintext = Fernet(key).decrypt(enc_path.read_bytes())
    except InvalidToken:
        return EnvRestoreResult(success=False, error="Wrong passphrase (or a corrupted backup).")
    except OSError as exc:
        return EnvRestoreResult(success=False, error=str(exc))

    target_path = Path(target_path)
    preserved: Optional[Path] = None
    try:
        if preserve_existing and target_path.exists():
            preserved = target_path.with_name(
                f"{target_path.name}.pre_restore_{datetime.now():%Y%m%d_%H%M%S}"
            )
            shutil.copy2(target_path, preserved)
        _atomic_write(target_path, plaintext)
    except OSError as exc:
        return EnvRestoreResult(success=False, error=str(exc), preserved_original=preserved)

    return EnvRestoreResult(success=True, restored_path=target_path, preserved_original=preserved)
