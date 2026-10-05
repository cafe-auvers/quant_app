# Security

## Secret handling

- Store credentials only in gitignored `.env` or approved OS/cloud secret
  facilities.
- Never commit `.env`, `.env.pc`, token caches, account numbers, private keys,
  database files, raw broker responses, or local `data/` state.
- Use placeholders such as `<account-number>` and `<secret>` in examples.
- Logs, journals, alerts, fixtures, screenshots, and reports must redact
  credentials and account/order identity where it is not operationally needed.

## Network and database

The private Supabase coordination schema is server-only: revoke PUBLIC/anon/authenticated
schema access and keep it out of browser/PostgREST schemas. Use the restricted runtime
DML/sequence role and verified TLS with the official CA; administrator credentials are
provisioning-only. Optional public web migrations are separate. A stale TiDB source must
never run as a second writer. Keep exported trade records and runtime backups private.

- Use TLS identity verification for Internet coordination SQL.
- Restrict MySQL LAN/Tailscale access to intended hosts/accounts.
- Treat Tailscale, WinRM trust, remote-control tokens, and autologin settings as
  administrative access.
- Validate SQL identifiers and use parameterized SQLAlchemy statements.
- Keep remote-control commands authenticated and narrowly allowlisted.

## Web/PWA control

- Require an authenticated server-side session for HTTP and `/live-updates`;
  validate Host/Origin and require CSRF for mutations.
- Keep passive and operator operation allowlists separate and revalidate that
  either the stable `Mobile Web` identity or the exact hosting-desktop identity
  owns Operator Control on each protected request.
- Treat WebSocket and typed pulse messages as invalidation hints only. They
  must contain no database credentials, device secrets, account secrets, or
  authoritative TradeCard payloads; receivers refetch canonical state.
- Never expose broker construction, execution-lease transfer, global risk, or
  workstation-power actions through the browser merely to reduce latency.

## Trading safety is security

Lease fencing, durable command identity, idempotency, mutation budgets,
ownership, capital reservation, and conservative reconciliation prevent both
accidental and duplicated broker effects. Do not weaken them for convenience or
performance.

## Repository checks

Before publishing:

1. scan tracked files only for tokens, private keys, passwords, and account
   identifiers;
2. inspect diffs for raw responses and local state;
3. verify `.gitignore` covers restore backups and runtime files;
4. rotate/revoke any credential that was ever exposed—deleting it from the
   latest commit is not sufficient.

For current host, shared-store, and backup prerequisites, see
[Supabase Deployment](Supabase-Deployment).
