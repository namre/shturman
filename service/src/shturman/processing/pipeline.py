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
Сообщения старше границы прогонов не занимают: окно прогона набирается только из годных
сообщений, а отметка переходит через всё остальное одним запросом.

Неудавшийся запрос (исполнитель отказал, задание не забрали, ответ не разобрался) не теряет
эпизод: его сообщения ставятся заново следующим прогоном, не больше `MAX_REPLANS` раз, после
чего эпизод считается пропущенным и попадает в счётчик итогов. Сообщения запроса хранятся
в `processing_requests`, а не в задании: содержимое закрытых заданий очередь стирает.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable, Sequence
from zoneinfo import ZoneInfo

import asyncpg

from .. import bridge
from . import commitments, dates, extract, people
from .extract import Episode, Msg

logger = logging.getLogger("shturman.processing")

HANDLER_EXTRACT = "commitments.extract"
HANDLER_RESOLVE = "commitments.resolve"
HANDLER_DIGEST = "commitments.digest"
STATE_KEY = "processing.state"       # {"watermark": messages.id, "floor": ISO-время, "more": bool}
NIGHTLY_KEY = "processing.nightly"   # {"night": "ГГГГ-ММ-ДД", "runs": N} — прогоны, запущенные за эту ночь
MAX_REPLANS = 2                      # сколько раз неудавшийся запрос ставится заново

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
    window: int = 20_000          # сколько годных новых сообщений берётся за прогон
    late_window: int = 500        # сколько сообщений с поздно появившимся текстом берётся за прогон
    first_run_days: int = 30      # при первом прогоне история старше не разбирается
    context_messages: int = 4     # сколько предыдущих сообщений показывается для понимания
    context_hours: int = 12
    known_limit: int = 15         # сколько уже записанных обязательств показывается для отметки дублей
    resolve_messages: int = 40    # сколько новых сообщений чата идёт в проверку статусов
    resolve_commitments: int = 20
    nightly_runs: int = 12        # сколько прогонов подряд можно запустить за одну ночь, пока есть что разбирать


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


async def _add_run_stats(conn: asyncpg.Connection, run_id: int, stats: dict[str, int]) -> None:
    """Прибавляет счётчики к итогам прогона (ключ results). Только числа, без текста переписки."""
    row = await conn.fetchrow("SELECT stats FROM processing_runs WHERE id = $1 FOR UPDATE", run_id)
    if row is None or not stats:
        return
    totals = (_loads(row["stats"]) or {}).get("results") or {}
    for key, value in stats.items():
        totals[key] = int(totals.get(key, 0)) + int(value)
    await conn.execute("UPDATE processing_runs SET stats = stats || $2::jsonb WHERE id = $1",
                       run_id, json.dumps({"results": totals}))


def _msg(row: asyncpg.Record) -> Msg:
    return Msg(id=row["id"], chat_id=row["chat_id"], sent_at=row["sent_at"],
               sender_peer_id=row["sender_peer_id"], sender_name=row["sender_name"],
               is_outgoing=bool(row["is_outgoing"]), text=row["text"] or "", forwarded=row["forwarded"],
               tg_id=row["tg_message_id"])


_MSG_COLUMNS = """m.id, m.chat_id, m.tg_message_id, m.sent_at, m.sender_peer_id, m.sender_name, m.is_outgoing,
                  m.text, m.forwarded_from IS NOT NULL AS forwarded"""

