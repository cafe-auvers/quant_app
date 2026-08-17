# KIS protocol fixtures

Redacted, real (not invented) request/response payloads and WebSocket frames
captured while filling in `docs/kis_capability_matrix.md` (Workstream 0).
Workstream 7's protocol tests (fake WebSocket integration server, order-
discovery response parsing) are built against these, not against shapes
guessed from the official sample.

## Rules for anything added here

- **Redact before committing**: no real account numbers, tokens, approval
  keys, or other PII. Replace with clearly-fake placeholders
  (`ACCT_REDACTED`, `TOKEN_REDACTED`, etc.) that preserve the field's shape
  (length, character class) so parsing logic is still exercised correctly.
- **Capture the real shape, not a cleaned-up version**: include whatever
  quirks the real API actually returns (unexpected null fields, inconsistent
  casing, etc.) — the point of these fixtures is to catch exactly the gap
  between "what the official sample implies" and "what the API actually
  does."
- **Name files by what they capture**, e.g.:
  - `order_submit_response.json`
  - `inquire_nccs_open_orders.json`
  - `inquire_ccnl_history_page.json`
  - `ws_hdfscnt0_frame.txt`
  - `ws_hdfsasp0_frame.txt`
  - `ws_subscribe_ack.txt`
  - `ws_subscribe_nack.txt`
  - `ws_execution_notice_encrypted.txt`
  - `ws_ping.txt` / `ws_pong.txt`

## Status

Workstream 0 is in progress. The `ws0_20260817_*` files are derived from
credentialed production/simulation captures and contain only redacted or
shape-only evidence. They currently cover subscription ACKs, configured U.S.
exchange key formats, the aggregate 41-registration production boundary, and
production open/history query shapes. A controlled simulation order request
also records the observed non-business-day rejection and proves that it left
no matching open order. Accepted simulation mutations, real event frames, and
execution notices are still missing; see
`docs/kis_capability_matrix.md` for the authoritative row status.
