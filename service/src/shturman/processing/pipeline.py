"""Прогон обработки: что нового в архиве, какие запросы поставить модели, что показать владельцу.

Ход прогона (docs/memory.md, «Ночная сборка», шаги 1–4 и 10 в части обязательств):

  1. Берутся сообщения, записанные после прошлого прогона (отметка — наибольший обработанный
     `messages.id`, хранится в `settings`). Исключённые чаты, удалённые и служебные сообщения,
     каналы, боты и «Избранное» пропускаются.
  2. Сообщения режутся на эпизоды; к модели идут только эпизоды с признаком обещания.
  3. Запросы ставятся в очередь (`bridge.request_structured`). Модели у сервиса нет: ответ
     вернётся позже в обработчик результата.
  4. Ответ проверяется текстом сообщений, обязательства записываются как предложения.
  5. Когда все запросы прогона разобраны, владельцу уходит одна сводка с кнопками.

Стоимость ограничена: у прогона есть нижняя граница по времени сообщения (`floor`: при первом
прогоне — последние 30 дней) и предел числа запросов к модели. Что пропущено, видно в итогах.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import asyncpg

from .. import bridge
from . import commitments, dates, extract, people
from .extract import Episode, Msg

logger = logging.getLogger("shturman.processing")

HANDLER_EXTRACT = "commitments.extract"
HANDLER_RESOLVE = "commitments.resolve"
HANDLER_DIGEST = "commitments.digest"
STATE_KEY = "processing.state"       # {"watermark": messages.id, "floor": ISO-время}
NIGHTLY_KEY = "processing.nightly"   # {"night": "ГГГГ-ММ-ДД"} — за какую ночь прогон уже запущен

# Кому сообщить, что прогон закончен (сборка страниц памяти). Обработчик вызывается внутри
# транзакции завершения прогона, поэтому должен быть коротким: поставить отметку, разбудить
# свою фоновую работу.
RunHook = Callable[[asyncpg.Connection, int], Awaitable[None]]
_after_run: list[RunHook] = []


def after_run(fn: RunHook) -> RunHook:
    """Регистрирует обработчик «прогон закончен»: fn(conn, run_id)."""
    _after_run.append(fn)
    return fn


# Типы чатов, которые не разбираются: каналы (вещание), боты, «Избранное».
SKIP_CHAT_TYPES = frozenset({"private_channel", "public_channel", "bot_chat", "saved_messages"})


@dataclass(frozen=True)
class Options:
    limit: int = 200              # предел запросов на извлечение за прогон
    window: int = 20_000          # сколько новых сообщений просматривается за прогон
    first_run_days: int = 30      # при первом прогоне история старше не разбирается
    context_messages: int = 4     # сколько предыдущих сообщений показывается для понимания
    context_hours: int = 12
    known_limit: int = 15         # сколько уже записанных обязательств показывается для отметки дублей
    resolve_messages: int = 40    # сколько новых сообщений чата идёт в проверку статусов
    resolve_commitments: int = 20


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def load_state(conn: asyncpg.Connection) -> dict[str, Any]:
    return _loads(await conn.fetchval("SELECT value FROM settings WHERE key = $1", STATE_KEY)) or {}


async def _save_setting(conn: asyncpg.Connection, key: str, value: dict[str, Any]) -> None:
    await conn.execute(
        """INSERT INTO settings (key, value) VALUES ($1, $2::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        key, json.dumps(value, ensure_ascii=False),
    )


def _msg(row: asyncpg.Record) -> Msg:
    return Msg(id=row["id"], chat_id=row["chat_id"], sent_at=row["sent_at"],
               sender_peer_id=row["sender_peer_id"], sender_name=row["sender_name"],
               is_outgoing=bool(row["is_outgoing"]), text=row["text"] or "", forwarded=row["forwarded"])


_MSG_COLUMNS = """m.id, m.chat_id, m.sent_at, m.sender_peer_id, m.sender_name, m.is_outgoing, m.text,
                  m.forwarded_from IS NOT NULL AS forwarded"""


def _chat_kind(chat_type: str) -> str:
    return "личный" if chat_type == "personal_chat" else "групповой"


def _labels_to_context(labels: dict[tuple, str]) -> list[dict[str, Any]]:
    return [{"label": label, "key": list(key)} for key, label in labels.items()]


