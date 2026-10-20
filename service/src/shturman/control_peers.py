"""Служебные Telegram-диалоги: постоянный запрет по точному ID, без исключения по имени.

register вызывается только доверенным Bot API getMe/привязкой сервиса. HTTP-маршрута для
реестра нет. Смена бота не удаляет прежний ID. Блокировка сериализует регистрацию с записью
архива; старые сообщения, версии и производные задания очищаются до первого OTP.
"""
from __future__ import annotations

import asyncpg

LOCK = "shturman.control_peers"


async def is_blocked(conn: asyncpg.Connection, peer_class: str, tg_id: int) -> bool:
    return peer_class == "user" and bool(await conn.fetchval(
        "SELECT 1 FROM control_peers WHERE tg_id = $1", tg_id))


async def blocked_ids(conn: asyncpg.Connection) -> frozenset[int]:
    return frozenset(r["tg_id"] for r in await conn.fetch("SELECT tg_id FROM control_peers"))


async def register(conn: asyncpg.Connection, bot_tg_id: int, *, reason: str = "service_bot") -> None:
    if isinstance(bot_tg_id, bool) or not isinstance(bot_tg_id, int) or bot_tg_id <= 0:
        raise ValueError("нужен точный Telegram user ID управляющего бота")
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", LOCK)
        await conn.execute(
            "INSERT INTO control_peers (tg_id, reason) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            bot_tg_id, reason)
        chats = [r["id"] for r in await conn.fetch(
            """SELECT c.id FROM chats c JOIN peers p ON p.id = c.peer_id
               WHERE p.class = 'user' AND p.tg_id = $1 ORDER BY c.id FOR UPDATE OF c""", bot_tg_id)]
        await conn.execute(
            """UPDATE tg_sync_chats SET enabled = false, auto_enabled = false
               WHERE peer_class = 'user' AND tg_id = $1""", bot_tg_id)
        await conn.execute("UPDATE chats SET excluded = true WHERE id = ANY($1::bigint[])", chats)
        ids = [r["id"] for r in await conn.fetch(
            """SELECT id FROM messages WHERE chat_id = ANY($1::bigint[])
               OR sender_peer_id IN (SELECT id FROM peers WHERE class = 'user' AND tg_id = $2)""", chats, bot_tg_id)]
        if not ids:
            return
        text_ids = [str(i) for i in ids]
        commitment_ids = [r["id"] for r in await conn.fetch(
            """SELECT id FROM commitments WHERE chat_id = ANY($1::bigint[])
               OR source_message_id = ANY($2::bigint[]) OR due_message_id = ANY($2::bigint[])""", chats, ids)]
        # Карантин охватывает и файл Markdown, и его поисковую копию. Файл/история находятся
        # вне Hermes; их нельзя выдавать через API до независимой очистки/пересборки.
        page_ids = [r["id"] for r in await conn.fetch(
            """SELECT DISTINCT g.id FROM pages g
               WHERE EXISTS (SELECT 1 FROM page_entries e JOIN page_entry_sources s ON s.entry_id = e.id
                             WHERE e.page_id = g.id AND s.message_id = ANY($1::bigint[]))
                  OR EXISTS (SELECT 1 FROM commitments c JOIN person_peers pp
                             ON pp.peer_id IN (c.debtor_peer_id, c.creditor_peer_id)
                             WHERE c.id = ANY($2::bigint[]) AND pp.person_id = g.person_id)
                  -- проекты: обязательства, чаты и факты из служебного диалога; профиль — его факты
                  OR EXISTS (SELECT 1 FROM commitments c WHERE c.id = ANY($2::bigint[]) AND c.project_id = g.project_id)
                  OR EXISTS (SELECT 1 FROM project_chats pc WHERE pc.project_id = g.project_id
                             AND pc.chat_id = ANY($4::bigint[]))
                  OR EXISTS (SELECT 1 FROM facts f WHERE f.source_message_id = ANY($1::bigint[])
                             AND ((f.subject_type = 'person' AND f.person_id = g.person_id AND g.entity_type = 'person')
                                  OR (f.subject_type = 'project' AND f.project_id = g.project_id)
                                  OR (f.subject_type = 'owner' AND g.entity_type = 'owner')))
                  OR EXISTS (SELECT 1 FROM jobs j,
                             jsonb_array_elements(CASE WHEN jsonb_typeof(j.context->'offered') = 'array'
                               THEN j.context->'offered' ELSE '[]'::jsonb END) x
                             WHERE j.context->>'page_id' = g.id::text AND x->>0 = ANY($3::text[]))""",
            ids, commitment_ids, text_ids, chats)]
        await conn.execute(
            """UPDATE pages SET security_quarantined = true, dirty = true, summary_job_id = NULL,
                      summary_state = 'failed' WHERE id = ANY($1::bigint[])""", page_ids)
        await conn.execute("DELETE FROM page_blocks WHERE page_id = ANY($1::bigint[])", page_ids)
        await conn.execute("DELETE FROM page_entries WHERE page_id = ANY($1::bigint[])", page_ids)
        # Предложения проектов, выведенные из служебного диалога: его чат в проекте, в причине
        # предложения или упоминание названия в его сообщениях. Их сообщения владельцу стираются ниже.
        proposal_ids = [r["id"] for r in await conn.fetch(
            """SELECT p.id FROM projects p WHERE p.status = 'proposed' AND (
                   EXISTS (SELECT 1 FROM project_chats pc WHERE pc.project_id = p.id AND pc.chat_id = ANY($1::bigint[]))
                   OR (jsonb_typeof(p.reason->'chats') = 'array' AND EXISTS (
                       SELECT 1 FROM jsonb_array_elements_text(p.reason->'chats') x WHERE x = ANY($3::text[])))
                   OR EXISTS (SELECT 1 FROM project_mentions pm WHERE pm.title_norm = p.title_norm
                              AND (pm.chat_id = ANY($1::bigint[]) OR pm.message_id = ANY($2::bigint[]))))""",
            chats, ids, [str(c) for c in chats])]
        # Reply prompts and draft cards hold copies rather than foreign keys to every
        # source message. Scrub all generations before deleting their source rows.
        task_ids = [r["id"] for r in await conn.fetch(
            """SELECT t.id FROM reply_tasks t
               WHERE t.chat_id = ANY($1::bigint[]) OR t.trigger_message_id = ANY($2::bigint[])
                  OR EXISTS (SELECT 1 FROM jsonb_array_elements(
                        CASE WHEN jsonb_typeof(t.source_refs) = 'array'
                             THEN t.source_refs ELSE '[]'::jsonb END) ref
                     WHERE (ref->>'kind' = 'chat' AND ref->>'message_id' = ANY($3::text[]))
                        OR (ref->>'kind' = 'memory' AND ref->>'source_id' IN
                            (SELECT entity_id FROM pages WHERE id = ANY($4::bigint[]))))
               ORDER BY t.id FOR UPDATE OF t""", chats, ids, text_ids, page_ids)]
        await conn.execute(
            """UPDATE jobs j SET status = 'failed', payload = '{}', result = NULL, context = '{}',
                      error = 'protected_control_peer', finished_at = now(), locked_until = NULL
               WHERE j.context->>'task_id' = ANY($1::text[])
                  OR j.id IN (SELECT job_id FROM reply_tasks WHERE id = ANY($2::bigint[]))
                  OR j.context->>'draft_id' IN
                     (SELECT id::text FROM outbox_drafts WHERE task_id = ANY($2::bigint[]))
                  OR EXISTS (SELECT 1 FROM reply_tasks t WHERE t.id = ANY($2::bigint[])
                             AND j.dedup_key LIKE 'reply-wait:' || t.id::text || ':%')""",
            [str(i) for i in task_ids], task_ids)
        await conn.execute("DELETE FROM outbox_drafts WHERE task_id = ANY($1::bigint[])", task_ids)
        await conn.execute(
            """UPDATE reply_tasks SET status = 'cancelled', error_code = 'protected_control_peer',
                      input_messages = '[]', source_refs = '[]', source_request = NULL,
                      owner_question = NULL, owner_answer = NULL, decision_expires_at = NULL,
                      updated_at = now() WHERE id = ANY($1::bigint[])""", task_ids)
        # Закрытое задание тоже может содержать старый ответ модели; очищаем его до удаления
        # ссылок из commitments/watch_hits. Поздний ответ исполнителя больше не применяется.
        await conn.execute(
            """UPDATE jobs j SET status = 'failed', payload = '{}', result = NULL, context = '{}',
                      error = 'protected_control_peer', finished_at = now(), locked_until = NULL
               WHERE j.context->>'chat_id' = ANY($1::text[])
                  OR j.context->>'page_id' = ANY($2::text[])
                  OR j.context->>'message_id' = ANY($3::text[])
                  OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(
                        CASE WHEN jsonb_typeof(j.context->'message_ids') = 'array'
                             THEN j.context->'message_ids' ELSE '[]'::jsonb END) x WHERE x = ANY($3::text[]))
                  OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(
                        CASE WHEN jsonb_typeof(j.context->'context_ids') = 'array'
                             THEN j.context->'context_ids' ELSE '[]'::jsonb END) x WHERE x = ANY($3::text[]))
                  OR j.id IN (SELECT job_id FROM processing_requests WHERE chat_id = ANY($4::bigint[]))
                  OR j.context->>'hit_id' IN (SELECT id::text FROM watch_hits WHERE message_id::text = ANY($3::text[]))
                  OR j.context->>'draft_id' IN (SELECT id::text FROM outbox_drafts WHERE chat_id = ANY($4::bigint[])
                                              OR trigger_message_id::text = ANY($3::text[]))
                  OR j.context->>'batch' IN (SELECT digest_batch FROM commitments WHERE id = ANY($5::bigint[]))
                  OR j.context->>'batch' IN (SELECT cc.digest_batch FROM commitment_changes cc
                         WHERE cc.commitment_id = ANY($5::bigint[]))
                  OR j.context->>'batch' IN (SELECT f.batch FROM facts f
                         WHERE f.source_message_id::text = ANY($3::text[]) AND f.batch IS NOT NULL)
                  OR j.context->>'batch' IN (SELECT p.batch FROM projects p
                         WHERE p.id = ANY($6::bigint[]) AND p.batch IS NOT NULL)""",
            [str(c) for c in chats], [str(p) for p in page_ids], text_ids, chats, commitment_ids, proposal_ids)
        await conn.execute(
            """UPDATE processing_requests SET state = 'failed' WHERE chat_id = ANY($1::bigint[])
               OR job_id IN (SELECT id FROM jobs WHERE error = 'protected_control_peer')""", chats)
        await conn.execute(
            """UPDATE pending_actions SET status = 'expired', summary = 'Служебный источник исключён',
                      payload = '{}', decided_at = now()
               WHERE payload->>'commitment_id' = ANY($1::text[])""",
            [str(i) for i in commitment_ids])
        await conn.execute("DELETE FROM outbox_drafts WHERE chat_id = ANY($1::bigint[]) OR trigger_message_id = ANY($2::bigint[])", chats, ids)
        # Служебный диалог не может быть чатом проекта; предложения проектов из него сняты выше.
        await conn.execute("DELETE FROM projects WHERE id = ANY($1::bigint[])", proposal_ids)
        await conn.execute("DELETE FROM project_chats WHERE chat_id = ANY($1::bigint[])", chats)
        # CASCADE удаляет версии, векторы, обязательства, факты, упоминания проектов и источники страниц.
        await conn.execute("DELETE FROM messages WHERE id = ANY($1::bigint[])", ids)
        # факты из этих сообщений ушли каскадом: закрытые ими прежние снова действуют
        from .processing import facts
        await facts.repair_chains(conn)


async def reconcile(conn: asyncpg.Connection) -> None:
    """Выполняется до запуска HTTP/фоновой работы: очищает найденные миграцией старые чаты."""
    for tg_id in sorted(await blocked_ids(conn)):
        await register(conn, tg_id)
