from cryptography.fernet import Fernet

import src.services.env_backup as env_backup
from src.services.env_backup import (
    ENV_BACKUP_FILENAME,
    ENV_BACKUP_MAGIC,
    SALT_FILENAME,
    SECRETS_DIRNAME,
    _derive_key,
    backup_env_file,
    env_backup_exists,
    restore_env_file,
)
from src.services.cloud_backup import BACKUP_SUBDIR_NAME


def _write_env(tmp_path, content='MYSQL_PASSWORD=hunter2\nKIS_PROD_APP_SECRET=abc123\n'):
    env_path = tmp_path / ".env"
    env_path.write_text(content, encoding="utf-8")
    return env_path


def test_backup_env_file_writes_single_atomic_envelope(tmp_path):
    env_path = _write_env(tmp_path)
    backup_root = tmp_path / "drive"

    result = backup_env_file(env_path, backup_root, "correct horse battery staple")

    assert result.success
    secrets_dir = backup_root / BACKUP_SUBDIR_NAME / SECRETS_DIRNAME
    assert (secrets_dir / ENV_BACKUP_FILENAME).is_file()
    assert not (secrets_dir / SALT_FILENAME).exists()
    # Ciphertext must not contain the plaintext secret anywhere.
    envelope = (secrets_dir / ENV_BACKUP_FILENAME).read_bytes()
    assert envelope.startswith(ENV_BACKUP_MAGIC)
    assert b"hunter2" not in envelope
    assert b"abc123" not in envelope


def test_backup_env_file_missing_source_fails_cleanly(tmp_path):
    result = backup_env_file(tmp_path / "does_not_exist.env", tmp_path / "drive", "passphrase")
    assert not result.success
    assert "does not exist" in result.error


def test_backup_env_file_requires_passphrase(tmp_path):
    env_path = _write_env(tmp_path)
    result = backup_env_file(env_path, tmp_path / "drive", "")
    assert not result.success
    assert "passphrase" in result.error.lower()


def test_backup_env_file_rejects_short_passphrase(tmp_path):
    env_path = _write_env(tmp_path)
    result = backup_env_file(env_path, tmp_path / "drive", "too-short")
    assert not result.success
    assert "at least" in result.error.lower()


def test_failed_env_backup_keeps_previous_envelope(tmp_path, monkeypatch):
    env_path = _write_env(tmp_path)
    backup_root = tmp_path / "drive"
    assert backup_env_file(env_path, backup_root, "correct horse battery staple").success
    enc_path = (
        backup_root
        / BACKUP_SUBDIR_NAME
        / SECRETS_DIRNAME
        / ENV_BACKUP_FILENAME
    )
    original = enc_path.read_bytes()

    def fail_write(dest, data):
        raise OSError("simulated full Drive")

    monkeypatch.setattr(env_backup, "_atomic_write", fail_write)
    result = backup_env_file(env_path, backup_root, "another durable passphrase")

    assert not result.success
    assert enc_path.read_bytes() == original


def test_env_backup_exists_before_and_after(tmp_path):
    backup_root = tmp_path / "drive"
    assert not env_backup_exists(backup_root)

    env_path = _write_env(tmp_path)
    backup_env_file(env_path, backup_root, "correct horse battery staple")

    assert env_backup_exists(backup_root)


def test_restore_env_file_round_trips_with_correct_passphrase(tmp_path):
    content = "MYSQL_PASSWORD=hunter2\nKIS_PROD_APP_SECRET=abc123\n"
    env_path = _write_env(tmp_path, content)
    backup_root = tmp_path / "drive"
    backup_env_file(env_path, backup_root, "correct horse battery staple")

    target = tmp_path / "restored" / ".env"
    result = restore_env_file(backup_root, target, "correct horse battery staple")

    assert result.success
    assert target.read_text(encoding="utf-8") == content
    assert result.preserved_original is None


def test_restore_env_file_wrong_passphrase_fails_cleanly(tmp_path):
    env_path = _write_env(tmp_path)
    backup_root = tmp_path / "drive"
    backup_env_file(env_path, backup_root, "correct horse battery staple")

    target = tmp_path / "restored" / ".env"
    result = restore_env_file(backup_root, target, "wrong passphrase entirely")

    assert not result.success
    assert "wrong passphrase" in result.error.lower()
    assert not target.exists()


def test_restore_env_file_supports_legacy_two_file_backup(tmp_path):
    backup_root = tmp_path / "drive"
    secrets_dir = backup_root / BACKUP_SUBDIR_NAME / SECRETS_DIRNAME
    secrets_dir.mkdir(parents=True)
    salt = b"0123456789abcdef"
    passphrase = "correct horse battery staple"
    plaintext = b"MYSQL_PASSWORD=legacy\n"
    (secrets_dir / SALT_FILENAME).write_bytes(salt)
    (secrets_dir / ENV_BACKUP_FILENAME).write_bytes(
        Fernet(_derive_key(passphrase, salt)).encrypt(plaintext)
    )

    target = tmp_path / "restored" / ".env"
    result = restore_env_file(backup_root, target, passphrase)

    assert result.success
    assert target.read_bytes() == plaintext


def test_restore_env_file_rejects_corrupt_envelope(tmp_path):
    backup_root = tmp_path / "drive"
    secrets_dir = backup_root / BACKUP_SUBDIR_NAME / SECRETS_DIRNAME
    secrets_dir.mkdir(parents=True)
    (secrets_dir / ENV_BACKUP_FILENAME).write_bytes(ENV_BACKUP_MAGIC + b"{")

    target = tmp_path / "restored" / ".env"
    result = restore_env_file(
        backup_root,
        target,
        "correct horse battery staple",
    )

    assert not result.success
    assert not target.exists()


def test_restore_env_file_preserves_existing_target(tmp_path):
    env_path = _write_env(tmp_path)
    backup_root = tmp_path / "drive"
    backup_env_file(env_path, backup_root, "passphrase123456")

    target = tmp_path / "restored" / ".env"
    target.parent.mkdir(parents=True)
    target.write_text("OLD_LOCAL_VALUE=1\n", encoding="utf-8")

    result = restore_env_file(backup_root, target, "passphrase123456")

    assert result.success
    assert result.preserved_original is not None
    assert result.preserved_original.read_text(encoding="utf-8") == "OLD_LOCAL_VALUE=1\n"
    assert "hunter2" in target.read_text(encoding="utf-8")


def test_restore_env_file_missing_backup_fails_cleanly(tmp_path):
    result = restore_env_file(tmp_path / "drive", tmp_path / ".env", "any passphrase")
    assert not result.success
    assert "No encrypted" in result.error
