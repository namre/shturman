"""Сквозная проверка плагина против НАСТОЯЩЕГО сервиса переписки.

Настоящие: сервис (отдельный процесс, Postgres), код плагина, python-telegram-bot, клиент сервиса.
Подставные: Telegram (маленький HTTP-сервер вместо api.telegram.org) и модель (вместо ctx.llm).

Запуск (интерпретатором, в котором установлен Hermes):

    SHTURMAN_TEST_DSN=postgresql://postgres@127.0.0.1:5432/shturman_test \
    E2E_SERVICE_PYTHON=/путь/к/окружению/сервиса/bin/python \
    python bridge_service.py

Схема `public` в базе из SHTURMAN_TEST_DSN ПЕРЕСОЗДАЁТСЯ: база должна быть отдельной тестовой.
Печатает строки «ok»/«FAIL» и завершается с кодом 1, если есть «FAIL».
"""
import asyncio
import importlib.util
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SERVICE_PYTHON = os.environ.get("E2E_SERVICE_PYTHON", "")
DSN = os.environ.get("SHTURMAN_TEST_DSN", "")
PLUGIN = ROOT / "plugins" / "shturman"
sys.path.insert(0, str(PLUGIN))

OWNER = {"id": 42, "is_bot": False, "first_name": "Иван", "last_name": "Иванов", "username": "ivan"}
PETR = {"id": 99, "is_bot": False, "first_name": "Пётр", "last_name": "Петров"}
BOT_USER = {"id": 1234567890, "is_bot": True, "first_name": "Штурман", "username": "shturman_bot"}
BOT_TOKEN = "1234567890:" + "A" * 35
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  ok   " if ok else "  FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.next_id = 7000
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                method = self.path.rsplit("/", 1)[-1]
                params = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
                outer.calls.append((method, params))
                result = True
                if method == "getMe":
                    result = BOT_USER
                elif method in ("sendMessage", "editMessageText"):
                    if method == "sendMessage":
                        outer.next_id += 1
                    result = {"message_id": int(params.get("message_id") or outer.next_id), "date": int(time.time()),
                              "chat": {"id": int(params["chat_id"]), "type": "private"}, "text": params.get("text", "")}
                elif method == "getBusinessConnection":
                    result = {"id": params["business_connection_id"], "user": OWNER, "user_chat_id": 42,
                              "date": int(time.time()), "is_enabled": True, "rights": {"can_reply": True}}
                data = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def of(self, method):
        return [p for m, p in self.calls if m == method]


class FakeLlm:
    """Вместо ctx.llm: на разбор переписки возвращает одно обязательство с дословной цитатой."""
    def __init__(self):
        self.calls = []

    async def acomplete_structured(self, *, instructions, input, json_schema=None, schema_name=None, task=None, **kw):
        text = input[0]["text"]
        self.calls.append(("structured", schema_name, task, kw.get("max_tokens")))
        if schema_name and "commitment" in schema_name and "update" not in schema_name:
            found = re.search(r"\[(\d+)\][^\n]*Пришлю смету в пятницу", text)
            items = []
            if found:
                n = int(found.group(1))
                items.append({"message": n, "source_quote": "Пришлю смету в пятницу", "what": "прислать смету",
                              "due_expression": "в пятницу", "due_message": n, "recipient": None, "duplicate_of": None})
            parsed = {"commitments": items}
        else:
            parsed = {"updates": []}
        return types.SimpleNamespace(parsed=parsed, text=json.dumps(parsed, ensure_ascii=False), model="fake-cheap")

    async def acomplete(self, messages, **kw):
        self.calls.append(("text", None, kw.get("task"), kw.get("max_tokens")))
        return types.SimpleNamespace(text="Спасибо!", model="fake-cheap")


