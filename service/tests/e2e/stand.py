"""Стенд для браузерной проверки страницы настройки: настоящий сервис, подставные Telegram и модель.

Что настоящее: сервис переписки целиком (все модули, uvicorn, Postgres), страница и её файлы.
Что подставное: Telegram для аккаунтов (подставной клиент из tests/tg), Bot API, сервер модели и
OpenAI для подписки ChatGPT (из tests/executor). В сеть стенд не ходит.

Запуск — см. README.md в этом каталоге. Стенд пересоздаёт схему `public` в базе из
SHTURMAN_TEST_DSN: база должна быть отдельной тестовой.

Рядом с сервисом поднимается «пульт» (порт сервиса + 1) — им сценарий делает то, что в жизни
делает человек в Telegram: сканирует QR, нажимает «Запустить» в боте, подключает бизнес-режим.

Для экрана «Память ассистента» пульт заводит (`POST /seed-memory`) переписку с двумя людьми, страницу
об одном из них со сводкой — ответ «модели» подставляет сам стенд, — предложение завести страницу о
втором и новую договорённость, ждущую решения.

Третий порт (порт сервиса + 2) — подставной «дашборд»: пустая страница и service worker на том же
имени узла, что и страница настройки, но на другом порту. С неё сценарий делает то, что делал бы
скрипт ассистента на адресе дашборда Hermes: `fetch` к API страницы, чтение `localStorage`,
подкладывание cookie, service worker. Страница открывается как http://localhost:<порт>, «дашборд» —
как http://localhost:<порт + 2>: имя одно, origin разные.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for extra in (HERE.parent, HERE.parent / "tg", HERE.parent / "executor"):
    sys.path.insert(0, str(extra))

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse, Response  # noqa: E402
from starlette.routing import Route  # noqa: E402
from telethon import errors  # noqa: E402

import exec_fakes  # noqa: E402
import openai_fakes  # noqa: E402
import tg_fakes  # noqa: E402
from shturman import db, netguard  # noqa: E402
from shturman.app import build_app  # noqa: E402
from shturman.config import Config  # noqa: E402
from shturman.executor import service as executor_service  # noqa: E402

DSN = os.environ.get("SHTURMAN_TEST_DSN", "")
PORT = int(os.environ.get("E2E_PORT", "8765"))
# «Внешний адрес» страницы и адрес подставного дашборда: одно имя, разные порты.
ORIGIN = os.environ.get("E2E_ORIGIN", f"http://localhost:{PORT}")
DASHBOARD = os.environ.get("E2E_DASHBOARD", f"http://localhost:{PORT + 2}")

DASHBOARD_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Подставной дашборд</title></head>
<body><h1>Подставной дашборд</h1><p>Отсюда сценарий пробует добраться до страницы настройки.</p></body></html>"""

# Service worker «дашборда»: забирает под себя все страницы, до которых дотянется, и запоминает их запросы.
DASHBOARD_WORKER = """const seen = [];
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => { seen.push(event.request.url); });
self.addEventListener("message", (event) => { event.source.postMessage({ seen }); });
"""

GOOD_TOKEN = exec_fakes.TOKEN                         # бот согласований стенда
BUSY_TOKEN = "7000000002:BUSY-bot-token_polled-by-another-0123456789"   # «бот ассистента»: его уже опрашивают
PASSWORD = tg_fakes.PASSWORD
LLM_KEY = "sk-e2e-not-a-real-key-0123456789"


class StandTelegram(exec_fakes.FakeTelegram):
    """Подставной Bot API: знает двух ботов — свободного и того, которого «уже опрашивают»."""

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(f"/bot{BUSY_TOKEN}/"):
            method = request.url.path.rsplit("/", 1)[1]
            if method == "getMe":
                return exec_fakes.ok({"id": 7000000002, "is_bot": True, "first_name": "Ассистент",
                                      "username": "ivan_assistant_bot", "can_connect_to_business": False})
            return exec_fakes.refusal(409, "Conflict: terminated by other getUpdates request; "
                                           "make sure that only one bot instance is running")
        if request.url.path.endswith("/getUpdates") and not self.updates:
            await asyncio.sleep(0.2)          # не крутить опрос вхолостую сотни раз в секунду
        return await super()._handle(request)


