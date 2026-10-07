"""Настоящий адаптер Telegram из Hermes 0.21.5 с плагином «Штурман», подключённый к ПОДСТАВНОМУ Telegram.

Проверяет то, что нельзя увидеть на одной библиотеке: где Hermes вызывает фабрику обработчиков,
запускается ли там исполнитель, доходят ли обновления до наших обработчиков в приложении Hermes
(TelegramApplication с учётом принятых обновлений) и что видит ядро.
Настоящего бота и токена здесь нет: адрес Bot API заменён настройкой extra.base_url.

Запуск (интерпретатором, в котором установлен Hermes; HERMES_HOME — пустой каталог стенда,
в котором `plugins/shturman` — ссылка на этот плагин, а в config.yaml — `plugins: {enabled: [shturman]}`):

    HERMES_HOME=/путь/к/каталогу/стенда python bridge_adapter.py

Печатает, что произошло на каждом шаге; сверять с ожидаемым выводом в README.md.
"""
import asyncio
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy", "TELEGRAM_PROXY"):
    os.environ.pop(name, None)
os.environ["HERMES_TELEGRAM_DISABLE_FALLBACK_IPS"] = "1"
os.environ["TELEGRAM_ALLOWED_USERS"] = "42"

sys.path.insert(0, str(ROOT / "plugins" / "shturman" / "tests"))
from conftest import FakeService  # подставной сервис переписки: записывает запросы

BOT_TOKEN = "1234567890:" + "A" * 35
BOT_USER = {"id": 1234567890, "is_bot": True, "first_name": "Штурман", "username": "shturman_bot",
            "can_join_groups": True, "can_read_all_group_messages": False, "supports_inline_queries": False,
            "can_connect_to_business": True}
OWNER = {"id": 42, "is_bot": False, "first_name": "Иван", "username": "ivan"}
PETR = {"id": 99, "is_bot": False, "first_name": "Пётр"}


class FakeTelegram:
    def __init__(self):
        self.calls, self.updates, self.lock = [], [], threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                method = self.path.rsplit("/", 1)[-1]
                params = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
                if method != "getUpdates":
                    outer.calls.append((method, params))
                result = True
                if method == "getMe":
                    result = BOT_USER
                elif method == "getUpdates":
                    offset = int(params.get("offset") or 0)
                    deadline = time.time() + min(float(params.get("timeout") or 0), 0.3)
                    while True:
                        with outer.lock:
                            result = [u for u in outer.updates if u["update_id"] >= offset]
                        if result or time.time() >= deadline:
                            break
                        time.sleep(0.02)
                elif method in ("getMyCommands",):
                    result = []
                elif method == "getWebhookInfo":
                    result = {"url": "", "has_custom_certificate": False, "pending_update_count": 0}
                elif method in ("sendMessage", "editMessageText"):
                    result = {"message_id": 9100 + len(outer.calls), "date": int(time.time()),
                              "chat": {"id": int(params["chat_id"]), "type": "private"}, "text": params.get("text", "")}
                data = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

            do_GET = do_POST

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def push(self, update):
        with self.lock:
            self.updates.append(update)

    def of(self, method):
        return [p for m, p in self.calls if m == method]


def ours():
    return sorted(t.get_name() for t in asyncio.all_tasks() if t.get_name().startswith("shturman:") and not t.done())


