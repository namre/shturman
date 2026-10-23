"""Прогон обработки: что нового в архиве, какие запросы поставить модели, что показать владельцу.

Ход прогона (docs/memory.md, «Ночная сборка», шаги 1–4 и 10 в части обязательств):

  1. Берутся сообщения, записанные после прошлого прогона (отметка — наибольший обработанный
     `messages.id`, хранится в `settings`). Исключённые чаты, удалённые и служебные сообщения,
     каналы, боты и «Избранное» пропускаются.
  2. Сообщения режутся на эпизоды; к модели идут только эпизоды с признаком обещания, факта
     или решения.
  3. Запросы ставятся в очередь (`bridge.request_structured`). Модели у сервиса нет: ответ
     вернётся позже в обработчик результата.
  4. Ответ проверяется текстом сообщений: обязательства записываются как предложения; факты
     о людях со страницей и о проектах — сразу (facts.py), факты о владельце — как предложения;
     незнакомые названия проектов копятся как упоминания (projects.py).
  5. Когда все запросы прогона разобраны, владельцу уходит одна сводка с кнопками, а также
     предложения проектов и фактов профиля — каждые одним сообщением.

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
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable, Sequence
from zoneinfo import ZoneInfo

import asyncpg

from .. import bridge
from . import commitments, dates, extract, facts, people, projects
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
    resolve_messages: int = 40    # сколько сообщений чата идёт в одну проверку статусов
    resolve_commitments: int = 20
    resolve_chats: int = 100      # в скольких чатах за прогон проверяется, не сделано ли обещанное
    after_messages: int = 40      # блок «После» (Р-96): сколько сообщений чата после эпизода видит модель
    after_days: int = 7           # и за сколько дней после эпизода
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
               tg_id=row["tg_message_id"], reply_to_owner=bool(row["reply_to_owner"]))


_MSG_COLUMNS = """m.id, m.chat_id, m.tg_message_id, m.sent_at, m.sender_peer_id, m.sender_name, m.is_outgoing,
                  m.text, m.forwarded_from IS NOT NULL AS forwarded,
                  (m.reply_to_tg_id IS NOT NULL AND EXISTS (
                      SELECT 1 FROM messages rq WHERE rq.chat_id = m.chat_id AND rq.tg_message_id = m.reply_to_tg_id
                                                  AND rq.is_outgoing)) AS reply_to_owner"""
# Сообщение годится в разговор для модели: не удалено, не скрыто защитой, обычное, с текстом.
_READABLE = "m.deleted_at IS NULL AND m.agent_visible AND m.kind = 'message' AND btrim(m.text) <> ''"

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


async def _after_for(conn: asyncpg.Connection, last: Msg, options: Options) -> list[Msg]:
    """Блок «После» (Р-96): что было в чате после эпизода до сего момента — не больше
    `after_messages` сообщений и `after_days` дней. Только то, что видно ассистенту: удалённое,
    скрытое защитой, служебное и сообщения ботов сюда не идут."""
    rows = await conn.fetch(
        f"""SELECT {_MSG_COLUMNS} {_FROM}
            WHERE m.chat_id = $3 AND (m.sent_at, m.id) > ($4, $5) AND m.sent_at <= $4 + make_interval(days => $6)
              AND ({_VERDICT}) = 'eligible'
            ORDER BY m.sent_at, m.id LIMIT $1""",
        options.after_messages, _EPOCH, last.chat_id, last.sent_at, last.id, options.after_days)
    return await _mark_service_sent(conn, [_msg(r) for r in rows])


def _who(peer_id: int | None, direction: str, labels: dict[tuple, str]) -> str:
    if direction == "owner_owes":
        return extract.OWNER_LABEL
    return labels.get(("peer", peer_id), "другой участник")


async def _enqueue_extract(
    conn: asyncpg.Connection, run_id: int, episode: Episode, chat_type: str, tz: str,
    options: Options, attempt: int = 0, late: bool = False, replace_ids: Sequence[int] = (),
) -> int | None:
    """Ставит запрос на извлечение по эпизоду. None — такой запрос уже ставился.

    К эпизоду добавляется блок «После» — что было в чате дальше (Р-96): по нему модель сразу
    говорит, не сделано ли обещанное. `replace_ids` — перепроверка: эти прежние предложения из
    того же эпизода заменяются тем, что найдётся по новым правилам."""
    episode.context = await _context_for(conn, episode.messages[0], options)
    episode.after = await _after_for(conn, max(episode.messages, key=lambda m: (m.sent_at, m.id)), options)
    labels = extract.speaker_labels(episode)
    replace = set(replace_ids)
    known_rows = [r for r in await commitments.existing_in_chat(conn, episode.chat_id, options.known_limit * 4)
                  if r["status"] in ("proposed", "open", "team") and r["id"] not in replace][: options.known_limit]
    known = [{"who": _who(r["debtor_peer_id"], r["direction"], labels), "what": r["what"],
              "due_expression": r["due_expression"]} for r in known_rows]
    titles = await projects.active_titles(conn)
    key = f"cm-x{extract.PROMPT_VERSION}:{episode.chat_id}:{episode.first_id}-{episode.last_id}"
    if late:
        # Тот же эпизод уже разбирали без расшифровки или пересказа вложения: ключ — по новому тексту.
        digest = hashlib.md5("\n".join(m.text for m in episode.messages).encode()).hexdigest()[:16]
        key = f"{key}:late:{digest}"
    if replace:
        key = f"{key}:rc{run_id}"     # перепроверка — свой запрос, даже если эпизод уже разбирался
    job_id = await bridge.request_structured(
        conn, handler=HANDLER_EXTRACT, instructions=extract.EXTRACT_INSTRUCTIONS,
        input=extract.build_extract_input(episode, labels, ZoneInfo(tz), chat_kind=_chat_kind(chat_type), known=known,
                                          projects=titles),
        json_schema=extract.EXTRACT_SCHEMA, schema_name="commitments",
        context={"run_id": run_id, "chat_id": episode.chat_id, "tz": tz,
                 "message_ids": [m.id for m in episode.messages],
                 "context_ids": [m.id for m in episode.context],
                 "after_ids": [m.id for m in episode.after],
                 "known_ids": [r["id"] for r in known_rows],
                 "replace_ids": sorted(replace),
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


# Что проверяется на выполнение (Р-96): принятое владельцем — с предложением изменения ему; ещё не
# принятое — тихо (сделанное до решения владельца снимается с показа). «В команде» отдельно не
# проверяется: это только для сведения, а групповые чаты тратили бы запросы каждый час; его
# состояние модель говорит при разборе (блок «После»), через 14 дней оно уходит само.
RESOLVE_STATUSES = ("proposed", "open")


async def _resolve_rows(conn: asyncpg.Connection, chat_id: int) -> list[asyncpg.Record]:
    """Обязательства чата под проверку и с какого места их проверять: с последнего проверенного
    сообщения, а при первой проверке — с самого обещания (не только новое за прогон)."""
    return await conn.fetch(
        f"""SELECT c.id, c.what, c.due_expression, c.debtor_peer_id, c.direction, c.source_message_id, c.status,
                   m.sent_at AS source_sent_at,
                   COALESCE(c.checked_until_at, m.sent_at) AS from_at, COALESCE(c.checked_until_id, m.id) AS from_id
            FROM commitments c JOIN chats ch ON ch.id = c.chat_id JOIN messages m ON m.id = c.source_message_id
            WHERE c.status = ANY($2::text[]) AND c.chat_id = $1 AND {commitments.VISIBLE}
            ORDER BY c.id""", chat_id, list(RESOLVE_STATUSES))


async def _chats_to_resolve(conn: asyncpg.Connection, limit: int) -> list[asyncpg.Record]:
    """Чаты, где после последней проверки обязательства появились годные сообщения."""
    return await conn.fetch(
        f"""SELECT c.chat_id, min(ch.type) AS chat_type FROM commitments c
            JOIN chats ch ON ch.id = c.chat_id JOIN messages m ON m.id = c.source_message_id
            WHERE c.status = ANY($1::text[]) AND {commitments.VISIBLE}
              AND ch.type <> ALL($3::text[])
              AND EXISTS (SELECT 1 FROM messages x
                          WHERE x.chat_id = c.chat_id
                            AND (x.sent_at, x.id) > (COALESCE(c.checked_until_at, m.sent_at),
                                                     COALESCE(c.checked_until_id, m.id))
                            AND x.deleted_at IS NULL AND x.agent_visible AND x.kind = 'message'
                            AND btrim(x.text) <> '')
            GROUP BY c.chat_id ORDER BY c.chat_id LIMIT $2""",
        list(RESOLVE_STATUSES), limit, sorted(SKIP_CHAT_TYPES))


async def _enqueue_resolve(
    conn: asyncpg.Connection, run_id: int, chat_id: int, messages: list[Msg] | None, chat_type: str, tz: str,
    options: Options, attempt: int = 0,
) -> int | None:
    """Запрос «что стало с обязательствами» по чату. Сообщения — с места, до которого уже проверено
    (при первой проверке — с момента обещания), не больше `resolve_messages`; `messages` задаются
    только при повторе неудавшегося запроса."""
    rows = await _resolve_rows(conn, chat_id)
    if not rows:
        return None
    if messages is None:
        start = min((r["from_at"], r["from_id"]) for r in rows)
        found = await conn.fetch(
            f"""SELECT {_MSG_COLUMNS} {_FROM}
                WHERE m.chat_id = $3 AND (m.sent_at, m.id) > ($4, $5) AND ({_VERDICT}) = 'eligible'
                ORDER BY m.sent_at, m.id LIMIT $1""",
            options.resolve_messages, _EPOCH, chat_id, start[0], start[1])
        messages = await _mark_service_sent(conn, [_msg(r) for r in found])
    messages = sorted(messages, key=lambda m: (m.sent_at, m.id))[: options.resolve_messages]
    if not messages:
        return None
    # в проверку — то, после чьей отметки есть сообщения; само сообщение-обещание выполнением не считается
    items = [r for r in rows
             if any((m.sent_at, m.id) > (r["from_at"], r["from_id"]) and m.id != r["source_message_id"]
                    for m in messages)][: options.resolve_commitments]
    if not items:
        return None
    episode = Episode(chat_id, messages)
    labels = extract.speaker_labels(episode)
    key = f"cm-r{extract.PROMPT_VERSION}:{chat_id}:{messages[0].id}-{messages[-1].id}:{items[0]['id']}-{items[-1]['id']}"
    last = messages[-1]
    job_id = await bridge.request_structured(
        conn, handler=HANDLER_RESOLVE, instructions=extract.RESOLVE_INSTRUCTIONS,
        input=extract.build_resolve_input(
            episode, labels, ZoneInfo(tz), chat_kind=_chat_kind(chat_type),
            commitments=[{"who": _who(r["debtor_peer_id"], r["direction"], labels), "what": r["what"],
                          "due_expression": r["due_expression"], "promised_at": r["source_sent_at"]}
                         for r in items]),
        json_schema=extract.RESOLVE_SCHEMA, schema_name="commitment_updates", max_tokens=1200,
        context={"run_id": run_id, "chat_id": chat_id, "tz": tz,
                 "message_ids": [m.id for m in messages], "context_ids": [],
                 "commitment_ids": [r["id"] for r in items], "labels": _labels_to_context(labels),
                 "checked": {"at": last.sent_at.isoformat(), "id": last.id}},
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
        """SELECT r.job_id, r.kind, r.chat_id, r.message_ids, r.attempt, pr.trigger = 'recheck' AS recheck
           FROM processing_requests r JOIN processing_runs pr ON pr.id = r.run_id
           WHERE r.state = 'retry' ORDER BY r.job_id LIMIT $1 FOR UPDATE OF r""", limit)
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
                replace = await _replaceable(conn, [m.id for m in messages]) if row["recheck"] else []
                job_id = await _enqueue_extract(conn, run_id, Episode(row["chat_id"], messages), chat_type, tz,
                                                options, attempt=row["attempt"] + 1, replace_ids=replace)
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
    more — осталось ли что разбирать; retry_waiting, given_up — неудавшиеся запросы;
    tokens_in_estimate — грубая оценка размера запросов прогона в токенах.

    Дневной прогон (`trigger="hourly"`, Р-96) берёт только «закрытые» эпизоды — после последнего
    сообщения прошло не меньше EPISODE_GAP; открытые ждут следующего прогона (отметка встаёт перед
    ними). Поздно появившийся текст вложений и голосовых он не разбирает — это делает ночной. При
    упоре в предел ничего не пропускается: всё, что не поместилось, откладывается.
    """
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(tz)
    limit = max(1, min(int(limit or options.limit), 1000))
    hourly = trigger == "hourly"
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
        swept_memory = {"facts": await facts.purge_orphans(conn),
                        "project_chats": await projects.purge_orphans(conn)}
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
            trigger, json.dumps({"tz": tz, "planned_at": now.isoformat()}))

        # сначала — долги прошлых прогонов; они тоже входят в предел запросов
        replanned = await _replan_failed(conn, run_id, tz, options, limit)
        budget = limit - replanned["retried"]

        eligible: list[Msg] = []
        chat_types: dict[int, str] = {}
        new_watermark, more, cap_reached = watermark, False, False
        episodes: list[Episode] = []
        signal: list[Episode] = []
        fact_skipped = 0
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
            if not rescan:
                # уже поставленное дневным прогоном впереди отметки повторно не ставится
                taken = await _already_planned(conn, [m.id for m in eligible])
                eligible = [m for m in eligible if m.id not in taken]
            if hourly:
                # открытый эпизод (разговор ещё идёт) ждёт: отметка встаёт перед его первым сообщением
                cutoff = now - extract.EPISODE_GAP
                still = [e for e in extract.build_episodes(eligible) if e.messages[-1].sent_at > cutoff]
                if still:
                    new_watermark = min(new_watermark, min(e.first_id for e in still) - 1)
                    waiting = {m.id for e in still for m in e.messages}
                    eligible = [m for m in eligible if m.id not in waiting]
                    more = True

            # Предел запросов: окно сужается до сообщений, эпизоды которых помещаются в предел.
            # Отметка ставится на границу окна, поэтому отложенное разберёт следующий прогон.
            # Обещания важнее фактов: при упоре в предел сначала планируются эпизоды с признаком
            # обещания, эпизоды только с признаком факта — на оставшиеся места; не поместившиеся
            # из них пропускаются (счётчик fact_episodes_skipped), обещания — откладываются.
            while True:
                episodes = extract.build_episodes(eligible)
                promise = [e for e in episodes if extract.has_promise_signal(e)]
                chosen = {id(e) for e in promise}
                fact_only = [e for e in episodes if id(e) not in chosen and extract.has_fact_signal(e)]
                if hourly:
                    # днём не пропускаем ничего: не поместившееся ждёт следующего (или ночного) прогона
                    promise, fact_only = sorted(promise + fact_only, key=lambda e: e.last_id), []
                if len(promise) + len(fact_only) <= budget:
                    signal = sorted(promise + fact_only, key=lambda e: e.last_id)
                    break
                cap_reached = more = True
                if len(promise) > budget:
                    new_watermark = min(new_watermark, promise[budget - 1].last_id)
                    eligible = [m for m in eligible if m.id <= new_watermark]
                    continue
                room = budget - len(promise)
                fact_skipped = len(fact_only) - room
                signal = sorted(promise + fact_only[:room], key=lambda e: e.last_id)
                break

        # Сообщения до прежней отметки, текст которых появился позже (расшифровка голосового,
        # разбор вложения): прошлый прогон видел их пустыми. Эпизод — сами эти сообщения,
        # предыдущие подтягиваются контекстом, как у любого эпизода. Делят тот же предел запросов.
        late, late_signal, late_seen = [], [], []
        if budget - len(signal) > 0 and watermark > 0 and not hourly:
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
                if extract.has_memory_signal(episode):
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
        # Снимается, только если текст тот же, что прочитан: расшифровка, записанная за время
        # прогона, оставит пометку следующему.
        seen_ids = set(late_seen)
        cleared = [*eligible, *(m for m in late if m.id in seen_ids)]
        if cleared:
            await conn.execute(
                """UPDATE messages m SET late_content = false
                   FROM unnest($1::bigint[], $2::text[]) AS v (id, digest)
                   WHERE m.id = v.id AND m.late_content AND md5(m.text) = v.digest""",
                [m.id for m in cleared], [hashlib.md5(m.text.encode()).hexdigest() for m in cleared])
        # Пометка у того, что разбираться не будет никогда (исключённый чат, канал, бот, старое,
        # служебное, удалённое), тоже снимается — кроме скрытого защитой: его могут открыть.
        if watermark > 0:
            await conn.execute(
                f"""UPDATE messages SET late_content = false WHERE id IN (
                        SELECT m.id {_FROM}
                        WHERE m.late_content AND m.id <= $1 AND ({_VERDICT}) NOT IN ('eligible', 'skipped_hidden'))""",
                watermark, floor)
        counts["late"] = len(late_seen)

        planned = already = 0
        late_ids = {id(e) for e in late_signal}
        for episode in [*signal, *late_signal]:
            job_id = await _enqueue_extract(conn, run_id, episode, chat_types[episode.chat_id], tz, options,
                                            late=id(episode) in late_ids)
            if job_id is None:
                already += 1
            else:
                planned += 1

        # Проверка «не сделано ли уже» (Р-96): принятое, ещё не принятое и «В команде» — во всех чатах,
        # где после последней проверки есть сообщения; первая проверка — с момента обещания.
        resolve_planned = 0
        resolve_room = options.resolve_chats
        if hourly:      # днём проверка закрытия делит тот же предел запросов
            resolve_room = max(0, min(resolve_room, limit - planned - replanned["retried"]))
        for found in await _chats_to_resolve(conn, resolve_room) if resolve_room else []:
            job_id = await _enqueue_resolve(conn, run_id, found["chat_id"], None, found["chat_type"], tz, options)
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
            "episodes_without_signal": len(episodes) - len(signal) - fact_skipped,
            "fact_episodes_skipped": fact_skipped, "late_planned": len(late_signal),
            "messages": counts, "cap_reached": cap_reached, "more": more,
            "watermark": new_state["watermark"], "floor": new_state["floor"],
            "expired": expired, "purged": swept, "purged_memory": swept_memory, "people": synced,
            "trigger": trigger, "tokens_in_estimate": await _run_tokens(conn, run_id),
        }
        await conn.execute(
            "UPDATE processing_runs SET stats = stats || $2::jsonb WHERE id = $1",
            run_id, json.dumps({"plan": {k: v for k, v in result.items() if k != "run_id"}}, ensure_ascii=False))
        if not anything:
            await finish_run(conn, run_id)
    logger.info("прогон %s: запросов %s, повторов %s, проверок статуса %s, новых сообщений %s",
                run_id, planned, replanned["retried"], resolve_planned, counts["new"])
    return result