def dialogs() -> list:
    """Полторы сотни диалогов: личные, группы, каналы — чтобы список листался и искался."""
    people = ["Иван Петров", "Мария", "Олег Смета", "Анна Дизайн", "Пётр Монтаж", "Ольга Бухгалтерия"]
    items = [tg_fakes.U_IVAN, tg_fakes.U_MARIA, tg_fakes.U_BOT, tg_fakes.U_TELEGRAM,
             tg_fakes.G_FAMILY, tg_fakes.C_SUPER, tg_fakes.C_NEWS]
    for i in range(90):
        first, _, last = (people[i % len(people)] + " ").partition(" ")
        items.append(tg_fakes.user(20_000 + i, first, f"{last.strip()} {i + 1}".strip()))
    for i in range(35):
        items.append(tg_fakes.group(30_000 + i, f"Объект № {i + 1}: рабочая группа"))
    for i in range(25):
        items.append(tg_fakes.channel(40_000 + i, f"Новости отрасли {i + 1}", broadcast=True))
    return items


def make_world(me) -> tg_fakes.World:
    world = tg_fakes.World(me)
    world.authorized = False
    world.dialogs = dialogs()
    world.add(*(tg_fakes.msg(i, ("user", tg_fakes.IVAN), f"Сообщение {i} про смету по фасадам",
                             sender=tg_fakes.IVAN) for i in range(1, 8)))
    return world


