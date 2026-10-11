# Web Review Performance Report

Current result for the final integration pass, recorded 2026-10-02 on Windows
11, Python 3.12.4, Intel64 Family 6 Model 142, 8 logical CPUs.

Both runs used 300 symbols, up to 750 daily bars, six calendar months of hourly
bars, an empty temporary cache for cold work, and the validated gzip cache for
warm work. Each run made 900 direct coordinator requests plus 300 authenticated
HTTP chart requests. No planning, broker, KIS, cloud, or mirror write occurred.

| Source and measurement | p50 | p95 | Mean | Wall time |
|---|---:|---:|---:|---:|
| DEMO cold visible 1D generation | 144.00 ms | 224.76 ms | 157.33 ms | 47.20 s |
| DEMO independent 1H generation | 191.78 ms | 302.26 ms | 204.07 ms | 61.23 s |
| DEMO warm bundle read | 1.11 ms | 1.75 ms | 1.32 ms | 0.40 s |
| DEMO authenticated warm HTTP | 26.43 ms | 44.13 ms | 30.10 ms | 300 requests |
| PC mirror cold visible 1D generation | 106.08 ms | 144.09 ms | 106.54 ms | 31.96 s |
| PC mirror independent 1H generation | 129.47 ms | 248.87 ms | 140.49 ms | 42.15 s |
| PC mirror warm bundle read | 1.42 ms | 2.04 ms | 1.52 ms | 0.46 s |
| PC mirror authenticated warm HTTP | 28.10 ms | 35.05 ms | 29.09 ms | 300 requests |

Every authenticated HTTP response carried an ETag. The DEMO cache occupied
36,551,975 bytes (34.86 MiB), including manifests; the PC-mirror cache occupied
18,868,858 bytes (17.99 MiB). Compressed daily-plus-hourly artifacts totaled
36,238,330 bytes for DEMO and 18,474,805 bytes for the mirror.

The process working set changed from 128,221,184 to 140,652,544 bytes during
the DEMO run and from 128,360,448 to 145,969,152 bytes during the PC-mirror
run. These figures include the Python runtime and loaded libraries. The
isolated warm publication read peaked at 68,562 bytes (DEMO) and 30,879 bytes
(mirror) of Python-traced allocations; neither figure is browser memory.

Reproduce the two current runs with:

```powershell
python scripts/benchmark_web_review.py --symbols 300
python scripts/benchmark_web_review.py --config config\web.local.json --symbols 300
```

When the real scanner has fewer than 300 matches, the real-data benchmark
keeps those ranked scanner symbols first and fills the sample from mirror
symbols that have both daily and hourly data. SQLite is opened with `mode=ro`
and `PRAGMA query_only=ON`.

## Browser-paint status

The static client now records at most 100 in-memory samples for:

- selection to first chart paint;
- previous/next stock to first chart paint;
- 1D/1H switch to first chart paint;
- warm-cache versus uncached navigation.

They are available in a signed-in tab through
`window.__quantWebMetrics.snapshot()` and can be cleared with
`window.__quantWebMetrics.clear()`. No sample is transmitted or persisted.
The in-app browser controller was unavailable during this pass, so no honest
desktop/iPhone chart-paint p50/p95 is claimed here. Collecting those values on
the user's real browser and iPhone remains part of UAT.

## Interpretation and limits

- DEMO measures deterministic local generation and is not a prediction of
  LAN, Supabase, Tailscale, or phone-network latency.
- PC-mirror values measure a local read-only SQLite mirror, not provider or
  Supabase planning-write latency.
- HTTP values use an authenticated in-process ASGI transport, so they include
  routing/auth/serialization but not TCP, TLS, browser parsing, or paint.
- Nearby prefetch remains bounded to the other current timeframe plus one
  previous and one next stock. The browser bundle cache remains capped at 36.

Current release reference: [Opening liquidity and mobile workflow (2026-10-08)](opening_liquidity_mobile_release_2026-10-08.md). Dated reports and archived plans retain their original scope.