def main():
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(name, None)       # всё в этой проверке — на локальном адресе
    if not DSN or not SERVICE_PYTHON:
        print("нужны SHTURMAN_TEST_DSN (отдельная тестовая база) и E2E_SERVICE_PYTHON", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="shturman-e2e-"))
    api_token, mcp_token = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    port = free_port()
    env = dict(os.environ,
               PYTHONPATH=str(ROOT / "service" / "src"),
               SHTURMAN_DSN=DSN,
               SHTURMAN_API_TOKEN=api_token, SHTURMAN_MCP_TOKEN=mcp_token,
               SHTURMAN_DATA_DIR=str(work / "data"), SHTURMAN_PORT=str(port),
               SHTURMAN_ALLOWED_HOSTS=f"127.0.0.1:{port}", SHTURMAN_TIMEZONE="Europe/Moscow",
               SHTURMAN_SENDING="on")       # в сервисе отправка по умолчанию выключена; проверке она нужна
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(name, None)
    # Начинаем с пустой схемы — так же, как тесты самого сервиса.
    subprocess.run([SERVICE_PYTHON, "-c", (
        "import asyncio, asyncpg, os\n"
        "async def go():\n"
        "    conn = await asyncpg.connect(os.environ['SHTURMAN_DSN'])\n"
        "    await conn.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')\n"
        "    await conn.close()\n"
        "asyncio.run(go())\n")], check=True, env=env)
    log = open(work / "service.log", "wb")
    service = subprocess.Popen([SERVICE_PYTHON, "-m", "shturman.cli", "serve"], env=env, cwd=str(ROOT / "service"),
                               stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def raw(method, path, body=None):
        """Прямой запрос к сервису — только для подготовки и проверки, не путь плагина."""
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        for _ in range(150):
            if service.poll() is not None:
                break
            try:
                with opener.open(base + "/health", timeout=1) as r:
                    health = json.loads(r.read())
                    break
            except Exception:
                time.sleep(0.2)
        else:
            health = None
        if service.poll() is not None or not health:
            log.flush()
            print("сервис не поднялся; хвост журнала:\n" + (work / "service.log").read_text()[-3000:])
            return 2
        print(f"сервис поднят: {health}, код из {ROOT / 'service' / 'src'}")

        os.environ["SHTURMAN_SERVICE_URL"] = base
        os.environ["SHTURMAN_API_TOKEN"] = api_token
        os.environ["SHTURMAN_STATE_DIR"] = str(work / "state")
        return asyncio.run(scenario(raw, base, api_token, work))
    finally:
        service.terminate()
        try:
            service.wait(timeout=15)
        except subprocess.TimeoutExpired:
            service.kill()
        log.close()
        text = (work / "service.log").read_text(errors="replace")
        print("--- журнал сервиса: строк", text.count("\n"), "| ERROR:", text.count("ERROR"), "| Traceback:", text.count("Traceback"))
        for secret, label in ((os.environ.get("SHTURMAN_API_TOKEN", "-"), "токен API"), ("Пришлю смету в пятницу", "текст сообщения")):
            print(f"    {label} в журнале сервиса: {'ДА' if secret in text else 'нет'}")
        if "Traceback" in text:
            print(text[-2500:])


async def scenario(raw, base, api_token, work):
    import telegram
    from telegram.ext import Application

    import shturman_bridge
    import shturman_telegram
    import shturman_tools
    from shturman_core import service_routes
    from shturman_core.service_client import ServiceClient
    from shturman_core.state import Store

    store = Store()
    tg = FakeTelegram()
    llm = FakeLlm()
    ui = ServiceClient(base, api_token, allow=service_routes.UI)

    async def wait(cond, timeout=20.0, step=0.05):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = cond()
            if value:
                return value
            await asyncio.sleep(step)
        return cond()

    async def ui_get(path, **query):
        return await asyncio.to_thread(ui.request, "GET", path, query=query or None)

    runtime = shturman_bridge.runtime()
    runtime.configure(types.SimpleNamespace(llm=llm))
    application = (Application.builder().token(BOT_TOKEN).base_url(tg.url + "/bot").updater(None)
                   .read_timeout(5).connect_timeout(5).build())
    shturman_telegram.wire(application, None)       # как Hermes: фабрика вызвана из корутины, до initialize()
    print("фабрика обработчиков вызвана до initialize(): задачи моста —",
          sorted(t.get_name() for t in asyncio.all_tasks() if t.get_name().startswith("shturman:")))
    await application.initialize()
    await application.start()

    async def feed(payload):
        await application.process_update(telegram.Update.de_json(payload, application.bot))

    def now():
        return int(time.time())

    def biz(text, user, mid, connection="bc1", **extra):
        return {"message_id": mid, "date": now(), "chat": {"id": PETR["id"], "type": "private",
                "first_name": "Пётр", "last_name": "Петров"}, "from": user, "text": text,
                "business_connection_id": connection, **extra}

    try:
        print("\n1. Владелец ещё не привязан")
        await feed({"update_id": 1, "business_connection": {"id": "bc1", "user": OWNER, "user_chat_id": 42,
                    "date": now(), "is_enabled": True, "rights": {"can_reply": True}}})
        await wait(lambda: runtime.stats.counters["rejected"] >= 1, 5)
        status = await ui_get("/api/status")
        check("подключение без привязанного владельца не принято", status["accounts"] == 0 and not status["owner_known"],
              f"accounts={status['accounts']} rejected={runtime.stats.counters['rejected']}")

        print("\n2. Владелец привязан в мастере (файл состояния) — шлюз сообщает сервису")
        store.write("owner", {"user_id": 42, "chat_id": 42, "name": "Иван"})
        ok = await wait(lambda: raw("GET", "/api/status")[1].get("owner_known"), 15)
        check("владелец передан сервису после привязки", ok)

        print("\n3. Бизнес-сообщение: подключение сервису неизвестно — плагин спрашивает Telegram и повторяет")
        await feed({"update_id": 2, "business_message": biz("Пришлю смету в пятницу", PETR, 101)})
        await wait(lambda: runtime.stats.counters["forwarded_messages"] >= 1)
        status = await ui_get("/api/status")
        check("сообщение собеседника в архиве", status["messages"] == 1 and status["accounts"] == 1, f"messages={status['messages']}")
        check("подключение запрошено у Telegram один раз", len(tg.of("getBusinessConnection")) == 1)
        await feed({"update_id": 3, "business_message": biz("Хорошо, жду", OWNER, 102)})
        await feed({"update_id": 4, "edited_business_message": biz("Пришлю смету в пятницу до обеда", PETR, 101, edit_date=now())})
        await feed({"update_id": 5, "business_message": biz("лишнее", PETR, 103)})
        await wait(lambda: runtime.stats.counters["forwarded_messages"] >= 4)
        await feed({"update_id": 6, "deleted_business_messages": {"business_connection_id": "bc1",
                    "chat": {"id": PETR["id"], "type": "private", "first_name": "Пётр"}, "message_ids": [103]}})
        await wait(lambda: runtime.stats.counters["forwarded_deleted"] >= 1)
        chats = (await ui_get("/api/chats"))["chats"]
        check("чат виден в списке чатов: 3 сообщения (одно изменено, одно удалено — помечено, не стёрто)", len(chats) == 1 and chats[0]["messages"] == 3,
              f"{[(c['title'], c['messages']) for c in chats]}")
        chat_id = chats[0]["id"]
        check("защита бизнес-режима не мешала архиву (плагин бизнес-режима не загружен)",
              shturman_telegram.business_plugin_active() is False)

        shturman_telegram.business_plugin_active = lambda: True
        await feed({"update_id": 60, "business_message": biz("И счёт заодно", PETR, 104)})
        await wait(lambda: runtime.stats.counters["forwarded_messages"] >= 5)
        shturman_telegram.business_plugin_active = lambda: False
        status = await ui_get("/api/status")
        check("при «загруженном» плагине бизнес-режима (защита снята) сообщение тоже в архиве", status["messages"] == 4,
              f"messages={status['messages']}")

        print("\n4. Чужое подключение (бота подключил к себе посторонний)")
        await feed({"update_id": 7, "business_connection": {"id": "bc-stranger", "user": PETR, "user_chat_id": 99,
                    "date": now(), "is_enabled": True, "rights": {"can_reply": True}}})
        await wait(lambda: runtime.stats.counters["rejected"] >= 2, 5)
        status = await ui_get("/api/status")
        check("чужое подключение сервис не принял", status["accounts"] == 1, f"rejected={runtime.stats.counters['rejected']}")

        print("\n5. Черновик через инструмент агента")
        draft_tool = shturman_tools._handler("shturman_draft_message")
        out = json.loads(await asyncio.to_thread(draft_tool, {"chat_id": chat_id, "text": "Спасибо, жду в пятницу"}))
        check("инструмент создал черновик и сказал, что ничего не отправлено",
              out.get("ok") and out.get("sent") is False and out.get("status") == "pending", json.dumps(out, ensure_ascii=False)[:160])
        draft_id = out.get("draft_id")
        card = await wait(lambda: [p for p in tg.of("sendMessage") if "reply_markup" in p and p["chat_id"] == "42"])
        check("notify.owner выполнен: карточка пришла владельцу с кнопками", bool(card))
        buttons = json.loads(card[-1]["reply_markup"])["inline_keyboard"] if card else []
        flat = [b for row in buttons for b in row]
        print("     кнопки:", [(b["text"], b["callback_data"][:6] + "…") for b in flat])
        check("карточка — обычный текст, данные кнопок начинаются с sh:",
              card and "parse_mode" not in card[-1] and all(b["callback_data"].startswith("sh:") for b in flat))
        check("от имени владельца пока ничего не ушло", not [p for p in tg.of("sendMessage") if p.get("business_connection_id")])
        drafts = (await ui_get("/api/outbox/drafts"))["drafts"]
        check("черновик ждёт решения", drafts and drafts[0]["status"] == "pending", drafts[0]["status"] if drafts else "нет")

        send_button = next((b for b in flat if "тправ" in b["text"]), flat[0] if flat else None)
        card_message_id = tg.next_id

        def press(user, update_id):
            return {"update_id": update_id, "callback_query": {
                "id": f"q{update_id}", "from": user, "chat_instance": "ci", "data": send_button["callback_data"],
                "message": {"message_id": card_message_id, "date": now(), "chat": {"id": user["id"] if user is PETR else 42, "type": "private"},
                            "text": card[-1]["text"], "from": BOT_USER, "reply_markup": {"inline_keyboard": buttons}}}}

        print("\n6. Нажатие постороннего")
        await feed(press(PETR, 20))
        await wait(lambda: tg.of("answerCallbackQuery"), 5)
        drafts = (await ui_get("/api/outbox/drafts"))["drafts"]
        check("нажатие не владельца не передано сервису, черновик не тронут",
              runtime.stats.counters["callbacks"] == 0 and runtime.stats.counters["callbacks_refused"] == 1
              and drafts[0]["status"] == "pending")

        print("\n7. Нажатие владельца «Отправить»")
        await feed(press(OWNER, 21))
        sent = await wait(lambda: [p for p in tg.of("sendMessage") if p.get("business_connection_id")])
        check("business.send выполнен через бизнес-подключение владельца",
              len(sent) == 1 and sent[0]["business_connection_id"] == "bc1" and sent[0]["chat_id"] == str(PETR["id"])
              and sent[0]["text"] == "Спасибо, жду в пятницу" and "parse_mode" not in sent[0],
              f"отправок: {len(sent)}")
        done = await wait(lambda: raw("GET", "/api/outbox/drafts")[1]["drafts"][0]["status"] == "sent")
        drafts = (await ui_get("/api/outbox/drafts"))["drafts"]
        check("черновик в сервисе — sent, номер сообщения записан",
              done and drafts[0]["sent_tg_message_ids"], f"status={drafts[0]['status']} ids={drafts[0]['sent_tg_message_ids']}")
        check("владельцу ответили на нажатие и обновили карточку",
              len(tg.of("answerCallbackQuery")) >= 2 and (tg.of("editMessageText") or tg.of("editMessageReplyMarkup")),
              f"answers={[p.get('text') for p in tg.of('answerCallbackQuery')]}")
        await feed(press(OWNER, 22))        # повторное нажатие
        await asyncio.sleep(1.5)
        check("повторное нажатие не даёт второй отправки",
              len([p for p in tg.of("sendMessage") if p.get("business_connection_id")]) == 1)

        print("\n8. Обязательства: разбор переписки моделью (llm.structured) и инструменты")
        code, plan = raw("POST", "/api/processing/run", {})
        print("     запуск обработки:", code, json.dumps(plan, ensure_ascii=False)[:200])
        await wait(lambda: any(c[0] == "structured" for c in llm.calls), 20)
        list_tool = shturman_tools._handler("shturman_commitments")
        update_tool = shturman_tools._handler("shturman_commitment_update")

        def listed(view):
            return json.loads(list_tool({"view": view})).get("commitments") or []

        proposed = await wait(lambda: listed("proposed") or listed("open"), 20)
        check("llm.structured выполнен, обязательство появилось", bool(proposed),
              f"вызовы модели: {llm.calls[:3]}; найдено: {[(c.get('id'), c.get('status'), c.get('what')) for c in proposed]}")
        if proposed:
            item = proposed[0]
            cid = item["id"]
            print("     обязательство:", {k: item.get(k) for k in ("id", "status", "what", "due_date", "due_expression", "direction")})
            if item["status"] == "proposed":
                refused = json.loads(update_tool({"commitment_id": cid, "action": "accept"}))
                check("принять предложение инструментом нельзя (действие владельца)", refused.get("reason") == "bad_args")
                digest = await wait(lambda: [p for p in tg.of("sendMessage") if "reply_markup" in p and "sh:" in p["reply_markup"]
                                             and p is not card[-1]], 15)
                accept = None
                for p in digest or []:
                    for row in json.loads(p["reply_markup"])["inline_keyboard"]:
                        for b in row:
                            print("     кнопка сводки:", b["text"], b["callback_data"][:8] + "…")
                            if accept is None and "✓" in b["text"]:
                                accept = (p, b)
                if accept:
                    p, b = accept
                    await feed({"update_id": 30, "callback_query": {"id": "q30", "from": OWNER, "chat_instance": "ci",
                                "data": b["callback_data"], "message": {"message_id": tg.next_id, "date": now(),
                                "chat": {"id": 42, "type": "private"}, "text": p["text"], "from": BOT_USER,
                                "reply_markup": json.loads(p["reply_markup"])}}})
                    opened = await wait(lambda: [c for c in listed("open") if c["id"] == cid], 10)
                    check("владелец принял предложение кнопкой — обязательство открыто", bool(opened))
                else:
                    code, _ = raw("POST", f"/api/commitments/{cid}/accept", {})
                    check("предложение принято (напрямую: кнопки в сводке не нашлось)", code == 200)
            one = json.loads(list_tool({"commitment_id": cid}))
            check("инструмент возвращает карточку обязательства", one.get("id") == cid and one.get("ok") is True)
            moved = json.loads(update_tool({"commitment_id": cid, "action": "reschedule", "due": "в понедельник"}))
            check("перенос срока словами владельца", moved.get("ok") is True, json.dumps(moved, ensure_ascii=False)[:200])
            closed = json.loads(update_tool({"commitment_id": cid, "action": "close"}))
            check("закрытие обязательства", closed.get("ok") is True, json.dumps(closed, ensure_ascii=False)[:160])
            check("в закрытых оно есть, в открытых нет",
                  any(c["id"] == cid for c in listed("closed")) and not any(c["id"] == cid for c in listed("open")))
            again = json.loads(update_tool({"commitment_id": cid, "action": "close"}))
            print("     повторное закрытие ->", json.dumps(again, ensure_ascii=False)[:160])
            reopened = json.loads(update_tool({"commitment_id": cid, "action": "reopen"}))
            check("возврат в работу", reopened.get("ok") is True)
        people_tool = shturman_tools._handler("shturman_people")
        found = json.loads(people_tool({"action": "search", "query": "Пётр"}))
        print("     люди:", [(p.get("id"), p.get("display_name")) for p in found.get("people", [])][:3])
        if found.get("people"):
            pid = found["people"][0]["id"]
            card_p = json.loads(people_tool({"action": "card", "person_id": pid}))
            alias = json.loads(people_tool({"action": "add_alias", "person_id": pid, "alias": "Петрович"}))
            check("люди: поиск, карточка, добавление имени", card_p.get("ok") and alias.get("ok"),
                  json.dumps(alias, ensure_ascii=False)[:120])
        else:
            check("люди: поиск отвечает", found.get("ok") is True)

        print("\n9. Проход дашборда к сервису (plugin_api за uvicorn)")
        await proxy_checks(work, base, api_token)

        print("\n10. Состояние моста")
        await asyncio.sleep(0.5)
        from shturman_core import bridge_stats
        from shturman_core import service_client as sc
        runtime_stats = runtime.stats.snapshot()
        print("     счётчики:", json.dumps(runtime_stats["counters"], ensure_ascii=False))
        await asyncio.get_running_loop().run_in_executor(None, lambda: bridge_stats.Heartbeat(store, runtime.stats).tick(force=True))
        state = bridge_stats.status(store, configured=sc.configured())
        check("страница состояния: исполнитель работает, сервис доступен, счётчики — числа",
              state["executor_running"] and state["reachable"] is True and state["counters"]["forwarded_messages"] >= 5
              and state["counters"]["jobs_done"] >= 3 and state["last_job_at"], json.dumps({k: state[k] for k in ("executor_running", "reachable", "queue")}))
        final = await ui_get("/api/status")
        print("     сервис:", json.dumps(final, ensure_ascii=False))
        check("в очереди сервиса нет зависших и неудачных заданий", final["jobs_failed"] == 0, f"failed={final['jobs_failed']} waiting={final['jobs_waiting']}")
    finally:
        await runtime.stop()
        await application.stop()
        await application.shutdown()
        tg.server.shutdown()
    failed = [name for name, ok in RESULTS if not ok]
    print(f"\nИТОГО: проверок {len(RESULTS)}, неудач {len(failed)}")
    for name in failed:
        print("   НЕ ПРОШЛО:", name)
    return 1 if failed else 0


async def proxy_checks(work, base, api_token):
    import http.client

    import uvicorn
    from fastapi import FastAPI

    name = "hermes_dashboard_plugin_shturman"
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/shturman")
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    prefix = "/api/plugins/shturman/service"

    def call(method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        conn.request(method, prefix + path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def direct(method, path):
        """Тот же запрос прямо во внутренний API сервиса, с его токеном: чтобы отличить «маршрута нет
        в проходе» от «маршрута нет у сервиса»."""
        target = urllib.parse.urlsplit(base)
        conn = http.client.HTTPConnection(target.hostname, target.port, timeout=30)
        conn.request(method, path, headers={"Authorization": f"Bearer {api_token}"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    try:
        status, data = await asyncio.to_thread(call, "GET", "/status")
        check("GET /service/status отдаёт состояние сервиса", status == 200 and "messages" in json.loads(data))
        refused = []
        for method, path in (("PUT", "/owner"), ("POST", "/jobs/claim"), ("POST", "/callbacks/telegram"),
                             ("POST", "/ingest/business/message"), ("POST", "/outbox/drafts")):
            s, _ = await asyncio.to_thread(call, method, path, b"{}", {"Content-Type": "application/json"})
            refused.append(s)
        check("внутренние маршруты через проход недоступны", all(s in (404, 405) for s in refused), str(refused))
        s, data = await asyncio.to_thread(call, "GET", "/tg/accounts")
        print("     /service/tg/accounts ->", s, data[:120].decode(errors="replace"))
        s, data = await asyncio.to_thread(call, "PUT", "/outbox/policy", json.dumps({"drafts_per_hour": 7}).encode(),
                                          {"Content-Type": "application/json"})
        check("правила отправки владелец меняет через проход", s == 200 and json.loads(data)["policy"]["drafts_per_hour"] == 7, str(s))

        # Вход в аккаунт Telegram, управление аккаунтами, выбор чатов и импорт выгрузки с версии 0.0.6
        # делаются только на странице настройки переписки (её отдаёт сам сервис, мимо Hermes).
        # Проход дашборда отклоняет такие запросы сам, до сервиса. Раньше здесь же проверялась
        # загрузка выгрузки в 300 МБ потоком: через дашборд выгрузка больше не идёт, и эту проверку
        # заменила браузерная проверка страницы сервиса (service/tests/e2e/README.md). Что проход
        # по-прежнему передаёт тело без изменений, проверяет tests/test_dashboard_service.py.
        closed = []
        body = json.dumps({"password": "ne-parol"}).encode()
        for method, path in (("POST", "/tg/login"), ("GET", "/tg/login/abc"), ("POST", "/tg/login/abc/password"),
                             ("POST", "/tg/accounts/1/sync"), ("POST", "/tg/accounts/1/logout"),
                             ("PUT", "/tg/accounts/1/options"), ("GET", "/tg/accounts/1/dialogs"),
                             ("POST", "/imports"), ("GET", "/imports"), ("GET", "/imports/" + "a" * 32 + "/scan"),
                             ("POST", "/imports/" + "a" * 32 + "/run"), ("DELETE", "/imports/" + "a" * 32)):
            has_body = method in ("POST", "PUT")
            s, _ = await asyncio.to_thread(call, method, path, body if has_body else None,
                                           {"Content-Type": "application/json"} if has_body else None)
            closed.append(s)
        check("вход в Telegram, аккаунты и импорт выгрузки через проход дашборда недоступны",
              all(s == 404 for s in closed), str(closed))
        s, data = await asyncio.to_thread(call, "GET", "/imports")
        s_api, _ = await asyncio.to_thread(direct, "GET", "/api/imports")
        check("у самого сервиса эти маршруты есть (оператору — через shturman call), а в проходе их нет",
              s == 404 and s_api == 200, f"проход {s}, сервис {s_api}")
    finally:
        server.should_exit = True
        await task


if __name__ == "__main__":
    sys.exit(main())