def _labels_from_context(raw: Any) -> dict[tuple, str]:
    out: dict[tuple, str] = {}
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("key"), list) and isinstance(item.get("label"), str):
            out[tuple(item["key"])] = item["label"]
    return out


async def _pending(conn: asyncpg.Connection, run_id: int) -> int:
    """Сколько запросов прогона ещё может вернуться. Задание, закрытое как неудачное без нас
    (вышли попытки), ожидаемым не считается."""
    return await conn.fetchval(
        """SELECT count(*) FROM processing_requests r JOIN jobs j ON j.id = r.job_id
           WHERE r.run_id = $1 AND r.state = 'pending' AND j.status IN ('queued', 'running')""",
        run_id)


# --- планирование ----------------------------------------------------------------------------

async def _context_for(conn: asyncpg.Connection, first: Msg, options: Options) -> list[Msg]:
    rows = await conn.fetch(
        f"""SELECT {_MSG_COLUMNS} FROM messages m
            WHERE m.chat_id = $1 AND (m.sent_at, m.id) < ($2, $3) AND m.sent_at > $2 - make_interval(hours => $4)
              AND m.deleted_at IS NULL AND m.kind = 'message' AND m.text <> ''
            ORDER BY m.sent_at DESC, m.id DESC LIMIT $5""",
        first.chat_id, first.sent_at, first.id, options.context_hours, options.context_messages)
    return [_msg(r) for r in reversed(rows)]


def _who(peer_id: int | None, direction: str, labels: dict[tuple, str]) -> str:
    if direction == "owner_owes":
        return extract.OWNER_LABEL
    return labels.get(("peer", peer_id), "другой участник")


