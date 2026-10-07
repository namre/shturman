"""Общее для тестов страницы настройки: сервис со страницей и «браузер» владельца.

Telegram для аккаунтов — подставной клиент из tests/tg, Bot API и сервер модели — из
tests/executor. «Браузер» — клиент httpx с банкой cookie, который, как настоящий браузер,
ставит `Origin` и метку страницы; токенов внутреннего API у него нет.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest_asyncio

for _extra in ("tg", "executor"):
    _path = str(Path(__file__).resolve().parents[1] / _extra)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import exec_fakes  # noqa: E402
import tg_fakes  # noqa: E402
from shturman.executor import service as executor_service  # noqa: E402
from shturman.setup_page import auth, service as setup_service  # noqa: E402

ORIGIN = "http://test"
PREFIX = "/shturman-setup"
API = PREFIX + "/api"
MODULES = ("shturman.api_core", "shturman.executor.service", "shturman.ingest_api", "shturman.tg.service",
           "shturman.setup_page.service")
TOKEN = exec_fakes.TOKEN
BUSY_TOKEN = "7000000002:BUSY-bot-token_polled-by-another-0123456789"
LLM_KEY = "sk-SENTINEL-llm-key-DoNotLeak-0123456789"
API_HASH = "0123456789abcdef0123456789abcdef"


class Telegram(exec_fakes.FakeTelegram):
    """Подставной Bot API с двумя ботами: свободным и тем, которого уже опрашивает другая программа."""

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(f"/bot{BUSY_TOKEN}/"):
            method = request.url.path.rsplit("/", 1)[1]
            self.requests.append((f"busy:{method}", {}))
            if method == "getMe":
                return exec_fakes.ok({"id": 7000000002, "is_bot": True, "first_name": "Ассистент",
                                      "username": "ivan_assistant_bot"})
            return exec_fakes.refusal(409, "Conflict: terminated by other getUpdates request")
        return await super()._handle(request)


class Browser:
    """Браузер владельца: cookie, `Origin`, метка страницы и значение защиты от подделки запроса."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self.http = httpx.AsyncClient(transport=transport, base_url=ORIGIN)
        self.csrf = ""

    def headers(self, **extra: str) -> dict[str, str]:
        out = {"Origin": ORIGIN, "X-Shturman-Setup": "1", "Sec-Fetch-Site": "same-origin"}
        if self.csrf:
            out["X-Shturman-Csrf"] = self.csrf
        out.update(extra)
        return out

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.http.get(API + path, headers={"Sec-Fetch-Site": "same-origin"}, **kw)

    async def send(self, method: str, path: str, body: Any = None, **kw: Any) -> httpx.Response:
        return await self.http.request(method, API + path, json={} if body is None else body,
                                       headers=self.headers(), **kw)

    async def post(self, path: str, body: Any = None) -> httpx.Response:
        return await self.send("POST", path, body)

    async def put(self, path: str, body: Any = None) -> httpx.Response:
        return await self.send("PUT", path, body)

    async def delete(self, path: str) -> httpx.Response:
        return await self.send("DELETE", path)

    async def login(self, conn: Any) -> httpx.Response:
        """Вход по одноразовой ссылке — так, как её выдаёт команда `shturman setup-link`."""
        token, _ = await auth.create_link(conn)
        response = await self.http.post(API + "/login/link", json={"token": token}, headers=self.headers())
        assert response.status_code == 200, response.text
        self.csrf = response.json()["csrf"]
        return response

    async def aclose(self) -> None:
        await self.http.aclose()


@pytest_asyncio.fixture
async def stand(make_client, conn, config, monkeypatch):
    """Сервис со страницей настройки. `await stand(**настройки)` → объект с браузером и подставными."""
    from types import SimpleNamespace

    telegram, llm = Telegram(), exec_fakes.FakeLlm("да")
    executor_service.TEST_OVERRIDES.update(
        bot_transport=telegram.transport(), llm_transport=llm.transport(), poll=0, idle=0.02, probe=0)
    monkeypatch.setattr(setup_service, "FAIL_DELAY", 0.0)
    browsers: list[Browser] = []

    async def start(*, modules=MODULES, world=None, **changes):
        cfg = dataclasses.replace(config, **changes)
        client, state = await make_client(*modules, cfg=cfg)
        manager = state.extras.get("tg")
        if manager is not None:
            world = world or tg_fakes.World(tg_fakes.HELPER)
            manager.client_factory = world.factory
            manager.pacing = 0.0

        def browser() -> Browser:
            b = Browser(client._transport)
            browsers.append(b)
            return b

        return SimpleNamespace(api=client, state=state, conn=conn, config=cfg, telegram=telegram, llm=llm,
                               world=world, manager=manager, browser=browser, page=browser())

    try:
        yield start
    finally:
        for b in browsers:
            await b.aclose()
        executor_service.TEST_OVERRIDES.clear()


async def bind_owner(s: Any, user: dict | None = None) -> None:
    """Владелец привязывается к боту согласований: ссылка со страницы, затем «Запустить» в боте."""
    await exec_fakes.until(lambda: getattr(s.state.extras["executor"].bot, "identity", None))   # бот вышел на связь
    link = (await s.page.post("/bot/bind")).json()["link"]
    s.telegram.text("/start " + link.split("start=")[1], user=user or exec_fakes.OWNER_USER)
    await exec_fakes.until(lambda: _bound(s))


async def _bound(s: Any) -> bool:
    return (await s.page.get("/state")).json()["bot"]["owner_bound"]


async def save_bot(s: Any, token: str = TOKEN) -> httpx.Response:
    first = await s.page.post("/bot/token", {"token": token})
    assert first.status_code == 200 and first.json()["status"] == "confirm", first.text
    return await s.page.post("/bot/token", {"token": token, "separate": True})


async def raw_request(s: Any, raw_path: bytes, headers: dict[str, str] | None = None, method: str = "GET") -> tuple[int, bytes]:
    """Запрос с путём ровно как он записан — без нормализации, которую делает клиент httpx.
    Так до сервиса доходят «..» и закодированные косые черты, как их прислал бы недобрый клиент."""
    from urllib.parse import unquote

    app = s.api._transport.app
    path = unquote(raw_path.split(b"?")[0].decode("latin-1"))
    sent = [(b"host", b"test")] + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": raw_path.split(b"?")[0], "query_string": b"", "root_path": "",
             "headers": sent, "client": ("127.0.0.1", 1), "server": ("test", 80)}
    out: dict[str, Any] = {"status": 0, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    await app(scope, receive, send)
    return out["status"], out["body"]