async def _already_planned(conn: asyncpg.Connection, ids: Sequence[int]) -> set[int]:
    """Сообщения, по которым запрос на извлечение уже ставился (дневной прогон ставит закрытые
    эпизоды впереди отметки — следующий прогон их не повторяет)."""
    if not ids:
        return set()
    rows = await conn.fetch(
        """SELECT DISTINCT x AS id FROM processing_requests r CROSS JOIN LATERAL unnest(r.message_ids) AS x
           WHERE r.kind = 'extract' AND r.message_ids && $1::bigint[] AND x = ANY($1::bigint[])""", list(ids))
    return {r["id"] for r in rows}


async def _run_tokens(conn: asyncpg.Connection, run_id: int) -> int:
    """Грубая оценка размера запросов прогона в токенах (по знакам инструкции и данных)."""
    chars = await conn.fetchval(
        """SELECT COALESCE(sum(length(j.payload->>'instructions') + length(j.payload->>'input')), 0)
           FROM processing_requests r JOIN jobs j ON j.id = r.job_id WHERE r.run_id = $1""", run_id)
    return round(int(chars or 0) / CHARS_PER_TOKEN)


# --- завершение прогона и сводка ------------------------------------------------------------------

async def send_digests(conn: asyncpg.Connection, *, label: int | str, tz: str) -> dict[str, int]:
    """Сводки владельцу: договорённости (с кнопками), проекты и факты профиля — каждые одним
    сообщением, только то, что ещё не показано. `label` — метка пачки (номер прогона или слота)."""
    zone = ZoneInfo(tz or "UTC")
    today = datetime.now(zone).date()
    digests = await commitments.build_digests(conn, run_id=label, today=today, tz=zone)
    for digest in digests:
        await bridge.notify_owner(conn, digest["text"], buttons=digest["buttons"],
                                  handler=HANDLER_DIGEST, context={"batch": digest["batch"]},
                                  dedup_key=f"cm-digest:{digest['batch']}")
    return {"digest_messages": len(digests),
            "projects_shown": await projects.send_digest(conn, run_id=label),
            "owner_facts_shown": await facts.send_owner_digest(conn, run_id=label),
            "project_facts_shown": await facts.send_project_digest(conn, run_id=label)}


