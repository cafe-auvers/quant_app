"""Check ready scan queues and cold daily archives without recording reviews."""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.request

from sqlalchemy import select

from src.services.replay_daily import ReplayDailyCache
from src.utils.config import DATA_DIR, get_env_value


def check_prepared_loads(repo, published, log=print, force=False):
    saved = repo.prepared.job("performance") or {}
    identity = (published["source_key"], published["rules_key"])
    if not force and (saved.get("source_key"), saved.get("rules_key")) == identity:
        return saved
    table = repo.prepared.scans
    rows = repo.prepared.rows(select(table.c.day).where(table.c.setup_name == "Setup 2",
        table.c.rules_key == published["rules_key"], table.c.day >= dt.date.fromisoformat(published["start_day"]),
        table.c.day <= dt.date.fromisoformat(published["end_day"])).order_by(table.c.day))
    if not rows:
        return None
    days = sorted({rows[0]["day"], rows[len(rows) // 2]["day"], rows[-1]["day"]})
    base = f"http://127.0.0.1:{int(get_env_value('REPLAY_PORT', '8765'))}"
    cache = ReplayDailyCache(repo)
    checks = []
    for day in days:
        begin = time.perf_counter()
        body = json.dumps({"day": str(day), "setup_name": "Setup 2"}).encode()
        request = urllib.request.Request(base + "/api/scans", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                scan = json.load(response)
            with urllib.request.urlopen(base + "/api/scans/" + scan["id"], timeout=20) as response:
                queue = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"Ready-load timing check will retry when the localhost server is available: {type(exc).__name__}")
            return None
        scan_seconds = time.perf_counter() - begin
        symbol = queue["results"][0]["symbol"] if queue["results"] else "SPY"
        begin = time.perf_counter()
        archive = cache.get({"symbol": symbol, "start_day": day}, refresh=True)
        archive.metrics_at(day)
        archive.visible(day)
        checks.append({"day": str(day), "symbol": symbol, "matches": len(queue["results"]),
            "scan_http_seconds": round(scan_seconds, 4),
            "cold_daily_archive_seconds": round(time.perf_counter() - begin, 4)})
    result = {"source_key": identity[0], "rules_key": identity[1],
        "measured_at": dt.datetime.now(dt.timezone.utc).isoformat(), "checks": checks,
        "target_seconds": 2, "within_target": all(row["scan_http_seconds"] <= 2 and
            row["cold_daily_archive_seconds"] <= 2 for row in checks),
        "scope": "First/middle/latest prepared dates: full scan HTTP load and cold chart archive. Browser rendering and session annotations excluded."}
    repo.prepared.save_job("performance", result)
    path = DATA_DIR / "breakout_replay" / "performance_ready.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
    temporary.replace(path)
    log(f"Prepared-load timing check: {'passed' if result['within_target'] else 'above target'}; {checks}")
    return result
