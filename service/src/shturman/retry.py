"""Повтор того, что не получилось из-за сбоя модели (решение Р-84).

Когда задания модели долго не выполняются (так было при сбое плагина, Р-83), очередь честно сдаётся:
  * вложение (фото, документ) после четырёх попыток помечается «не получилось» (`media_state =
    'failed'`), с причиной `model:…` (исполнитель отказал), `lost_job` (задание пропало или не
    дождалось ответа) или `bad_answer` (ответ не разобрался);
  * запрос обработки переписки (обязательства, факты, проекты — один запрос на эпизод) после двух
    повторов помечается «пропущен» (`processing_requests.state = 'given_up'`).

Повтор возвращает их в работу:
  * вложения — снова в очередь разбора (`pending`, попытки с нуля), только если причина — сбой
    модели, сообщение не старше `SHTURMAN_MEDIA_DAYS` дней, не удалено, его чат не исключён и разбор
    включён владельцем. «Пропущено» (вид файла, размер, файла нет, источника нет) не повторяется;
  * запросы обработки — в «повторить» (`retry`): следующий прогон (ночью) поставит их заново первым
    делом; их сообщения проверяются ещё раз (исключённый чат, удалённое, скрытое защитой — не идут).
Страницы памяти, сводка которых не получилась, пересобираются следующей сборкой сами — их только
считаем.

Повторный разбор снова отправляет данные провайдеру модели и тратит запросы, поэтому повтор
запускает владелец: кнопкой на странице настройки или оператор по его решению командой
`shturman retry-failed [--since ДАТА]`. Во внутреннем API его нет: ассистент его не запускает.
Итог — только числа: в `settings` (`retry.last`), в `GET /api/status` и в `./ops/doctor.sh`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import asyncpg

LAST_KEY = "retry.last"

# Причины «не получилось» у вложения, которые значат сбой модели, а не сам файл.
_MEDIA_MODEL = """(m.media_error IN ('lost_job', 'bad_answer') OR m.media_error LIKE 'model:%')"""
_MEDIA = f"""
FROM messages m JOIN chats c ON c.id = m.chat_id
WHERE m.media_state = 'failed' AND {_MEDIA_MODEL} AND m.deleted_at IS NULL AND NOT c.excluded
  AND m.sent_at >= now() - make_interval(days => $1)
  AND ($2::timestamptz IS NULL OR m.media_at >= $2)
"""
_REQUESTS = """
FROM processing_requests r JOIN chats c ON c.id = r.chat_id
WHERE r.state = 'given_up' AND NOT c.excluded AND ($1::timestamptz IS NULL OR r.created_at >= $1)
"""


async def _media_enabled(conn: asyncpg.Connection) -> bool:
    from . import media
    return await media.enabled(conn)


async def candidates(conn: asyncpg.Connection, *, media_days: int, since: datetime | None = None) -> dict[str, Any]:
    """Сколько можно вернуть: вложения, запросы обработки; страницы памяти — для сведения."""
    media = await conn.fetchval(f"SELECT count(*) {_MEDIA}", media_days, since)
    requests = await conn.fetchval(f"SELECT count(*) {_REQUESTS}", since)
    other = await conn.fetchval(
        f"SELECT count(*) FROM messages m WHERE m.media_state = 'failed' AND NOT {_MEDIA_MODEL}")
    pages = await conn.fetchval("SELECT count(*) FROM pages WHERE summary_state = 'failed'")
    return {"media": int(media), "requests": int(requests), "media_not_model": int(other),
            "pages": int(pages), "media_enabled": await _media_enabled(conn)}


async def run(conn: asyncpg.Connection, *, media_days: int, since: datetime | None = None,
              by: str = "page") -> dict[str, Any]:
    """Возвращает в работу. Итог (только числа) запоминается в `settings`, ключ `retry.last`."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.retry'))")
        enabled = await _media_enabled(conn)
        media = 0
        if enabled:
            done = await conn.execute(
                f"""UPDATE messages SET media_state = 'pending', media_error = NULL, media_attempts = 0,
                           media_job = NULL, media_at = NULL
                    WHERE id IN (SELECT m.id {_MEDIA})""", media_days, since)
            media = int(done.split()[-1])
        # Та же блокировка, что у планирования прогона: прогон не идёт одновременно с возвратом.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.processing'))")
        done = await conn.execute(
            f"UPDATE processing_requests SET state = 'retry' WHERE job_id IN (SELECT r.job_id {_REQUESTS})", since)
        requests = int(done.split()[-1])
        if requests:
            # Следующий прогон этой же ночи продолжит, пока есть что повторить (pipeline.nightly_tick).
            await conn.execute(
                """UPDATE settings SET value = jsonb_set(value, '{more}', 'true'::jsonb), updated_at = now()
                   WHERE key = 'processing.state'""")
        result = {"at": datetime.now(timezone.utc).isoformat(), "media": media, "requests": requests,
                  "media_enabled": enabled, "since": since.isoformat() if since else None, "by": by}
        await conn.execute(
            """INSERT INTO settings (key, value) VALUES ($1, $2::jsonb)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            LAST_KEY, json.dumps(result))
    return result


async def last(conn: asyncpg.Connection) -> dict[str, Any] | None:
    raw = await conn.fetchval("SELECT value FROM settings WHERE key = $1", LAST_KEY)
    if isinstance(raw, str):
        raw = json.loads(raw)
    return raw if isinstance(raw, dict) else None


async def overview(conn: asyncpg.Connection, *, media_days: int) -> dict[str, Any]:
    """Поля для `GET /api/status` и `./ops/doctor.sh`: только числа."""
    found = await candidates(conn, media_days=media_days)
    done = await last(conn) or {}
    return {"retry_media_failed": found["media"], "retry_requests_given_up": found["requests"],
            "retry_last_media": int(done.get("media") or 0), "retry_last_requests": int(done.get("requests") or 0),
            "retry_last_at": done.get("at")}