async def finish_run(conn: asyncpg.Connection, run_id: int) -> int:
    """Закрывает прогон. Сводку владельцу сразу отправляет только прогон, запущенный вручную;
    ночной, дневной и перепроверка копят предложения до ближайшего часа сводки (`digest_tick`,
    Р-96): ночью — ничего, днём — не чаще заданного. Возвращает число отправленных сообщений-сводок
    договорённостей."""
    row = await conn.fetchrow("SELECT status, stats, trigger FROM processing_runs WHERE id = $1 FOR UPDATE", run_id)
    if row is None or row["status"] == "done":
        return 0
    stats = _loads(row["stats"]) or {}
    # окно «за 30 дней» для предложений проектов отсчитывается от времени планирования прогона
    planned_at = stats.get("planned_at")
    memory = await projects.propose(
        conn, run_id=run_id, now=datetime.fromisoformat(planned_at) if isinstance(planned_at, str) else None)
    sent = {"digest_messages": 0}
    if row["trigger"] == "manual":
        sent = await send_digests(conn, label=run_id, tz=stats.get("tz") or "UTC")
    merges = await conn.fetchval("SELECT count(*) FROM person_proposals WHERE status = 'pending'")
    await conn.execute(
        """UPDATE processing_runs SET status = 'done', finished_at = now(), stats = stats || $2::jsonb
           WHERE id = $1""",
        run_id, json.dumps({"merge_proposals_pending": merges, **memory, **sent}))
    for fn in _after_run:
        await fn(conn, run_id)
    return sent["digest_messages"]


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
    after_ids = [i for i in ctx.get("after_ids") or [] if isinstance(i, int)]
    if not isinstance(chat_id, int) or not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        return None
    chat = await conn.fetchrow(
        """SELECT c.id, c.type, c.peer_id, c.excluded, pr.class AS peer_class,
                  (SELECT p.id FROM peers p WHERE p.class = 'user' AND p.tg_id = a.tg_user_id) AS owner_peer_id,
                  (SELECT p.name FROM peers p WHERE p.class = 'user' AND p.tg_id = a.tg_user_id) AS owner_name,
                  (SELECT p.username FROM peers p WHERE p.class = 'user' AND p.tg_id = a.tg_user_id) AS owner_username
           FROM chats c JOIN accounts a ON a.id = c.account_id JOIN peers pr ON pr.id = c.peer_id
           WHERE c.id = $1""", chat_id)
    if chat is None or chat["excluded"]:
        return None
    rows = await conn.fetch(
        # Скрытое защитой от внедрённых инструкций — как удалённое: в запрос к модели не идёт.
        f"""SELECT {_MSG_COLUMNS}, (m.deleted_at IS NOT NULL OR NOT m.agent_visible) AS deleted, m.kind
            FROM messages m
            WHERE m.chat_id = $1 AND m.id = ANY($2::bigint[])""",
        chat_id, [i for i in [*ids, *context_ids, *after_ids] if isinstance(i, int)])
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
    after = await _mark_service_sent(conn, [restore(i) for i in after_ids])
    return Episode(chat_id, messages, context, after), chat


