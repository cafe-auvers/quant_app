from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.utils.env_sync import synchronize_environment_files
from src.utils.config import load_runtime_config
from src.utils import env_sync as env_sync_module


def _values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value
    return values


def test_sync_keeps_credentials_and_migrates_runtime_values_to_json(tmp_path):
    template = tmp_path / ".env.example"
    env = tmp_path / ".env"
    pc_env = tmp_path / ".env.pc"
    runtime_defaults = tmp_path / "config" / "runtime.json"
    runtime_local = tmp_path / "config" / "runtime.local.json"
    runtime_defaults.parent.mkdir()
    template.write_text(
        "# latest template comment\n"
        "MYSQL_USER=\n"
        "MYSQL_PASSWORD=\n"
        "API_TOKEN=\n",
        encoding="utf-8",
    )
    runtime_defaults.write_text(
        json.dumps(
            {
                "MYSQL_HOST": "template-host",
                "MYSQL_PORT": "3306",
                "NEW_SETTING": "safe-default",
            }
        ),
        encoding="utf-8",
    )
    env.write_text(
        "# old local layout\n"
        "API_TOKEN=private-token\n"
        "MYSQL_USER=private-db-user\n"
        "MYSQL_HOST=private-db\n",
        encoding="utf-8",
    )

    result = synchronize_environment_files(
        template,
        env,
        pc_env,
        runtime_defaults,
        runtime_local,
    )

    assert result.env_changed is True
    assert result.pc_env_changed is True
    assert result.runtime_local_changed is True
    assert result.migrated_runtime_keys == ("MYSQL_HOST",)
    assert result.added_env_keys == ("MYSQL_PASSWORD",)
    assert result.mysql_values_blanked == 2
    assert _values(env) == {
        "MYSQL_USER": "private-db-user",
        "MYSQL_PASSWORD": "",
        "API_TOKEN": "private-token",
    }
    assert json.loads(runtime_local.read_text(encoding="utf-8")) == {
        "MYSQL_HOST": "private-db"
    }
    assert "# latest template comment" in env.read_text(encoding="utf-8")

    pc_values = _values(pc_env)
    assert pc_values["MYSQL_USER"] == ""
    assert pc_values["MYSQL_PASSWORD"] == ""
    assert pc_values["API_TOKEN"] == "private-token"


def test_sync_is_idempotent_and_does_not_rewrite_current_files(tmp_path):
    template = tmp_path / ".env.example"
    env = tmp_path / ".env"
    pc_env = tmp_path / ".env.pc"
    runtime_defaults = tmp_path / "config" / "runtime.json"
    runtime_local = tmp_path / "config" / "runtime.local.json"
    runtime_defaults.parent.mkdir()
    runtime_defaults.write_text("{}\n", encoding="utf-8")
    template.write_text("A=one\nMYSQL_PASSWORD=\n", encoding="utf-8")

    first = synchronize_environment_files(
        template, env, pc_env, runtime_defaults, runtime_local
    )
    env_mtime = env.stat().st_mtime_ns
    pc_env_mtime = pc_env.stat().st_mtime_ns
    second = synchronize_environment_files(
        template, env, pc_env, runtime_defaults, runtime_local
    )

    assert first.env_changed is True
    assert first.pc_env_changed is True
    assert second.env_changed is False
    assert second.pc_env_changed is False
    assert env.stat().st_mtime_ns == env_mtime
    assert pc_env.stat().st_mtime_ns == pc_env_mtime


def test_sync_rejects_duplicate_template_keys_without_writing(tmp_path):
    template = tmp_path / ".env.example"
    env = tmp_path / ".env"
    pc_env = tmp_path / ".env.pc"
    runtime_defaults = tmp_path / "config" / "runtime.json"
    runtime_local = tmp_path / "config" / "runtime.local.json"
    runtime_defaults.parent.mkdir()
    runtime_defaults.write_text("{}\n", encoding="utf-8")
    template.write_text("DUPLICATE=one\nDUPLICATE=two\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate environment key"):
        synchronize_environment_files(
            template, env, pc_env, runtime_defaults, runtime_local
        )

    assert not env.exists()
    assert not pc_env.exists()


def test_runtime_and_secret_schemas_cannot_overlap(tmp_path):
    template = tmp_path / ".env.example"
    env = tmp_path / ".env"
    pc_env = tmp_path / ".env.pc"
    runtime_defaults = tmp_path / "config" / "runtime.json"
    runtime_defaults.parent.mkdir()
    template.write_text("API_TOKEN=\n", encoding="utf-8")
    runtime_defaults.write_text('{"API_TOKEN":"not-allowed"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="both secret and runtime"):
        synchronize_environment_files(
            template,
            env,
            pc_env,
            runtime_defaults,
            tmp_path / "config" / "runtime.local.json",
        )


def test_sync_rejects_unclassified_env_keys_without_rewriting(tmp_path):
    template = tmp_path / ".env.example"
    env = tmp_path / ".env"
    pc_env = tmp_path / ".env.pc"
    defaults = tmp_path / "config" / "runtime.json"
    local = tmp_path / "config" / "runtime.local.json"
    defaults.parent.mkdir()
    template.write_text("API_TOKEN=\n", encoding="utf-8")
    defaults.write_text("{}\n", encoding="utf-8")
    original = "API_TOKEN=private\nNON_SECRET_FLAG=true\n"
    env.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="Unclassified .env key"):
        synchronize_environment_files(template, env, pc_env, defaults, local)

    assert env.read_text(encoding="utf-8") == original
    assert not local.exists()
    assert not pc_env.exists()


def test_runtime_loader_merges_only_known_local_overrides(tmp_path):
    defaults = tmp_path / "runtime.json"
    local = tmp_path / "runtime.local.json"
    defaults.write_text('{"MODE":"SAFE","LIMIT":"2"}\n', encoding="utf-8")
    local.write_text('{"LIMIT":"3"}\n', encoding="utf-8")

    assert load_runtime_config(defaults, local) == {"MODE": "SAFE", "LIMIT": "3"}

    local.write_text('{"UNKNOWN":"value"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown local runtime"):
        load_runtime_config(defaults, local)


def test_runtime_migration_is_persisted_before_legacy_env_is_stripped(
    tmp_path, monkeypatch
):
    template = tmp_path / ".env.example"
    env = tmp_path / ".env"
    pc_env = tmp_path / ".env.pc"
    defaults = tmp_path / "config" / "runtime.json"
    local = tmp_path / "config" / "runtime.local.json"
    defaults.parent.mkdir()
    template.write_text("API_TOKEN=\n", encoding="utf-8")
    defaults.write_text('{"RUNTIME_LIMIT":"2"}\n', encoding="utf-8")
    env.write_text("API_TOKEN=private\nRUNTIME_LIMIT=7\n", encoding="utf-8")
    real_writer = env_sync_module._write_if_changed

    def fail_runtime_write(path, content):
        if Path(path) == local.resolve():
            raise OSError("simulated runtime write failure")
        return real_writer(path, content)

    monkeypatch.setattr(env_sync_module, "_write_if_changed", fail_runtime_write)

    with pytest.raises(OSError, match="simulated runtime write failure"):
        synchronize_environment_files(template, env, pc_env, defaults, local)

    assert "RUNTIME_LIMIT=7" in env.read_text(encoding="utf-8")
    assert not pc_env.exists()
