# Two-PC Data Sync: Always-On PC as the Data Server

Status: all scripts described below exist in `scripts/`. What's left is
running them on the actual PC (they can't be run from here) — see the
step-by-step runbook in this repo's chat history / your own notes for the
exact order.

## Roles

- **Laptop** — where development happens (edits, commits, `git push`). Also
  a normal client of the app: `main.py` runs here and reads from the shared
  database, same as it would run on the PC.
- **Always-on PC** — never used for development. Two jobs only:
  1. Host the single shared MySQL database (`quant_app`) that both machines
     read from.
  2. Run `historical.py` on a schedule to keep that database's price
     history, hourly bars, chart indicators, and scanner metrics fresh.

There is exactly **one** database. The laptop does not have its own local
copy — its `.env` `MYSQL_HOST` points at the PC's LAN IP, so "the data" and
"the PC's data" are the same thing, always. This matters for the questions
below.

## Daily workflow (target design)

```
08:00 KST  BIOS RTC alarm wakes the PC
           -> Windows auto-login (registry AutoAdminLogon)
           -> Task Scheduler "at logon" task runs a wrapper script that:
                1. git fetch + reset the PC's repo clone to origin/master
                2. compares the DB's actual latest stored date against the
                   date the dashboard itself would expect (same check as
                   "Needs refresh" in the UI) -- if behind, runs
                   historical.py --mode 1d then --mode 1h; a multi-day gap
                   self-heals in one go since historical.py refetches a wide
                   window (1y), not just "yesterday"
                3. launches main.py so the dashboard is visible if you check in
10:00 KST  Scheduled shutdown (guarded so it won't kill an in-progress refresh)
```

Built:
- `scripts/pc_morning_routine.ps1` — steps 1–4 above (git sync, refresh, launch).
- `scripts/run_daily_refresh.py` — the DB-freshness gate (compares actual latest stored date vs. expected, same as the dashboard's "Needs refresh") + `historical.py` calls.
- `scripts/setup_pc_autologin.ps1` — configures Windows `AutoAdminLogon`.
- `scripts/setup_pc_morning_task.ps1` — registers the "at logon" Task Scheduler task.
- `scripts/setup_mysql_lan_access.ps1` (from earlier) — LAN firewall rule + prints the `my.ini`/`GRANT` steps.
- `Configure-AutomaticShutdown.ps1` (in the separate `PC-Automation` folder, since it's generic, not quant_app-specific) — the 10:00 shutdown.
- BIOS wake instructions — `docs` in `PC-Automation`.

None of these have been run on the actual PC yet — they only exist as
files in this repo/checkout. Everything below still needs to be executed on
that machine.

Live trading/monitoring is out of scope for this machine under the current
plan — the PC is off for the entire US trading session, so it can only ever
refresh yesterday's completed data, never watch positions in real time.

---

## How do I make sure the PC is running the latest `historical.py`?

**Via git — the repo already has a remote** (`origin` →
`https://github.com/cafe-auvers/quant_app.git`), so this doesn't need a new
sync mechanism invented, just a step in the PC's morning routine:

1. You develop and commit on the laptop as normal, and `git push` when a
   change is ready.
2. Every morning before running `historical.py`, the PC's wrapper script
   does `git fetch origin` then `git reset --hard origin/master` on its own
   clone. Using `reset --hard` (not `pull`) is deliberate: the PC's clone is
   a deployment target, not a workspace — nobody edits code on it, so there
   should never be local changes to merge, and a hard reset guarantees the
   PC always runs an exact, reproducible copy of what's on GitHub rather
   than risking a stuck merge conflict in an unattended run.
3. **One prerequisite this needs on the PC**: non-interactive git auth. A
   scheduled task has no one there to type a GitHub password/token if
   prompted. Either sign in once via Git Credential Manager on that PC so
   the credential is cached, or set the PC's clone to use an SSH remote with
   a deploy key. Worth confirming this works with a manual `git fetch` on
   the PC before wiring it into the scheduled task.

Net effect: whatever's on `origin/master` when the PC wakes at 08:00 KST is
what runs that morning. If you push a fix at 11pm your time, the PC picks it
up on its next wake automatically — no manual copying between machines.

## What happens if the PC doesn't work one day?

Nothing catastrophic, but nothing catches up automatically either until it's
fixed. Concretely:

- **The laptop's `main.py` doesn't crash.** `init_mysql_engine()` already
  catches connection failures and returns `None`; the app logs "MySQL cache
  disabled" and keeps running with database-backed scanning/cache-freshness
  features degraded (per the README, the app is designed to run without
  MySQL at all). You'd notice slower/rate-limited live lookups rather than a
  hard failure.
- **Data just goes stale for however many days the PC is down.** There's no
  staleness banner in the UI today (I checked — it doesn't exist), so this
  is a silent gap, not a loud one. Worth being aware you're looking at
  possibly-old cached data rather than assuming freshness.
- **Recovery is automatic once the PC is back**, no manual backfill needed:
  `historical.py`'s daily/hourly fetches pull a wide window each run (`1y`
  for daily, `730d` for hourly), not just "since last run," so a multi-day
  gap self-heals on the very next successful run.
- **Root causes worth checking if this happens**: BIOS RTC alarm didn't fire
  (power/PSU-switch prerequisites — see `PC-Automation/docs/BIOS-Startup-
  Instructions.md`), Windows didn't auto-login, or the git-sync step failed
  silently. The wrapper script (once built) should log each step so you can
  tell which one broke.

## If I run `historical.py` manually on the laptop, is that valid?

Yes, **as long as the PC/MySQL is reachable over LAN when you do it** —
because the laptop's `.env` points at the *same* central database, a manual
`python historical.py --mode 1d` run from the laptop writes to the exact
same tables the PC's scheduled run does. It's not a separate/local copy that
could drift out of sync; it's just an extra, ad hoc trigger of the same
pipeline. Good for "I don't want to wait until tomorrow morning" or "the
scheduled run didn't fire, let me kick it manually."

**What it can't do**: serve as an offline fallback while the PC is actually
down. Since MySQL only exists on the PC, if the PC is unreachable there is
no database for the laptop to write to either — you'd hit the same "MySQL
cache disabled" degraded mode described above, from either machine. There's
currently no local/offline database on the laptop to fall back to; if you
want that (a laptop-local mirror that can absorb writes while the PC is
down and reconcile later), that's a real design decision — not something
implied by today's architecture — and would need its own discussion before
building.