def _mentions_owner(text: str, chat: asyncpg.Record) -> bool:
    """В тексте обещания назван владелец: по имени (любая падежная форма — по началу слова) или @адресом."""
    username = (chat["owner_username"] or "").lstrip("@").lower()
    if username and re.search(rf"@{re.escape(username)}\b", text, flags=re.IGNORECASE):
        return True
    first = ((chat["owner_name"] or "").split() or [""])[0].lower().replace("ё", "е")
    if len(first) < 4 or not first.isalpha():
        return False
    stem = first[: max(4, len(first) - 2)]
    return any(word.startswith(stem) for word in extract._words(text))


def _parties(candidate: extract.Candidate, chat: asyncpg.Record) -> tuple[int | None, int | None, str]:
    """Кто должен, кому и как это выглядит относительно владельца. Должник — автор сообщения.

    «Между другими людьми» (others) — только если владелец в обещании не участвует: не получатель
    по словам модели, не автор просьбы, на которую согласились, обещание — не ответ (reply) на его
    сообщение и не называет его по имени или @адресу (Р-96)."""
    message = candidate.message
    personal = chat["type"] == "personal_chat" and chat["peer_class"] == "user"
    recipient = candidate.recipient_key
    recipient_peer = recipient[1] if recipient and recipient[0] == "peer" else None
    if message.is_outgoing:
        debtor = message.sender_peer_id or chat["owner_peer_id"]
        creditor = recipient_peer or (chat["peer_id"] if personal else None)
        return debtor, creditor, "owner_owes"
    to_owner = (personal or recipient == ("owner",) or message.reply_to_owner
                or (candidate.request is not None and candidate.request.is_outgoing)
                or _mentions_owner(f"{candidate.quote} {candidate.what}", chat))
    if to_owner:
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
    # перепроверка (Р-96): прежние предложения этого эпизода заменяются найденным по новым правилам
    replaced = set()
    for old_id in [i for i in ctx.get("replace_ids") or [] if isinstance(i, int)]:
        if await commitments.quiet_close(conn, old_id, kind="recheck", evidence_message_id=None,
                                         reason="recheck", actor="auto"):
            replaced.add(old_id)
    _bump(stats, "replaced_by_recheck", len(replaced))
    existing: list[Any] = [r for r in await commitments.existing_in_chat(conn, episode.chat_id)
                           if r["id"] not in replaced]
    known_ids = [i for i in ctx.get("known_ids") or [] if isinstance(i, int)]
    tz = ctx.get("tz") or "UTC"
    parties = {c.position: _parties(c, chat) for c in candidates}
    # пары об одном деле: остаётся одно — где владелец сторона, иначе раньше сказанное. Склеиваются только
    # обязательства одного должника (совместимый кредитор) и взаимный созвон двух сторон друг с другом (Д-59):
    # у двух людей с одинаковым «пришлю отчёт» — два обязательства.
    candidates, pairs = extract.merge_duplicates(
        candidates, lambda c: (parties[c.position][2] == "others", c.message.sent_at, c.message.id),
        parties=lambda c: parties[c.position][:2])
    _bump(stats, "duplicates_in_answer", len(pairs))
    # до какого сообщения модель уже видела чат: с него продолжит проверка закрытия (исчезнувшие —
    # пустые заглушки `_load_episode` — не в счёт)
    seen = [m for m in [*episode.messages, *episode.after] if m.text]
    checked = max(seen, key=lambda m: (m.sent_at, m.id)) if seen else None
    for candidate in candidates:
        debtor, creditor, direction = parties[candidate.position]
        due = commitments.candidate_due(candidate, tz)
        hinted = None
        if candidate.duplicate_of is not None and 1 <= candidate.duplicate_of <= len(known_ids):
            hinted = known_ids[candidate.duplicate_of - 1]
        if candidate.project is not None:
            await _mention(conn, candidate.project, episode, ctx, stats)
        if commitments.find_duplicate(
                existing, source_message_id=candidate.message.id, debtor_peer_id=debtor,
                what=candidate.what, quote=candidate.quote, due_date=due.due_date, hinted_id=hinted) is not None:
            stats["duplicates"] = stats.get("duplicates", 0) + 1
            continue
        commitment_id, outcome = await commitments.propose(
            conn, chat_id=episode.chat_id, candidate=candidate, debtor_peer_id=debtor,
            creditor_peer_id=creditor, direction=direction, tz=tz, run_id=ctx.get("run_id"),
            model=result.get("model") if isinstance(result.get("model"), str) else None,
            project_id=await projects.for_commitment(conn, episode.chat_id, candidate.project),
            checked=checked, prompt_version=extract.PROMPT_VERSION)
        existing.append({"id": commitment_id, "source_message_id": candidate.message.id,
                         "debtor_peer_id": debtor, "what": candidate.what, "source_quote": candidate.quote,
                         "due_date": due.due_date,
                         "status": "proposed" if outcome == "held" else outcome if outcome in ("proposed", "team")
                         else "expired"})
        for peer_id in (debtor, creditor):
            if peer_id is not None:
                await people.ensure_person_for_peer(conn, peer_id)
        # счётчики итогов прогона: что предложено, что отложено на сутки, что «В команде», что снято тихо
        if outcome in ("proposed", "held"):
            _bump(stats, "proposed")
        if outcome != "proposed":
            _bump(stats, {"held": "held_immediate", "team": "team"}.get(outcome, f"quiet_{outcome}"))
    await _apply_memory(conn, ctx, result, episode, stats)
    return stats