# Что делать с сообщением при планировании. $2 — нижняя граница по времени сообщения.
_FROM = "FROM messages m JOIN chats c ON c.id = m.chat_id LEFT JOIN peers sp ON sp.id = m.sender_peer_id"
_SKIP_TYPES_SQL = ", ".join(f"'{name}'" for name in sorted(SKIP_CHAT_TYPES))   # свои константы, не ввод
_VERDICT = f"""CASE WHEN c.excluded THEN 'skipped_excluded'
                    WHEN c.type IN ({_SKIP_TYPES_SQL}) THEN 'skipped_chat_type'
                    WHEN sp.is_bot IS TRUE THEN 'skipped_bot'
                    WHEN m.deleted_at IS NOT NULL THEN 'skipped_deleted'
                    WHEN NOT m.agent_visible THEN 'skipped_hidden'
                    WHEN m.kind <> 'message' THEN 'skipped_service'
                    WHEN m.sent_at < $2 THEN 'skipped_old'
                    WHEN btrim(m.text) = '' THEN 'skipped_empty'
                    ELSE 'eligible' END"""
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _sent_by_service() -> Callable[..., Awaitable[set[int]]] | None:
    """`outbox.sent_by_service(conn, chat_id, tg_message_ids) -> set[int]`, если шлюз отправки её даёт."""
    try:
        from .. import outbox
    except Exception:   # шлюза отправки в сборке может не быть
        return None
    return getattr(outbox, "sent_by_service", None)


