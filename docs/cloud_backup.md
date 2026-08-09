# Offsite Backup of User-State Files

## Why

Most of `data/*.json` is gitignored (see `.gitignore`) -- it's runtime
state, not code, and churns on every UI action. That also means, unlike
everything under `src/`, it exists **only** on the machine that wrote it.
`watchlist`/`buylist`/`trade_plans` additionally get pushed to the PC's
shared MySQL (`app_state_sync` table, see
[pc_sync_data_pipeline.md](pc_sync_data_pipeline.md)) whenever the PC is
reachable, but the rest never do. A dead disk or an accidental delete on
the laptop has no recovery path without this.

**11 files are backed up** -- `STATE_BACKUP_FILENAMES` in
`src/services/cloud_backup.py` is the source of truth for the exact list:

| File | What it is |
|---|---|
| `watchlist.json` | Watchlist |
| `buylist.json` | Buy Dashboard state |
| `trade_plans.json` | Legacy trade plans |
| `scanner_setups.json` | Saved scanner threshold presets |
| `chart_drawings.json` | User-drawn chart lines |
| `tab_options.json` | Which tabs are shown |
| `settings.json` | Keyboard shortcuts, chart pan step |
| `orders.json` | **Order ledger -- real trade/order history** |
| `execution_queue.json` | **Live execution queue -- active trading state** |
| `legacy_non_prod_buylist.json` | Archived non-PROD buylist rows |
| `legacy_non_prod_execution_queue.json` | Archived non-PROD queue rows |

`orders.json` and `execution_queue.json` were missing from the first
version of this list -- they'd been grouped in with cosmetic settings files
and the actual order/trading data got overlooked. Fixed; both are included
now.

**Deliberately left out** (not oversights -- each is either regenerable or
genuinely optional):
- `data/local_mirror.db` -- disposable market-data cache, rebuilds itself
  (see "What this does NOT recover" below).
- `data/sp500_tickers.csv`, `data/us_kis_tickers.csv` -- ticker-universe
  caches, auto-re-fetched from source whenever the cache file is missing
  (`get_sp500_tickers`/`get_us_kis_tickers` in `data_loader.py`).
- `data/watchlist_snapshot_*.json` -- one-off manual debug exports (Save
  Data Snapshot button), not live state.
- `data/device_role.json`, `data/state_metadata.json` -- see "What this
  does NOT recover" below for why these are safe to skip.

## What it does

`src/services/cloud_backup.py` copies those files into a local folder
that a sync client (Google Drive for Desktop, OneDrive, Dropbox -- anything
that mirrors a cloud folder to a local path) is already watching. It never
talks to a cloud API itself; the sync client already running on the machine
does the actual upload. It runs after every successful `StateSaveManager`
save and is throttled to at most once per 10 minutes so it doesn't spam the
sync client on rapid edits. The backup is fully best-effort --
if the destination folder isn't found, or the copy fails, the local save
that already happened is unaffected and the app keeps working normally.

Two tiers are kept under `<synced folder>/quant_app_backup/`:

- `current/` -- the latest copy of each file, overwritten every backup pass.
- `daily/<YYYY-MM-DD>/` -- one full snapshot per calendar day, so a bad
  edit noticed days later is still recoverable (the single `.bak` generation
  that `save_json` keeps next to each file on disk only survives one bad
  write). The latest 21 daily snapshot directories are kept by default,
  then older dated snapshots are pruned automatically.

Each JSON file is parsed before it replaces a good cloud copy. A malformed
local file is reported and skipped, and later backup passes fill any files
missing from an interrupted daily snapshot without changing the files that
were already captured that day.

## Setup

1. Install **Google Drive for Desktop** and sign in:
   https://www.google.com/drive/download/
2. During normal automatic backup, the app only writes into this folder.
   Reading from it happens only when you explicitly choose a restore. Either
   Google Drive mode works:
   - **Mirror files** (recommended) -- creates a real local folder with
     actual file contents, usually mounted as a drive letter (`G:\My Drive`
     by default).
   - **Stream files** -- files show as on-demand placeholders until opened,
     but writing new files into the folder still works and still uploads.
