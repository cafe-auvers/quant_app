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

## Environment files

`.env.example` is tracked and contains no private values. `.env` and `.env.pc`
are local and ignored. Every normal startup merges new template keys into
`.env` while preserving existing values, then regenerates `.env.pc` with
`MYSQL_*` values blank for manual PC setup.

Manual synchronization:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\sync_pc_env.ps1
```

Never commit `.env`, `.env.pc`, KIS token caches, certificates with private
keys, account state, database files, or `data/*.json` runtime state.

## Optional services

- MySQL: configure `MYSQL_*`; the single-machine app can run without it.
- Coordination SQL: configure `COORD_DB_*` with TLS verification.
- KIS: configure only the required `KIS_PROD_*` values locally.
- Cloud backup: configure `QUANT_BACKUP_DIR` or use supported Google Drive
  auto-detection.

Proceed to [Configuration](Configuration).