async def plan_run(
    conn: asyncpg.Connection, *, tz: str, trigger: str = "manual", since: datetime | None = None,
    limit: int | None = None, rescan: bool = False, now: datetime | None = None,
    options: Options = Options(),
) -> dict[str, Any]:
    """Планирует прогон: ставит запросы к модели и возвращает счётчики.

    since  — нижняя граница по времени сообщения; запоминается и действует на следующие прогоны;
    limit  — предел запросов на извлечение за этот прогон;
    rescan — начать просмотр архива заново (вместе с since: разобрать более старую историю).
             Уже разобранное повторно не предлагается: совпадения отсекаются при записи.
    """
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(tz)
    limit = max(1, min(int(limit or options.limit), 1000))
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.processing'))")
        running = await conn.fetchval("SELECT id FROM processing_runs WHERE status = 'running'")
        if running is not None:
            pending = await _pending(conn, running)
            if pending:
                return {"status": "already_running", "run_id": running, "pending": pending,
                        "planned": 0, "resolve_planned": 0}
            await finish_run(conn, running)

        await scrub_closed_jobs(conn)
        swept = await commitments.purge_orphans(conn)
        expired = await commitments.expire_stale(conn)
        synced = await people.sync_people(conn)

        state = await load_state(conn)
        if since is not None:
            floor = since if since.tzinfo else since.replace(tzinfo=zone)
        elif state.get("floor"):
            floor = datetime.fromisoformat(state["floor"])
        else:
            floor = now - timedelta(days=options.first_run_days)
        watermark = 0 if rescan else int(state.get("watermark") or 0)

        rows = await conn.fetch(
            f"""SELECT {_MSG_COLUMNS}, m.kind, m.deleted_at IS NOT NULL AS deleted,
                       c.type AS chat_type, c.excluded, sp.is_bot IS TRUE AS from_bot
                FROM messages m JOIN chats c ON c.id = m.chat_id
                LEFT JOIN peers sp ON sp.id = m.sender_peer_id
                WHERE m.id > $1 ORDER BY m.id LIMIT $2""",
            watermark, options.window)
        eligible: list[Msg] = []
        chat_types: dict[int, str] = {}
        verdicts: list[tuple[int, str]] = []    # (messages.id, что с сообщением сделано)
        for row in rows:
            if row["excluded"]:
                verdict = "skipped_excluded"
            elif row["chat_type"] in SKIP_CHAT_TYPES:
                verdict = "skipped_chat_type"
            elif row["from_bot"]:
                verdict = "skipped_bot"         # бот в группе: его «отправлю отчёт» — не обещание человека
            elif row["deleted"]:
                verdict = "skipped_deleted"
            elif row["kind"] != "message":
                verdict = "skipped_service"
            elif row["sent_at"] < floor:
                verdict = "skipped_old"
            elif not (row["text"] or "").strip():
                verdict = "skipped_empty"
            else:
                verdict = "eligible"
                eligible.append(_msg(row))
                chat_types[row["chat_id"]] = row["chat_type"]
            verdicts.append((row["id"], verdict))
        new_watermark = rows[-1]["id"] if rows else watermark
        more = len(rows) == options.window

        # Предел запросов: окно сужается до сообщений, эпизоды которых помещаются в предел.
        # Отметка ставится на границу окна, поэтому отложенное разберёт следующий прогон.
        cap_reached = False
        while True:
            episodes = extract.build_episodes(eligible)
            signal = [e for e in episodes if extract.has_promise_signal(e)]
            if len(signal) <= limit:
                break
            cap_reached = more = True
            new_watermark = signal[limit - 1].last_id
            eligible = [m for m in eligible if m.id <= new_watermark]

        # итоги — только по сообщениям до отметки; остальное отложено до следующего прогона
        counts = {"new": 0, "eligible": 0, "skipped_old": 0, "skipped_excluded": 0, "skipped_chat_type": 0,
                  "skipped_bot": 0, "skipped_service": 0, "skipped_deleted": 0, "skipped_empty": 0, "deferred": 0}
        for message_id, verdict in verdicts:
            if message_id > new_watermark:
                counts["deferred"] += 1
            else:
                counts["new"] += 1
                counts[verdict] += 1

        run_id = await conn.fetchval(
            "INSERT INTO processing_runs (trigger, stats) VALUES ($1, $2::jsonb) RETURNING id",
            trigger, json.dumps({"tz": tz}))

        planned = already = 0
        for episode in signal:
            episode.context = await _context_for(conn, episode.messages[0], options)
            labels = extract.speaker_labels(episode)
            known_rows = [r for r in await commitments.existing_in_chat(conn, episode.chat_id, options.known_limit * 4)
                          if r["status"] in ("proposed", "open")][: options.known_limit]
            known = [{"who": _who(r["debtor_peer_id"], r["direction"], labels), "what": r["what"],
                      "due_expression": r["due_expression"]} for r in known_rows]
            job_id = await bridge.request_structured(
                conn, handler=HANDLER_EXTRACT, instructions=extract.EXTRACT_INSTRUCTIONS,
                input=extract.build_extract_input(
                    episode, labels, zone, chat_kind=_chat_kind(chat_types[episode.chat_id]), known=known),
                json_schema=extract.EXTRACT_SCHEMA, schema_name="commitments",
                context={"run_id": run_id, "chat_id": episode.chat_id, "tz": tz,
                         "message_ids": [m.id for m in episode.messages],
                         "context_ids": [m.id for m in episode.context],
                         "known_ids": [r["id"] for r in known_rows],
                         "labels": _labels_to_context(labels)},
                dedup_key=f"cm-x{extract.PROMPT_VERSION}:{episode.chat_id}:{episode.first_id}-{episode.last_id}",
            )
            if job_id is None:
                already += 1
                continue
            planned += 1
            await conn.execute(
                "INSERT INTO processing_requests (job_id, run_id, kind, chat_id) VALUES ($1, $2, 'extract', $3)",
                job_id, run_id, episode.chat_id)

        resolve_planned = await _plan_resolve(conn, run_id, eligible, chat_types, tz, options)

        new_state = {"watermark": max(new_watermark, 0), "floor": floor.isoformat()}
        await _save_setting(conn, STATE_KEY, new_state)
        result = {
            "status": "planned" if planned or resolve_planned else "nothing_to_do",
            "run_id": run_id, "planned": planned, "resolve_planned": resolve_planned,
            "already_planned": already, "episodes": len(episodes),
            "episodes_without_signal": len(episodes) - len(signal),
            "messages": counts, "cap_reached": cap_reached, "more": more,
            "watermark": new_state["watermark"], "floor": new_state["floor"],
            "expired": expired, "purged": swept, "people": synced,
        }
        await conn.execute(
            "UPDATE processing_runs SET stats = stats || $2::jsonb WHERE id = $1",
            run_id, json.dumps({"plan": {k: v for k, v in result.items() if k != "run_id"}}, ensure_ascii=False))
        if not planned and not resolve_planned:
            await finish_run(conn, run_id)
    logger.info("прогон %s: запросов %s, проверок статуса %s, новых сообщений %s",
                run_id, planned, resolve_planned, counts["new"])
    return result


