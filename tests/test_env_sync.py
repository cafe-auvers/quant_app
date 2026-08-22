from __future__ import annotations

from pathlib import Path

import pytest

from src.utils.env_sync import synchronize_environment_files


def _values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value
    return values


def test_sync_uses_latest_template_without_replacing_private_values(tmp_path):
    template = tmp_path / ".env.example"
    env = tmp_path / ".env"
    pc_env = tmp_path / ".env.pc"
    template.write_text(
        "# latest template comment\n"
        "MYSQL_HOST=template-host\n"
        "MYSQL_PORT=3306\n"
        "API_TOKEN=\n"
        "NEW_SETTING=safe-default\n",
        encoding="utf-8",
    )
    env.write_text(
        "# old local layout\n"
        "API_TOKEN=private-token\n"
        "MYSQL_HOST=private-db\n"
        "LOCAL_ONLY=keep-me\n",
        encoding="utf-8",
    )

    result = synchronize_environment_files(template, env, pc_env)

    assert result.env_changed is True
    assert result.pc_env_changed is True
    assert result.added_env_keys == ("MYSQL_PORT", "NEW_SETTING")
    assert result.mysql_values_blanked == 2
    assert _values(env) == {
        "MYSQL_HOST": "private-db",
        "MYSQL_PORT": "3306",
        "API_TOKEN": "private-token",
        "NEW_SETTING": "safe-default",
        "LOCAL_ONLY": "keep-me",
    }
    assert "# latest template comment" in env.read_text(encoding="utf-8")

    pc_values = _values(pc_env)
    assert pc_values["MYSQL_HOST"] == ""
    assert pc_values["MYSQL_PORT"] == ""
    assert pc_values["API_TOKEN"] == "private-token"
    assert pc_values["NEW_SETTING"] == "safe-default"
    assert pc_values["LOCAL_ONLY"] == "keep-me"


def test_sync_is_idempotent_and_does_not_rewrite_current_files(tmp_path):
    template = tmp_path / ".env.example"
    env = tmp_path / ".env"
    pc_env = tmp_path / ".env.pc"
    template.write_text("A=one\nMYSQL_DB=quant_app\n", encoding="utf-8")

    first = synchronize_environment_files(template, env, pc_env)
    env_mtime = env.stat().st_mtime_ns
    pc_env_mtime = pc_env.stat().st_mtime_ns
    second = synchronize_environment_files(template, env, pc_env)

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
    template.write_text("DUPLICATE=one\nDUPLICATE=two\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate environment key"):
        synchronize_environment_files(template, env, pc_env)

    assert not env.exists()
    assert not pc_env.exists()
