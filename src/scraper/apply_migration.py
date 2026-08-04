"""Apply a SQL migration file directly against Postgres.

PostgREST (what the scraper's normal SUPABASE_KEY talks to) cannot run DDL —
schema changes need a real Postgres connection. Requires SUPABASE_DB_URL (see
.env.example), which is separate from the scraper's normal runtime credentials
and only needed for this admin path.

    python -m scraper.apply_migration supabase/migrations/0003_ticket_links.sql
"""

from __future__ import annotations

import sys
from pathlib import Path

from .core.config import settings


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m scraper.apply_migration <path-to-sql-file>")
        return 2

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"not found: {path}")
        return 1
    if not settings.supabase_db_url:
        print("SUPABASE_DB_URL is not set (see .env.example) — cannot run DDL without it.")
        return 1

    import psycopg

    sql = path.read_text(encoding="utf-8")
    with psycopg.connect(settings.supabase_db_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    print(f"applied {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