async def _mark_service_sent(conn: asyncpg.Connection, messages: Sequence[Msg]) -> list[Msg]:
    """Помечает исходящие, которые за владельца отправил сам сервис (автоответ доверенным):
    они остаются в разговоре для понимания, но обещаниями владельца не считаются."""
    check = _sent_by_service()
    if check is None:
        return list(messages)
    by_chat: dict[int, list[int]] = {}
    for message in messages:
        if message.is_outgoing and message.tg_id is not None:
            by_chat.setdefault(message.chat_id, []).append(message.tg_id)
    sent: set[tuple[int, int]] = set()
    for chat_id, tg_ids in by_chat.items():
        sent.update((chat_id, int(tg_id)) for tg_id in await check(conn, chat_id, tg_ids))
    return [dataclasses.replace(m, by_service=True) if (m.chat_id, m.tg_id) in sent else m for m in messages]


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
              AND m.deleted_at IS NULL AND m.agent_visible AND m.kind = 'message' AND m.text <> ''
            ORDER BY m.sent_at DESC, m.id DESC LIMIT $5""",
        first.chat_id, first.sent_at, first.id, options.context_hours, options.context_messages)
    return await _mark_service_sent(conn, [_msg(r) for r in reversed(rows)])


def _who(peer_id: int | None, direction: str, labels: dict[tuple, str]) -> str:
    if direction == "owner_owes":
        return extract.OWNER_LABEL
    return labels.get(("peer", peer_id), "другой участник")


async def _enqueue_extract(
    conn: asyncpg.Connection, run_id: int, episode: Episode, chat_type: str, tz: str,
    options: Options, attempt: int = 0,
) -> int | None:
    """Ставит запрос на извлечение по эпизоду. None — такой запрос уже ставился."""
    episode.context = await _context_for(conn, episode.messages[0], options)
    labels = extract.speaker_labels(episode)
    known_rows = [r for r in await commitments.existing_in_chat(conn, episode.chat_id, options.known_limit * 4)
                  if r["status"] in ("proposed", "open")][: options.known_limit]
    known = [{"who": _who(r["debtor_peer_id"], r["direction"], labels), "what": r["what"],
              "due_expression": r["due_expression"]} for r in known_rows]
    key = f"cm-x{extract.PROMPT_VERSION}:{episode.chat_id}:{episode.first_id}-{episode.last_id}"
    job_id = await bridge.request_structured(
        conn, handler=HANDLER_EXTRACT, instructions=extract.EXTRACT_INSTRUCTIONS,
        input=extract.build_extract_input(episode, labels, ZoneInfo(tz), chat_kind=_chat_kind(chat_type), known=known),
        json_schema=extract.EXTRACT_SCHEMA, schema_name="commitments",
        context={"run_id": run_id, "chat_id": episode.chat_id, "tz": tz,
                 "message_ids": [m.id for m in episode.messages],
                 "context_ids": [m.id for m in episode.context],
                 "known_ids": [r["id"] for r in known_rows],
                 "labels": _labels_to_context(labels)},
        # повтор неудавшегося запроса — другое задание: прежний ключ его не блокирует
        dedup_key=key if attempt == 0 else f"{key}:r{attempt}",
    )
    if job_id is not None:
        await conn.execute(
            """INSERT INTO processing_requests (job_id, run_id, kind, chat_id, message_ids, attempt)
               VALUES ($1, $2, 'extract', $3, $4::bigint[], $5)""",
            job_id, run_id, episode.chat_id, [m.id for m in episode.messages], attempt)
    return job_id


async def _enqueue_resolve(
    conn: asyncpg.Connection, run_id: int, chat_id: int, messages: list[Msg], chat_type: str, tz: str,
    options: Options, attempt: int = 0,
) -> int | None:
    """Для чата с открытыми обязательствами и новыми сообщениями — запрос «что с ними стало»."""
    messages = sorted(messages, key=lambda m: (m.sent_at, m.id))[-options.resolve_messages:]
    open_rows = await conn.fetch(
        f"""SELECT c.id, c.what, c.due_expression, c.debtor_peer_id, c.direction, c.source_message_id
            FROM commitments c JOIN chats ch ON ch.id = c.chat_id JOIN messages m ON m.id = c.source_message_id
            WHERE c.status = 'open' AND c.chat_id = $1 AND {commitments.VISIBLE} ORDER BY c.id""", chat_id)
    # сообщение, в котором дано само обещание, его же выполнением не считается
    items = [r for r in open_rows
             if any(m.id != r["source_message_id"] for m in messages)][: options.resolve_commitments]
    if not items or not messages:
        return None
    episode = Episode(chat_id, messages)
    labels = extract.speaker_labels(episode)
    key = f"cm-r{extract.PROMPT_VERSION}:{chat_id}:{messages[0].id}-{messages[-1].id}"
    job_id = await bridge.request_structured(
        conn, handler=HANDLER_RESOLVE, instructions=extract.RESOLVE_INSTRUCTIONS,
        input=extract.build_resolve_input(
            episode, labels, ZoneInfo(tz), chat_kind=_chat_kind(chat_type),
            commitments=[{"who": _who(r["debtor_peer_id"], r["direction"], labels), "what": r["what"],
                          "due_expression": r["due_expression"]} for r in items]),
        json_schema=extract.RESOLVE_SCHEMA, schema_name="commitment_updates", max_tokens=1200,
        context={"run_id": run_id, "chat_id": chat_id, "tz": tz,
                 "message_ids": [m.id for m in messages], "context_ids": [],
                 "commitment_ids": [r["id"] for r in items], "labels": _labels_to_context(labels)},
        dedup_key=key if attempt == 0 else f"{key}:r{attempt}",
    )
    if job_id is not None:
        await conn.execute(
            """INSERT INTO processing_requests (job_id, run_id, kind, chat_id, message_ids, attempt)
               VALUES ($1, $2, 'resolve', $3, $4::bigint[], $5)""",
            job_id, run_id, chat_id, [m.id for m in messages], attempt)
    return job_id


async def _recover_lost(conn: asyncpg.Connection) -> None:
    """Запросы, чьи задания закрыты как неудачные без нашего обработчика (сняты, пропали):
    их сообщения тоже должны быть разобраны заново."""
    rows = await conn.fetch(
        """UPDATE processing_requests r
           SET state = CASE WHEN r.attempt < $1 THEN 'retry' ELSE 'given_up' END
           FROM jobs j WHERE j.id = r.job_id AND r.state = 'pending' AND j.status = 'failed'
           RETURNING r.run_id, r.state""", MAX_REPLANS)
    for row in rows:
        key = "requests_to_retry" if row["state"] == "retry" else "requests_given_up"
        await _add_run_stats(conn, row["run_id"], {"failed_requests": 1, key: 1})


async def _replan_failed(
    conn: asyncpg.Connection, run_id: int, tz: str, options: Options, limit: int,
) -> dict[str, int]:
    """Ставит заново запросы, которые не удались в прошлых прогонах."""
    out = {"retried": 0, "retry_dropped": 0}
    rows = await conn.fetch(
        """SELECT job_id, kind, chat_id, message_ids, attempt FROM processing_requests
           WHERE state = 'retry' ORDER BY job_id LIMIT $1 FOR UPDATE""", limit)
    for row in rows:
        # берём только то, что всё ещё годится: чат не исключён, сообщения не удалены
        found = await conn.fetch(
            f"""SELECT {_MSG_COLUMNS}, c.type AS chat_type {_FROM}
                WHERE m.id = ANY($1::bigint[]) AND m.chat_id = $3 AND ({_VERDICT}) = 'eligible' ORDER BY m.id""",
            list(row["message_ids"]), _EPOCH, row["chat_id"])
        job_id = None
        if found:
            messages = await _mark_service_sent(conn, [_msg(r) for r in found])
            messages.sort(key=lambda m: (m.sent_at, m.id))
            chat_type = found[0]["chat_type"]
            if row["kind"] == "extract":
                job_id = await _enqueue_extract(conn, run_id, Episode(row["chat_id"], messages), chat_type, tz,
                                                options, attempt=row["attempt"] + 1)
            else:
                job_id = await _enqueue_resolve(conn, run_id, row["chat_id"], messages, chat_type, tz,
                                                options, attempt=row["attempt"] + 1)
        await conn.execute("UPDATE processing_requests SET state = 'failed' WHERE job_id = $1", row["job_id"])
        out["retried" if job_id is not None else "retry_dropped"] += 1
    return out


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

    В итогах: planned / resolve_planned / retried — поставленные запросы; already_planned — эпизоды,
    запрос по которым уже ставился; messages — что сделано с сообщениями до отметки (new, eligible,
    skipped_*, assistant — написанные сервисом, deferred — отложенные за отметкой); cap_reached,
    more — осталось ли что разбирать; retry_waiting, given_up — неудавшиеся запросы.
    """
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(tz)
    limit = max(1, min(int(limit or options.limit), 1000))
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.processing'))")
        await _recover_lost(conn)
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

        run_id = await conn.fetchval(
            "INSERT INTO processing_runs (trigger, stats) VALUES ($1, $2::jsonb) RETURNING id",
            trigger, json.dumps({"tz": tz}))

        # сначала — долги прошлых прогонов; они тоже входят в предел запросов
        replanned = await _replan_failed(conn, run_id, tz, options, limit)
        budget = limit - replanned["retried"]

        eligible: list[Msg] = []
        chat_types: dict[int, str] = {}
        new_watermark, more, cap_reached = watermark, False, False
        episodes: list[Episode] = []
        signal: list[Episode] = []
        if budget <= 0:
            more = cap_reached = True      # новые сообщения подождут: предел ушёл на повторы
        else:
            # Окно набирается только из годных сообщений: старая история после большого импорта,
            # каналы и исключённые чаты его не занимают.
            rows = await conn.fetch(
                f"""SELECT {_MSG_COLUMNS}, c.type AS chat_type {_FROM}
                    WHERE m.id > $1 AND m.sent_at >= $2 AND ({_VERDICT}) = 'eligible'
                    ORDER BY m.id LIMIT $3""",
                watermark, floor, options.window)
            for row in rows:
                chat_types[row["chat_id"]] = row["chat_type"]
            eligible = await _mark_service_sent(conn, [_msg(row) for row in rows])
            more = len(rows) == options.window
            if more:
                new_watermark = rows[-1]["id"]
            else:   # годное кончилось: отметка переходит через весь остаток архива
                new_watermark = max(watermark, await conn.fetchval("SELECT COALESCE(max(id), 0) FROM messages"))

            # Предел запросов: окно сужается до сообщений, эпизоды которых помещаются в предел.
            # Отметка ставится на границу окна, поэтому отложенное разберёт следующий прогон.
            while True:
                episodes = extract.build_episodes(eligible)
                signal = [e for e in episodes if extract.has_promise_signal(e)]
                if len(signal) <= budget:
                    break
                cap_reached = more = True
                new_watermark = signal[budget - 1].last_id
                eligible = [m for m in eligible if m.id <= new_watermark]

        # Сообщения до прежней отметки, текст которых появился позже (расшифровка голосового,
        # разбор вложения): прошлый прогон видел их пустыми. Эпизод — сами эти сообщения,
        # предыдущие подтягиваются контекстом, как у любого эпизода. Делят тот же предел запросов.
        late, late_signal, late_seen = [], [], []
        if budget - len(signal) > 0 and watermark > 0:
            late_rows = await conn.fetch(
                f"""SELECT {_MSG_COLUMNS}, c.type AS chat_type {_FROM}
                    WHERE m.late_content AND m.id <= $1 AND m.sent_at >= $2 AND ({_VERDICT}) = 'eligible'
                    ORDER BY m.id LIMIT $3""",
                watermark, floor, options.late_window)
            for row in late_rows:
                chat_types[row["chat_id"]] = row["chat_type"]
            late = await _mark_service_sent(conn, [_msg(row) for row in late_rows])
            room = budget - len(signal)
            for episode in extract.build_episodes(late):
                if extract.has_promise_signal(episode):
                    if len(late_signal) >= room:
                        cap_reached = more = True
                        continue      # не поместилось: пометка остаётся до следующего прогона
                    late_signal.append(episode)
                late_seen.extend(m.id for m in episode.messages)
            seen = set(late_seen)
            late = [m for m in late if m.id in seen]

        # итоги — только по сообщениям до отметки; остальное отложено до следующего прогона
        counts = {"new": 0, "eligible": 0, "skipped_old": 0, "skipped_excluded": 0, "skipped_chat_type": 0,
                  "skipped_bot": 0, "skipped_service": 0, "skipped_deleted": 0, "skipped_empty": 0,
                  "skipped_hidden": 0}
        for row in await conn.fetch(
                f"SELECT ({_VERDICT}) AS verdict, count(*) AS n {_FROM} WHERE m.id > $1 AND m.id <= $3 GROUP BY 1",
                watermark, floor, new_watermark):
            counts[row["verdict"]] = row["n"]
            counts["new"] += row["n"]
        counts["assistant"] = sum(1 for m in eligible if m.by_service)
        counts["deferred"] = await conn.fetchval("SELECT count(*) FROM messages WHERE id > $1", new_watermark)

        # Рассмотренное снимается с пометки «текст появился позже»: и поздние сообщения, и
        # годные из окна (их текст уже был на месте). Скрытое защитой остаётся помеченным.
        cleared = [m.id for m in eligible] + late_seen
        if cleared:
            await conn.execute(
                "UPDATE messages SET late_content = false WHERE late_content AND id = ANY($1::bigint[])", cleared)
        counts["late"] = len(late_seen)

        planned = already = 0
        for episode in [*signal, *late_signal]:
            job_id = await _enqueue_extract(conn, run_id, episode, chat_types[episode.chat_id], tz, options)
            if job_id is None:
                already += 1
            else:
                planned += 1

        resolve_planned = 0
        by_chat: dict[int, list[Msg]] = {}
        for message in [*eligible, *late]:
            by_chat.setdefault(message.chat_id, []).append(message)
        if by_chat:
            with_open = await conn.fetch(
                "SELECT DISTINCT chat_id FROM commitments WHERE status = 'open' AND chat_id = ANY($1::bigint[])",
                list(by_chat))
            for chat_id in sorted(r["chat_id"] for r in with_open):
                job_id = await _enqueue_resolve(conn, run_id, chat_id, by_chat[chat_id], chat_types[chat_id], tz, options)
                resolve_planned += int(job_id is not None)

        waiting = await conn.fetchrow(
            """SELECT count(*) FILTER (WHERE state = 'retry') AS retry,
                      count(*) FILTER (WHERE state = 'given_up') AS given_up FROM processing_requests""")
        more = more or waiting["retry"] > 0
        new_state = {"watermark": max(new_watermark, 0), "floor": floor.isoformat(), "more": more}
        await _save_setting(conn, STATE_KEY, new_state)
        anything = planned or resolve_planned or replanned["retried"]
        result = {
            "status": "planned" if anything else "nothing_to_do",
            "run_id": run_id, "planned": planned, "resolve_planned": resolve_planned,
            "retried": replanned["retried"], "retry_dropped": replanned["retry_dropped"],
            "retry_waiting": waiting["retry"], "given_up": waiting["given_up"],
            "already_planned": already, "episodes": len(episodes),
            "episodes_without_signal": len(episodes) - len(signal), "late_planned": len(late_signal),
            "messages": counts, "cap_reached": cap_reached, "more": more,
            "watermark": new_state["watermark"], "floor": new_state["floor"],
            "expired": expired, "purged": swept, "people": synced,
        }
        await conn.execute(
            "UPDATE processing_runs SET stats = stats || $2::jsonb WHERE id = $1",
            run_id, json.dumps({"plan": {k: v for k, v in result.items() if k != "run_id"}}, ensure_ascii=False))
        if not anything:
            await finish_run(conn, run_id)
    logger.info("прогон %s: запросов %s, повторов %s, проверок статуса %s, новых сообщений %s",
                run_id, planned, replanned["retried"], resolve_planned, counts["new"])
    return result


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


