# Quick Start

## Safe local start

1. Use Python 3.11 or 3.12 on Windows.
2. From the repository root, install the tested dependency graph:

   ```powershell
   python -m pip install --require-hashes -r requirements.lock
   python -m pip check
   ```

3. Copy no secrets into source files. Put optional MySQL and KIS values in the
   gitignored `.env`; use `.env.example` as the schema.
4. Start the desktop app:

   ```powershell
   python main.py
   ```

5. Run the complete offline test suite before changing trading code:

   ```powershell
   pytest -q
   ```

The app can open without MySQL or KIS. Database-backed scanning, cache
freshness, account data, and broker operations will remain unavailable or
read-only as appropriate.

## First-session checklist

- Confirm the toolbar Live Trading control is off.
- The Buy Board runtime may be available, but confirm
  `KIS_LIVE_EXECUTION_MODE=DISABLED`, Live Trading is locked/off, and broker
  mutations remain blocked unless you are following the controlled-live
  runbook.
- Open Health and review MySQL, KIS age, mirror freshness, reconciliation,
  journal health, and free-space checks.
- Use Scanner, Watchlist, Buylist, and charts before configuring execution.
- Never use real credentials to run tests.

See [Configuration](Configuration) before enabling optional services.
