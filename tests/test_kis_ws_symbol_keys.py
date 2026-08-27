from __future__ import annotations

import json
import os

import pytest

from scripts.manage_kis_ws_symbol_keys import main as manage_main
from src.services.kis_ws_symbol_keys import (
    KisWsSymbolKeyStore,
    KisWsSymbolKeysError,
    derive_symbol_key_from_kis_master,
    read_symbol_keys_file,
    update_symbol_keys_file,
    write_symbol_keys_file,
)


def _write_kis_master(path, rows):
    path.write_text(
        "Symbol,KisSymbol,Exchange,Name,KoreanName,Currency\n"
        + "".join(
            f"{symbol},{kis_symbol},{exchange},Example,,USD\n"
            for symbol, kis_symbol, exchange in rows
        ),
        encoding="utf-8",
    )


def test_missing_key_is_atomically_provisioned_from_kis_master(tmp_path):
    key_path = tmp_path / "kis_ws_symbol_keys.json"
    master_path = tmp_path / "us_kis_tickers.csv"
    write_symbol_keys_file({"AAPL": "DNASAAPL"}, key_path)
    _write_kis_master(master_path, [("RNG", "RNG", "NYS")])
    store = KisWsSymbolKeyStore(
        key_path,
        legacy_json="{}",
        universe_path=master_path,
        auto_provision=True,
    )

    assert store.resolve("rng") == "DNYSRNG"
    assert read_symbol_keys_file(key_path) == {
        "AAPL": "DNASAAPL",
        "RNG": "DNYSRNG",
    }
    assert key_path.with_suffix(".json.bak").is_file()


def test_master_provisioning_uses_kis_native_symbol_spelling(tmp_path):
    master_path = tmp_path / "us_kis_tickers.csv"
    _write_kis_master(master_path, [("BRK-B", "BRK/B", "NYS")])

    assert derive_symbol_key_from_kis_master("BRK-B", master_path) == "DNYSBRK/B"


def test_master_provisioning_fails_closed_for_unknown_or_unsupported_exchange(
    tmp_path,
):
    master_path = tmp_path / "us_kis_tickers.csv"
    _write_kis_master(master_path, [("SHOP", "SHOP", "TSE")])

    with pytest.raises(KisWsSymbolKeysError, match="no supported US exchange"):
        derive_symbol_key_from_kis_master("SHOP", master_path)
    with pytest.raises(KisWsSymbolKeysError, match="not present"):
        derive_symbol_key_from_kis_master("MISSING", master_path)


def test_file_replaces_legacy_env_and_hot_reloads_additions(tmp_path):
    path = tmp_path / "kis_ws_symbol_keys.json"
    write_symbol_keys_file({"AAPL": "DNASAAPL"}, path)
    store = KisWsSymbolKeyStore(path, legacy_json='{"OLD":"DNASOLD"}')

    first = store.snapshot()
    assert dict(first.keys) == {"AAPL": "DNASAAPL"}
    assert first.source == "FILE"

    update_symbol_keys_file(set_values={"MSFT": "DNASMSFT"}, path=path)

    assert store.resolve("MSFT") == "DNASMSFT"
    assert store.snapshot().generation > first.generation
    assert "OLD" not in store.snapshot().keys


def test_legacy_env_is_only_a_missing_file_migration_fallback(tmp_path):
    path = tmp_path / "kis_ws_symbol_keys.json"
    store = KisWsSymbolKeyStore(path, legacy_json='{"AAPL":"DNASAAPL"}')

    snapshot = store.snapshot()

    assert snapshot.source == "LEGACY_ENV"
    assert store.resolve("AAPL") == "DNASAAPL"


def test_invalid_initial_file_does_not_hide_behind_legacy_env(tmp_path):
    path = tmp_path / "kis_ws_symbol_keys.json"
    path.write_text("{", encoding="utf-8")
    store = KisWsSymbolKeyStore(path, legacy_json='{"AAPL":"DNASAAPL"}')

    snapshot = store.snapshot()

    assert snapshot.keys == {}
    assert snapshot.last_error
    with pytest.raises(RuntimeError, match="No live-verified"):
        store.resolve("AAPL")


def test_malformed_or_missing_intraday_file_retains_last_known_good_map(tmp_path):
    path = tmp_path / "kis_ws_symbol_keys.json"
    write_symbol_keys_file({"AAPL": "DNASAAPL"}, path)
    store = KisWsSymbolKeyStore(path, legacy_json="{}")
    assert store.resolve("AAPL") == "DNASAAPL"

    path.write_text("{", encoding="utf-8")
    malformed = store.snapshot()
    assert malformed.keys["AAPL"] == "DNASAAPL"
    assert "last-known-good" in malformed.last_error

    path.unlink()
    missing = store.snapshot()
    assert missing.keys["AAPL"] == "DNASAAPL"
    assert missing.last_error

    write_symbol_keys_file({"AAPL": "DNASAAPL", "MSFT": "DNASMSFT"}, path)
    assert store.resolve("MSFT") == "DNASMSFT"
    assert store.snapshot().last_error == ""


def test_atomic_updates_preserve_other_symbols_and_create_backup(tmp_path):
    path = tmp_path / "kis_ws_symbol_keys.json"
    write_symbol_keys_file({"AAPL": "DNASAAPL"}, path)

    updated = update_symbol_keys_file(
        set_values={"msft": "DNASMSFT"},
        path=path,
    )

    assert updated == {"AAPL": "DNASAAPL", "MSFT": "DNASMSFT"}
    assert read_symbol_keys_file(path) == updated
    assert path.with_suffix(".json.bak").is_file()


def test_migration_refuses_to_overwrite_a_conflicting_reviewed_key(tmp_path):
    path = tmp_path / "kis_ws_symbol_keys.json"
    write_symbol_keys_file({"AAPL": "OLDKEY"}, path)

    with pytest.raises(KisWsSymbolKeysError, match="refusing to replace"):
        update_symbol_keys_file(
            set_values={"AAPL": "NEWKEY"},
            path=path,
            refuse_conflicts=True,
        )

    assert read_symbol_keys_file(path) == {"AAPL": "OLDKEY"}


def test_cli_migrates_without_modifying_environment_value(tmp_path, monkeypatch, capsys):
    path = tmp_path / "kis_ws_symbol_keys.json"
    original = json.dumps({"AAPL": "DNASAAPL", "MSFT": "DNASMSFT"})
    monkeypatch.setenv("KIS_WS_SYMBOL_KEYS_JSON", original)

    assert manage_main(["--file", str(path), "migrate-env"]) == 0

    assert read_symbol_keys_file(path) == {
        "AAPL": "DNASAAPL",
        "MSFT": "DNASMSFT",
    }
    assert os.environ["KIS_WS_SYMBOL_KEYS_JSON"] == original
    assert "were not modified" in capsys.readouterr().out


def test_symbol_key_file_is_in_the_existing_offsite_state_backup_allowlist():
    from src.services.cloud_backup import STATE_BACKUP_FILENAMES

    assert "kis_ws_symbol_keys.json" in STATE_BACKUP_FILENAMES