def _bump(stats: dict[str, int], key: str, by: int = 1) -> None:
    if by:
        stats[key] = stats.get(key, 0) + by


async def _mention(conn: asyncpg.Connection, label: str, episode: Episode, ctx: dict[str, Any],
                   stats: dict[str, int]) -> int | None:
    """Метка проекта от модели: действующий проект — его номер; незнакомое название, которое
    действительно есть в тексте эпизода, — упоминание для будущего предложения владельцу."""
    found = await projects.resolve(conn, label)
    if found is None and extract.find_mention(label, episode) is not None:
        _bump(stats, "project_mentions", await projects.record_mentions(
            conn, label, episode, run_id=ctx.get("run_id")))
    return found


async def _subject(conn: asyncpg.Connection, fact: extract.FactCandidate, episode: Episode,
                   ctx: dict[str, Any], stats: dict[str, int]) -> tuple[str, int | None, int | None] | None:
    """О ком факт — решает код: владелец; подтверждённый владельцем человек (о неподтверждённом
    факты не пишутся); действующий проект."""
    if fact.about == "owner":
        return "owner", None, None
    if fact.about == "project":
        project_id = await _mention(conn, fact.project, episode, ctx, stats) if fact.project else None
        return ("project", None, project_id) if project_id is not None else None
    peer_id = fact.speaker_key[1] if fact.speaker_key and fact.speaker_key[0] == "peer" else None
    person_id = await people.person_for_peer(conn, peer_id) if peer_id is not None else None
    if person_id is None or not await conn.fetchval(
            "SELECT confirmed AND NOT is_owner AND merged_into IS NULL FROM people WHERE id = $1", person_id):
        return None
    if fact.project:
        await _mention(conn, fact.project, episode, ctx, stats)
    return "person", person_id, None


