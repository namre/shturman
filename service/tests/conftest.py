import io
import json
import os

import asyncpg
import pytest
import pytest_asyncio

from shturman import db

DSN = os.environ.get("SHTURMAN_TEST_DSN", "postgresql://postgres@127.0.0.1:54329/shturman_test")

OWNER = 1000
IVAN = 2001
MARIA = 2002


def msg(mid, ts, sender_id, sender, text, **extra):
    base = {
        "id": mid, "type": "message",
        "date": "2026-09-12T10:00:00", "date_unixtime": str(ts),
        "from": sender, "from_id": f"user{sender_id}",
        "text": text,
        "text_entities": [{"type": "plain", "text": text}] if isinstance(text, str) else [],
    }
    base.update(extra)
    return base


def full_export(chats):
    return {
        "about": "Here is the data you requested.",
        "personal_information": {"user_id": OWNER, "first_name": "Евгений", "last_name": "Тестов"},
        "contacts": {"about": "", "list": []},
        "chats": {"about": "", "list": chats},
        "left_chats": {"about": "", "list": []},
    }


def as_file(obj) -> io.BytesIO:
    return io.BytesIO(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


@pytest.fixture
def sample_export():
    t = 1789200000
    ivan = {
        "name": "Иван Петров", "type": "personal_chat", "id": IVAN,
        "messages": [
            msg(1, t, IVAN, "Иван Петров", "Добрый день! Пришлю смету по фасадам к пятнице."),
            msg(2, t + 60, OWNER, "Евгений Тестов", "Хорошо, жду. Сроки монтажа не сдвигаем."),
            msg(3, t + 120, IVAN, "Иван Петров",
                ["Договор лежит ", {"type": "link", "text": "https://example.org/doc"}, ", посмотрите"],
                text_entities=[{"type": "plain", "text": "Договор лежит "},
                               {"type": "link", "text": "https://example.org/doc"},
                               {"type": "plain", "text": ", посмотрите"}],
                reply_to_message_id=2),
            {"id": 4, "type": "service", "date": "2026-09-12T10:05:00", "date_unixtime": str(t + 300),
             "actor": "Иван Петров", "actor_id": f"user{IVAN}", "action": "phone_call",
             "duration_seconds": 42, "text": "", "text_entities": []},
            msg(5, t + 400, IVAN, "Иван Петров", "", media_type="voice_message",
                file="chats/chat_01/voice_messages/audio_1.ogg", duration_seconds=7),
            msg(6, t + 500, IVAN, "Иван Петров", "Фото объекта",
                photo="(File not included. Change data exporting settings to download.)"),
        ],
    }
    family = {
        "name": "Семья", "type": "private_group", "id": 3001,
        "messages": [msg(10, t + 10, MARIA, "Мария", "Купи хлеба и молока")],
    }
    channel = {
        "name": "Стройка: новости", "type": "public_channel", "id": 4001,
        "messages": [
            {"id": 1, "type": "message", "date": "x", "date_unixtime": str(t + 20),
             "from": "Стройка: новости", "from_id": "channel4001",
             "text": "Цены на арматуру выросли", "text_entities": []},
        ],
    }
    empty = {"name": "Пустой", "type": "personal_chat", "id": 2999, "messages": []}
    return full_export([ivan, family, channel, empty])


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    await c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await db.migrate(c)
    try:
        yield c
    finally:
        await c.close()


API_TOKEN = "test-api-token-0123456789abcdef0123456789"
MCP_TOKEN = "test-mcp-token-0123456789abcdef0123456789"
API_AUTH = {"Authorization": f"Bearer {API_TOKEN}"}
MCP_AUTH = {"Authorization": f"Bearer {MCP_TOKEN}"}


@pytest.fixture
def config(tmp_path):
    from shturman.config import Config

    return Config(dsn=DSN, api_token=API_TOKEN, mcp_token=MCP_TOKEN, data_dir=tmp_path / "data",
                  allowed_hosts=("test",))


@pytest_asyncio.fixture
async def make_client(conn, config):
    """Поднимает сервис из указанных модулей в том же цикле, что и тест.

    Использование:  client, state = await make_client("shturman.api_core")
    Клиент уже ходит с токеном внутреннего API; для MCP передавайте headers=MCP_AUTH.
    """
    import contextlib

    import httpx

    from shturman.app import build_app

    stack = contextlib.AsyncExitStack()

    async def factory(*modules, cfg=None):
        gate = build_app(cfg or config, migrate=False, modules=modules)
        await stack.enter_async_context(gate.inner.router.lifespan_context(gate.inner))
        client = await stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gate), base_url="http://test", headers=API_AUTH))
        return client, gate.inner.state.shturman

    try:
        yield factory
    finally:
        await stack.aclose()


# --- подтверждение владельцем в своём боте согласований (см. shturman/confirm.py) ---

@pytest.fixture
def own_bot():
    """У сервиса свой бот согласований: действия, расширяющие права ассистента, ждут нажатия владельца."""
    from shturman import bridge

    bridge.set_builtin({bridge.NOTIFY_OWNER, bridge.NOTIFY_EDIT})
    yield
    bridge.set_builtin(())