3. That's it -- `resolve_backup_root()` auto-detects the folder on a backup
   attempt (checks drive letters `A:` through `Z:` for a `My Drive` folder,
   then a few common home-folder locations). If Drive is not mounted yet,
   the app retries discovery on later saves, at most once per 10 minutes.
4. To point at a specific folder instead of relying on auto-detection (a
   renamed Drive folder, OneDrive, a different drive letter, testing),
   set `QUANT_BACKUP_DIR` in `.env`:
   ```text
   QUANT_BACKUP_DIR=G:\My Drive
   ```

## Recovering after a crash

### In-app (normal case -- the app itself still runs)

**File > Restore from Cloud Backup...** -- lists "Latest" plus every
available daily snapshot date, restores the one you pick into `data/`
by first validating and staging an immutable local copy. The app then closes
cleanly, preserves existing files into a timestamped
`pre_restore_backup_...` folder, applies the staged copy, and restarts. This
ordering prevents shutdown saves from overwriting the restored files. It is
the normal path any time you want to roll back, not just after a crash.

### From scratch (the laptop itself is gone -- nothing installed yet)

On the replacement machine, before the app can even run:

1. Install Google Drive for Desktop and sign into the **same Google
   account** the backups were written from -- your files are already
   waiting under `My Drive\quant_app_backup\` (Google's cloud storage isn't
   tied to the machine that uploaded them; any device signed into that
   account sees the same `My Drive`).
2. `git clone` the repo, run `pip install -r requirements.txt`.
3. Restore `.env` with the encrypted backup command described below, or
   recreate it manually. If `QUANT_BACKUP_DIR` was only stored in the lost
   `.env`, pass `--backup-dir` explicitly for this first restore.
4. Run `python scripts/restore_from_cloud_backup.py` (the same restore
   logic as the in-app dialog, usable before the GUI can even launch --
   e.g. to sanity-check the data landed correctly, or in case a broken
   Python/Qt environment needs fixing before `main.py` will start at all).
   `--list` shows available daily snapshots; `--snapshot 2026-08-05` picks
   one instead of the latest.
5. Launch the app normally from here on; use **File > Restore** for
   anything further (e.g. picking a different date after looking at what
   came back).

Either path: `watchlist`/`buylist`/`trade_plans` also reconcile against the
shared PC MySQL database on startup if it's reachable
([pc_sync_data_pipeline.md](pc_sync_data_pipeline.md)) -- expected, and it
merges by revision rather than clobbering the restore. `scanner_setups`/
`chart_drawings`/`tab_options`/`settings` are **not** stored in MySQL at
all, so this restore is their only recovery path. If this machine should
resume being the primary editor, click **"Become Main Device"** once the
restored data looks right -- a fresh machine always starts pull-only, it
doesn't happen automatically.

## What this does NOT recover

The `data/*.json` restore is necessary but **not sufficient** to get back
to normal work after a total laptop loss. Everything else needed:

| Piece | Backed up here? | How it actually comes back |
|---|---|---|
| `data/*.json` (11 files, see table above) | Yes | This module + File > Restore |
| Code (`src/`, etc.) | No -- it's in git | `git clone` |
| Python deps | No | `pip install -r requirements.txt` |
| **`.env`** (MySQL password, KIS keys/secrets, OpenAI key) | **Yes, encrypted, opt-in only** | See "Backing up `.env`" below |
| KIS token caches (`.kis_token_cache_*.json`) | No, and doesn't need to be | Auto-regenerates on next login (23h-lifetime cache, not data) |
| `data/local_mirror.db` (price/chart/scanner cache) | No, and doesn't need to be | Rebuilds itself from PC MySQL on next reachable connection, or a fresh `historical.py` pull from Yahoo/KIS if the PC is *also* gone -- it's market data, not yours, always re-fetchable |
| `data/sp500_tickers.csv`, `data/us_kis_tickers.csv` | No, and doesn't need to be | Auto-re-fetched from source (Wikipedia / KIS master file) the next time the scanner needs a universe and finds no cache |
| `data/device_role.json` | No, and shouldn't be | Regenerates automatically; a copied one resets itself by design |
| `data/state_metadata.json` (sync bookkeeping) | No, and doesn't need to be | Its absence just makes the next reconcile trust remote MySQL for `watchlist`/`buylist`/`trade_plans` -- the safe default, not a data-loss path (`activate_device_as_main` explicitly pulls before ever claiming write ownership, so there's no blind-overwrite risk either) |
| `data/watchlist_snapshot_*.json` (manual debug exports) | No | Not live state; re-create with the Save Data Snapshot button if still needed |

**Bottom line:** for the common case (laptop dies, PC + MySQL survive), the
JSON restore plus a fresh `git clone` + `.env` restore is everything you
need -- the market-data cache and the watchlist/buylist/trade-plan state all
rebuild or reconcile on their own.

## Backing up `.env`

Unlike the 11 JSON files, `.env` holds real secrets -- MySQL password,
KIS trading keys, OpenAI key -- so it is **never** written in plaintext to
the Drive folder, and it is **never** backed up automatically. Both are
deliberate: a consumer sync folder is a bad place for plaintext secrets,
and prompting for a passphrase from a background thread on a timer is both
bad UX and arguably bad security practice.

`src/services/env_backup.py` encrypts `.env` with a key derived
(PBKDF2-HMAC-SHA256, 600,000 iterations) from a passphrase you type and a
random salt, using Fernet (AES-128-CBC + HMAC) for the actual encryption.
The non-secret salt, KDF parameters, and ciphertext are stored together in
one atomically replaced `secrets/env.enc` envelope, avoiding a broken
salt/ciphertext pair if a write is interrupted. The passphrase is never
stored anywhere -- not in `.env`, not next to the backup, not in this repo. A
compromised Google account alone can't read your secrets back; the
passphrase is also required, and only you hold it. **Losing the passphrase
means the backup is permanently unrecoverable -- there is no backdoor.**
Keep it in a password manager.

**Back up:** File > Backup .env to Cloud (Encrypted)... (or
`python scripts/backup_env_to_cloud.py` from a terminal). Prompts for a
passphrase (twice, to confirm), then encrypts and writes. Do this again
any time you rotate a credential -- only the latest copy is kept, there's
no daily history for this one (no value in an old copy of rotated-out
credentials). A minimum of 12 characters is enforced; use a long, unique
password-manager-generated passphrase.

**Restore:** File > Restore .env from Cloud (Encrypted)... (or
`python scripts/restore_env_from_cloud.py`, usable before the GUI can even
launch on a fresh machine). Prompts for the same passphrase; a wrong one
fails cleanly (Fernet authenticates the ciphertext, so it can't silently
decrypt to garbage) rather than writing corrupted output. Any existing
local `.env` is preserved first, same rule as the JSON restore.

## Verifying it's working

Check the in-app log panel after your first save of a session -- it logs
either the resolved backup destination or an explicit note that none was
found. From then on, look under `<synced folder>/quant_app_backup/current/`
in Drive on the web to confirm the files are actually arriving. The app can
confirm only the local synced-folder write; Google Drive for Desktop is
responsible for the upload and this feature cannot prove that upload
completed.

The 11 state JSON files are plaintext in Drive and can contain positions,
orders, and strategy state. Protect the Google account with a strong unique
password and multi-factor authentication. Only `.env` receives the
additional passphrase encryption described above.

## What's intentionally out of scope

- `data/local_mirror.db` (and its `.db-wal`/`.db-shm`) -- disposable market-data
  cache, not user state; excluded from `.gitignore`'s data rules too, and
  explicitly called out in `pc_sync_data_pipeline.md` as "must not be
  committed." No value in backing up a cache that rebuilds itself.
- `data/device_role.json` -- per-machine sync identity. Backing it up would
  be meaningless (and `state_sync.py` already resets a copied role file by
  design if its stored hostname doesn't match).
- Plaintext `.env`, token caches, and loose credential files -- never copied
  by the automatic state backup. `.env` is supported only through the
  separate manual encrypted-envelope workflow above.