async def _apply_memory(conn: asyncpg.Connection, ctx: dict[str, Any], result: dict[str, Any],
                        episode: Episode, stats: dict[str, int]) -> None:
    """Факты, решения и упоминания проектов из того же ответа (PROMPT_VERSION 3)."""
    parsed = result.get("parsed")
    found, dropped = extract.validate_facts(parsed, episode, _labels_from_context(ctx.get("labels")))
    for key, value in dropped.items():
        _bump(stats, f"facts_dropped_{key}", value)
    model = result.get("model") if isinstance(result.get("model"), str) else None
    for fact in found:
        subject = await _subject(conn, fact, episode, ctx, stats)
        if subject is None:
            _bump(stats, "facts_without_subject")
            continue
        subject_type, person_id, project_id = subject
        _, outcome = await facts.record(
            conn, fact, subject_type=subject_type, person_id=person_id, project_id=project_id,
            tz=ctx.get("tz") or "UTC", run_id=ctx.get("run_id"), model=model)
        _bump(stats, {"active": "facts_recorded", "proposed": "owner_facts_proposed",
                      "duplicate": "facts_duplicates"}.get(outcome, f"facts_skipped_{outcome}"))
    mentions, dropped = extract.validate_projects(parsed, episode)
    for key, value in dropped.items():
        _bump(stats, f"projects_dropped_{key}", value)
    for mention in mentions:
        await _mention(conn, mention.title, episode, ctx, stats)


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
        if row is None or row["status"] not in RESOLVE_STATUSES or update.message.id == row["source_message_id"] \
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
        if row["status"] in commitments.QUIET_CLOSABLE:
            # Ещё не принятое владельцем (Р-96): сделанное или отменённое тихо снимается с показа,
            # перенесённый срок записывается сразу. Принятое (open) сюда не попадает никогда.
            if update.kind == "rescheduled":
                done = await commitments.quiet_reschedule(
                    conn, row["id"], expression=update.new_due_expression, due=due,
                    evidence_message_id=update.message.id)
                _bump(stats, "quiet_rescheduled" if done else "dropped_stale")
            else:
                reason = await commitments.quiet_close(conn, row["id"], kind=update.kind,
                                                       evidence_message_id=update.message.id)
                _bump(stats, f"quiet_{reason}" if reason else "dropped_stale")
            continue
        change_id = await commitments.propose_change(
            conn, commitment_id=row["id"], kind=update.kind, evidence_message_id=update.message.id,
            quote=update.quote, new_due_expression=update.new_due_expression, due=due, run_id=ctx.get("run_id"))
        key = "changes_proposed" if change_id is not None else "duplicates"
        stats[key] = stats.get(key, 0) + 1
    # до какого сообщения проверено: следующая проверка этих обязательств начнётся после него
    checked = ctx.get("checked") if isinstance(ctx.get("checked"), dict) else None
    if checked and isinstance(checked.get("id"), int) and isinstance(checked.get("at"), str):
        await conn.execute(
            """UPDATE commitments SET checked_until_at = $2, checked_until_id = $3
               WHERE id = ANY($1::bigint[])
                 AND (checked_until_at IS NULL OR (checked_until_at, checked_until_id) < ($2, $3))""",
            ids, datetime.fromisoformat(checked["at"]), checked["id"])
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


# --- перепроверка по новым правилам (Р-96) --------------------------------------------------------------

RECHECK_KEY = "processing.recheck"   # {"prompt_version": "4", "run_id": N, "at": ISO, "extract": n, "resolve": n}
RECHECK_LIMIT = 300                  # запросов к модели за одну перепроверку по умолчанию
CHARS_PER_TOKEN = 3.0                # грубая оценка для русского текста: столько знаков на токен
ANSWER_TOKENS = {"extract": 350, "resolve": 120}   # обычный размер ответа модели, токенов (оценка)


class _DryRun(Exception):
    """Перепроверка «посчитать и не ставить»: транзакция откатывается."""


async def _replaceable(conn: asyncpg.Connection, message_ids: Sequence[int]) -> list[int]:
    """Предложения, извлечённые прежней инструкцией из этих сообщений и ещё не решённые владельцем."""
    rows = await conn.fetch(
        """SELECT id FROM commitments WHERE status = ANY($3::text[]) AND source_message_id = ANY($1::bigint[])
                                         AND prompt_version IS DISTINCT FROM $2""",
        list(message_ids), extract.PROMPT_VERSION, list(commitments.QUIET_CLOSABLE))
    return [r["id"] for r in rows]


async def _recheck_episodes(
    conn: asyncpg.Connection, limit: int,
) -> tuple[list[tuple[Episode, str, list[int]]], int, int]:
    """Эпизоды-источники ещё не решённых предложений прежней инструкции: эпизод строится заново тем же
    правилом (пауза EPISODE_GAP) из годных сообщений чата вокруг обещания. Возвращает эпизоды (с
    видом чата и номерами заменяемых предложений), сколько предложений не найдено (эпизод не собрался)
    и сколько не вошло в предел запросов (дождутся следующего запуска с --force)."""
    rows = await conn.fetch(
        f"""SELECT c.id, c.chat_id, c.source_message_id, m.sent_at, ch.type AS chat_type
            FROM commitments c JOIN chats ch ON ch.id = c.chat_id JOIN messages m ON m.id = c.source_message_id
            WHERE c.status = ANY($2::text[]) AND c.prompt_version IS DISTINCT FROM $1 AND {commitments.VISIBLE}
              AND ch.type <> ALL($3::text[])
            ORDER BY m.sent_at, c.id""",
        extract.PROMPT_VERSION, list(commitments.QUIET_CLOSABLE), sorted(SKIP_CHAT_TYPES))
    found: dict[tuple[int, int, int], tuple[Episode, str, list[int]]] = {}
    by_message: dict[int, tuple[int, int, int]] = {}
    missed = over = 0
    for row in rows:
        key = by_message.get(row["source_message_id"])
        if key is None:
            if len(found) >= limit:
                over += 1
                continue
            around = await conn.fetch(
                f"""SELECT {_MSG_COLUMNS} {_FROM}
                    WHERE m.chat_id = $3 AND m.sent_at BETWEEN $4::timestamptz - interval '12 hours'
                                                      AND $4::timestamptz + interval '12 hours'
                      AND ({_VERDICT}) = 'eligible'
                    ORDER BY m.sent_at, m.id LIMIT $1""",
                400, _EPOCH, row["chat_id"], row["sent_at"])
            messages = await _mark_service_sent(conn, [_msg(r) for r in around])
            episode = next((e for e in extract.build_episodes(messages)
                            if any(m.id == row["source_message_id"] for m in e.messages)), None)
            if episode is None:
                missed += 1
                continue
            key = (episode.chat_id, episode.first_id, episode.last_id)
            if key not in found:
                found[key] = (episode, row["chat_type"], [])
            for message in episode.messages:
                by_message.setdefault(message.id, key)
        found[key][2].append(row["id"])
    return list(found.values()), missed, over


