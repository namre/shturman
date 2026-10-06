"""Подставной клиент Telegram для тестов модуля tg. В сеть ничего не ходит.

Подмена стоит на границе, которую модуль сам и определил: объект с методами клиента Telethon,
которыми пользуется сервис (`connect`, `get_me`, `iter_dialogs`, `qr_login`, вызов запроса…).
Ответы собраны из настоящих объектов TL. Каждый запрос подставной клиент проверяет настоящим
перечнем разрешённых запросов (`RequestPolicy`) — так тесты заодно показывают, что чтение под
ролью owner обходится только разрешёнными запросами.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest_asyncio
from telethon import errors, utils
from telethon.tl import functions, types

from shturman.tg import normalize
from shturman.tg.manager import TgManager

from conftest import DSN

SELF_ID = 1000          # основной аккаунт владельца (как в tests/conftest.py)
HELPER_ID = 5000        # аккаунт-помощник
IVAN, MARIA, BOT = 2001, 2002, 2100
GROUP, SUPER, CHANNEL = 3001, 4002, 4001
PASSWORD = "очень-секретный-пароль-77"
T0 = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)


def user(uid: int, first: str, last: str | None = None, **kw: Any) -> types.User:
    return types.User(id=uid, first_name=first, last_name=last, access_hash=uid * 7, **kw)


def group(gid: int, title: str) -> types.Chat:
    return types.Chat(id=gid, title=title, photo=types.ChatPhotoEmpty(), participants_count=3,
                      date=T0, version=1)


def channel(cid: int, title: str, **kw: Any) -> types.Channel:
    return types.Channel(id=cid, title=title, photo=types.ChatPhotoEmpty(), date=T0,
                         access_hash=cid * 7, **kw)


ME = user(SELF_ID, "Евгений", "Тестов", is_self=True)
HELPER = user(HELPER_ID, "Помощник", is_self=True)
U_IVAN = user(IVAN, "Иван", "Петров", username="ivan_p")
U_MARIA = user(MARIA, "Мария")
U_BOT = user(BOT, "Погода", bot=True, username="weather_bot")
U_TELEGRAM = user(777000, "Telegram")
G_FAMILY = group(GROUP, "Семья")
C_SUPER = channel(SUPER, "Стройка: чат", megagroup=True)
C_NEWS = channel(CHANNEL, "Стройка: новости", broadcast=True, username="stroyka_news")
ENTITIES = [ME, HELPER, U_IVAN, U_MARIA, U_BOT, U_TELEGRAM, G_FAMILY, C_SUPER, C_NEWS]


def peer(key: tuple[str, int]) -> Any:
    return normalize.to_peer(key)


def msg(mid: int, key: tuple[str, int], text: str, *, sender: int | None = None, out: bool = False,
        at: datetime | None = None, **kw: Any) -> types.Message:
    return types.Message(
        id=mid, peer_id=peer(key), date=at or T0 + timedelta(minutes=mid), message=text, out=out,
        from_id=types.PeerUser(sender) if sender else None, **kw)


def now_msg(mid: int, key: tuple[str, int], text: str, **kw: Any) -> types.Message:
    """Сообщение с нынешним временем — для сверки удалений, которая смотрит недавние."""
    return msg(mid, key, text, at=datetime.now(timezone.utc) - timedelta(minutes=5), **kw)


class World:
    """Состояние «Telegram» для подставного клиента."""

    def __init__(self, me: types.User = ME) -> None:
        self.me = me
        self.authorized = True
        self.password: str | None = None      # облачный пароль, если включён
        self.hint = "кличка кота"
        self.history: dict[tuple[str, int], dict[int, Any]] = {}
        self.dialogs: list[Any] = []          # сущности в порядке списка диалогов
        self.unknown: set[tuple[str, int]] = set()   # собеседники без ключа доступа
        self.fail: dict[type, list[BaseException]] = {}   # ошибки по классу запроса, по очереди
        self.lost: dict[tuple[str, int], BaseException] = {}   # чаты, к которым доступа больше нет
        self.next_id = 9000
        self.clients: list[FakeClient] = []
        self.connect_error: BaseException | None = None

    def add(self, *messages: Any) -> None:
        for m in messages:
            self.history.setdefault(normalize.peer_key(m.peer_id), {})[m.id] = m

    def remove(self, key: tuple[str, int], *ids: int) -> None:
        for i in ids:
            self.history[key].pop(i, None)

    def factory(self, role: str, path: Any, policy: Any, on_reconnect: Any) -> "FakeClient":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()  # настоящий клиент создаёт файл сессии
        client = FakeClient(self, role, policy, on_reconnect)
        self.clients.append(client)
        return client

    @property
    def last(self) -> "FakeClient":
        return self.clients[-1]


class FakeQR:
    def __init__(self, client: "FakeClient") -> None:
        self.client = client
        self.issued = 0
        self._issue()

    def _issue(self) -> None:
        self.issued += 1
        self.token = f"QRTOKEN{self.issued}SECRET"
        self.url = f"tg://login?token={self.token}"
        self.expires = self.client.clock() + timedelta(seconds=30)

    async def recreate(self) -> None:
        self._issue()

    async def wait(self, timeout: float | None = None) -> Any:
        client = self.client
        client.waits.append(timeout)
        if client.scan_timeouts > 0:      # код не отсканировали вовремя
            client.scan_timeouts -= 1
            client.advance(30)
            raise asyncio.TimeoutError()
        outcome = await client.scan
        client.scan = asyncio.get_running_loop().create_future()
        if isinstance(outcome, BaseException):
            raise outcome
        client.authorized = True
        return outcome


class FakeClient:
    def __init__(self, world: World, role: str, policy: Any, on_reconnect: Any = None) -> None:
        self.world, self.role, self.policy, self.on_reconnect = world, role, policy, on_reconnect
        self.authorized = world.authorized
        self.connected = False
        self.disconnected: asyncio.Future = asyncio.get_running_loop().create_future()
        self.requests: list[Any] = []
        self.handlers: list[tuple[Any, Any]] = []
        self.catch_ups = 0
        self.logged_out = False
        self.qr: FakeQR | None = None
        self.scan: asyncio.Future = asyncio.get_running_loop().create_future()
        self.scan_timeouts = 0
        self.waits: list[float | None] = []
        self.now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    # --- время (для входа по QR) ---

    def clock(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    # --- соединение ---

    async def connect(self) -> None:
        if self.world.connect_error is not None:
            raise self.world.connect_error
        self.connected = True
        if self.disconnected.done():
            self.disconnected = asyncio.get_running_loop().create_future()

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self) -> None:
        self.connected = False
        if not self.disconnected.done():
            self.disconnected.set_result(None)

    def drop(self) -> None:
        """Соединение потеряно окончательно (Telethon исчерпал попытки)."""
        self.connected = False
        if not self.disconnected.done():
            self.disconnected.set_result(None)

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> Any:
        await self(functions.users.GetUsersRequest([types.InputUserSelf()]))
        return self.world.me if self.authorized else None

    async def catch_up(self) -> None:
        self.catch_ups += 1

    async def log_out(self) -> bool:
        await self(functions.auth.LogOutRequest())
        self.logged_out, self.authorized = True, False
        await self.disconnect()
        return True

    # --- вход ---

    async def qr_login(self) -> FakeQR:
        self.policy.check(functions.auth.ExportLoginTokenRequest(1, "x", []))
        self.qr = FakeQR(self)
        return self.qr

    async def sign_in(self, *, password: str) -> Any:
        self.policy.check(functions.account.GetPasswordRequest())
        if password != self.world.password:
            raise errors.PasswordHashInvalidError(None)
        self.authorized = True
        return self.world.me

    # --- обновления ---

    def add_event_handler(self, callback: Any, event: Any) -> None:
        self.handlers.append((callback, event))

    async def emit(self, update: Any, entities: Any = ENTITIES) -> None:
        update._entities = {utils.get_peer_id(e): e for e in entities}
        for callback, event in self.handlers:
            if event.filter(update) is not None:
                await callback(update)

    # --- запросы ---

    async def get_input_entity(self, p: Any) -> Any:
        key = normalize.peer_key(p)
        if key in self.world.unknown:
            raise ValueError("Could not find the input entity")
        if key[0] == "user":
            return types.InputPeerUser(key[1], key[1] * 7)
        if key[0] == "chat":
            return types.InputPeerChat(key[1])
        return types.InputPeerChannel(key[1], key[1] * 7)

    async def iter_dialogs(self):
        await self(functions.messages.GetDialogsRequest(
            offset_date=None, offset_id=0, offset_peer=types.InputPeerEmpty(), limit=100, hash=0))
        for entity in self.world.dialogs:
            key = normalize.entity_key(entity)
            top = max(self.world.history.get(key, {0: None}))
            yield SimpleNamespace(entity=entity, dialog=SimpleNamespace(top_message=top))

    async def __call__(self, request: Any) -> Any:
        self.policy.check(request)
        if not self.connected:
            raise ConnectionError("not connected")
        self.requests.append(request)
        queue = self.world.fail.get(type(request))
        if queue:
            raise queue.pop(0)
        handler = getattr(self, "_" + type(request).__name__, None)
        return await handler(request) if handler else None

    def of(self, cls: type) -> list[Any]:
        return [r for r in self.requests if isinstance(r, cls)]

    @staticmethod
    def _key(input_peer: Any) -> tuple[str, int]:
        if isinstance(input_peer, (types.InputPeerUser, types.InputUser)):
            return "user", input_peer.user_id
        if isinstance(input_peer, types.InputPeerChat):
            return "chat", input_peer.chat_id
        return "channel", input_peer.channel_id

    def _wrap(self, key: tuple[str, int], selected: list[Any], total: int) -> Any:
        users = [e for e in ENTITIES if isinstance(e, types.User)]
        chats = [e for e in ENTITIES if not isinstance(e, types.User)]
        if key[0] == "channel":
            return types.messages.ChannelMessages(pts=1, count=total, messages=selected, topics=[],
                                                  chats=chats, users=users)
        if total <= len(selected):
            return types.messages.Messages(messages=selected, topics=[], chats=chats, users=users)
        return types.messages.MessagesSlice(count=total, messages=selected, topics=[], chats=chats, users=users)

    async def _GetHistoryRequest(self, r: Any) -> Any:
        key = self._key(r.peer)
        if key in self.world.lost:
            raise self.world.lost[key]
        every = sorted(self.world.history.get(key, {}).values(), key=lambda m: -m.id)
        if r.add_offset < 0:   # страница сразу после offset_id, к новым
            newer = sorted((m for m in every if m.id >= r.offset_id), key=lambda m: m.id)[:r.limit]
            selected = list(reversed(newer))
        else:
            selected = [m for m in every if not r.offset_id or m.id < r.offset_id][:r.limit]
        return self._wrap(key, selected, len(every))

    def _by_ids(self, pool: dict[int, Any], wanted: list[Any]) -> list[Any]:
        return [pool.get(w.id) or types.MessageEmpty(id=w.id, peer_id=None) for w in wanted]

    async def _GetMessagesRequest(self, r: Any) -> Any:
        if hasattr(r, "channel"):
            key = ("channel", r.channel.channel_id)
            if key in self.world.lost:
                raise self.world.lost[key]
            found = self._by_ids(self.world.history.get(key, {}), r.id)
            return types.messages.ChannelMessages(pts=1, count=len(found), messages=found, topics=[],
                                                  chats=[], users=[])
        pool: dict[int, Any] = {}
        for key, items in self.world.history.items():
            if key[0] != "channel":
                pool.update(items)
        return types.messages.Messages(messages=self._by_ids(pool, r.id), topics=[], chats=[], users=[])

    async def _SendMessageRequest(self, r: Any) -> Any:
        key = self._key(r.peer)
        self.world.next_id += 1
        mid = self.world.next_id
        date = datetime.now(timezone.utc).replace(microsecond=0)
        if key[0] == "user":
            return types.UpdateShortSentMessage(id=mid, pts=1, pts_count=1, date=date, out=True)
        sent = types.Message(id=mid, peer_id=peer(key), date=date, message=r.message, out=True,
                             from_id=types.PeerUser(self.world.me.id))
        update = (types.UpdateNewChannelMessage if key[0] == "channel" else types.UpdateNewMessage)(sent, 1, 1)
        return types.Updates([types.UpdateMessageID(mid, r.random_id), update],
                             users=[self.world.me], chats=[C_SUPER, G_FAMILY], date=date, seq=0)

    async def _SetTypingRequest(self, r: Any) -> bool:
        return True

    async def _GetPasswordRequest(self, r: Any) -> Any:
        return SimpleNamespace(hint=self.world.hint)


# --- общие фикстуры ---

@pytest_asyncio.fixture
async def pool(conn):
    """Пул к тестовой базе. Зависит от `conn`: схема уже пересоздана."""
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=6)
    try:
        yield p
    finally:
        await p.close()


def tg_config(config: Any) -> Any:
    return dataclasses.replace(config, tg_api_id=12345, tg_api_hash="0123456789abcdef0123456789abcdef")


async def start_account(manager: TgManager, world: World, slot: str) -> Any:
    """Запускает сессию так, как она стартует с диска: файл есть, вход уже был."""
    from shturman.tg.client import session_path

    path = session_path(manager.config, slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    manager.client_factory = world.factory
    await manager.start()
    rt = manager.runtimes[slot]
    await asyncio.wait_for(rt.ready.wait(), 5)
    return rt


async def settle(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("условие не наступило за отведённое время")
        await asyncio.sleep(0.01)