async def main():
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    service, tg = FakeService(), FakeTelegram()
    os.environ["SHTURMAN_SERVICE_URL"], os.environ["SHTURMAN_API_TOKEN"] = service.url, service.token
    jobs = [{"id": 1, "kind": "notify.owner", "attempt": 1,
             "payload": {"text": "Карточка черновика", "silent": False,
                         "buttons": [[{"text": "Отправить", "data": "sh:ob:1:ok"}]]}}]

    def claim(record):
        kinds = record["json"]["kinds"]
        taken = [j for j in jobs if j["kind"] in kinds][:1]
        for j in taken:
            jobs.remove(j)
        return 200, {"jobs": taken}

    service.replies[("POST", "/api/jobs/claim")] = claim
    service.replies[("POST", "/api/callbacks/telegram")] = (200, {"answer": "Принято", "edit_text": "Готово", "remove_buttons": True})

    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    discover_plugins()
    loaded = [p for p in get_plugin_manager().list_plugins() if p.get("name") == "shturman"][0]
    print("плагин загружен Hermes:", loaded.get("enabled"), "ошибка:", loaded.get("error"))

    import shturman_bridge
    import shturman_telegram
    from shturman_core.state import Store
    Store().write("owner", {"user_id": 42, "chat_id": 42, "name": "Иван"})
    print("плагин бизнес-режима загружен:", shturman_telegram.business_plugin_active())

    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token=BOT_TOKEN,
                                             extra={"base_url": tg.url + "/bot", "base_file_url": tg.url + "/file/bot"}))
    core_events = []

    async def to_agent(event):
        core_events.append((getattr(event, "text", None), getattr(getattr(event, "source", None), "user_id", None),
                            getattr(getattr(event, "source", None), "chat_id", None)))
        return None

    adapter.set_message_handler(to_agent)
    runtime = shturman_bridge.runtime()
    print("до connect(): задачи моста:", ours())
    connected = await adapter.connect()
    print("connect() ->", connected, "| приложение:", type(adapter._app).__name__, "| running:", adapter._app.running)
    print("после connect(): задачи моста:", ours())
    print("исполнитель держит приложение адаптера:", runtime.application is adapter._app)
    print("группы обработчиков:", {g: len(hs) for g, hs in adapter._app.handlers.items()})
    order0 = [getattr(getattr(h.callback, "__wrapped__", h.callback), "__name__", "?") for h in adapter._app.handlers[0]]
    print("группа 0 по порядку:", order0)

    now = int(time.time())
    def biz(mid, user, **extra):
        return {"message_id": mid, "date": now, "chat": {"id": 99, "type": "private", "first_name": "Пётр"},
                "from": user, "business_connection_id": "bc1", **extra}

    tg.push({"update_id": 100, "business_connection": {"id": "bc1", "user": OWNER, "user_chat_id": 42, "date": now,
             "is_enabled": True, "rights": {"can_reply": True}}})
    tg.push({"update_id": 101, "business_message": biz(1, PETR, text="Пришлю смету в пятницу")})
    tg.push({"update_id": 102, "business_message": biz(2, OWNER, photo=[{"file_id": "a", "file_unique_id": "b", "width": 9, "height": 9}], caption="вот")})
    tg.push({"update_id": 103, "business_message": biz(3, OWNER, text="/new")})
    tg.push({"update_id": 104, "message": {"message_id": 50, "date": now, "chat": {"id": 42, "type": "private"}, "from": OWNER, "text": "привет, Штурман"}})

    async def wait(cond, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not cond():
            await asyncio.sleep(0.05)
        return cond()

    await wait(lambda: len(service.calls("POST", "/api/ingest/business/message")) >= 3 and core_events)
    await wait(lambda: tg.of("sendMessage"))
    await asyncio.sleep(1.0)
    print("сервис получил: подключений", len(service.calls("POST", "/api/ingest/business/connection")),
          "| сообщений", len(service.calls("POST", "/api/ingest/business/message")),
          "| владелец", [r["json"] for r in service.calls("PUT", "/api/owner")][:1])
    print("ядро Hermes передало агенту:", core_events)
    cards = [p for p in tg.of("sendMessage") if "reply_markup" in p]
    print("notify.owner через бота адаптера:", [(p["chat_id"], p["text"], "parse_mode" in p) for p in cards])
    print("итог задания сообщён:", [r["path"] for r in service.requests if "/complete" in r["path"] or "/fail" in r["path"]])

    # кнопка сервиса и кнопка ядра
    card_message = {"message_id": 9200, "date": now, "chat": {"id": 42, "type": "private"}, "from": BOT_USER,
                    "text": "Карточка черновика", "reply_markup": {"inline_keyboard": [[{"text": "Отправить", "callback_data": "sh:ob:1:ok"}]]}}
    tg.push({"update_id": 105, "callback_query": {"id": "q1", "from": OWNER, "chat_instance": "c", "data": "sh:ob:1:ok", "message": card_message}})
    tg.push({"update_id": 106, "callback_query": {"id": "q2", "from": PETR, "chat_instance": "c", "data": "sh:ob:1:ok", "message": card_message}})
    tg.push({"update_id": 107, "callback_query": {"id": "q3", "from": OWNER, "chat_instance": "c", "data": "ea:once:zzz", "message": card_message}})
    await wait(lambda: len(tg.of("answerCallbackQuery")) >= 2 and tg.of("editMessageText"))
    await asyncio.sleep(1.0)
    print("нажатий передано сервису:", [r["json"] for r in service.calls("POST", "/api/callbacks/telegram")])
    print("ответы на нажатия:", [(p.get("callback_query_id"), p.get("text")) for p in tg.of("answerCallbackQuery")])
    print("правка карточки:", [(p.get("text"), "reply_markup" in p, "parse_mode" in p) for p in tg.of("editMessageText")])
    print("счётчики:", {k: v for k, v in runtime.stats.counters.items() if v})

    # переподключение: Hermes строит новое приложение и снова вызывает фабрику
    first_app = adapter._app
    await adapter.disconnect()
    print("после disconnect(): задачи моста:", ours(), "| бот готов:", runtime.executor.bot.ready())
    jobs.append({"id": 2, "kind": "notify.owner", "attempt": 1, "payload": {"text": "После переподключения", "buttons": None, "silent": True}})
    await asyncio.sleep(3.2)
    print("пока бот отключён, задания боту не забираются:", any(j["id"] == 2 for j in jobs))
    connected = await adapter.connect(is_reconnect=True)
    print("повторный connect() ->", connected, "| новое приложение:", adapter._app is not first_app,
          "| исполнитель взял его:", runtime.application is adapter._app, "| задачи моста:", ours())
    await wait(lambda: any(p.get("text") == "После переподключения" for p in tg.of("sendMessage")))
    print("задание выполнено новым приложением:", any(p.get("text") == "После переподключения" for p in tg.of("sendMessage")))
    tg.push({"update_id": 108, "business_message": biz(4, PETR, text="после переподключения")})
    await wait(lambda: len(service.calls("POST", "/api/ingest/business/message")) >= 4)
    print("сообщений в сервисе после переподключения:", len(service.calls("POST", "/api/ingest/business/message")))
    await adapter.disconnect()
    await runtime.stop()
    print("после остановки моста: задачи:", ours())
    print("методы Bot API, которые вызывал адаптер:", sorted({m for m, _ in tg.calls}))
    service.close()


if __name__ == "__main__":
    asyncio.run(main())
