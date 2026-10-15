"""Стенд для браузерной проверки страницы настройки: настоящий сервис, подставные Telegram и модель.

Что настоящее: сервис переписки целиком (все модули, uvicorn, Postgres), страница и её файлы.
Что подставное: Telegram для аккаунтов (подставной клиент из tests/tg), Bot API, сервер модели и
OpenAI для подписки ChatGPT (из tests/executor). В сеть стенд не ходит.

Запуск — см. README.md в этом каталоге. Стенд пересоздаёт схему `public` в базе из
SHTURMAN_TEST_DSN: база должна быть отдельной тестовой.

Рядом с сервисом поднимается «пульт» (порт сервиса + 1) — им сценарий делает то, что в жизни
делает человек в Telegram: сканирует QR, нажимает «Запустить» в боте, подключает бизнес-режим.

Для списка чатов большого аккаунта (Р-81) пульт добавляет тысячи диалогов, задержку страницы и
«подождите» от Telegram на середине загрузки (`POST /dialogs/big`), а `GET /dialogs/seen` говорит, какие
страницы отдал подставной Telegram.

Для сфер и раскладок списка чатов (Р-82) пульт заводит папки Telegram у подставного Telegram, разное
время последнего сообщения и три сферы с чатами (`POST /spheres/seed`), добавляет в папку новый чат
(`POST /spheres/folder-new`) и говорит, что стало со сферами и журналом (`GET /spheres/seen`).

Для экрана «Память ассистента» пульт заводит (`POST /seed-memory`) переписку с двумя людьми и группу,
страницу об одном человеке со сводкой и фактом — ответ «модели» подставляет сам стенд, — проект с
решением и фактами, а ждущими решения — страницу о втором человеке, его договорённость, факт о
владельце и предложенный ассистентом проект.

Для разбора входящих (Р-90) пульт заводит чаты (`POST /triage/seed`), шлёт живые сообщения, на которые
подставная модель отвечает готовым выводом (`POST /triage/live?case=…`), прогоняет обход «через 2 часа»
(`POST /triage/remind`) и говорит, что ушло владельцу (`GET /triage/seen`).

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
from datetime import datetime, timedelta, timezone
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
from shturman.setup_page import service as setup_service  # noqa: E402

# Вход по коду из бота при заданном адресе дашборда принимается только через сокет обратного прокси
# (Р-88, `service._via_proxy`). Стенд слушает порт, а «прокси» для него — сам браузер сценария: считаем
# каждый запрос пришедшим через сокет. Отказ через порт проверяют тесты (`test_code_login.py`).
setup_service._via_proxy = lambda request, state: True

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
        response = await super()._handle(request)
        if request.url.path.endswith("/sendMessage") and response.status_code == 200:
            # номер отправленного сообщения — чтобы пульт нажимал кнопку под нужной карточкой
            sent = json.loads(response.content)["result"]
            for method, params in reversed(self.requests):
                if method == "sendMessage" and params.get("text") == sent.get("text") and "_message_id" not in params:
                    params["_message_id"] = sent["message_id"]
                    break
        return response


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


def normalize_key(entity) -> tuple[str, int]:
    from shturman.tg import normalize
    return normalize.entity_key(entity)


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
            if "Код входа" in text.split("\n")[0]:
                return JSONResponse({"code": "".join(ch for ch in text.split("\n")[0] if ch.isdigit())})
        return JSONResponse({"code": None})

    async def bot_sent(request):
        """Пакет 2: что бот прислал владельцу (текст и кнопки) — для снимков сообщений бота."""
        out = []
        for name, params in telegram.requests:
            if name in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
                out.append({"method": name, "text": params.get("text"), "reply_markup": params.get("reply_markup"),
                            "message_id": params.get("message_id")})
        return JSONResponse(out)

    async def bot_press(request):
        """Владелец нажал кнопку под сообщением бота: ?data=sh:…"""
        telegram.press(request.query_params["data"])
        await asyncio.sleep(0.6)
        return JSONResponse({"ok": True})

    async def bot_reply(request):
        """Владелец ответил на последнее сообщение бота (ForceReply): ?text=…"""
        telegram.push(message={"message_id": 900, "date": int(time.time()), "from": exec_fakes.OWNER_USER,
                               "chat": exec_fakes.private(exec_fakes.OWNER_USER), "text": request.query_params["text"],
                               "reply_to_message": {"message_id": telegram.last_message_id()}})
        await asyncio.sleep(0.6)
        return JSONResponse({"ok": True})

    async def bot_say(request):
        """Владелец написал боту: ?text=…"""
        telegram.text(request.query_params["text"])
        await asyncio.sleep(0.6)
        return JSONResponse({"ok": True})

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
        """Память ассистента: страница об Ольге со сводкой, фактом и договорённостью; проект
        «Береговой» с чатом, решением и фактом. Ждут решения: страница о Сергее, его новая
        договорённость, факт о владельце и предложенный ассистентом проект."""
        from datetime import datetime, timedelta, timezone

        from shturman import authority, bridge, jobs, store
        from shturman.processing import commitments, facts, pages_build, people, projects
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
                -100555: ("Береговой: стройка", [
                    (5001, "Решили: фасад корпуса 2 — керамогранит."),
                    (owner_tg, "Согласен. Сдача корпуса — в марте."),
                    (5001, "Смета по фасаду — 12 млн.")]),
                -100556: ("Склад на Окружной: снабжение", [
                    (5002, "Арматуру на склад привезут в четверг."),
                    (owner_tg, "Хорошо.")]),
            }
            names = {5001: "Ольга Смирнова", 5002: "Сергей Ковалёв"}
            ids, peers, chats = {}, {}, {}
            for tg_id, (name, lines) in talk.items():
                record = (ChatRecord("user", tg_id, "personal_chat", name) if tg_id > 0
                          else ChatRecord("channel", -tg_id, "private_supergroup", name))
                chat_id, _ = await store.ensure_chat(c, account, record)
                rows = [(chat_id, MessageRecord(
                    tg_message_id=n + 1, sent_at=start + timedelta(hours=n), kind="message", sender_class="user",
                    sender_tg_id=who, sender_name=names.get(who, "Владелец"), text=text, entities=None,
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

            async def fact(subject, message_id, text, *, kind="fact", slot=None, status="active", origin="other",
                           person_id=None, project_id=None):
                fact_id = await c.fetchval(
                    """INSERT INTO facts (subject_type, person_id, project_id, kind, slot, text, text_norm, valid_from,
                                          status, origin, source_message_id, source_quote)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $6) RETURNING id""",
                    subject, person_id, project_id, kind, slot, text, facts.text_norm(text), start.date(), status,
                    origin, message_id)
                if status == "active":
                    await facts.rechain(c, subject, person_id, project_id, slot)
                return fact_id

            with authority.setup_context("stand", action="stand.seed"):
                project = (await projects.create_project(
                    c, "ЖК «Береговой»", [chats[-100555]], ["Береговой"]))["project"]["id"]
            await fact("project", ids[-100555][0], "фасад корпуса 2 — керамогранит", kind="decision", project_id=project)
            await fact("project", ids[-100555][2], "12 млн", slot="смета фасада", project_id=project)
            await fact("project", ids[-100555][1], "март", slot="срок сдачи корпуса", origin="owner", project_id=project)
            await fact("person", ids[5001][3], "руководитель проектного отдела", slot="должность", person_id=olga)
            await fact("owner", ids[5001][1], "отвечаю за бюджет фасадов", slot="роль", status="proposed",
                       origin="owner")
            # предложение по групповому чату: по упоминаниям оно снялось бы обходом без записей об упоминаниях
            await projects.create_project(c, "Склад на Окружной", [chats[-100556]], origin="model",
                                          reason={"messages": 24, "chats": [chats[-100556]]})

            # сводку пишет «модель» стенда: задание не должно уйти своей модели сервиса
            kept = set(bridge._builtin_kinds)
            bridge.set_builtin(kept - {bridge.LLM_STRUCTURED, bridge.LLM_TEXT})
            try:
                plan = await pages_build.build(c, app.config.pages_dir, tz=app.config.timezone, trigger="manual")
            finally:
                bridge.set_builtin(kept)
            olga_msgs, site = ids[5001], ids[-100555]
            for job in await jobs.claim(c, [bridge.LLM_STRUCTURED], worker="stand", limit=20):
                context = (await jobs.get(c, job["id"]))["context"] or {}
                offered = {i for i, _ in context.get("offered") or []}
                said = [
                    ("Руководит проектным отделом; вопросы по чертежам — к ней.", olga_msgs[3], "other"),
                    ("Готовит проект фасада по корпусу 2.", olga_msgs[0], "other"),
                    ("Бюджет фасада — в пределах 12 млн.", olga_msgs[1], "owner"),
                    ("Фасад корпуса 2 — керамогранит, смета 12 млн.", site[2], "other"),
                    ("Сдача корпуса 2 — в марте.", site[1], "owner"),
                ]
                await bridge.deliver_result(c, job["id"], {"parsed": {"statements": [
                    {"text": text, "sources": [source], "origin": origin, "contradiction": False}
                    for text, source, origin in said if source in offered]}, "text": "", "model": "stand"})
            await pages_build.finish_build(c, app.config.pages_dir, tz=app.config.timezone)
            await c.execute(
                """INSERT INTO page_proposals (person_id, reason) VALUES ($1, '{"messages": 24, "commitments": 0}'::jsonb)
                   ON CONFLICT (person_id) DO NOTHING""", sergey)
        return JSONResponse({"ok": True, "olga": olga, "sergey": sergey, "project": project, "plan": plan.get("status")})


    # --- большой аккаунт (Р-81): тысячи диалогов, страницы с задержкой, «подождите» на середине ---

    async def dialogs_big(request):
        """У аккаунта стало ещё n диалогов; страница списка «думает» delay секунд, запрос № flood_at
        (от следующего) получает FLOOD_WAIT на seconds секунд. Список в базе — «старый», чтобы «Обновить»
        пошла в Telegram. ?role=owner|assistant&n=4000&delay=0.4&flood_at=20&seconds=25"""
        q = request.query_params
        role = q.get("role", "owner")
        tg_fakes.crowd(worlds[role], int(q.get("n", "4000")), delay=float(q.get("delay", "0.4")),
                       flood_at=int(q.get("flood_at", "0")), seconds=int(q.get("seconds", "25")))
        await conn.execute("UPDATE tg_dialog_lists SET completed_at = completed_at - interval '1 hour'")
        return JSONResponse({"ok": True, "dialogs": len(worlds[role].dialogs)})

    async def dialogs_seen(request):
        """Сколько раз спрашивали список, с какого места отдана каждая страница, сколько в базе."""
        role = request.query_params.get("role", "owner")
        world = worlds[role]
        return JSONResponse({"calls": world.dialog_calls, "pages": world.dialog_pages, "dialogs": len(world.dialogs),
                             "stored": await conn.fetchval("SELECT count(*) FROM tg_dialogs")})

    # --- сферы и папки Telegram (Р-82) ---

    async def spheres_seed(request):
        """Папки Telegram у подставного Telegram, разное время последнего сообщения (для раскладки «по
        активности»), три сферы с чатами и сфера у проекта; список перечитывается из «Telegram» — с
        папками. ?role=owner|assistant"""
        from shturman import spheres
        from shturman.tg import dialogs as dl

        role = request.query_params.get("role", "owner")
        world = worlds[role]
        world.dialog_delay, world.dialog_fail = 0.0, {}
        groups = [e for e in world.dialogs if getattr(e, "id", 0) in range(30_000, 30_035)]
        channels = [e for e in world.dialogs if getattr(e, "id", 0) in range(40_000, 40_025)]
        people = [e for e in world.dialogs if getattr(e, "id", 0) in range(20_000, 20_090)]
        world.folders = [
            tg_fakes.folder(2, "Работа", emoticon="💼", pinned=[tg_fakes.U_IVAN],
                            include=[tg_fakes.C_SUPER, *people[:24], *groups[:12]]),
            tg_fakes.folder(3, "Объекты", emoticon="🏗", groups=True, exclude_muted=True, exclude=[tg_fakes.G_FAMILY]),
            tg_fakes.folder(4, "Новости", emoticon="📰", broadcasts=True),
            tg_fakes.folder(5, "Семья", emoticon="🏠", include=[tg_fakes.U_MARIA, tg_fakes.G_FAMILY]),
        ]
        world.muted = {("chat", g.id) for g in groups[30:]}
        world.unread = {("user", p.id) for p in people[:6]}
        # время последнего сообщения убывает по списку, как у Telegram: свежие чаты наверху, тысячи старых внизу
        now = datetime.now(timezone.utc)

        def ago(i: int) -> timedelta:
            if i < 8:
                return timedelta(hours=i + 1)
            if i < 40:
                return timedelta(days=1, hours=4 * (i - 8))
            if i < 90:
                return timedelta(days=8, hours=8 * (i - 40))
            if i < 160:
                return timedelta(days=31, hours=16 * (i - 90))
            return timedelta(days=100, hours=2 * (i - 160))

        world.dates = {normalize_key(e): now - ago(i) for i, e in enumerate(world.dialogs)}
        rt = state["app"].extras["tg"].runtimes[role]
        await conn.execute("UPDATE tg_dialog_lists SET completed_at = completed_at - interval '1 hour'")
        if not rt.dialogs_load.running:
            state["app"].extras["tg"].start_dialogs(rt)
        await asyncio.wait_for(rt.dialogs_load.done.wait(), 120)
        app = state["app"]
        async with app.pool.acquire() as c:
            account = rt.account_id
            work = await spheres.create_sphere(c, "Работа РАЗУМ", "стройка, продажи, совещания")
            home = await spheres.create_sphere(c, "Личное", "семья, дом, дача")
            teach = await spheres.create_sphere(c, "Преподавание", "ММА, магистратура 09.04.03")
            await spheres.add_chats(c, work["id"], account, [("user", tg_fakes.IVAN), ("channel", tg_fakes.C_SUPER.id),
                                                             *[("chat", g.id) for g in groups[:6]]])
            await spheres.add_chats(c, home["id"], account, [("user", tg_fakes.MARIA), ("chat", tg_fakes.G_FAMILY.id),
                                                             ("user", tg_fakes.IVAN)])
            await spheres.add_chats(c, teach["id"], account, [("user", p.id) for p in people[60:66]])
            project = await c.fetchval("SELECT id FROM projects WHERE status = 'active' ORDER BY id LIMIT 1")
            if project is not None:
                await spheres.set_project_sphere(c, project, work["id"])
            stored = await c.fetchval("SELECT count(*) FROM tg_dialogs WHERE account_id = $1", account)
            folders = len(await dl.folders(c, account))
        return JSONResponse({"ok": True, "dialogs": stored, "folders": folders,
                             "spheres": [work["id"], home["id"], teach["id"]]})

    async def spheres_folder_new(request):
        """В папке «Работа» появился новый чат (вверху списка) — список перечитывается из «Telegram»."""
        role = request.query_params.get("role", "owner")
        world = worlds[role]
        newcomer = tg_fakes.user(29_001, "Новый", "Заказчик", contact=True)
        if newcomer.id not in {getattr(e, "id", 0) for e in world.dialogs}:
            world.dialogs.insert(0, newcomer)
            world.dates[("user", newcomer.id)] = datetime.now(timezone.utc) - timedelta(minutes=5)
            old = world.folders[0]
            world.folders[0] = tg_fakes.types.DialogFilter(
                id=old.id, title=old.title, emoticon=old.emoticon, pinned_peers=old.pinned_peers,
                include_peers=[*old.include_peers, tg_fakes.input_peer(newcomer)], exclude_peers=old.exclude_peers)
        rt = state["app"].extras["tg"].runtimes[role]
        await conn.execute("UPDATE tg_dialog_lists SET completed_at = completed_at - interval '1 hour'")
        if not rt.dialogs_load.running:
            state["app"].extras["tg"].start_dialogs(rt)
        await asyncio.wait_for(rt.dialogs_load.done.wait(), 120)
        return JSONResponse({"ok": True})

    async def spheres_seen(request):
        """Сферы, чаты в них и журнал «sphere.*», «tg.bulk_*», «app.*» — для проверок сценария."""
        rows = await conn.fetch("SELECT id, title FROM spheres ORDER BY position, id")
        counts = {r["sphere_id"]: r["n"] for r in await conn.fetch(
            "SELECT sphere_id, count(*) AS n FROM sphere_chats GROUP BY sphere_id")}
        audit = await conn.fetch(
            """SELECT action, outcome, detail FROM setup_audit
               WHERE action LIKE 'sphere.%' OR action LIKE 'tg.bulk%' OR action LIKE 'app.sphere%' OR action LIKE 'app.bulk%'
               ORDER BY id""")
        return JSONResponse({"spheres": [{"id": r["id"], "title": r["title"], "chats": counts.get(r["id"], 0)} for r in rows],
                             "enabled": await conn.fetchval("SELECT count(*) FROM tg_sync_chats WHERE enabled"),
                             "audit": [dict(r) for r in audit]})

    # --- уведомления, наблюдатель, повтор после сбоя (Р-84) ---

    async def notify_seed(request):
        """Личный чат «Анна Ким» и группа «Стройка ЖК: прораб» у основного аккаунта; живые сообщения —
        личное и обращение к владельцу разметкой упоминания — уходят уведомлениями через бота стенда.
        Для повтора: два фото «не получилось» (сбой модели и сам файл) и запрос разбора переписки, от
        которого прогоны отказались."""
        from shturman import bridge, events, jobs, store
        from shturman.records import ChatRecord, MessageRecord

        app = state["app"]
        now = datetime.now(timezone.utc)
        async with app.pool.acquire() as c:
            account = await c.fetchval("SELECT id FROM accounts WHERE role = 'owner' ORDER BY id LIMIT 1")
            owner_tg = await c.fetchval("SELECT tg_user_id FROM accounts WHERE id = $1", account)
            anna, _ = await store.ensure_chat(c, account, ChatRecord("user", 5101, "personal_chat", "Анна Ким",
                                                                     username="anna_kim_e2e"))
            site, _ = await store.ensure_chat(c, account, ChatRecord("channel", 5201, "private_supergroup",
                                                                     "Стройка ЖК: прораб"))

            def rec(n, at, sender, name, text, entities=None, media=None):
                return MessageRecord(tg_message_id=n, sent_at=at, kind="message", sender_class="user",
                                     sender_tg_id=sender, sender_name=name, text=text, entities=None,
                                     reply_to_tg_id=None, forwarded_from=None, edited_at=None, media_type=media,
                                     media_path=None, service_action=None, telegram_entities=entities,
                                     is_forwarded=False, telegram_via_bot=False, telegram_sender_bot=False)

            mention = "Евгений"
            group_text = f"{mention}, посмотрите акт по фасаду — нужна подпись до пятницы"
            ents = [{"type": "mention_name", "offset": 0, "length": len(mention), "text": mention, "user_id": owner_tg}]
            await store.upsert_messages(c, [
                (anna, rec(1, now - timedelta(minutes=1), 5101, "Анна Ким", "Добрый день! Когда сможете посмотреть смету по корпусу 2?"), False),
                (anna, rec(2, now - timedelta(days=2), 5101, "Анна Ким", "", media="photo"), False),
                (anna, rec(3, now - timedelta(days=2), 5101, "Анна Ким", "", media="photo"), False),
                (site, rec(1, now - timedelta(seconds=30), 5202, "Пётр Сидоров", group_text, ents), False),
                (site, rec(2, now - timedelta(seconds=20), 5203, "Олег", "Срочно нужен аванс по договору поставки", []), False),
            ], source="session", owner_tg_id=owner_tg)
            ids = {(r["chat_id"], r["tg_message_id"]): r["id"] for r in await c.fetch(
                "SELECT id, chat_id, tg_message_id FROM messages WHERE chat_id = ANY($1::bigint[])", [anna, site])}
            await c.execute("UPDATE messages SET media_state = 'failed', media_error = 'model:плагин не ответил', "
                            "media_attempts = 4, media_at = now() - interval '1 day' WHERE id = $1", ids[(anna, 2)])
            await c.execute("UPDATE messages SET media_state = 'skipped', media_error = 'unsupported', "
                            "media_at = now() WHERE id = $1", ids[(anna, 3)])
            await c.execute("""INSERT INTO setup_state (key, value) VALUES ('media', '{"enabled": true}'::jsonb)
                               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""")
            run = await c.fetchval("INSERT INTO processing_runs (trigger, status) VALUES ('nightly', 'done') RETURNING id")
            job = await jobs.enqueue(c, bridge.LLM_STRUCTURED, {}, handler="commitments.extract", executor="plugin")
            await c.execute("UPDATE jobs SET status = 'failed', finished_at = now() WHERE id = $1", job)
            await c.execute("""INSERT INTO processing_requests (job_id, run_id, kind, chat_id, message_ids, attempt, state)
                               VALUES ($1, $2, 'extract', $3, $4::bigint[], 2, 'given_up')""",
                            job, run, anna, [ids[(anna, 1)]])
        for chat_id, n in ((anna, 1), (site, 1)):
            app.events.publish(events.MESSAGE_LIVE, {"account_id": account, "chat_id": chat_id,
                                                      "message_id": ids[(chat_id, n)], "source": "session",
                                                      "outgoing": False, "edited": False, "via_bot": False})
        await app.events.drain()
        state["watch_message"] = (account, site, ids[(site, 2)])
        return JSONResponse({"ok": True, "anna": anna, "site": site})

    async def notify_watch_live(request):
        """Сообщение «Срочно нужен аванс…» в группе пришло живым — для правил наблюдателя."""
        from shturman import events
        account, site, message_id = state["watch_message"]
        state["app"].events.publish(events.MESSAGE_LIVE, {"account_id": account, "chat_id": site,
                                                          "message_id": message_id, "source": "session",
                                                          "outgoing": False, "edited": False, "via_bot": False})
        await state["app"].events.drain()
        return JSONResponse({"ok": True})

    async def notify_seen(request):
        """Что ушло владельцу в бота, что в настройках, правилах, очередях и журнале — для проверок."""
        sent = [p.get("text", "") for p in telegram.calls("sendMessage")
                if str(p.get("text", "")).startswith(("💬", "📣", "🔔"))]
        rows = await conn.fetch(
            """SELECT action, outcome, detail FROM setup_audit
               WHERE action LIKE 'notify.%' OR action LIKE 'watch.%' OR action = 'retry.failed' OR action = 'app.notify'
               ORDER BY id""")
        return JSONResponse({
            "sent": sent,
            "settings": json.loads(await conn.fetchval("SELECT value::text FROM settings WHERE key = 'notify.settings'")
                                   or "null"),
            "rules": [dict(r) for r in await conn.fetch(
                "SELECT id, name, enabled, chat_ids, sphere_ids, keywords FROM watch_rules ORDER BY id")],
            "hits": await conn.fetchval("SELECT count(*) FROM watch_hits"),
            "media": [dict(r) for r in await conn.fetch(
                "SELECT media_state, media_error FROM messages WHERE media_state IS NOT NULL ORDER BY id")],
            "requests": [r["state"] for r in await conn.fetch("SELECT state FROM processing_requests ORDER BY job_id")],
            "audit": [dict(r) for r in rows]})

    # --- репетиция ответов (Р-85) ---

    async def rehearsal_live(request):
        """Анна Ким (из `/notify/seed`) пишет в личку живое сообщение; подставная модель ответит так, как
        ответила бы на задачу ответа. Репетиция готовит карточку через бота стенда."""
        from shturman import events, store
        from shturman.records import MessageRecord

        app = state["app"]
        reply = json.dumps({"outcome": "reply", "text": "Добрый вечер! Да, в 10 буду на объекте, смету возьму с собой."},
                           ensure_ascii=False)

        # Ответ — только на запрос репетиции (в нём есть сообщение Анны); остальные запросы модели стенда
        # получают обычный ответ. Своя модель стенда к этому месту — подписка ChatGPT или ключ API: заготовка
        # кладётся обоим.
        def for_rehearsal(queue, make):
            def answer(body):
                if "Завтра в 10 на объекте" in json.dumps(body, ensure_ascii=False):
                    return make(body)
                queue.insert(0, answer)          # не тот запрос — ждём своего
                return None
            return answer

        llm.script.append(for_rehearsal(llm.script, lambda body: exec_fakes.FakeLlm.completion(reply, body["model"])))
        openai.responses_script.append(for_rehearsal(openai.responses_script,
                                                     lambda body: openai_fakes.completed(reply, body["model"])))
        async with app.pool.acquire() as c:
            account = await c.fetchval("SELECT id FROM accounts WHERE role = 'owner' ORDER BY id LIMIT 1")
            owner_tg = await c.fetchval("SELECT tg_user_id FROM accounts WHERE id = $1", account)
            anna = await c.fetchval(
                "SELECT c.id FROM chats c JOIN peers p ON p.id = c.peer_id WHERE c.account_id = $1 AND p.tg_id = 5101",
                account)
            number = 100 + int(await c.fetchval("SELECT count(*) FROM rehearsal_items"))
            record = MessageRecord(
                tg_message_id=number, sent_at=datetime.now(timezone.utc), kind="message", sender_class="user",
                sender_tg_id=5101, sender_name="Анна Ким", text="Добрый вечер! Завтра в 10 на объекте получится?",
                entities=None, reply_to_tg_id=None, forwarded_from=None, edited_at=None, media_type=None,
                media_path=None, service_action=None, telegram_entities=[], is_forwarded=False,
                telegram_via_bot=False, telegram_sender_bot=False)
            result = await store.upsert_messages(c, [(anna, record, False)], source="session", owner_tg_id=owner_tg)
            message_id = (result.new_ids or result.known_ids)[0]
        app.events.publish(events.MESSAGE_LIVE, {"account_id": account, "chat_id": anna, "message_id": message_id,
                                                  "source": "session", "outgoing": False, "edited": False,
                                                  "via_bot": False})
        await app.events.drain()
        return JSONResponse({"ok": True, "message_id": message_id})

    def rehearsal_cards() -> list[dict]:
        return [p for p in telegram.calls("sendMessage") if str(p.get("text", "")).startswith("🎭")]

    async def rehearsal_press(request):
        """Владелец нажимает оценку под последней карточкой репетиции в Telegram."""
        label = {"good": "Годится", "almost": "Почти — поправил бы", "bad": "Не годится"}[request.query_params["rating"]]
        card = rehearsal_cards()[-1]
        buttons = exec_fakes.buttons_of(card)
        telegram.press(buttons[label], user=exec_fakes.OWNER_USER, message_id=card["_message_id"],
                       markup=card["reply_markup"])
        return JSONResponse({"ok": True})

    async def rehearsal_seen(request):
        """Карточки, ушедшие владельцу, правки кнопок, настройки, строки репетиции, журнал — для проверок.
        И главное: ни одной отправки собеседнику."""
        rows = await conn.fetch(
            "SELECT action, outcome, detail FROM setup_audit WHERE action LIKE 'rehearsal.%' ORDER BY id")
        return JSONResponse({
            "cards": [{"text": p["text"], "buttons": [[b["text"] for b in row] for row in
                                                      (p.get("reply_markup") or {}).get("inline_keyboard", [])],
                       "silent": bool(p.get("disable_notification"))} for p in rehearsal_cards()],
            "marked": [[b["text"] for row in p["reply_markup"]["inline_keyboard"] for b in row]
                       for p in telegram.calls("editMessageReplyMarkup") if p["reply_markup"].get("inline_keyboard")],
            "notified": [p.get("text", "") for p in telegram.calls("sendMessage")
                         if str(p.get("text", "")).startswith(("💬", "🔔"))],
            "business_sent": len(telegram.sent(business=True)),
            "drafts": await conn.fetchval("SELECT count(*) FROM outbox_drafts"),
            "tasks": await conn.fetchval("SELECT count(*) FROM reply_tasks"),
            "settings": json.loads(await conn.fetchval("SELECT value::text FROM settings WHERE key = 'rehearsal.settings'")
                                   or "null"),
            "items": [dict(r) for r in await conn.fetch(
                "SELECT state, reason, outcome, rating, owner_variant IS NOT NULL AS variant "
                "FROM rehearsal_items ORDER BY id")],
            "audit": [dict(r) for r in rows]})

    # --- разбор входящих (Р-90) ---

    TRIAGE_CASES = {
        # случай: (кто пишет, имя, вид чата, текст, ответ подставной модели)
        "urgent": (5301, "Петров", "personal",
                   "Евгений Сергеевич, бетон не пришёл, машина стоит у ворот, без вас не решить — ответьте до 14:00",
                   {"needs_reply": True, "urgency": "urgent", "want": "бетон не пришёл, просит решение до 14:00",
                    "due": "до 14:00", "thread": ""}),
        "question": (5302, "Иван Смирнов", "personal", "Добрый день! Пришлёте смету по корпусу 3?",
                     {"needs_reply": True, "urgency": "normal", "want": "просит смету по корпусу 3", "due": "",
                      "thread": ""}),
        "thanks": (5302, "Иван Смирнов", "personal", "Спасибо, получил!",
                   {"needs_reply": False, "urgency": "normal", "want": "благодарит", "due": "", "thread": ""}),
        "group": (5303, "Пётр Сидоров", "group", "Евгений, переносим заливку на завтра или ждём бетон?",
                  {"needs_reply": True, "urgency": "important", "want": "переносить ли заливку на завтра",
                   "due": "", "thread": "поставка бетона задерживается, бригада ждёт решения о заливке"}),
    }

    async def triage_live(request):
        """Живое сообщение для разбора входящих: ?case=urgent|question|thanks|group. Подставная модель
        отвечает на запрос разбора готовым выводом; ожидание пачки на стенде нулевое."""
        from shturman import events, store
        from shturman.records import ChatRecord, MessageRecord

        app = state["app"]
        app.extras["triage"].wait = 0
        case = request.query_params["case"]
        sender, name, kind, text, verdict = TRIAGE_CASES[case]
        reply = json.dumps(verdict, ensure_ascii=False)

        def for_triage(queue, make):
            def answer(body):
                dumped = json.dumps(body, ensure_ascii=False)
                if text[:25] in dumped and "inbox_triage" in dumped:
                    return make(body)
                queue.insert(0, answer)          # не тот запрос — ждём своего
                return None
            return answer

        llm.script.append(for_triage(llm.script, lambda body: exec_fakes.FakeLlm.completion(reply, body["model"])))
        openai.responses_script.append(for_triage(openai.responses_script,
                                                  lambda body: openai_fakes.completed(reply, body["model"])))
        async with app.pool.acquire() as c:
            account = await c.fetchval("SELECT id FROM accounts WHERE role = 'owner' ORDER BY id LIMIT 1")
            owner_tg = await c.fetchval("SELECT tg_user_id FROM accounts WHERE id = $1", account)
            if kind == "personal":
                chat_id, _ = await store.ensure_chat(c, account, ChatRecord("user", sender, "personal_chat", name))
                entities = []
            else:
                chat_id, _ = await store.ensure_chat(c, account, ChatRecord("channel", 5401, "private_supergroup",
                                                                             "Корпус 3"))
                entities = [{"type": "mention_name", "offset": 0, "length": 7, "text": "Евгений", "user_id": owner_tg}]
            number = 300 + int(await c.fetchval("SELECT count(*) FROM messages WHERE chat_id = $1", chat_id))
            record = MessageRecord(
                tg_message_id=number, sent_at=datetime.now(timezone.utc), kind="message", sender_class="user",
                sender_tg_id=sender, sender_name=name, text=text, entities=None, reply_to_tg_id=None,
                forwarded_from=None, edited_at=None, media_type=None, media_path=None, service_action=None,
                telegram_entities=entities, is_forwarded=False, telegram_via_bot=False, telegram_sender_bot=False)
            result = await store.upsert_messages(c, [(chat_id, record, False)], source="session", owner_tg_id=owner_tg)
            message_id = (result.new_ids or result.known_ids)[0]
        app.events.publish(events.MESSAGE_LIVE, {"account_id": account, "chat_id": chat_id, "message_id": message_id,
                                                  "source": "session", "outgoing": False, "edited": False,
                                                  "via_bot": False})
        await app.events.drain()
        return JSONResponse({"ok": True, "message_id": message_id})

    async def triage_seed(request):
        """Личные чаты «Петров» и «Иван Смирнов» и группа «Корпус 3» у основного аккаунта — без сообщений:
        чтобы владелец мог найти их в правилах разбора до первого сообщения."""
        from shturman import store
        from shturman.records import ChatRecord

        async with state["app"].pool.acquire() as c:
            account = await c.fetchval("SELECT id FROM accounts WHERE role = 'owner' ORDER BY id LIMIT 1")
            for sender, name in ((5301, "Петров"), (5302, "Иван Смирнов")):
                await store.ensure_chat(c, account, ChatRecord("user", sender, "personal_chat", name))
            await store.ensure_chat(c, account, ChatRecord("channel", 5401, "private_supergroup", "Корпус 3"))
        return JSONResponse({"ok": True})

    async def triage_remind(request):
        """Прошло больше двух часов рабочего времени: обход разбора с часами «через 2 ч 1 мин»."""
        app = state["app"]
        out = await app.extras["triage"].sweep(datetime.now(timezone.utc) + timedelta(minutes=121))
        return JSONResponse(out)

    TRIAGE_HEADS = ("🔴", "🟠", "💬 Ждёт", "📣 В ", "⏰")

    async def triage_seen(request):
        """Карточки разбора, ушедшие владельцу, прежние уведомления, настройки, строки разбора, журнал."""
        cards = [p for p in telegram.calls("sendMessage") if str(p.get("text", "")).startswith(TRIAGE_HEADS)]
        rows = await conn.fetch("SELECT action, outcome, detail FROM setup_audit WHERE action LIKE 'triage.%' ORDER BY id")
        return JSONResponse({
            "cards": [{"text": p["text"], "buttons": [[b["text"] for b in row] for row in
                                                      (p.get("reply_markup") or {}).get("inline_keyboard", [])],
                       "silent": bool(p.get("disable_notification"))} for p in cards],
            "notified": [p.get("text", "") for p in telegram.calls("sendMessage")
                         if str(p.get("text", "")).startswith(("💬 Вам написали", "📣 Вас упомянули", "🔔"))],
            "settings": json.loads(await conn.fetchval("SELECT value::text FROM settings WHERE key = 'triage.settings'")
                                   or "null"),
            "items": [dict(r) for r in await conn.fetch(
                "SELECT kind, state, urgency, needs_reply, show_state, wait_state FROM triage_items ORDER BY id")],
            "audit": [dict(r) for r in rows]})

    async def dashboard_page(request):
        return HTMLResponse(DASHBOARD_PAGE)

    async def dashboard_worker(request):
        return Response(DASHBOARD_WORKER, media_type="text/javascript")

    dashboard = Starlette(routes=[Route("/", dashboard_page), Route("/sw.js", dashboard_worker)])

    control = Starlette(routes=[
        Route("/scan", scan, methods=["POST"]), Route("/start", start, methods=["POST"]),
        Route("/last-code", last_code), Route("/business", business, methods=["POST"]), Route("/seen", seen),
        Route("/bot/sent", bot_sent), Route("/bot/press", bot_press, methods=["POST"]),
        Route("/bot/say", bot_say, methods=["POST"]), Route("/bot/reply", bot_reply, methods=["POST"]),
        Route("/chatgpt/authorize", chatgpt_authorize, methods=["POST"]),
        Route("/seed-memory", seed_memory, methods=["POST"]),
        Route("/dialogs/big", dialogs_big, methods=["POST"]), Route("/dialogs/seen", dialogs_seen),
        Route("/spheres/seed", spheres_seed, methods=["POST"]),
        Route("/spheres/folder-new", spheres_folder_new, methods=["POST"]), Route("/spheres/seen", spheres_seen),
        Route("/notify/seed", notify_seed, methods=["POST"]),
        Route("/notify/watch-live", notify_watch_live, methods=["POST"]), Route("/notify/seen", notify_seen),
        Route("/rehearsal/live", rehearsal_live, methods=["POST"]),
        Route("/rehearsal/press", rehearsal_press, methods=["POST"]), Route("/rehearsal/seen", rehearsal_seen),
        Route("/triage/seed", triage_seed, methods=["POST"]), Route("/triage/live", triage_live, methods=["POST"]),
        Route("/triage/remind", triage_remind, methods=["POST"]), Route("/triage/seen", triage_seen)])

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