@pytest.fixture(params=["свой бот", "без своего бота"])
def either_mode(request):
    """Оба режима по очереди. Значение — есть ли у сервиса свой бот."""
    from shturman import bridge

    with_bot = request.param == "свой бот"
    bridge.set_builtin({bridge.NOTIFY_OWNER, bridge.NOTIFY_EDIT} if with_bot else ())
    yield with_bot
    bridge.set_builtin(())


class Approvals:
    """Владелец в боте согласований: ждущие действия и нажатия под их карточками.

    Нажатие приходит тем же путём, что и от настоящего бота: `bridge.dispatch_callback` с данными
    кнопки и идентификатором нажавшего."""

    def __init__(self, conn, owner=OWNER):
        self.conn, self.owner = conn, owner

    def waiting(self, response) -> int:
        """Ответ маршрута — «ждёт подтверждения»: 202, номер действия и текст карточки простыми словами."""
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "pending_confirmation" and isinstance(body["action_id"], int), body
        summary = body["summary"]
        assert isinstance(summary, str) and len(summary) > 20
        assert "{" not in summary and '":' not in summary, summary   # не сырой JSON
        assert body["expires_at"] and body["note"]
        return body["action_id"]

    async def press(self, action_id: int, yes: bool = True, user: int | None = None) -> dict:
        from shturman import bridge

        nonce = await self.conn.fetchval("SELECT nonce FROM pending_actions WHERE id = $1", action_id)
        data = f"sh:cf:{'y' if yes else 'n'}:{action_id}:{nonce}"
        return await bridge.dispatch_callback(self.conn, data, self.owner if user is None else user)

    async def lapse(self, action_id: int) -> None:
        await self.conn.execute(
            "UPDATE pending_actions SET expires_at = now() - interval '1 minute' WHERE id = $1", action_id)

    async def status(self, action_id: int) -> str:
        return await self.conn.fetchval("SELECT status FROM pending_actions WHERE id = $1", action_id)

    async def pending(self) -> int:
        return await self.conn.fetchval("SELECT count(*) FROM pending_actions WHERE status = 'pending'")

    async def card(self, action_id: int) -> str:
        """Текст карточки, которая ушла владельцу по этому действию."""
        from shturman import bridge

        return await self.conn.fetchval(
            """SELECT payload->>'text' FROM jobs
               WHERE kind = $1 AND executor = 'builtin' AND (context->>'action_id')::bigint = $2""",
            bridge.NOTIFY_OWNER, action_id)

    async def gate(self, send, read, before, after) -> None:
        """Со своим ботом действие ничего не меняет до нажатия; «нет» и истёкший срок — тоже ничего;
        после «да» — применяется. send() -> ответ маршрута, read() -> то, что должно измениться."""
        action = self.waiting(await send())
        assert await read() == before
        assert (await self.press(action, yes=False))["answer"] == "Отклонено."
        assert await read() == before and await self.status(action) == "rejected"

        action = self.waiting(await send())
        await self.lapse(action)
        assert (await self.press(action))["answer"] == "Срок вышел."
        assert await read() == before and await self.status(action) == "expired"

        action = self.waiting(await send())
        assert await read() == before
        assert (await self.press(action, user=self.owner + 1))["answer"] == "Кнопка недоступна."   # не владелец
        assert await read() == before
        done = await self.press(action)
        assert done["answer"] == "Сделано.", done
        assert await read() == after and await self.status(action) == "applied"
        assert (await self.press(action))["answer"] == "Действие уже недоступно."   # второй раз не применяется


@pytest.fixture
def approvals(conn):
    return Approvals(conn)


# --- защита от внедрённых инструкций ---

class FakeScorer:
    """Подставной классификатор: слово «взлом» в тексте — внедрённая инструкция."""

    name = "fake/guard"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.error: Exception | None = None

    def score(self, texts):
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return [0.97 if "взлом" in t.lower() else 0.02 for t in texts]


@pytest.fixture(autouse=True)
def _no_guard_left_behind():
    """Работающая защита — состояние процесса: тест не должен оставить её следующему."""
    from shturman import guard

    guard.set_current(None)
    yield
    guard.set_current(None)


@pytest_asyncio.fixture
async def guarded(conn):
    """Включённая защита с подставной моделью — как её ставит guard.service, но без фонового
    обхода: тест сам вызывает `guard.sweep()`, когда он нужен."""
    from types import SimpleNamespace

    from shturman import guard
    from shturman.events import MESSAGES_HIDDEN, Events
    from shturman.guard import core

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    scorer, events = FakeScorer(), Events()
    hidden: list[int] = []

    async def on_hidden(payload):
        hidden.extend(payload["message_ids"])

    events.subscribe(MESSAGES_HIDDEN, on_hidden)
    active = core.Guard(pool, core.Settings(threshold=0.9), model=scorer, events=events)
    guard.set_current(active)
    try:
        yield SimpleNamespace(guard=active, scorer=scorer, pool=pool, events=events, hidden=hidden)
    finally:
        guard.set_current(None)
        await events.drain()
        await pool.close()