async def _plan_resolve(
    conn: asyncpg.Connection, run_id: int, eligible: list[Msg], chat_types: dict[int, str],
    tz: str, options: Options,
) -> int:
    """Для чатов с открытыми обязательствами и новыми сообщениями — запрос «что с ними стало»."""
    by_chat: dict[int, list[Msg]] = {}
    for message in eligible:
        by_chat.setdefault(message.chat_id, []).append(message)
    if not by_chat:
        return 0
    open_rows = await conn.fetch(
        """SELECT c.id, c.chat_id, c.what, c.due_expression, c.debtor_peer_id, c.direction, c.source_message_id
           FROM commitments c WHERE c.status = 'open' AND c.chat_id = ANY($1::bigint[]) ORDER BY c.id""",
        list(by_chat))
    planned = 0
    for chat_id in sorted({r["chat_id"] for r in open_rows}):
        messages = sorted(by_chat[chat_id], key=lambda m: (m.sent_at, m.id))[-options.resolve_messages:]
        # сообщение, в котором дано само обещание, его же выполнением не считается
        items = [r for r in open_rows if r["chat_id"] == chat_id
                 and any(m.id != r["source_message_id"] for m in messages)][: options.resolve_commitments]
        if not items:
            continue
        episode = Episode(chat_id, messages)
        labels = extract.speaker_labels(episode)
        job_id = await bridge.request_structured(
            conn, handler=HANDLER_RESOLVE, instructions=extract.RESOLVE_INSTRUCTIONS,
            input=extract.build_resolve_input(
                episode, labels, ZoneInfo(tz), chat_kind=_chat_kind(chat_types[chat_id]),
                commitments=[{"who": _who(r["debtor_peer_id"], r["direction"], labels), "what": r["what"],
                              "due_expression": r["due_expression"]} for r in items]),
            json_schema=extract.RESOLVE_SCHEMA, schema_name="commitment_updates", max_tokens=1200,
            context={"run_id": run_id, "chat_id": chat_id, "tz": tz,
                     "message_ids": [m.id for m in messages], "context_ids": [],
                     "commitment_ids": [r["id"] for r in items], "labels": _labels_to_context(labels)},
            dedup_key=f"cm-r{extract.PROMPT_VERSION}:{chat_id}:{messages[0].id}-{messages[-1].id}",
        )
        if job_id is None:
            continue
        planned += 1
        await conn.execute(
            "INSERT INTO processing_requests (job_id, run_id, kind, chat_id) VALUES ($1, $2, 'resolve', $3)",
            job_id, run_id, chat_id)
    return planned


# --- завершение прогона и сводка ------------------------------------------------------------------

async def finish_run(conn: asyncpg.Connection, run_id: int) -> int:
    """Закрывает прогон и отправляет владельцу сводку: одну на прогон, по несколько пунктов
    в сообщении. Возвращает число отправленных сообщений."""
    row = await conn.fetchrow("SELECT status, stats FROM processing_runs WHERE id = $1 FOR UPDATE", run_id)
    if row is None or row["status"] == "done":
        return 0
    stats = _loads(row["stats"]) or {}
    today = datetime.now(ZoneInfo(stats.get("tz") or "UTC")).date()
    digests = await commitments.build_digests(conn, run_id=run_id, today=today)
    for digest in digests:
        await bridge.notify_owner(conn, digest["text"], buttons=digest["buttons"],
                                  handler=HANDLER_DIGEST, context={"batch": digest["batch"]},
                                  dedup_key=f"cm-digest:{digest['batch']}")
    merges = await conn.fetchval("SELECT count(*) FROM person_proposals WHERE status = 'pending'")
    await conn.execute(
        """UPDATE processing_runs SET status = 'done', finished_at = now(), stats = stats || $2::jsonb
           WHERE id = $1""",
        run_id, json.dumps({"digest_messages": len(digests), "merge_proposals_pending": merges}))
    for fn in _after_run:
        await fn(conn, run_id)
    return len(digests)


