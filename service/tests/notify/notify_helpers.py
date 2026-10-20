"""Общее для тестов уведомлений владельцу (notify/, Р-84): архив с чатами, живые сообщения, очередь.

Фоновая отправка в этих тестах не запущена: тест сам зовёт `Notifier.on_message` (как подписчик
события) и `Notifier.flush(now=…)` (как фоновый обход) — без гонок и без ожидания по часам.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest_asyncio

from shturman import authority, bridge, store
from shturman.notify import core
from shturman.notify import settings as ns
from shturman.records import ChatRecord, MessageRecord

OWNER = 1000      # владелец: основной аккаунт и привязка к боту согласований
HELPER = 1500     # аккаунт ассистента
ANNA = 2001
PETR = 2002
OTHER = 2003


@pytest_asyncio.fixture
async def nv(make_client, conn, own_bot):
    client, state = await make_client("shturman.api_core")
    with authority.owner_context(OWNER, chat_id=OWNER, action="test.fixture.bind"):
        await bridge.set_owner(conn, OWNER, OWNER)
    owner_acc = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    helper_acc = await store.ensure_account(conn, HELPER, "Помощник", "assistant")
    # @имя владельца, как его сообщил Telegram (живой источник пишет его в peers)
    await store.ensure_peer(conn, "user", OWNER, name="Евгений", username="evgenii_e")
    notifier = core.Notifier(state.pool, tz=lambda: "Europe/Moscow")
    # Без тихих часов по умолчанию: иначе исход зависел бы от того, в котором часу идут тесты.
    # Тихие часы проверяются отдельно — с окном вокруг текущего времени.
    await configure(conn, quiet=False)
    return SimpleNamespace(client=client, state=state, conn=conn, n=notifier, owner_acc=owner_acc,
                           helper_acc=helper_acc)


async def configure(conn, **changes) -> dict:
    s = {**await ns.load(conn), **ns.validate(changes)}
    await ns.save(conn, s)
    return s


async def chat(conn, account_id, tg_id=ANNA, *, cls="user", type_="personal_chat", name="Анна Ким",
               username=None, is_bot=None) -> int:
    chat_id, _ = await store.ensure_chat(conn, account_id, ChatRecord(cls, tg_id, type_, name, username=username,
                                                                       is_bot=is_bot))
    return chat_id


def now() -> datetime:
    return datetime.now(timezone.utc)


async def message(conn, chat_id, tg_message_id, text, *, sender=ANNA, sender_name="Анна Ким", outgoing=False,
                  age=0.0, entities=None, reply_to=None, topic=None, forwarded=False, via_bot=False,
                  sender_bot=False, media_type=None, duration=None, source="session", edited=False) -> int:
    at = now() - timedelta(seconds=age)
    record = MessageRecord(
        tg_message_id=tg_message_id, sent_at=at, kind="message", sender_class="user", sender_tg_id=sender,
        sender_name=sender_name, text=text, entities=None, reply_to_tg_id=reply_to,
        forwarded_from="Кто-то" if forwarded else None, edited_at=at if edited else None,
        media_type=media_type, media_path=None, service_action=None,
        telegram_entities=entities if source == "session" else None, topic_tg_id=topic,
        is_forwarded=forwarded if source == "session" else None,
        telegram_via_bot=via_bot if source == "session" else None,
        telegram_sender_bot=sender_bot if source == "session" else None, media_duration=duration)
    result = await store.upsert_messages(conn, [(chat_id, record, outgoing)], source=source, owner_tg_id=OWNER)
    return (result.new_ids or result.known_ids)[0]


def payload(account_id, chat_id, message_id, **flags) -> dict:
    out = {"account_id": account_id, "chat_id": chat_id, "message_id": message_id, "source": "session",
           "outgoing": False, "edited": False, "via_bot": False}
    out.update(flags)
    return out


async def live(env, account_id, chat_id, message_id, **flags):
    return await env.n.on_message(payload(account_id, chat_id, message_id, **flags))


async def sent(conn) -> list[str]:
    """Тексты уведомлений, вставших в очередь сообщений владельцу (по порядку)."""
    rows = await conn.fetch(
        """SELECT payload FROM jobs WHERE kind = $1 AND dedup_key LIKE 'notify:%' ORDER BY id""",
        bridge.NOTIFY_OWNER)
    import json
    out = []
    for r in rows:
        p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
        # Кнопок отправки и ответа у уведомления нет: только «Подготовить ответ» (ничего не отправляет),
        # «Напомнить через час» и «Не сообщать об этом чате» — все модуля уведомлений (Б14), и «Ответить
        # голосом» (Р-103) — запись своего ответа, тоже без отправки.
        for row in p["buttons"] or []:
            for b in row:
                assert (b["data"].startswith("sh:nt:") and b["text"] in (
                    "Подготовить ответ", "Напомнить через час", "Не сообщать об этом чате")) or \
                    (b["data"].startswith("sh:vr:a:n:") and b["text"] == "🎙 Ответить голосом"), b
        out.append(p["text"])
    return out


async def items(conn) -> list[tuple]:
    return [(r["kind"], r["reason"], r["state"], r["dropped_reason"]) for r in await conn.fetch(
        "SELECT kind, reason, state, dropped_reason FROM notify_items ORDER BY id")]


def mention(text: str, word: str, user_id: int) -> list[dict]:
    """Разметка «упоминание по числовому ID» на слове `word` в тексте (смещения — в UTF-16)."""
    start = len(text[:text.index(word)].encode("utf-16-le")) // 2
    return [{"type": "mention_name", "offset": start, "length": len(word.encode("utf-16-le")) // 2,
             "text": word, "user_id": user_id}]


def entity(text: str, word: str, kind: str, **extra) -> dict:
    start = len(text[:text.index(word)].encode("utf-16-le")) // 2
    return {"type": kind, "offset": start, "length": len(word.encode("utf-16-le")) // 2, "text": word, **extra}
