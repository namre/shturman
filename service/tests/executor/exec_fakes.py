"""Общее для тестов своего исполнителя: подставной Telegram, подставной сервер модели, стенд.

В сеть тесты не ходят: клиенты получают `httpx.MockTransport`. Подставной Telegram ведёт себя
как настоящий в том, что важно для проверок: отдаёт обновления начиная с `offset` и забывает
подтверждённые, выдаёт номера сообщений, помнит все запросы.
"""

import asyncio
import dataclasses
import json
import time
from types import SimpleNamespace

import httpx
import pytest_asyncio

from shturman import bridge
from shturman.executor import binding
from shturman.executor.bot import Bot
from shturman.executor.botapi import BotApi
from shturman.executor.worker import Worker

# Токен-метка: по нему тесты ищут утечки в журнале, ошибках заданий и ответах API.
TOKEN = "7000000001:SENTINEL-bot-token_DoNotLeak-0123456789"
BOT_ID = 7000000001
BOT_NAME = "shturman_soglasovaniya_bot"
OWNER = 1000
IVAN = 2001
STRANGER = 6666
OWNER_USER = {"id": OWNER, "is_bot": False, "first_name": "Евгений", "last_name": "Тестов"}
IVAN_USER = {"id": IVAN, "is_bot": False, "first_name": "Иван", "last_name": "Петров"}
STRANGER_USER = {"id": STRANGER, "is_bot": False, "first_name": "Некто"}


def ok(result) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def refusal(code: int, description: str, **parameters) -> httpx.Response:
    body = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return httpx.Response(code, json=body)


def private(user: dict) -> dict:
    return {"id": user["id"], "type": "private", "first_name": user.get("first_name", "")}


class FakeTelegram:
    """Подставной Bot API. `script[метод]` — очередь заготовленных ответов или исключений."""

    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.requests: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.script: dict[str, list] = {}
        self.connections: dict[str, dict] = {}
        self.me = {"id": BOT_ID, "is_bot": True, "first_name": "Согласования", "username": BOT_NAME,
                   "can_connect_to_business": True}
        self._update_id = 100
        self._message_id = 500
        self.foreign_paths: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        prefix = f"/bot{self.token}/"
        if not request.url.path.startswith(prefix):
            self.foreign_paths.append(request.url.path)
            return httpx.Response(404, json={"ok": False, "error_code": 404, "description": "Not Found"})
        method = request.url.path[len(prefix):]
        params = json.loads(request.content or b"{}")
        self.requests.append((method, params))
        queued = self.script.get(method)
        if queued:
            item = queued.pop(0)
            if isinstance(item, Exception):
                raise item
            if callable(item):
                item = item(params)
                if asyncio.iscoroutine(item):
                    item = await item
            if item is not None:
                return item
        if method == "getMe":
            return ok(self.me)
        if method == "getUpdates":
            offset = params.get("offset")
            if offset is not None:
                self.updates = [u for u in self.updates if u["update_id"] >= offset]   # подтверждённые забыты
            if not self.updates:
                await asyncio.sleep(0.01)
            return ok(list(self.updates))
        if method == "sendMessage":
            self._message_id += 1
            return ok({"message_id": self._message_id, "date": int(time.time()),
                       "chat": {"id": params["chat_id"], "type": "private"}, "text": params["text"]})
        if method == "getBusinessConnection":
            link = self.connections.get(params.get("business_connection_id"))
            return ok(link) if link else refusal(400, "Bad Request: business connection not found")
        return ok(True)

    # --- что видел Telegram ---

    def calls(self, method: str) -> list[dict]:
        return [params for name, params in self.requests if name == method]

    def sent(self, *, business: bool = False) -> list[dict]:
        return [p for p in self.calls("sendMessage") if bool(p.get("business_connection_id")) == business]

    def last_message_id(self) -> int:
        return self._message_id

    # --- что присылает Telegram ---

    def push(self, **update) -> int:
        self._update_id += 1
        self.updates.append({"update_id": self._update_id, **update})
        return self._update_id

    def text(self, text: str, *, user: dict = OWNER_USER, chat: dict | None = None) -> int:
        return self.push(message={"message_id": 1, "date": int(time.time()), "from": user,
                                  "chat": chat or private(user), "text": text})

    def press(self, data: str, *, user: dict = OWNER_USER, message_id: int = 501, chat: dict | None = None,
              markup: dict | None = None, query_id: str | None = None) -> int:
        message = {"message_id": message_id, "date": int(time.time()), "chat": chat or private(OWNER_USER)}
        if markup is not None:
            message["reply_markup"] = markup
        return self.push(callback_query={"id": query_id or f"q{self._update_id + 1}", "from": user,
                                         "message": message, "chat_instance": "ci", "data": data})


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


async def no_sleep(seconds: float) -> None:
    return None


async def until(check, *, timeout: float = 5.0):
    """Ждёт, пока проверка не вернёт истину (для тестов с работающими фоновыми задачами)."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("условие не наступило за отведённое время")
        await asyncio.sleep(0.02)


@pytest_asyncio.fixture
async def rig(make_client, conn, config):
    """Сервис со своим ботом, собранный вручную: опрос и исполнитель запускаются по шагам из теста."""
    cfg = dataclasses.replace(config, bot_token=TOKEN, sending=True, send_daily_hard_cap=1000)
    client, state = await make_client("shturman.api_core", "shturman.ingest_api", cfg=cfg)
    bridge.set_builtin({bridge.NOTIFY_OWNER, bridge.NOTIFY_EDIT})
    tg = FakeTelegram()
    api = BotApi(TOKEN, transport=tg.transport())
    clock = Clock()
    bot = Bot(state, api, poll=0, clock=clock, sleep=no_sleep)
    worker = Worker(state, api=api, bot=bot, sleep=no_sleep)
    bot.wake = worker.wake
    await bot.ensure_identity()
    try:
        yield SimpleNamespace(client=client, state=state, conn=conn, tg=tg, api=api, bot=bot, worker=worker,
                              clock=clock, config=cfg)
    finally:
        bridge.set_builtin(())
        await api.aclose()


async def bind(rig, user: dict = OWNER_USER) -> str:
    """Привязывает владельца так, как это делает человек: команда оператора, затем «Запустить» в боте."""
    code, _ = await binding.create_code(rig.conn)
    rig.tg.text(f"/start {code}", user=user)
    await rig.bot.poll_once()
    return code


def buttons_of(params: dict) -> dict[str, str]:
    """Кнопки отправленного сообщения: {подпись: данные}."""
    rows = (params.get("reply_markup") or {}).get("inline_keyboard") or []
    return {b["text"]: b["callback_data"] for row in rows for b in row}


class FakeLlm:
    """Подставной сервер Chat Completions. `script` — очередь ответов; пусто — отвечает `answer`."""

    def __init__(self, answer: str = "ответ") -> None:
        self.answer = answer
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.script: list = []
        self.served_model: str | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    @staticmethod
    def completion(text, model="served-model") -> httpx.Response:
        return httpx.Response(200, json={
            "id": "c1", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        })

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            if callable(item):
                item = item(body)
                if asyncio.iscoroutine(item):
                    item = await item
            if item is not None:
                return item
        return self.completion(self.answer, self.served_model or body["model"])
