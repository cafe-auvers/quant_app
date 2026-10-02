# Web Review Performance Report

Recorded: 2026-10-01T08:46:56Z

This is a reproducible local server/data-path benchmark, not a latency
guarantee. It follows the lightweight UI path: request only the visible 1D
bundle while navigating, fill 1H in the delayed background, then repeat 1D
navigation against the validated gzip cache.

| Measurement | p50 | p95 | Mean | Wall time |
|---|---:|---:|---:|---:|
| Cold visible navigation (1D only) | 91.05 ms | 122.36 ms | 94.95 ms | 28.49 s |
| Cold visible provider + normalize + compress | 91.05 ms | 122.35 ms | 94.94 ms | included above |
| Delayed background 1H preparation | 80.18 ms | 113.40 ms | 84.11 ms | 25.23 s |
| Warm visible navigation (direct gzip hit) | 0.86 ms | 1.81 ms | 1.01 ms | 0.30 s |
| Warm publish-ready bundle read | 0.86 ms | 1.80 ms | 1.01 ms | included above |

## Conditions

- Source: `DEMO`.
- Cache: cold empty temporary directory, then warm validated local gzip files.
- Requests: 900 (300 visible cold + 300 background + 300 visible warm).
- Coverage settings: 750 daily bars and 6 calendar months hourly.
- Worker limit: 4; navigation itself is sequential to make per-symbol values comparable.
- Compressed chart bytes: 26,029,638 (24.82 MiB).
- Cache disk including manifests: 26,274,887 (25.06 MiB).
- Peak Python allocations for one isolated warm publication read, observed by `tracemalloc`: 52,004 (0.05 MiB). This excludes native-library allocations and is kept outside latency timing.
- Hardware/runtime: Windows-11-10.0.26200-SP0; processor `Intel64 Family 6 Model 142 Stepping 12, GenuineIntel`; 8 logical CPUs; Python 3.12.4.

## Limitations

- DEMO results measure deterministic local generation, normalization,
  compression, checksum, and disk cache behavior; they do not predict provider,
  LAN, TiDB, Supabase, or phone-network latency.
- Values are server/data preparation timings. Browser layout/paint and a real
  iPhone are outside this script and are checked separately with viewport
  inspection.
- The benchmark intentionally uses a temporary cache and performs no planning,
  execution, broker, KIS, or cloud writes.

## Planning interaction path

Planning and Buy Today responsiveness is handled separately from this chart
benchmark. The initiating browser renders allowed changes optimistically before
awaiting persistence, then reconciles with canonical state or rolls back. Other
open clients are invalidated through an authenticated WebSocket, and desktops
receive typed change pulses. This removes manual-refresh latency from the
normal interaction path, but this report does not claim a measured WAN/TiDB
commit time. See [Web/PWA Operator Synchronization](web_operator_sync.md).