async def recheck(
    conn: asyncpg.Connection, *, tz: str, limit: int = RECHECK_LIMIT, dry_run: bool = False, force: bool = False,
    now: datetime | None = None, options: Options = Options(),
) -> dict[str, Any]:
    """Перепроверка уже найденных договорённостей по новым правилам (`shturman commitments recheck`, Р-96).

      * ещё не решённые владельцем предложения и «В команде», извлечённые прежней инструкцией, —
        их эпизоды разбираются заново (с блоком «После»); прежние предложения заменяются (тихо,
        `expired` с причиной recheck), найденное заново проходит те же правила, что ночью:
        сделанное до показа — не предлагается, быстрое — ждёт суток, «между другими» — в «В команде»;
      * принятые (open) — проверка закрытия с момента обещания; найденное — предложение владельцу
        «похоже, уже сделано» с цитатой (кнопкой в боте, как всегда).

    Не больше `limit` запросов к модели; повторно для той же версии инструкции не запускается (`force`
    — запустить ещё раз). `dry_run` — только посчитать запросы и оценить размер, ничего не ставя.
    Итог — счётчики, без текста переписки."""
    now = now or datetime.now(timezone.utc)
    limit = max(1, min(int(limit), 2000))
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.processing'))")
            await _recover_lost(conn)
            done = _loads(await conn.fetchval("SELECT value FROM settings WHERE key = $1", RECHECK_KEY)) or {}
            if done.get("prompt_version") == extract.PROMPT_VERSION and not force:
                return {"status": "already_done", "prompt_version": extract.PROMPT_VERSION,
                        "run_id": done.get("run_id"), "at": done.get("at")}
            running = await conn.fetchval("SELECT id FROM processing_runs WHERE status = 'running'")
            if running is not None:
                if await _pending(conn, running):
                    return {"status": "already_running", "run_id": running}
                await finish_run(conn, running)
            run_id = await conn.fetchval(
                "INSERT INTO processing_runs (trigger, stats) VALUES ('recheck', $1::jsonb) RETURNING id",
                json.dumps({"tz": tz, "planned_at": now.isoformat()}))
            episodes, missed, over = await _recheck_episodes(conn, limit)
            extract_planned = extract_skipped = 0
            for episode, chat_type, replace in episodes:
                job_id = await _enqueue_extract(conn, run_id, episode, chat_type, tz, options, replace_ids=replace)
                if job_id is None:
                    extract_skipped += 1
                else:
                    extract_planned += 1
            resolve_planned = 0
            room = max(0, limit - extract_planned)
            for found in await _chats_to_resolve(conn, min(room, options.resolve_chats * 5)):
                job_id = await _enqueue_resolve(conn, run_id, found["chat_id"], None, found["chat_type"], tz, options)
                resolve_planned += int(job_id is not None)
            sizes = await conn.fetch(
                """SELECT r.kind, count(*) AS n,
                          sum(length(j.payload->>'instructions') + length(j.payload->>'input')) AS chars
                   FROM processing_requests r JOIN jobs j ON j.id = r.job_id WHERE r.run_id = $1 GROUP BY r.kind""",
                run_id)
            chars = {r["kind"]: int(r["chars"] or 0) for r in sizes}
            tokens_in = round(sum(chars.values()) / CHARS_PER_TOKEN)
            tokens_out = extract_planned * ANSWER_TOKENS["extract"] + resolve_planned * ANSWER_TOKENS["resolve"]
            proposals = sum(len(ids) for _, _, ids in episodes)
            result = {
                "status": "planned" if extract_planned or resolve_planned else "nothing_to_do",
                "run_id": run_id, "dry_run": dry_run, "prompt_version": extract.PROMPT_VERSION,
                "proposals_to_recheck": proposals, "proposals_not_found": missed, "proposals_over_limit": over,
                "extract_planned": extract_planned, "extract_already_planned": extract_skipped,
                "resolve_planned": resolve_planned, "requests": extract_planned + resolve_planned,
                "tokens_in_estimate": tokens_in, "tokens_out_estimate": tokens_out, "limit": limit,
            }
            if dry_run:
                raise _DryRun(json.dumps(result))
            await conn.execute("UPDATE processing_runs SET stats = stats || $2::jsonb WHERE id = $1",
                               run_id, json.dumps({"plan": {k: v for k, v in result.items() if k != "run_id"}}))
            await _save_setting(conn, RECHECK_KEY, {
                "prompt_version": extract.PROMPT_VERSION, "run_id": run_id, "at": now.isoformat(),
                "extract": extract_planned, "resolve": resolve_planned})
            if not result["requests"]:
                await finish_run(conn, run_id)
    except _DryRun as planned:
        result = json.loads(str(planned))
        result.pop("run_id", None)
    logger.info("перепроверка договорённостей: запросов %s (извлечение %s, проверка закрытия %s)%s",
                result.get("requests"), result.get("extract_planned"), result.get("resolve_planned"),
                " — только оценка" if dry_run else "")
    return result


async def recheck_state(conn: asyncpg.Connection) -> dict[str, Any]:
    """Выполнялась ли перепроверка для текущей версии инструкции и сколько ещё прежних — для doctor."""
    done = _loads(await conn.fetchval("SELECT value FROM settings WHERE key = $1", RECHECK_KEY)) or {}
    old = await conn.fetchval(
        """SELECT count(*) FROM commitments WHERE status IN ('proposed', 'open', 'team')
                                            AND prompt_version IS DISTINCT FROM $1""", extract.PROMPT_VERSION)
    return {"recheck_done": done.get("prompt_version") == extract.PROMPT_VERSION,
            "recheck_at": done.get("at"), "old_prompt_items": int(old or 0)}


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


# --- дневные прогоны и часы сводки (Р-96) ---------------------------------------------------------------

HOURLY_KEY = "processing.hourly"     # {"at": ISO-время последнего дневного прогона, "capped": bool}
DIGEST_KEY = "processing.digest"     # {"day": "ГГГГ-ММ-ДД", "slots": ["13:00", ...]} — какие сводки уже ушли
DIGEST_CATCH_UP = timedelta(hours=3)   # сводка, пропущенная из-за простоя, уходит не позже этого


