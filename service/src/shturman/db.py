"""Подключение к базе и применение миграций."""

from __future__ import annotations

from importlib import resources

import asyncpg

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


async def connect(dsn: str) -> asyncpg.Connection:
    return await asyncpg.connect(dsn)


def _migration_files() -> list[tuple[str, str]]:
    root = resources.files("shturman") / "migrations"
    files = sorted(p for p in root.iterdir() if p.name.endswith(".sql"))
    return [(p.name, p.read_text(encoding="utf-8")) for p in files]


async def migrate(conn: asyncpg.Connection) -> list[str]:
    """Применяет недостающие миграции по порядку имён. Возвращает имена применённых."""
    # Блокировка на время миграции: два процесса, запущенные одновременно, не мешают друг другу.
    await conn.execute("SELECT pg_advisory_lock(hashtext('shturman.migrate'))")
    try:
        await conn.execute(_MIGRATIONS_TABLE)
        done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
        applied: list[str] = []
        for name, sql in _migration_files():
            if name in done:
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
            applied.append(name)
        return applied
    finally:
        await conn.execute("SELECT pg_advisory_unlock(hashtext('shturman.migrate'))")