async def _settle(conn: asyncpg.Connection, job: dict[str, Any], state: str, stats: dict[str, int]) -> None:
    """Отмечает запрос разобранным; если он последний в прогоне — завершает прогон."""
    await _scrub_job(conn, job["id"])
    run_id = (job.get("context") or {}).get("run_id")
    if not isinstance(run_id, int):
        return
    # строка прогона блокируется: два последних ответа, пришедшие одновременно, разберутся по очереди
    row = await conn.fetchrow("SELECT status, stats FROM processing_runs WHERE id = $1 FOR UPDATE", run_id)
    if row is None:
        return
    await conn.execute("UPDATE processing_requests SET state = $2 WHERE job_id = $1", job["id"], state)
    totals = (_loads(row["stats"]) or {}).get("results") or {}
    for key, value in stats.items():
        totals[key] = int(totals.get(key, 0)) + int(value)
    await conn.execute("UPDATE processing_runs SET stats = stats || $2::jsonb WHERE id = $1",
                       run_id, json.dumps({"results": totals}))
    if row["status"] == "running" and await _pending(conn, run_id) == 0:
        await finish_run(conn, run_id)


async def _scrub_job(conn: asyncpg.Connection, job_id: int) -> None:
    """Стирает из закрытого задания текст переписки. В очереди лежит копия сообщений (запрос
    к модели, её ответ, текст сводки); после разбора она не нужна и не должна переживать
    удаление самих сообщений. Ключ защиты от повторов остаётся."""
    await conn.execute("UPDATE jobs SET payload = '{}'::jsonb, result = NULL WHERE id = $1", job_id)


async def scrub_closed_jobs(conn: asyncpg.Connection) -> int:
    """То же для заданий, закрытых без нашего обработчика (исполнитель пропал, попытки вышли)."""
    done = await conn.execute(
        """UPDATE jobs SET payload = '{}'::jsonb, result = NULL
           WHERE handler = ANY($1::text[]) AND status IN ('done', 'failed') AND payload <> '{}'::jsonb""",
        [HANDLER_EXTRACT, HANDLER_RESOLVE, HANDLER_DIGEST])
    return int(done.split()[-1])


async def finish_stale_runs(conn: asyncpg.Connection) -> int:
    """Закрывает прогон, запросы которого уже не вернутся (исполнитель пропал, попытки вышли)."""
    async with conn.transaction():
        await scrub_closed_jobs(conn)
        running = await conn.fetchval("SELECT id FROM processing_runs WHERE status = 'running'")
        if running is None or await _pending(conn, running):
            return 0
        await finish_run(conn, running)
    return 1


# --- разбор ответов модели ---------------------------------------------------------------------------

async def _load_episode(conn: asyncpg.Connection, ctx: dict[str, Any]) -> tuple[Episode, asyncpg.Record] | None:
    """Восстанавливает эпизод по идентификаторам из контекста задания. Текст берётся из архива
    заново: сообщение могли удалить или исправить, пока запрос ждал в очереди."""
    chat_id, ids, context_ids = ctx.get("chat_id"), ctx.get("message_ids"), ctx.get("context_ids") or []
    if not isinstance(chat_id, int) or not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        return None
    chat = await conn.fetchrow(
        """SELECT c.id, c.type, c.peer_id, c.excluded, pr.class AS peer_class,
                  (SELECT p.id FROM peers p WHERE p.class = 'user' AND p.tg_id = a.tg_user_id) AS owner_peer_id
           FROM chats c JOIN accounts a ON a.id = c.account_id JOIN peers pr ON pr.id = c.peer_id
           WHERE c.id = $1""", chat_id)
    if chat is None or chat["excluded"]:
        return None
    rows = await conn.fetch(
        f"""SELECT {_MSG_COLUMNS}, m.deleted_at IS NOT NULL AS deleted, m.kind FROM messages m
            WHERE m.chat_id = $1 AND m.id = ANY($2::bigint[])""",
        chat_id, [i for i in [*ids, *context_ids] if isinstance(i, int)])
    by_id = {r["id"]: r for r in rows}

    def restore(message_id: int) -> Msg:
        row = by_id.get(message_id)
        if row is None or row["deleted"] or row["kind"] != "message":
            # номер сохраняется, но из исчезнувшего сообщения ничего извлечь нельзя
            return Msg(id=message_id, chat_id=chat_id, sent_at=datetime.now(timezone.utc), sender_peer_id=None,
                       sender_name=None, is_outgoing=False, text="", forwarded=True)
        return _msg(row)

    return Episode(chat_id, [restore(i) for i in ids],
                   [restore(i) for i in context_ids if isinstance(i, int)]), chat