async def main() -> None:
    if not DSN:
        sys.exit("нужна переменная SHTURMAN_TEST_DSN с отдельной тестовой базой")
    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await db.migrate(conn)

    data_dir = Path(os.environ.get("E2E_DATA") or tempfile.mkdtemp(prefix="shturman-e2e-"))
    config = Config(
        dsn=DSN, api_token="e2e-api-token-0123456789abcdef0123456789", mcp_token="e2e-mcp-token-0123456789abcdef0123456789",
        host="127.0.0.1", port=PORT, data_dir=data_dir,
        allowed_hosts=(f"127.0.0.1:{PORT}", f"localhost:{PORT}"), setup_origin=ORIGIN, dashboard_origin=DASHBOARD)

    async def resolve(host: str, port: int) -> list[str]:
        """Имена узлов в сеть не уходят: любое «разрешается» в один адрес в интернете, а
        inner.example — внутрь сервера (так сценарий проверяет фильтр адреса модели)."""
        return ["127.0.0.1"] if host == "inner.example" else ["93.184.216.34"]

    netguard.resolver = resolve

    telegram, llm, openai = StandTelegram(), exec_fakes.FakeLlm("да"), openai_fakes.FakeOpenAI()
    executor_service.TEST_OVERRIDES.update(
        bot_transport=telegram.transport(), llm_transport=llm.transport(), chatgpt_transport=openai.transport(),
        poll=0, idle=0.2, probe=0)
    worlds = {"owner": make_world(tg_fakes.ME), "assistant": make_world(tg_fakes.HELPER)}
    worlds["assistant"].password = PASSWORD        # у помощника включён облачный пароль

    gate = build_app(config, migrate=False)
    state = {}

    def factory(role, path, policy, on_reconnect):
        return worlds[role].factory(role, path, policy, on_reconnect)

    # --- пульт: действия человека в Telegram ---

    async def scan(request):
        """Человек отсканировал QR телефоном. ?role=owner|assistant"""
        role = request.query_params.get("role", "assistant")
        world = worlds[role]
        client = world.last
        if world.password and not request.query_params.get("nopassword"):
            client.scan.set_exception(errors.SessionPasswordNeededError(None))
        else:
            client.scan.set_result(world.me)
        return JSONResponse({"ok": True})

    async def start(request):
        """Человек открыл ссылку привязки и нажал «Запустить». ?code=…"""
        telegram.text(f"/start {request.query_params['code']}", user=exec_fakes.OWNER_USER)
        return JSONResponse({"ok": True})

    async def last_code(request):
        """Последний код входа, который бот прислал владельцу."""
        for params in reversed(telegram.calls("sendMessage")):
            text = params.get("text", "")
            if text.startswith("Код входа"):
                return JSONResponse({"code": "".join(ch for ch in text.split("\n")[0] if ch.isdigit())})
        return JSONResponse({"code": None})

    async def business(request):
        """Человек включил режим у @BotFather и подключил бота в настройках Telegram."""
        telegram.me["can_connect_to_business"] = True
        if request.query_params.get("connect"):
            link = {"id": "bc-e2e", "user": exec_fakes.OWNER_USER, "user_chat_id": exec_fakes.OWNER,
                    "date": int(time.time()), "is_enabled": True, "rights": {"can_reply": False}}
            telegram.connections["bc-e2e"] = link
            telegram.push(business_connection=link)
            telegram.push(business_message={
                "message_id": 77, "date": int(time.time()), "business_connection_id": "bc-e2e",
                "from": exec_fakes.IVAN_USER, "chat": exec_fakes.private(exec_fakes.IVAN_USER),
                "text": "Добрый день! Смету пришлю к пятнице."})
        return JSONResponse({"ok": True})

    async def chatgpt_authorize(request):
        """Человек вошёл в ChatGPT по адресу входа и разрешил доступ: что окажется в адресной строке. ?url=…"""
        return JSONResponse({"address": openai.authorize(request.query_params["url"])})

    async def seen(request):
        """Что видел подставной Telegram и что лежит в журнале действий — для проверок сценария."""
        rows = await conn.fetch("SELECT action, outcome, detail FROM setup_audit ORDER BY id")
        return JSONResponse({"bot_methods": [name for name, _ in telegram.requests if name != "getUpdates"][-40:],
                             "audit": [dict(r) for r in rows],
                             "llm_requests": len(llm.requests)})

    async def seed_memory(request):
        """Память ассистента: страница об Ольге со сводкой и договорённостью, предложение завести
        страницу о Сергее и его новая договорённость, ждущая решения."""
        from datetime import datetime, timedelta, timezone

        from shturman import authority, bridge, jobs, store
        from shturman.processing import commitments, pages_build, people
        from shturman.records import ChatRecord, MessageRecord

        app = state["app"]
        start = datetime.now(timezone.utc) - timedelta(days=3)
        async with app.pool.acquire() as c:
            account = await c.fetchval("SELECT id FROM accounts WHERE role = 'owner' ORDER BY id LIMIT 1")
            owner_tg = await c.fetchval("SELECT tg_user_id FROM accounts WHERE id = $1", account)
            owner_peer = await store.ensure_peer(c, "user", owner_tg, name="Владелец")
            talk = {
                5001: ("Ольга Смирнова", [
                    (5001, "Добрый день! Проект фасада по корпусу 2 пришлю до пятницы."),
                    (owner_tg, "Хорошо. Бюджет держим в пределах 12 млн."),
                    (5001, "Поняла. Подрядчика по остеклению выберем вместе на следующей неделе."),
                    (5001, "Я теперь руковожу проектным отделом, вопросы по чертежам — ко мне.")]),
                5002: ("Сергей Ковалёв", [
                    (5002, "График поставок бетона пришлю до среды."),
                    (owner_tg, "Жду.")]),
            }
            ids, peers, chats = {}, {}, {}
            for tg_id, (name, lines) in talk.items():
                chat_id, _ = await store.ensure_chat(c, account, ChatRecord("user", tg_id, "personal_chat", name))
                rows = [(chat_id, MessageRecord(
                    tg_message_id=n + 1, sent_at=start + timedelta(hours=n), kind="message", sender_class="user",
                    sender_tg_id=who, sender_name=name if who == tg_id else "Владелец", text=text, entities=None,
                    reply_to_tg_id=None, forwarded_from=None, edited_at=None, media_type=None, media_path=None,
                    service_action=None)) for n, (who, text) in enumerate(lines)]
                await store.upsert_messages(c, rows, source="session", owner_tg_id=owner_tg)
                ids[tg_id] = [r["id"] for r in await c.fetch(
                    "SELECT id FROM messages WHERE chat_id = $1 ORDER BY tg_message_id", chat_id)]
                peers[tg_id] = await c.fetchval("SELECT id FROM peers WHERE class = 'user' AND tg_id = $1", tg_id)
                chats[tg_id] = chat_id
            olga = await people.ensure_person_for_peer(c, peers[5001])
            sergey = await people.ensure_person_for_peer(c, peers[5002])
            await people.confirm_person(c, olga)

            async def promise(tg_id, what, due):
                return await c.fetchval(
                    """INSERT INTO commitments (chat_id, source_message_id, debtor_peer_id, creditor_peer_id, direction,
                                                what, source_quote, due_expression, due_reason)
                       VALUES ($1, $2, $3, $4, 'owed_to_owner', $5, $6, $7, 'no_deadline') RETURNING id""",
                    chats[tg_id], ids[tg_id][0], peers[tg_id], owner_peer, what, talk[tg_id][1][0][1], due)

            open_one = await promise(5001, "прислать проект фасада по корпусу 2", "до пятницы")
            with authority.setup_context("stand", action="stand.seed"):
                await commitments.accept(c, open_one)
            await promise(5002, "прислать график поставок бетона", "до среды")

            # сводку пишет «модель» стенда: задание не должно уйти своей модели сервиса
            kept = set(bridge._builtin_kinds)
            bridge.set_builtin(kept - {bridge.LLM_STRUCTURED, bridge.LLM_TEXT})
            try:
                plan = await pages_build.build(c, app.config.pages_dir, tz=app.config.timezone, trigger="manual")
            finally:
                bridge.set_builtin(kept)
            olga_msgs = ids[5001]
            for job in await jobs.claim(c, [bridge.LLM_STRUCTURED], worker="stand", limit=20):
                await bridge.deliver_result(c, job["id"], {"parsed": {"statements": [
                    {"text": "Руководит проектным отделом; вопросы по чертежам — к ней.", "sources": [olga_msgs[3]],
                     "origin": "other", "contradiction": False},
                    {"text": "Готовит проект фасада по корпусу 2.", "sources": [olga_msgs[0]], "origin": "other",
                     "contradiction": False},
                    {"text": "Бюджет фасада — в пределах 12 млн.", "sources": [olga_msgs[1]], "origin": "owner",
                     "contradiction": False},
                ]}, "text": "", "model": "stand"})
            await pages_build.finish_build(c, app.config.pages_dir, tz=app.config.timezone)
            await c.execute(
                """INSERT INTO page_proposals (person_id, reason) VALUES ($1, '{"messages": 24, "commitments": 0}'::jsonb)
                   ON CONFLICT (person_id) DO NOTHING""", sergey)
        return JSONResponse({"ok": True, "olga": olga, "sergey": sergey, "plan": plan.get("status")})

    async def dashboard_page(request):
        return HTMLResponse(DASHBOARD_PAGE)

    async def dashboard_worker(request):
        return Response(DASHBOARD_WORKER, media_type="text/javascript")

    dashboard = Starlette(routes=[Route("/", dashboard_page), Route("/sw.js", dashboard_worker)])

    control = Starlette(routes=[
        Route("/scan", scan, methods=["POST"]), Route("/start", start, methods=["POST"]),
        Route("/last-code", last_code), Route("/business", business, methods=["POST"]), Route("/seen", seen),
        Route("/chatgpt/authorize", chatgpt_authorize, methods=["POST"]),
        Route("/seed-memory", seed_memory, methods=["POST"])])

    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(gate.inner.router.lifespan_context(gate.inner))
        state["app"] = gate.inner.state.shturman
        manager = state["app"].extras["tg"]
        manager.client_factory = factory
        manager.pacing = 0.0

        servers = [
            uvicorn.Server(uvicorn.Config(gate, host="127.0.0.1", port=PORT, log_level="warning",
                                          access_log=False, proxy_headers=False, server_header=False, lifespan="off")),
            uvicorn.Server(uvicorn.Config(control, host="127.0.0.1", port=PORT + 1, log_level="warning",
                                          access_log=False)),
            uvicorn.Server(uvicorn.Config(dashboard, host="127.0.0.1", port=PORT + 2, log_level="warning",
                                          access_log=False)),
        ]
        print(json.dumps({"ready": True, "url": f"{ORIGIN}/shturman-setup/", "dashboard": DASHBOARD,
                          "control": f"http://127.0.0.1:{PORT + 1}", "data_dir": str(data_dir),
                          "good_token": GOOD_TOKEN, "busy_token": BUSY_TOKEN, "password": PASSWORD,
                          "llm_key": LLM_KEY, "dsn": DSN}), flush=True)
        await asyncio.gather(*(server.serve() for server in servers))
    executor_service.TEST_OVERRIDES.clear()
    await conn.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
