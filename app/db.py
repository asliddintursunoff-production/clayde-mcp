"""Postgres connection pool and a minimal migration runner."""

from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def run_migrations(database_url: str) -> list[str]:
    """Apply every migrations/*.sql file that has not been applied yet, in name order.

    Args:
        database_url: Postgres connection string.

    Returns:
        Names of the files applied in this call.
    """
    applied: list[str] = []

    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
        # One lock so two app instances starting together don't race
        await conn.execute("SELECT pg_advisory_lock(727001)")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
        )
        done = {row[0] for row in await (await conn.execute("SELECT name FROM schema_migrations")).fetchall()}

        # Each file runs in its own transaction together with its bookkeeping row
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)

        await conn.execute("SELECT pg_advisory_unlock(727001)")

    return applied


async def _configure(conn: psycopg.AsyncConnection) -> None:
    """Register the pgvector type on every new pooled connection."""
    await register_vector_async(conn)


# TCP keepalives notice a dead peer quickly instead of hanging on it
KEEPALIVE = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3}


def create_pool(database_url: str, max_size: int) -> AsyncConnectionPool:
    """Create (but don't open) the async pool; rows come back as dicts.

    Connections can die under the pool (database restart, network idle cut-off), so each one is
    checked before it is handed out and replaced if broken; idle and old connections are recycled.
    """
    return AsyncConnectionPool(
        database_url,
        min_size=1,
        max_size=max_size,
        open=False,
        configure=_configure,
        check=AsyncConnectionPool.check_connection,
        max_idle=300,
        max_lifetime=1800,
        reconnect_timeout=60,
        kwargs={"row_factory": dict_row, **KEEPALIVE},
    )
