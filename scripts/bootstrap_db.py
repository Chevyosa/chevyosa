"""
scripts/bootstrap_db.py — Ensure the RAG database and pgvector extension exist.

Runs before the migration so a fresh Postgres (e.g. the Docker postgres service,
where only the default/portfolio DB is created) gets the `rag` database and the
`vector` extension automatically. Safe to run repeatedly (idempotent).

Run with: uv run python scripts/bootstrap_db.py
"""

import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg

from config import settings


async def bootstrap() -> None:
    dsn = settings.supabase_db_url
    parts = urlsplit(dsn)

    db_name = parts.path.lstrip("/") or "postgres"
    if not db_name:
        print("[ERROR] No database name in SUPABASE_DB_URL")
        sys.exit(1)

    # Connect to the 'postgres' maintenance DB to create the target DB if needed.
    admin_dsn = urlunsplit(
        (parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment)
    )
    print(f"Ensuring database '{db_name}' exists...")
    conn = await asyncpg.connect(dsn=admin_dsn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", db_name
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            print(f"  Created database '{db_name}'")
        else:
            print(f"  Database '{db_name}' already exists")
    finally:
        await conn.close()

    # Enable pgvector extension in the target DB.
    print("Ensuring 'vector' extension...")
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        print("  vector extension ready")
    finally:
        await conn.close()

    print("Bootstrap complete!")


if __name__ == "__main__":
    asyncio.run(bootstrap())
