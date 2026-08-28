# Installation and Environment Setup

## Supported environment

- Windows desktop
- Python 3.11 or 3.12
- PyQt5/PyQtWebEngine
- Optional MySQL-compatible market database
- Optional TLS-only coordination database

Create and activate a virtual environment if desired, then install the lock:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --require-hashes -r requirements.lock
python -m pip check
```

`requirements.txt` is the supported direct dependency range; the hash-locked
file is the reproducible installation input.

## Credential and runtime configuration

`.env.example` is the tracked credential-only schema. `.env` and `.env.pc` are
local and ignored. Non-secret settings live in tracked `config/runtime.json`;
machine overrides live in ignored `config/runtime.local.json`. Startup migrates
recognized legacy runtime values out of `.env` without changing them, then
regenerates credential-only `.env.pc` with `MYSQL_*` credentials blank.

Manual synchronization:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\sync_pc_env.ps1
```

Never commit `.env`, `.env.pc`, `config/runtime.local.json`, KIS token caches, certificates with private
keys, account state, database files, or `data/*.json` runtime state.

## Optional services

- MySQL: put username/password in `.env` and host/port/database in runtime config.
- Coordination SQL: put username/password in `.env` and host/port/TLS path in runtime config.
- KIS: configure only the required `KIS_PROD_*` values locally.
- Cloud backup: configure `QUANT_BACKUP_DIR` in runtime config or use supported Google Drive
  auto-detection.

Proceed to [Configuration](Configuration).