def _parties(candidate: extract.Candidate, chat: asyncpg.Record) -> tuple[int | None, int | None, str]:
    """Кто должен, кому и как это выглядит относительно владельца. Должник — автор сообщения."""
    message = candidate.message
    personal = chat["type"] == "personal_chat" and chat["peer_class"] == "user"
    recipient = candidate.recipient_key
    recipient_peer = recipient[1] if recipient and recipient[0] == "peer" else None
    if message.is_outgoing:
        debtor = message.sender_peer_id or chat["owner_peer_id"]
        creditor = recipient_peer or (chat["peer_id"] if personal else None)
        return debtor, creditor, "owner_owes"
    if personal or recipient == ("owner",):
        return message.sender_peer_id, chat["owner_peer_id"], "owed_to_owner"
    return message.sender_peer_id, recipient_peer, "others"


async def _apply_extraction(conn: asyncpg.Connection, ctx: dict[str, Any], result: dict[str, Any]) -> dict[str, int]:
    loaded = await _load_episode(conn, ctx)
    if loaded is None:
        return {"skipped_requests": 1}
    episode, chat = loaded
    candidates, dropped = extract.validate_extraction(
        result.get("parsed"), episode, _labels_from_context(ctx.get("labels")))
    stats = {f"dropped_{k}": v for k, v in dropped.items() if v}
    existing: list[Any] = list(await commitments.existing_in_chat(conn, episode.chat_id))
    known_ids = [i for i in ctx.get("known_ids") or [] if isinstance(i, int)]
    tz = ctx.get("tz") or "UTC"
    for candidate in candidates:
        debtor, creditor, direction = _parties(candidate, chat)
        due = commitments.candidate_due(candidate, tz)
        hinted = None
        if candidate.duplicate_of is not None and 1 <= candidate.duplicate_of <= len(known_ids):
            hinted = known_ids[candidate.duplicate_of - 1]
        if commitments.find_duplicate(
                existing, source_message_id=candidate.message.id, debtor_peer_id=debtor,
                what=candidate.what, quote=candidate.quote, due_date=due.due_date, hinted_id=hinted) is not None:
            stats["duplicates"] = stats.get("duplicates", 0) + 1
            continue
        commitment_id = await commitments.propose(
            conn, chat_id=episode.chat_id, candidate=candidate, debtor_peer_id=debtor,
            creditor_peer_id=creditor, direction=direction, tz=tz, run_id=ctx.get("run_id"),
            model=result.get("model") if isinstance(result.get("model"), str) else None)
        existing.append({"id": commitment_id, "source_message_id": candidate.message.id,
                         "debtor_peer_id": debtor, "what": candidate.what, "source_quote": candidate.quote,
                         "due_date": due.due_date, "status": "proposed"})
        for peer_id in (debtor, creditor):
            if peer_id is not None:
                await people.ensure_person_for_peer(conn, peer_id)
        stats["proposed"] = stats.get("proposed", 0) + 1
    return stats


