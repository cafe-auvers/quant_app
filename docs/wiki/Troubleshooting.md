# Troubleshooting

## App opens without database data

- Check Health and `MYSQL_*` values in local `.env`.
- Confirm network/Tailscale reachability and MySQL grants/TLS policy.
- On a laptop, confirm `data/local_mirror.db` exists and mirror freshness is
  acceptable.
- The UI is allowed to run without MySQL; execution paths may still fail closed.

## Scanner or chart is empty

- Verify the selected source/universe and latest cache date.
- Run the appropriate 1D/1H refresh; inspect its status file/log.
- Confirm the symbol normalization and interval/source in the database.
- KIS intraday must remain disabled if mappings are unverified; research may
  fall back to Yahoo where supported.

## Buy Board is read-only or blocked

Read the readiness tooltip. Common causes are engine disabled, no current
lease/ownership, stale account reconciliation, missing WebSocket ACKs, stale
quotes, database write failure, mutation budget, unresolved order, or Live
Trading off. Fix the underlying cause; never bypass a gate.

## Order state is ambiguous

Do not submit again. Query/reconcile the existing durable client/broker
identity, review account-wide open/history/reserved orders and positions, and
keep the card pending until evidence is unambiguous.

## Environment changes are missing

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\sync_pc_env.ps1
```

Existing `.env` values win. Generated `.env.pc` intentionally blanks all
`MYSQL_*` values.

## Qt/WebEngine rendering problems

Do not force global software rendering unless the machine requires it. Confirm
PyQtWebEngine is installed from the lock and review the application log for
actionable errors.