async def _settle(conn: asyncpg.Connection, job: dict[str, Any], ok: bool, stats: dict[str, int]) -> None:
    """Отмечает запрос разобранным; если он последний в прогоне — завершает прогон.

    Неудавшийся запрос помечается к повтору (или пропущенным, если повторы исчерпаны). Прогон
    находится по `processing_requests`, а не по контексту задания: контекст может быть уже стёрт.
    """
    await _scrub_job(conn, job["id"])
    request = await conn.fetchrow(
        "SELECT run_id, attempt FROM processing_requests WHERE job_id = $1 FOR UPDATE", job["id"])
    if request is None:
        return
    run_id = request["run_id"]
    # строка прогона блокируется: два последних ответа, пришедшие одновременно, разберутся по очереди
    row = await conn.fetchrow("SELECT status FROM processing_runs WHERE id = $1 FOR UPDATE", run_id)
    if row is None:
        return
    stats = dict(stats)
    if ok:
        state = "done"
    else:
        state = "retry" if request["attempt"] < MAX_REPLANS else "given_up"
        stats["failed_requests"] = stats.get("failed_requests", 0) + 1
        stats["requests_to_retry" if state == "retry" else "requests_given_up"] = 1
    await conn.execute("UPDATE processing_requests SET state = $2 WHERE job_id = $1", job["id"], state)
    await _add_run_stats(conn, run_id, stats)
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
        await _recover_lost(conn)
        running = await conn.fetchval("SELECT id FROM processing_runs WHERE status = 'running'")
        if running is None or await _pending(conn, running):
            return 0
        await finish_run(conn, running)
    return 1


