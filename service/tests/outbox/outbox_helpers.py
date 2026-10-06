"""Общее для тестов шлюза отправки: подставной шлюз Telegram, «плагин» и заготовки данных.

Telegram заменён объектом FakeTg в `state.extras["tg"]`; модель и бот — прямой работой с очередью
заданий (`jobs.claim` + `bridge.deliver_result` / `deliver_failure`), как это делал бы плагин.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest_asyncio

from shturman import bridge, jobs, store
from shturman.events import MESSAGE_LIVE
from shturman.outbox import autoreply, drafts, policy
from shturman.records import ChatRecord, MessageRecord

OWNER = 1000      # основной аккаунт владельца (только чтение + бизнес-бот)
HELPER = 1500     # аккаунт-помощник
IVAN = 2001
MARIA = 2002
STRANGER = 6666


class FakeTg:
    """Подставной шлюз сессий: запоминает, что и кому «отправлено», и умеет падать по заказу."""

    def __init__(self) -> None:
        self.sendable: set[int] = set()
        self.sent: list[dict] = []
        self.calls = 0
        self.typing: list[tuple] = []
        self.fail: list = []        # очередь исключений для следующих вызовов; None — без ошибки
        self.delay = 0.0
        self._next_id = 9000

    def can_send(self, account_id: int) -> bool:
        return account_id in self.sendable

    async def send_text(self, account_id, peer_class, tg_id, text, *, reply_to_tg_id=None):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            exc = self.fail.pop(0)
            if exc is not None:
                raise exc
        self._next_id += 1
        self.sent.append({"account_id": account_id, "peer_class": peer_class, "tg_id": tg_id, "text": text,
                          "reply_to_tg_id": reply_to_tg_id, "id": self._next_id, "at": time.monotonic()})
        return self._next_id

    async def set_typing(self, account_id, peer_class, tg_id, on):
        self.typing.append((account_id, tg_id, on))


@pytest_asyncio.fixture
async def env(make_client, conn):
    client, state = await make_client("shturman.api_core", "shturman.outbox.service")
    tg = FakeTg()
    state.extras["tg"] = tg
    await bridge.set_owner(conn, OWNER, OWNER)
    owner_acc = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    helper_acc = await store.ensure_account(conn, HELPER, "Помощник", "assistant")
    tg.sendable.add(helper_acc)
    # В тестах паузы нулевые: проверяется порядок действий, а не ожидание.
    await policy.update(conn, {"min_pause_seconds": 0, "part_pause_seconds": 0})
    await autoreply.update(conn, {"pause_seconds": 0, "debounce_seconds": 0})
    return SimpleNamespace(client=client, state=state, mod=state.extras["outbox"], tg=tg, conn=conn,
                           owner_acc=owner_acc, helper_acc=helper_acc)


async def add_chat(conn, account_id, tg_id=IVAN, *, cls="user", type_="personal_chat", name="Иван Петров",
                   username=None, is_bot=None, exclude=False) -> int:
    chat_id, _ = await store.ensure_chat(
        conn, account_id, ChatRecord(cls, tg_id, type_, name, username=username, is_bot=is_bot), exclude=exclude)
    return chat_id


async def add_message(conn, chat_id, tg_message_id, text, *, sender=IVAN, sender_name="Иван Петров",
                      outgoing=False, age=0.0, edited=False, owner_tg_id=OWNER) -> int:
    """Кладёт сообщение в архив так, как это делает живой источник. Возвращает messages.id."""
    at = datetime.now(timezone.utc) - timedelta(seconds=age)
    record = MessageRecord(
        tg_message_id=tg_message_id, sent_at=at, kind="message", sender_class="user", sender_tg_id=sender,
        sender_name=sender_name, text=text, entities=None, reply_to_tg_id=None, forwarded_from=None,
        edited_at=at if edited else None, media_type=None, media_path=None, service_action=None)
    result = await store.upsert_messages(conn, [(chat_id, record, outgoing)], source="session",
                                         owner_tg_id=owner_tg_id)
    return (result.new_ids or result.known_ids)[0]


async def business(conn, account_id, *, can_reply=True, enabled=True, connection_id="bc-1") -> str:
    await conn.execute(
        """INSERT INTO business_connections (id, account_id, can_reply, enabled) VALUES ($1, $2, $3, $4)
           ON CONFLICT (id) DO UPDATE SET can_reply = EXCLUDED.can_reply, enabled = EXCLUDED.enabled""",
        connection_id, account_id, can_reply, enabled)
    return connection_id


async def take(conn, kind, *, complete=None) -> list[dict]:
    """Забирает из очереди все задания вида `kind`, как плагин. complete — результат для закрытия."""
    claimed = sorted(await jobs.claim(conn, [kind], worker="test", limit=20), key=lambda job: job["id"])
    if complete is not None:
        for index, job in enumerate(claimed):
            result = complete(index, job) if callable(complete) else complete
            await bridge.deliver_result(conn, job["id"], result)
    return claimed


async def owner_messages(conn) -> list[dict]:
    """Сообщения владельцу, вставшие в очередь (и закрывает их как доставленные)."""
    return await take(conn, bridge.NOTIFY_OWNER, complete=lambda i, job: {"message_id": 7000 + job["id"]})


def texts(notes: list[dict]) -> str:
    return "\n=====\n".join(n["payload"]["text"] for n in notes)


def button(notes: list[dict], label: str) -> str:
    for note in notes:
        for row in note["payload"]["buttons"] or []:
            for item in row:
                if item["text"] == label:
                    return item["data"]
    raise AssertionError(f"кнопки «{label}» нет")


async def press(env, data: str, user: int = OWNER) -> dict:
    """Нажатие кнопки тем же путём, каким его передаёт плагин."""
    response = await env.client.post("/api/callbacks/telegram", json={"data": data, "from_user_id": user})
    assert response.status_code == 200, response.text
    return response.json()


async def new_draft(env, chat_id, text="Добрый день! Смету пришлю в пятницу.", **extra):
    response = await env.client.post("/api/outbox/drafts", json={"chat_id": chat_id, "text": text, **extra})
    return response


async def draft_row(conn, draft_id):
    return await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1", draft_id)


async def live(env, chat_id, message_id, *, account_id, source="session", **flags) -> None:
    """Событие «новое живое сообщение» и ожидание, пока подписчики его разберут."""
    payload = {"account_id": account_id, "chat_id": chat_id, "message_id": message_id, "source": source,
               "outgoing": False, "edited": False, "via_bot": False}
    payload.update(flags)
    payload = {k: v for k, v in payload.items() if v is not ...}   # `ключ=...` — убрать ключ из события
    env.state.events.publish(MESSAGE_LIVE, payload)
    await env.state.events.drain()


async def settle(env) -> None:
    await drafts.settle(env.mod)
