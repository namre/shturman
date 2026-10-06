"""Общие заготовки тестов обработки: аккаунт, чаты, сообщения, подставной ответ модели."""

from datetime import datetime, timedelta, timezone

from shturman import bridge, jobs, store
from shturman.records import ChatRecord, MessageRecord

OWNER = 1000
TZ = "Europe/Moscow"
# вторник 6 октября 2026, 14:00 по Москве
T0 = datetime(2026, 10, 6, 11, 0, tzinfo=timezone.utc)


def rec(mid, text, *, sender, name, at=T0, kind="message", forwarded=None, sender_class="user"):
    return MessageRecord(
        tg_message_id=mid, sent_at=at, kind=kind, sender_class=sender_class, sender_tg_id=sender,
        sender_name=name, text=text, entities=None, reply_to_tg_id=None, forwarded_from=forwarded,
        edited_at=None, media_type=None, media_path=None,
        service_action="phone_call" if kind == "service" else None,
    )


async def account(conn):
    account_id = await store.ensure_account(conn, OWNER, "Владелец")
    await store.ensure_peer(conn, "user", OWNER, name="Евгений Тестов")
    return account_id


async def chat(conn, account_id, tg_id, name, *, type_="personal_chat", cls="user", username=None, exclude=False):
    chat_id, _ = await store.ensure_chat(
        conn, account_id, ChatRecord(cls, tg_id, type_, name, username=username), exclude=exclude)
    return chat_id


async def say(conn, chat_id, lines, *, start=T0, step=60, first_id=1):
    """lines: [(tg_id отправителя, имя, текст)] или со словарём доп. полей четвёртым элементом.
    Возвращает идентификаторы строк архива по порядку."""
    rows = []
    for n, line in enumerate(lines):
        sender, name, text = line[:3]
        extra = line[3] if len(line) > 3 else {}
        at = extra.pop("at", None) or start + timedelta(seconds=step * n)
        rows.append((chat_id, rec(first_id + n, text, sender=sender, name=name, at=at, **extra)))
    await store.upsert_messages(conn, rows, source="session", owner_tg_id=OWNER)
    found = await conn.fetch(
        "SELECT id, tg_message_id FROM messages WHERE chat_id = $1 AND tg_message_id = ANY($2::bigint[])",
        chat_id, [r[1].tg_message_id for r in rows])
    by_tg = {r["tg_message_id"]: r["id"] for r in found}
    return [by_tg[r[1].tg_message_id] for r in rows]


async def peer_id(conn, tg_id, cls="user"):
    return await conn.fetchval("SELECT id FROM peers WHERE class = $1 AND tg_id = $2", cls, tg_id)


async def claim(conn, kind=bridge.LLM_STRUCTURED, limit=20):
    """Забирает задания так, как это делает плагин: без контекста сервиса."""
    return await jobs.claim(conn, [kind], worker="test", limit=limit)


async def answer(conn, job, parsed, *, model="test-model"):
    """Подставной ответ модели на задание."""
    return await bridge.deliver_result(conn, job["id"], {"parsed": parsed, "text": "", "model": model})


async def press(conn, data):
    return await bridge.dispatch_callback(conn, data, OWNER)


def buttons_of(job):
    """Все кнопки уведомления одной строкой: [(текст, данные)]."""
    return [(b["text"], b["data"]) for row in (job["payload"].get("buttons") or []) for b in row]
