# KIS Dashboard Integration

## Current State

The repository now has a read-only KIS account snapshot path that can be used from both the command line and the Dashboard tab.

Relevant files:

- `src/api/kis_account_snapshot_dual.py`: standalone KIS balance/holdings client for SIM and PROD profiles.
- `src/api/kis_account_profiles_env_example.txt`: environment variable template for KIS credentials.
- `src/ui/main_window.py`: Dashboard UI section for fetching and displaying account snapshots.
- `.gitignore`: ignores `.env` and `.kis_token_cache*.json`.

## Dashboard Behavior

The Dashboard tab includes a **KIS Account Snapshot** section with:

- Profile selector: `SIM` or `PROD`.
- Section toggles: domestic, overseas, or both.
- Configuration status text showing missing env keys or the masked account number.
- `Refresh KIS Snapshot` button.
- Summary text for fetched cash/evaluation data.
- Holdings table with market, symbol, name, quantity, average price, current price, evaluation value, and P/L percentage.

The dashboard only calls read-only balance/holdings APIs. Order placement is not wired into the UI.

## Configuration

Copy the needed values from `src/api/kis_account_profiles_env_example.txt` into `.env`.

For PROD, actual credentials are stored in `.env`. `src/api/kis_config.py` is now only a compatibility loader for older scripts.

Required PROD values:

```env
KIS_PROD_BASE_URL=https://openapi.koreainvestment.com:9443
KIS_PROD_APP_KEY=...
KIS_PROD_APP_SECRET=...
KIS_PROD_ACCOUNT_NO=63187257-01
```

KIS balance APIs require the account number in each request, and the available sample/documented balance flows do not expose a general "list all my accounts from app key" endpoint. The Dashboard account dropdown therefore lists locally configured account numbers.

For SIM, an 8-digit account number is accepted and defaults to product code `01`:

```env
KIS_SIM_ACCOUNT_NO=50194787
KIS_SIM_ACCOUNT_PRODUCT_CODE=01
```

If KIS returns `INPUT INVALID_CHECK_ACNO`, the app key is working but KIS rejected the selected account number/product code. Confirm the virtual/live account number and product code in KIS, then update the relevant `KIS_*_ACCOUNT_NO` or `KIS_*_ACCOUNT_PRODUCT_CODE` value.

Read-only product-code diagnostic:

```powershell
python src\api\kis_account_snapshot_dual.py --env PROD --probe-product-codes "01,03,08,22"
```

Last PROD diagnostic result for account `63******`:

- Product codes tested: `01`, `03`, `08`, `22`
- Result: all rejected by KIS with `INPUT INVALID_CHECK_ACNO`
- Interpretation: the PROD app key is not linked to that live account, the live account number is different, or KIS requires a different account registration/API approval for balance inquiry.

If KIS returns `EGW00201`, the per-second API request limit was exceeded. The client retries with backoff and the Dashboard briefly disables the refresh button after each attempt, but repeated manual refreshes can still trigger the limit.

Minimum SIM profile:

```env
KIS_SIM_APP_KEY=your_sim_app_key
KIS_SIM_APP_SECRET=your_sim_app_secret
KIS_SIM_ACCOUNT_NO=12345678-01
```

Multiple PROD accounts can be configured either as a comma-separated list:

```env
KIS_PROD_ACCOUNTS=12345678-01,87654321-01
```

or as numbered entries:

```env
KIS_PROD_ACCOUNT_NO_1=12345678-01
KIS_PROD_ACCOUNT_NO_2=87654321-01
```

The same patterns work for SIM by replacing `KIS_PROD_` with `KIS_SIM_`.

Token caches default to `.kis_token_cache_sim.json` and `.kis_token_cache_prod.json`.

## Command-Line Verification

Before using the dashboard, verify connectivity directly:

```powershell
python src\api\kis_account_snapshot_dual.py --env SIM --domestic
python src\api\kis_account_snapshot_dual.py --env PROD --domestic
python src\api\kis_account_snapshot_dual.py --env SIM --domestic --overseas
```

Use `--force-token` if KIS rejects a cached token.

## Watchlist Market Data Fetch

`src/api/kis_fetch_all_daily.py` now supports a watchlist-specific overseas fetch using the PROD keys in `src/api/kis_config.py`.

Run:

```powershell
python src\api\kis_fetch_all_daily.py --watchlist-overseas --output data\kis_watchlist_overseas_latest.csv
```

The command reads `data/watchlist.json`, tries U.S. exchanges in this order: `NAS`, `NYS`, `AMS`, and saves the latest KIS daily OHLCV row for each resolved ticker.

Last verified watchlist result:

- Rows fetched: 9
- Output file: `data/kis_watchlist_overseas_latest.csv`
- Symbols fetched: `CDW`, `DLTR`, `MRVL`, `SNDK`, `CRL`, `DELL`, `HPE`, `HUM`, `MGM`
- Data date returned by KIS: `2026-06-24`

## Next Steps

- Add real SIM credentials to `.env` first and test the standalone script.
- Confirm the correct overseas `TR_ID` for your KIS account if overseas holdings return a TR_ID error.
- After SIM works, add PROD credentials and test PROD from the command line before using the dashboard.
- Decide whether KIS overseas daily data should replace yfinance for the Charts tab, or only act as a fallback/verification source.
