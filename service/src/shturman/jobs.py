"""Очередь заданий для исполнителя вне сервиса.

Сервис не ходит к модели и не владеет ботом: и то и другое есть только у Hermes. Поэтому всё,
что требует модели или сообщения владельцу, сервис ставит в очередь, а плагин «Штурмана»
в Hermes забирает задание, выполняет и возвращает результат.

Задание берётся «в аренду» на время: если исполнитель пропал, оно возвращается в очередь.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import asyncpg

DEFAULT_LEASE = 300  # секунд на выполнение одного задания

_CLAIM = """
UPDATE jobs SET status = 'running', attempts = attempts + 1, worker = $3,
       locked_until = now() + make_interval(secs => $4)
WHERE id IN (
    SELECT id FROM jobs
    WHERE kind = ANY($1::text[])
      AND ((status = 'queued' AND run_after <= now())
           OR (status = 'running' AND locked_until < now() AND attempts < max_attempts))
    ORDER BY run_after, id
    LIMIT $2
    FOR UPDATE SKIP LOCKED
)
RETURNING id, kind, payload, attempts
"""


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def enqueue(
    conn: asyncpg.Connection, kind: str, payload: dict[str, Any], *,
    handler: str | None = None, context: dict[str, Any] | None = None,
    dedup_key: str | None = None, run_after: datetime | None = None, max_attempts: int = 3,
) -> int | None:
    """Ставит задание. Возвращает его идентификатор или None, если такое уже ставилось."""
    return await conn.fetchval(
        """INSERT INTO jobs (kind, handler, payload, context, dedup_key, run_after, max_attempts)
           VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, COALESCE($6, now()), $7)
           ON CONFLICT (kind, dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING
           RETURNING id""",
        kind, handler, json.dumps(payload, ensure_ascii=False),
        json.dumps(context or {}, ensure_ascii=False), dedup_key, run_after, max_attempts,
    )


async def claim(
    conn: asyncpg.Connection, kinds: Sequence[str], *, worker: str, limit: int = 1,
    lease: int = DEFAULT_LEASE,
) -> list[dict[str, Any]]:
    rows = await conn.fetch(_CLAIM, list(kinds), max(1, min(limit, 20)), worker, float(lease))
    return [
        {"id": r["id"], "kind": r["kind"], "payload": _loads(r["payload"]), "attempt": r["attempts"]}
        for r in rows
    ]


async def get(conn: asyncpg.Connection, job_id: int) -> dict[str, Any] | None:
    row = await conn.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
    if row is None:
        return None
    out = dict(row)
    for key in ("payload", "context", "result"):
        out[key] = _loads(out[key])
    return out


async def complete(conn: asyncpg.Connection, job_id: int, result: dict[str, Any]) -> dict[str, Any] | None:
    """Закрывает задание. Возвращает его строку (с контекстом) или None, если оно уже закрыто."""
    row = await conn.fetchrow(
        """UPDATE jobs SET status = 'done', result = $2::jsonb, finished_at = now(), locked_until = NULL
           WHERE id = $1 AND status = 'running'
           RETURNING *""",
        job_id, json.dumps(result, ensure_ascii=False),
    )
    if row is None:
        return None
    out = dict(row)
    for key in ("payload", "context", "result"):
        out[key] = _loads(out[key])
    return out


async def fail(
    conn: asyncpg.Connection, job_id: int, error: str, *, retry_in: int | None = 60
) -> str:
    """Отмечает неудачу. Пока попытки остались, задание возвращается в очередь. Возвращает новый статус."""
    row = await conn.fetchrow(
        "SELECT attempts, max_attempts FROM jobs WHERE id = $1 AND status = 'running' FOR UPDATE", job_id
    )
    if row is None:
        return "unknown"
    final = retry_in is None or row["attempts"] >= row["max_attempts"]
    if final:
        await conn.execute(
            "UPDATE jobs SET status = 'failed', error = $2, finished_at = now(), locked_until = NULL WHERE id = $1",
            job_id, error[:2000],
        )
        return "failed"
    await conn.execute(
        """UPDATE jobs SET status = 'queued', error = $2, locked_until = NULL, run_after = $3 WHERE id = $1""",
        job_id, error[:2000], datetime.now(timezone.utc) + timedelta(seconds=retry_in),
    )
    return "queued"


async def reap(conn: asyncpg.Connection) -> int:
    """Закрывает как неудачные задания, у которых вышли и аренда, и попытки."""
    done = await conn.execute(
        """UPDATE jobs SET status = 'failed', error = COALESCE(error, 'исполнитель не ответил'),
                  finished_at = now(), locked_until = NULL
           WHERE status = 'running' AND locked_until < now() AND attempts >= max_attempts"""
    )
    return int(done.split()[-1])