def parse_slots(raw: str | None) -> tuple[time, ...]:
    """«13:00,19:00» -> часы сводки. Неразборчивое пропускается; пустое — сводки только после ручного прогона."""
    out = []
    for part in (raw or "").replace(";", ",").split(","):
        try:
            out.append(time.fromisoformat(part.strip()))
        except ValueError:
            continue
    return tuple(sorted(set(out)))


@dataclass(frozen=True)
class Schedule:
    """Расписание дневной обработки: как часто (минут, 0 — только ночью), пределы запросов к модели
    у дневных прогонов в час и в сутки, часы сводки с кнопками."""

    every: int = 60
    hourly_cap: int = 40
    daily_cap: int = 300
    digest_at: tuple[time, ...] = (time(13, 0), time(19, 0))

    @classmethod
    def from_config(cls, config: Any) -> "Schedule":
        return cls(every=int(getattr(config, "processing_every", 60)),
                   hourly_cap=int(getattr(config, "processing_hourly_cap", 40)),
                   daily_cap=int(getattr(config, "processing_daily_cap", 300)),
                   digest_at=parse_slots(getattr(config, "digest_at", "13:00,19:00")))


async def _quiet(conn: asyncpg.Connection, local: datetime) -> bool:
    """Тихие часы владельца — те же, что у уведомлений (ночью сервис ему ничего не шлёт)."""
    from ..notify import core as notify_core
    from ..notify import settings as notify_settings
    return notify_core.in_quiet(await notify_settings.load(conn), local)


async def daytime_used(conn: asyncpg.Connection, *, now: datetime, tz: str) -> dict[str, int]:
    """Сколько запросов к модели поставили дневные прогоны за последний час и с начала суток владельца."""
    local = now.astimezone(ZoneInfo(tz))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    row = await conn.fetchrow(
        """SELECT count(*) FILTER (WHERE r.created_at >= $1) AS hour, count(*) AS day
           FROM processing_requests r JOIN processing_runs pr ON pr.id = r.run_id
           WHERE pr.trigger = 'hourly' AND r.created_at >= $2""",
        now - timedelta(hours=1), midnight)
    return {"hour": int(row["hour"]), "day": int(row["day"])}


async def hourly_tick(
    conn: asyncpg.Connection, *, tz: str, schedule: Schedule, now: datetime | None = None,
    options: Options = Options(),
) -> dict[str, Any] | None:
    """Дневной прогон: раз в `schedule.every` минут вне тихих часов — только новые закрытые эпизоды,
    проверка закрытия и отложенная проверка быстрых обещаний. Не больше пределов запросов в час и
    в сутки; что не поместилось — ждёт следующего дневного или ночного прогона (отметка не уходит)."""
    if schedule.every <= 0:
        return None
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(tz))
    if await _quiet(conn, local):
        return None
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.processing.hourly'))")
        record = _loads(await conn.fetchval("SELECT value FROM settings WHERE key = $1", HOURLY_KEY)) or {}
        last = datetime.fromisoformat(record["at"]) if isinstance(record.get("at"), str) else None
        if last is not None and now - last < timedelta(minutes=schedule.every):
            return None
        if await conn.fetchval("SELECT 1 FROM processing_runs WHERE status = 'running'"):
            return None     # идёт ночной или прошлый дневной прогон — ждём его
        used = await daytime_used(conn, now=now, tz=tz)
        budget = min(schedule.hourly_cap - used["hour"], schedule.daily_cap - used["day"])
        if budget <= 0:
            await _save_setting(conn, HOURLY_KEY, {"at": now.isoformat(), "capped": True})
            logger.info("дневной прогон отложен: предел запросов (за час %s, за сутки %s)", used["hour"], used["day"])
            return {"status": "capped", "used": used, "planned": 0, "resolve_planned": 0}
        await _save_setting(conn, HOURLY_KEY, {"at": now.isoformat(), "capped": False})
        return await plan_run(conn, tz=tz, trigger="hourly", limit=budget, now=now, options=options)


async def digest_tick(
    conn: asyncpg.Connection, *, tz: str, slots: Sequence[time], now: datetime | None = None,
) -> dict[str, int] | None:
    """Сводка владельцу с кнопками — в часы сводки (по умолчанию 13:00 и 19:00), не чаще; пропущенная
    из-за простоя — в пределах DIGEST_CATCH_UP; в тихие часы — никогда. Новые предложения до того
    видны в приложении («Согласования», «Договорённости»)."""
    if not slots:
        return None
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(tz))
    if await _quiet(conn, local):
        return None
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.processing.digest'))")
        record = _loads(await conn.fetchval("SELECT value FROM settings WHERE key = $1", DIGEST_KEY)) or {}
        day = local.date().isoformat()
        done = set(record.get("slots") or []) if record.get("day") == day else set()
        naive = local.replace(tzinfo=None)
        due = [slot for slot in slots if f"{slot:%H:%M}" not in done
               and timedelta(0) <= naive - datetime.combine(local.date(), slot) < DIGEST_CATCH_UP]
        if not due:
            return None
        # прошедшие сегодня часы сводки отмечаются все: пропущенные две сводки — одна
        done.update(f"{slot:%H:%M}" for slot in slots if datetime.combine(local.date(), slot) <= naive)
        await _save_setting(conn, DIGEST_KEY, {"day": day, "slots": sorted(done)})
        return await send_digests(conn, label=f"s{local:%m%d%H%M}", tz=tz)


async def overview(conn: asyncpg.Connection) -> dict[str, Any]:
    """Поля для `GET /api/status` и строки `processing` в ./ops/doctor.sh: только числа."""
    runs = await conn.fetchrow(
        """SELECT count(*) AS runs,
                  COALESCE(sum(COALESCE((stats->'plan'->>'planned')::int, 0)
                               + COALESCE((stats->'plan'->>'resolve_planned')::int, 0)
                               + COALESCE((stats->'plan'->>'retried')::int, 0)), 0) AS requests,
                  COALESCE(sum(COALESCE((stats->'plan'->>'tokens_in_estimate')::int, 0)), 0) AS tokens
           FROM processing_runs
           WHERE started_at > now() - interval '24 hours' AND stats ? 'plan'""")
    hourly = _loads(await conn.fetchval("SELECT value FROM settings WHERE key = $1", HOURLY_KEY)) or {}
    out = {"processing_runs_24h": int(runs["runs"]), "processing_requests_24h": int(runs["requests"]),
           "processing_tokens_24h": int(runs["tokens"]), "processing_capped": bool(hourly.get("capped"))}
    out.update(await recheck_state(conn))
    return out