# --- разбор ответов модели ---------------------------------------------------------------------------

class _BadAnswer(Exception):
    """Ответ модели не похож на то, что просили (не JSON, не тот вид): запрос будет повторён."""


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
        # Скрытое защитой от внедрённых инструкций — как удалённое: в запрос к модели не идёт.
        f"""SELECT {_MSG_COLUMNS}, (m.deleted_at IS NOT NULL OR NOT m.agent_visible) AS deleted, m.kind
            FROM messages m
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

    messages = await _mark_service_sent(conn, [restore(i) for i in ids])
    context = await _mark_service_sent(conn, [restore(i) for i in context_ids if isinstance(i, int)])
    return Episode(chat_id, messages, context), chat


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
    if not extract.well_formed(result.get("parsed"), "commitments"):
        raise _BadAnswer()
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
    if not extract.well_formed(result.get("parsed"), "updates"):
        raise _BadAnswer()
    episode, _ = loaded
    updates, dropped = extract.validate_resolution(result.get("parsed"), episode, len(ids))
    stats = {f"dropped_{k}": v for k, v in dropped.items() if v}
    tz = ctx.get("tz") or "UTC"
    for update in updates:
        row = await conn.fetchrow(
            f"""SELECT c.id, c.status, c.source_message_id, m.sent_at FROM commitments c
                JOIN chats ch ON ch.id = c.chat_id JOIN messages m ON m.id = c.source_message_id
                WHERE c.id = $1 AND c.chat_id = $2 AND {commitments.VISIBLE}""",
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
        ok = True
    except _BadAnswer:
        stats, ok = {"dropped_malformed": 1}, False
    except Exception as exc:
        # Ответ модели не должен ронять очередь: запрос отмечается неудачным, прогон идёт дальше.
        # В журнал идёт только вид ошибки: в её тексте и в трассировке может оказаться переписка.
        logger.error("не удалось разобрать ответ на задание %s: %s", job.get("id"), type(exc).__name__)
        stats, ok = {"handler_errors": 1}, False
    await _settle(conn, job, ok, stats)


@bridge.on_result(HANDLER_EXTRACT)
async def on_extract_result(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    await _handle(conn, job, result, _apply_extraction)


@bridge.on_result(HANDLER_RESOLVE)
async def on_resolve_result(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    await _handle(conn, job, result, _apply_resolution)


@bridge.on_failure(HANDLER_EXTRACT)
@bridge.on_failure(HANDLER_RESOLVE)
async def on_request_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    """Исполнитель окончательно отказал или задание не забрали вовремя."""
    await _settle(conn, job, False, {})


@bridge.on_result(HANDLER_DIGEST)
async def on_digest_sent(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    await _scrub_job(conn, job["id"])


@bridge.on_failure(HANDLER_DIGEST)
async def on_digest_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    """Сводка до владельца не дошла: её пункты снова считаются непоказанными и уйдут со следующей —
    но не больше `commitments.DIGEST_MAX_SENDS` раз. Счётчики — в итогах прогона."""
    await _scrub_job(conn, job["id"])
    batch = (job.get("context") or {}).get("batch")
    if not isinstance(batch, str):
        return
    counts = await commitments.unmark_batch(conn, batch)
    run_id = batch.partition(".")[0]
    if run_id.isdigit():
        await _add_run_stats(conn, int(run_id), {
            "digest_failures": 1, "digest_items_requeued": counts["requeued"],
            "digest_items_dropped": counts["dropped"]})


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

    Продолжение. Если прогон закончился, а разбирать ещё есть что (`more` в отметке — большой
    импорт, предел запросов), следующий запускается сразу после него, не дожидаясь следующей
    ночи; за одну ночь — не больше `options.nightly_runs` прогонов.
    """
    zone = ZoneInfo(tz)
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    night = None
    for day in (local.date(), local.date() - timedelta(days=1)):
        scheduled = datetime.combine(day, at, tzinfo=zone)
        if scheduled <= local < scheduled + catch_up:
            night = day.isoformat()
            break
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.processing.nightly'))")
        record = _loads(await conn.fetchval("SELECT value FROM settings WHERE key = $1", NIGHTLY_KEY)) or {}
        if night is not None and record.get("night") != night:
            await _save_setting(conn, NIGHTLY_KEY, {"night": night, "runs": 1})
            return await plan_run(conn, tz=tz, trigger="nightly", now=now, options=options)
        runs = int(record.get("runs") or 1)
        if not record.get("night") or runs >= options.nightly_runs:
            return None
        if not (await load_state(conn)).get("more"):
            return None
        if await conn.fetchval("SELECT 1 FROM processing_runs WHERE status = 'running'"):
            return None     # предыдущий прогон ещё ждёт ответов модели
        await _save_setting(conn, NIGHTLY_KEY, {"night": record["night"], "runs": runs + 1})
        return await plan_run(conn, tz=tz, trigger="nightly", now=now, options=options)
