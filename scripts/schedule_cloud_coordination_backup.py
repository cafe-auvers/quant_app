"""Schedule the verified cloud backup using Supabase Cron and encrypted Vault."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from src.utils.config import load_env_file
from src.infrastructure.database.coordination_engine import (
    get_coordination_database_config, create_coordination_connection_engine,
)

JOB_NAME = "quant-coordination-backup-daily"


def validate_daily_schedule(schedule: str) -> str:
    """Allow one run per UTC day so the Free usage estimate remains valid."""
    match = re.fullmatch(r"(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*", schedule.strip())
    if not match or not (0 <= int(match[1]) <= 59 and 0 <= int(match[2]) <= 23):
        raise ValueError("Use one daily UTC schedule: MINUTE HOUR * * *")
    return f"{int(match[1])} {int(match[2])} * * *"


COMMAND = """
select net.http_post(
  url := (select decrypted_secret from vault.decrypted_secrets where name='quant_backup_project_url')
         || '/functions/v1/coordination-backup',
  headers := jsonb_build_object(
      'Content-Type','application/json',
      'Authorization','Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name='quant_backup_service_role_key')
  ),
  body := '{}'::jsonb,
  timeout_milliseconds := 60000
);
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--schedule", default="15 0 * * *", help="UTC; default is 09:15 KST daily")
    args = parser.parse_args()
    engine = None
    try:
        schedule = validate_daily_schedule(args.schedule)
        config = get_coordination_database_config()
        project = str(config["user"]).partition(".")[2]
        if args.url.rstrip("/") != f"https://{project}.supabase.co":
            raise ValueError("URL does not match the configured Supabase project")
        credentials = load_env_file()
        if not credentials.get("SUPABASE_SERVICE_ROLE_KEY") or not credentials.get("SUPABASE_DB_PASSWORD"):
            raise ValueError("Server JWT and bootstrap database password are required")
        config["user"] = f"postgres.{project}"
        config["password"] = credentials["SUPABASE_DB_PASSWORD"]
        engine = create_coordination_connection_engine(config, read_only=False)
        with engine.begin() as conn:
            # DBAPI keeps credential-bearing arguments out of SQLAlchemy errors.
            with conn.connection.driver_connection.cursor() as cursor:
                for name, value in (
                    ("quant_backup_project_url", args.url.rstrip("/")),
                    ("quant_backup_service_role_key", credentials["SUPABASE_SERVICE_ROLE_KEY"]),
                ):
                    cursor.execute("select id from vault.secrets where name=%s", (name,))
                    existing = cursor.fetchone()
                    if existing:
                        cursor.execute("select vault.update_secret(%s,%s,%s)", (existing[0], value, name))
                    else:
                        cursor.execute("select vault.create_secret(%s,%s)", (value, name))
            job_id = conn.execute(text("select cron.schedule(:name,:schedule,:command)"), {
                "name": JOB_NAME, "schedule": schedule, "command": COMMAND,
            }).scalar_one()
            job = dict(conn.execute(text("select jobid,jobname,schedule,active from cron.job where jobid=:id"), {"id": job_id}).mappings().one())
        print(json.dumps(job))
        return 0
    except Exception as exc:
        print(f"Cloud scheduling failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
