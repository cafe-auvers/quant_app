# Remote startup and live switch

The shared Live Trading ON choice persists across restarts and later trading
days for the same approved release. OFF remains durable. A different release
still needs explicit authorization; ownership, broker reconciliation, account
freshness, market data, risk limits and all order-boundary checks still apply.
Legacy session-scoped records retain their original expiry until an authorized
ON write replaces them. Supervised tools can explicitly request session scope
with `persist_across_sessions=False`.

For the existing approved PC release, `scripts/resume_live_trading.py` can run
from the separate local operations directory every minute. It renews only an
already-ON record whose release matches the pinned approved checkout and
manifest. It requires the configured PC to own execution and publish fresh
ACTIVE readiness. A database transaction serializes renewal with OFF, and the
normal control audit records every renewal. Missing state, explicit OFF,
administrative OFF, another owner, stale readiness or a changed release is
never automatically enabled. This compatibility task does not submit orders.
Once the persistent implementation is deployed and its ON choice confirmed,
disable the compatibility task.

A detached Buy Today card can recover its original pending entry from a unique
application-owned exact broker order in the current session. Recovery requires
complete holdings/order evidence, matching quantity, price, submission chronology
and the reservation's original notional and stop risk. Current-candidate partial
or full fills also require matching fresh holdings before restoring their stop;
consumed reservation proof is loaded only for that recovery. Conflicting,
historical or unowned orders cannot establish this link. A separate entry guard
blocks another proposal whenever an open entry or an unrepresented confirmed
fill exists for the same account and candidate. Positive holdings also block a
stale Buy Today card. Confirmed fills remain in projected exposure until
holdings catch up; confirmed later sells reduce that exposure. Delayed holdings
cannot erase a proven position and its stop while its net fills remain positive.
Recovery preserves broker orders; normal lifecycle management then resumes.

Recovery snapshots named `data/*.json.corrupt-*` are private runtime state.
Ignore and preserve them so a recovered snapshot cannot make an otherwise
approved checkout appear dirty and block the next startup.

Deploy trading source only after the shutdown exposure fence passes with fresh
broker evidence or a confirmed ready successor. A closed market alone does not
make open swing holdings safe to leave without an executor. Keep the running
executor intact if the fence refuses shutdown. A web-only restart can proceed
independently, using a SQLite-aware state backup and the existing private HTTPS
route and authentication settings.