async def _apply_resolution(conn: asyncpg.Connection, ctx: dict[str, Any], result: dict[str, Any]) -> dict[str, int]:
    loaded = await _load_episode(conn, ctx)
    ids = [i for i in ctx.get("commitment_ids") or [] if isinstance(i, int)]
    if loaded is None or not ids:
        return {"skipped_requests": 1}
    episode, _ = loaded
    updates, dropped = extract.validate_resolution(result.get("parsed"), episode, len(ids))
    stats = {f"dropped_{k}": v for k, v in dropped.items() if v}
    tz = ctx.get("tz") or "UTC"
    for update in updates:
        row = await conn.fetchrow(
            """SELECT c.id, c.status, c.source_message_id, m.sent_at FROM commitments c
               JOIN messages m ON m.id = c.source_message_id WHERE c.id = $1 AND c.chat_id = $2""",
            ids[update.commitment_index], episode.chat_id)
        # основание — только более позднее сообщение, чем само обещание
        if row is None or row["status"] != "open" or update.message.id == row["source_message_id"] \
                or update.message.sent_at < row["sent_at"]:
            stats["dropped_stale"] = stats.get("dropped_stale", 0) + 1
            continue
        due = None
        if update.kind == "rescheduled":
            due = dates.resolve_due_expression(update.new_due_expression, update.message.sent_at, tz)
            if not due.resolved:
                # перенос без вычисленной даты не предлагается: срок остаётся прежним
                stats["dropped_unresolved_due"] = stats.get("dropped_unresolved_due", 0) + 1
                continue
        change_id = await commitments.propose_change(
            conn, commitment_id=row["id"], kind=update.kind, evidence_message_id=update.message.id,
            quote=update.quote, new_due_expression=update.new_due_expression, due=due, run_id=ctx.get("run_id"))
        key = "changes_proposed" if change_id is not None else "duplicates"
        stats[key] = stats.get(key, 0) + 1
    return stats


async def _handle(conn: asyncpg.Connection, job: dict[str, Any], result: Any, apply) -> None:
    ctx = job.get("context") or {}
    try:
        async with conn.transaction():
            stats = await apply(conn, ctx, result if isinstance(result, dict) else {})
        state = "done"
    except Exception as exc:
        # Ответ модели не должен ронять очередь: запрос отмечается неудачным, прогон идёт дальше.
        # В журнал идёт только вид ошибки: в её тексте и в трассировке может оказаться переписка.
        logger.error("не удалось разобрать ответ на задание %s: %s", job.get("id"), type(exc).__name__)
        stats, state = {"handler_errors": 1}, "failed"
    await _settle(conn, job, state, stats)


@bridge.on_result(HANDLER_EXTRACT)
async def on_extract_result(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    await _handle(conn, job, result, _apply_extraction)


@bridge.on_result(HANDLER_RESOLVE)
async def on_resolve_result(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    await _handle(conn, job, result, _apply_resolution)


@bridge.on_failure(HANDLER_EXTRACT)
@bridge.on_failure(HANDLER_RESOLVE)
async def on_request_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    await _settle(conn, job, "failed", {"failed_requests": 1})


@bridge.on_result(HANDLER_DIGEST)
async def on_digest_sent(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    await _scrub_job(conn, job["id"])


@bridge.on_failure(HANDLER_DIGEST)
async def on_digest_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    """Сводка до владельца не дошла: её пункты снова считаются непоказанными и уйдут со следующей."""
    await _scrub_job(conn, job["id"])
    batch = (job.get("context") or {}).get("batch")
    if isinstance(batch, str):
        await commitments.unmark_batch(conn, batch)


@bridge.on_callback(commitments.CALLBACK_MODULE)
async def on_button(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    return await commitments.handle_callback(conn, rest)


# --- ночной запуск ---------------------------------------------------------------------------------

async def nightly_tick(
    conn: asyncpg.Connection, *, tz: str, at: time, now: datetime | None = None,
    catch_up: timedelta = timedelta(hours=6), options: Options = Options(),
) -> dict[str, Any] | None:
    """Запускает ночной прогон, если наступило его время и за эту ночь он ещё не запускался.

    Отметка о ночи пишется в базу в одной транзакции с планированием, поэтому перезапуск сервиса
    второй прогон за ту же ночь не вызовет. Если сервис не работал в назначенное время, прогон
    запускается при первой возможности в пределах `catch_up`, позже — ждёт следующей ночи.
    """
    zone = ZoneInfo(tz)
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    night = None
    for day in (local.date(), local.date() - timedelta(days=1)):
        scheduled = datetime.combine(day, at, tzinfo=zone)
        if scheduled <= local < scheduled + catch_up:
            night = day.isoformat()
            break
    if night is None:
        return None
    async with conn.transaction():
        claimed = await conn.fetchval(
            """INSERT INTO settings (key, value) VALUES ($1, $2::jsonb)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
               WHERE settings.value IS DISTINCT FROM EXCLUDED.value
               RETURNING 1""",
            NIGHTLY_KEY, json.dumps({"night": night}))
        if not claimed:
            return None
        return await plan_run(conn, tz=tz, trigger="nightly", now=now, options=options)
