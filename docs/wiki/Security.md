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

- Use TLS identity verification for Internet coordination SQL.
- Restrict MySQL LAN/Tailscale access to intended hosts/accounts.
- Treat Tailscale, WinRM trust, remote-control tokens, and autologin settings as
  administrative access.
- Validate SQL identifiers and use parameterized SQLAlchemy statements.
- Keep remote-control commands authenticated and narrowly allowlisted.

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
