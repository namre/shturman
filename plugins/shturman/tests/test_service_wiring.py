"""Мост к сервису переписки на настоящей библиотеке python-telegram-bot.

Сервис переписки и Telegram заменены маленькими HTTP-серверами, всё остальное настоящее:
обработчики, очередь пересылки, клиент сервиса, бот библиотеки и её разбор ответов Telegram.

В CI библиотеки нет (она приходит вместе с Hermes), поэтому там тест пропускается.
"""

import asyncio
import json
import logging
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

telegram = pytest.importorskip("telegram")
from telegram.ext import Application, ApplicationHandlerStop, CallbackContext, MessageHandler, filters  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shturman_bridge  # noqa: E402
import shturman_telegram  # noqa: E402
from shturman_core.executor import (  # noqa: E402
    BUSINESS_SEND, LLM_STRUCTURED, NOTIFY_EDIT, NOTIFY_OWNER, Executor, NotSent,
)
from shturman_core.state import Store  # noqa: E402

BOT_TOKEN = "1234567890:" + "A" * 35
OWNER = {"id": 42, "is_bot": False, "first_name": "Иван", "last_name": "Иванов", "username": "ivan"}
STRANGER = {"id": 99, "is_bot": False, "first_name": "Пётр"}
BOT_USER = {"id": 1234567890, "is_bot": True, "first_name": "Штурман", "username": "shturman_bot"}
CONNECTION = "/api/ingest/business/connection"
MESSAGE = "/api/ingest/business/message"
DELETED = "/api/ingest/business/deleted"
CALLBACK = "/api/callbacks/telegram"
SECRET = "Пароль от сейфа 7391 — никому"


def business_message(text=SECRET, user=STRANGER, mid=1, **extra):
    body = {"message_id": mid, "date": int(time.time()), "chat": {"id": user["id"], "type": "private"},
            "from": user, "business_connection_id": "bc1", **extra}
    if text is not None:
        body["text"] = text
    return body


def connection_update(user=OWNER, update_id=8):
    return {"update_id": update_id, "business_connection": {
        "id": "bc1", "user": user, "user_chat_id": user["id"], "date": int(time.time()),
        "is_enabled": True, "rights": {"can_reply": True}}}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("SHTURMAN_STATE_DIR", str(tmp_path / "state"))
    for name in shturman_telegram._ALLOWLIST_ENV:
        monkeypatch.delenv(name, raising=False)
    shturman_telegram._unbound_replied.clear()
    fresh = shturman_bridge.Runtime()
    monkeypatch.setattr(shturman_bridge, "_runtime", fresh)
    Store().write("owner", {"user_id": 42, "chat_id": 42, "name": "Иван"})
    return fresh


@pytest.fixture
def app(runtime, monkeypatch):
    monkeypatch.setattr(shturman_telegram, "business_plugin_active", lambda: False)
    application = Application.builder().token(BOT_TOKEN).updater(None).build()
    shturman_telegram.wire(application, None)
    return application


def dispatch(app, runtime, payload, *, until=None, timeout=3.0):
    """Как диспетчер библиотеки: группы по возрастанию, в группе — первый подошедший обработчик,
    ApplicationHandlerStop прекращает обход. Затем ждём фоновую работу моста."""
    update = telegram.Update.de_json(payload, app.bot)
    handled = []

    async def go():
        loop = asyncio.get_running_loop()
        try:
            for group in sorted(app.handlers):
                for handler in app.handlers[group]:
                    check = handler.check_update(update)
                    if check is None or check is False:
                        continue
                    context = CallbackContext.from_update(update, app)
                    handled.append((group, handler.callback.__name__))
                    await handler.handle_update(update, app, check, context)
                    break
        except ApplicationHandlerStop:
            pass
        deadline = loop.time() + timeout
        while until is not None and not until() and loop.time() < deadline:
            await asyncio.sleep(0.01)
        other = [t for t in asyncio.all_tasks()
                 if t is not asyncio.current_task() and not t.get_name().startswith("shturman:")]
        if other:
            await asyncio.wait(other, timeout=timeout)       # обработчики с block=False
        await runtime.stop()

    asyncio.run(go())
    runtime.configure(None)                                  # следующему обновлению — новый запуск
    return handled


# --- запись бизнес-сообщений в архив: в обоих состояниях защиты ---

def test_guard_active_message_is_archived_and_still_hidden_from_core(app, runtime, service_env):
    service = service_env
    handled = dispatch(app, runtime, {"update_id": 1, "business_message": business_message()},
                       until=lambda: service.calls("POST", MESSAGE))
    assert handled[0] == (shturman_telegram.ARCHIVE_GROUP, "archive")       # архив — раньше всех
    assert (0, "drop_business_message") in handled                          # защита по-прежнему сработала
    sent = service.calls("POST", MESSAGE)[0]["json"]
    assert sent["edited"] is False
    assert sent["message"]["business_connection_id"] == "bc1" and sent["message"]["text"] == SECRET
    assert sent["message"]["from"]["id"] == 99 and sent["message"]["chat"] == {"id": 99, "type": "private"}
    assert isinstance(sent["message"]["date"], int)                         # вид Bot API, а не объекты библиотеки
    assert service.calls("PUT", "/api/owner")[0]["json"] == {"user_id": 42, "chat_id": 42}


def test_business_plugin_active_message_is_archived_and_left_to_that_plugin(app, runtime, service_env, monkeypatch):
    monkeypatch.setattr(shturman_telegram, "business_plugin_active", lambda: True)
    service = service_env
    handled = dispatch(app, runtime, {"update_id": 2, "business_message": business_message()},
                       until=lambda: service.calls("POST", MESSAGE))
    assert (shturman_telegram.ARCHIVE_GROUP, "archive") in handled
    assert (0, "drop_business_message") not in handled                      # текст достаётся плагину бизнес-режима
    assert len(service.calls("POST", MESSAGE)) == 1


def test_archive_does_not_depend_on_a_handler_that_stops_propagation(app, runtime, service_env, monkeypatch):
    """Плагин бизнес-режима в группе −1 может остановить обход (правка черновика). Архив идёт раньше."""
    monkeypatch.setattr(shturman_telegram, "business_plugin_active", lambda: True)

    async def official_edit_capture(update, context):
        raise ApplicationHandlerStop

    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, official_edit_capture), group=-1)
    service = service_env
    handled = dispatch(app, runtime, {"update_id": 3, "business_message": business_message()},
                       until=lambda: service.calls("POST", MESSAGE))
    assert [g for g, _ in handled] == [shturman_telegram.ARCHIVE_GROUP, -1]  # дальше −1 обход не пошёл
    assert len(service.calls("POST", MESSAGE)) == 1


@pytest.mark.parametrize("plugin_active", [False, True])
def test_business_media_never_reaches_core_and_is_archived(app, runtime, service_env, monkeypatch, plugin_active):
    """Фото из бизнес-чата подошло бы обработчику медиа в ядре Hermes, а плагин бизнес-режима
    берёт только текст. Поэтому защита перехватывает такие сообщения всегда."""
    monkeypatch.setattr(shturman_telegram, "business_plugin_active", lambda: plugin_active)
    photo = business_message(text=None, user=OWNER, photo=[
        {"file_id": "a", "file_unique_id": "b", "width": 10, "height": 10}], caption="смета")
    service = service_env
    handled = dispatch(app, runtime, {"update_id": 4, "business_message": photo},
                       until=lambda: service.calls("POST", MESSAGE))
    assert (0, "drop_business_message") in handled
    assert service.calls("POST", MESSAGE)[0]["json"]["message"]["caption"] == "смета"


def test_edits_deletions_and_connections_are_forwarded(app, runtime, service_env):
    service = service_env
    dispatch(app, runtime, {"update_id": 5, "edited_business_message": business_message("исправлено", edit_date=1)},
             until=lambda: service.calls("POST", MESSAGE))
    assert service.calls("POST", MESSAGE)[0]["json"]["edited"] is True
    dispatch(app, runtime, {"update_id": 6, "deleted_business_messages": {
        "business_connection_id": "bc1", "chat": {"id": 99, "type": "private", "first_name": "Пётр"},
        "message_ids": [1, 2]}}, until=lambda: service.calls("POST", DELETED))
    assert service.calls("POST", DELETED)[0]["json"] == {
        "business_connection_id": "bc1", "chat": {"id": 99, "type": "private", "first_name": "Пётр"},
        "message_ids": [1, 2]}
    handled = dispatch(app, runtime, connection_update(), until=lambda: service.calls("POST", CONNECTION))
    sent = service.calls("POST", CONNECTION)[0]["json"]["connection"]
    assert sent["id"] == "bc1" and sent["user"]["id"] == 42 and sent["is_enabled"] is True
    assert sent["rights"] == {"can_reply": True}
    # прежний наблюдатель за подключением работает как раньше
    assert (shturman_telegram.OBSERVER_GROUP, "observe") in handled and Store().read("business")["connected"] is True


def test_unknown_connection_is_fetched_from_telegram_and_message_retried(app, runtime, service_env, monkeypatch):
    service = service_env
    known = []

    def message_reply(record):
        if not known:
            return 409, {"error": "неизвестное бизнес-подключение", "code": "unknown_connection"}
        return 200, {"stored": True}

    def connection_reply(record):
        known.append(record["json"]["connection"]["id"])
        return 200, {"ok": True}

    service.replies[("POST", MESSAGE)] = message_reply
    service.replies[("POST", CONNECTION)] = connection_reply
    asked = []

    async def get_business_connection(self, business_connection_id, **kwargs):
        asked.append(business_connection_id)
        return telegram.BusinessConnection.de_json(connection_update()["business_connection"], self)

    monkeypatch.setattr(telegram.ext.ExtBot, "get_business_connection", get_business_connection)
    monkeypatch.setattr(Application, "running", property(lambda self: True))
    dispatch(app, runtime, {"update_id": 7, "business_message": business_message()},
             until=lambda: len(service.calls("POST", MESSAGE)) == 2)
    assert asked == ["bc1"] and known == ["bc1"]
    assert [r["path"] for r in service.requests if "/ingest/" in r["path"]] == [MESSAGE, CONNECTION, MESSAGE]


def test_while_no_owner_is_bound_nothing_is_archived(app, runtime, service_env):
    """Привязка сброшена (ссылка восстановления) или ещё не сделана: переписка в сервис не уходит."""
    Store().delete("owner")
    handled = dispatch(app, runtime, {"update_id": 13, "business_message": business_message()}, timeout=0.3)
    dispatch(app, runtime, connection_update(update_id=14), timeout=0.3)
    assert (0, "drop_business_message") in handled
    assert [r for r in service_env.requests if "/ingest/" in r["path"]] == []
    assert runtime.stats.counters["rejected"] == 2


def test_ordinary_messages_are_not_forwarded(app, runtime, service_env):
    message = {"message_id": 1, "date": int(time.time()), "text": "привет",
               "chat": {"id": 42, "type": "private"}, "from": OWNER}
    handled = dispatch(app, runtime, {"update_id": 9, "message": message})
    assert shturman_telegram.ARCHIVE_GROUP not in [g for g, _ in handled]
    assert service_env.calls("POST") == []


def test_without_service_everything_new_is_silent_and_the_guard_still_works(app, runtime, service, caplog):
    """Сервис не подключён (нет токена): ни запросов, ни ошибок, прежнее поведение на месте."""
    with caplog.at_level(logging.WARNING):
        handled = dispatch(app, runtime, {"update_id": 10, "business_message": business_message()})
        dispatch(app, runtime, connection_update(update_id=11))
    assert (0, "drop_business_message") in handled
    assert service.requests == [] and runtime.running is False and caplog.records == []
    assert Store().read("business")["connected"] is True
    assert not (Path(Store().root) / "bridge.json").exists()


def test_service_outage_never_breaks_the_gateway(app, runtime, monkeypatch, caplog):
    monkeypatch.setenv("SHTURMAN_SERVICE_URL", "http://127.0.0.1:9")       # там никто не слушает
    monkeypatch.setenv("SHTURMAN_API_TOKEN", "t" * 40)
    with caplog.at_level(logging.DEBUG):
        handled = dispatch(app, runtime, {"update_id": 12, "business_message": business_message()}, timeout=0.3)
    assert (shturman_telegram.ARCHIVE_GROUP, "archive") in handled and (0, "drop_business_message") in handled
    assert SECRET not in caplog.text and "t" * 40 not in caplog.text


# --- кнопки сервиса ---

@pytest.fixture
def presses(monkeypatch):
    seen = {"answers": [], "texts": [], "markups": []}

    async def answer(self, text=None, **kwargs):
        seen["answers"].append(text)

    async def edit_message_text(self, text, **kwargs):
        seen["texts"].append((text, kwargs))

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        seen["markups"].append(reply_markup)

    monkeypatch.setattr(telegram.CallbackQuery, "answer", answer)
    monkeypatch.setattr(telegram.CallbackQuery, "edit_message_text", edit_message_text)
    monkeypatch.setattr(telegram.CallbackQuery, "edit_message_reply_markup", edit_message_reply_markup)
    return seen


def press(data="sh:d:12:ok", user=OWNER, chat_id=42, update_id=50):
    return {"update_id": update_id, "callback_query": {
        "id": "q1", "from": user, "chat_instance": "ci", "data": data,
        "message": {"message_id": 700, "date": int(time.time()), "chat": {"id": chat_id, "type": "private"},
                    "text": "Карточка черновика", "from": BOT_USER,
                    "reply_markup": {"inline_keyboard": [[{"text": "Отправить", "callback_data": data}]]}}}}


def test_owner_press_goes_to_service_and_card_is_updated(app, runtime, service_env, presses):
    service = service_env
    service.replies[("POST", CALLBACK)] = (200, {
        "answer": "Отправляю", "edit_text": "Черновик № 12: отправляется\n<b>не разметка</b>", "remove_buttons": True})
    handled = dispatch(app, runtime, press(), until=lambda: presses["texts"])
    # В группе ядра Hermes нажатие достаётся нашему обработчику — общий обработчик кнопок его не увидит.
    assert [h for h in handled if h[0] == 0] == [(0, "on_service_button")]
    assert service.calls("POST", CALLBACK)[0]["json"] == {"data": "sh:d:12:ok", "from_user_id": 42}
    assert presses["answers"] == ["Отправляю"]
    text, kwargs = presses["texts"][0]
    assert text == "Черновик № 12: отправляется\n<b>не разметка</b>"
    assert kwargs["parse_mode"] is None and kwargs["reply_markup"] is None    # обычный текст, кнопки сняты


def test_press_can_keep_buttons_or_only_remove_them(app, runtime, service_env, presses):
    service = service_env
    service.replies[("POST", CALLBACK)] = (200, {"answer": "Пока нельзя", "edit_text": "Подождите", "remove_buttons": False})
    dispatch(app, runtime, press(), until=lambda: presses["texts"])
    markup = presses["texts"][0][1]["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "sh:d:12:ok"         # кнопки переданы заново и остались

    service.replies[("POST", CALLBACK)] = (200, {"answer": "Готово", "edit_text": None, "remove_buttons": True})
    dispatch(app, runtime, press(update_id=51), until=lambda: presses["markups"])
    assert presses["markups"] == [None] and len(presses["texts"]) == 1

    service.replies[("POST", CALLBACK)] = (200, {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False})
    dispatch(app, runtime, press(update_id=52), until=lambda: len(presses["answers"]) == 3)
    assert presses["answers"][-1] == "Кнопка недоступна." and len(presses["texts"]) == 1 and len(presses["markups"]) == 1


@pytest.mark.parametrize("payload", [
    press(user=STRANGER, chat_id=99), press(user=STRANGER, chat_id=42), press(user=OWNER, chat_id=-100500),
])
def test_press_by_anyone_else_is_not_forwarded(app, runtime, service_env, presses, payload):
    dispatch(app, runtime, payload, until=lambda: presses["answers"])
    assert service_env.calls("POST", CALLBACK) == []
    assert presses["answers"] == ["Кнопка недоступна."] and presses["texts"] == [] and presses["markups"] == []
    assert runtime.stats.counters["callbacks_refused"] == 1


def test_press_without_bound_owner_is_not_forwarded(app, runtime, service_env, presses):
    Store().delete("owner")
    dispatch(app, runtime, press(), until=lambda: presses["answers"])
    assert service_env.calls("POST", CALLBACK) == [] and presses["answers"] == ["Кнопка недоступна."]


def test_service_down_answers_politely_and_leaves_buttons(app, runtime, presses, monkeypatch):
    monkeypatch.setenv("SHTURMAN_SERVICE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SHTURMAN_API_TOKEN", "t" * 40)
    dispatch(app, runtime, press(), until=lambda: presses["answers"])
    assert presses["answers"] == ["Сервис переписки недоступен, попробуйте позже"]
    assert presses["texts"] == [] and presses["markups"] == []


def test_service_refusal_or_absence_leaves_buttons(app, runtime, service, presses, monkeypatch):
    dispatch(app, runtime, press(), until=lambda: presses["answers"])         # сервис не подключён
    assert presses["answers"] == ["Сервис переписки недоступен, попробуйте позже"] and service.requests == []
    monkeypatch.setenv("SHTURMAN_SERVICE_URL", service.url)
    monkeypatch.setenv("SHTURMAN_API_TOKEN", service.token)
    runtime._next_config_check = 0
    service.replies[("POST", CALLBACK)] = (400, {"error": "поле data: нужна непустая строка"})
    dispatch(app, runtime, press(update_id=53), until=lambda: len(presses["answers"]) == 2)
    assert presses["answers"][-1] == "Сервис переписки недоступен, попробуйте позже" and presses["texts"] == []


@pytest.mark.parametrize("data", ["ea:once:1", "bd:send:5", "mp:next", "shx:1", " sh:1"])
def test_foreign_buttons_are_left_to_their_owners(app, runtime, service_env, presses, data):
    handled = dispatch(app, runtime, press(data=data))
    assert (0, "on_service_button") not in handled and service_env.calls("POST", CALLBACK) == []


# --- бот: настоящая библиотека и её разбор ответов «Telegram» ---

class FakeTelegram:
    """Вместо api.telegram.org. `mode` решает, чем ответить на sendMessage и editMessageText."""

    def __init__(self) -> None:
        self.mode = "ok"
        self.calls: list[tuple[str, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                method = self.path.rsplit("/", 1)[-1]
                params = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
                outer.calls.append((method, params))
                status, body = 200, {"ok": True, "result": True}
                if method == "getMe":
                    body = {"ok": True, "result": BOT_USER}
                elif method in ("sendMessage", "editMessageText"):
                    status, body = outer.reply(params)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)   # HTTP/1.0: соединения не держатся
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def reply(self, params):
        mode = self.mode
        if mode == "ok":
            return 200, {"ok": True, "result": {
                "message_id": 4321, "date": int(time.time()),
                "chat": {"id": int(params["chat_id"]), "type": "private"}, "text": params.get("text", "")}}
        if mode == "hang":
            time.sleep(1.5)
            return 200, {"ok": True, "result": True}
        if mode == "429":
            return 429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 7",
                         "parameters": {"retry_after": 7}}
        descriptions = {"400": "Bad Request: BUSINESS_PEER_INVALID", "403": "Forbidden: bot was blocked by the user",
                        "401": "Unauthorized", "500": "Internal Server Error", "502": "Bad Gateway",
                        "same": "Bad Request: message is not modified: specified new message content and reply "
                                "markup are exactly the same", "gone": "Bad Request: message to edit not found",
                        "locked": "Bad Request: message can't be edited"}
        status = 400 if mode in ("same", "gone", "locked") else int(mode)
        return status, {"ok": False, "error_code": status, "description": descriptions[mode]}

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def tg():
    fake = FakeTelegram()
    yield fake
    fake.close()


def with_bot(tg, scenario):
    """Запускает сценарий с настоящим приложением библиотеки, которое ходит в подставной Telegram."""
    async def go():
        runtime = shturman_bridge.Runtime()
        application = (Application.builder().token(BOT_TOKEN).base_url(tg.url + "/bot").updater(None)
                       .read_timeout(0.4).write_timeout(0.4).connect_timeout(0.4).pool_timeout(0.4).build())
        runtime.application = application
        bot = shturman_bridge.PtbBot(runtime)
        assert bot.ready() is False                 # приложение ещё не запущено — задания боту не берутся
        await application.initialize()
        await application.start()
        try:
            assert bot.ready() is True
            return await scenario(bot)
        finally:
            await application.stop()
            await application.shutdown()

    return asyncio.run(go())


def executor_for(bot):
    async def no_service(*args, **kwargs):
        return {}

    return Executor(no_service, llm=None, bot=bot, owner=lambda: {"user_id": 42, "chat_id": 42})


SEND = {"business_connection_id": "bc1", "chat_id": 555, "text": "Буду в 15:00 <b>*</b>", "reply_to_message_id": 77}


def test_business_send_on_the_wire(tg):
    outcome = with_bot(tg, lambda bot: executor_for(bot).execute(BUSINESS_SEND, SEND))
    assert outcome.result == {"message_id": 4321}
    method, params = tg.calls[-1]
    assert method == "sendMessage" and params["business_connection_id"] == "bc1" and params["chat_id"] == "555"
    assert params["text"] == "Буду в 15:00 <b>*</b>" and "parse_mode" not in params      # обычный текст
    assert json.loads(params["reply_parameters"])["message_id"] == 77
    assert [m for m, _ in tg.calls].count("sendMessage") == 1


@pytest.mark.parametrize("mode", ["400", "403", "401", "429"])
def test_definite_telegram_refusal_is_not_sent(tg, mode):
    tg.mode = mode
    outcome = with_bot(tg, lambda bot: executor_for(bot).execute(BUSINESS_SEND, SEND))
    assert outcome.error.startswith("not_sent:") and outcome.retry_in is None
    assert [m for m, _ in tg.calls].count("sendMessage") == 1


@pytest.mark.parametrize("mode", ["500", "502", "hang"])
def test_server_error_or_timeout_is_an_unknown_outcome_and_is_never_resent(tg, mode):
    tg.mode = mode
    outcome = with_bot(tg, lambda bot: executor_for(bot).execute(BUSINESS_SEND, SEND))
    assert not outcome.ok and not outcome.error.startswith("not_sent:") and outcome.retry_in is None
    assert [m for m, _ in tg.calls].count("sendMessage") == 1        # ни библиотека, ни мы запрос не повторили


def test_connection_that_never_opened_is_not_sent(tg):
    async def scenario(bot):
        tg.close()                                   # «Telegram» исчез: соединение установить не удастся
        return await executor_for(bot).execute(BUSINESS_SEND, SEND)

    outcome = with_bot(tg, scenario)
    assert outcome.error.startswith("not_sent:") and "ConnectError" in outcome.error
    assert [m for m, _ in tg.calls].count("sendMessage") == 0


def test_notify_owner_on_the_wire_is_plain_text_with_buttons(tg):
    payload = {"text": "Черновик для *Петра* <i>не разметка</i>", "silent": True,
               "buttons": [[{"text": "Отправить", "data": "sh:d:12:ok"}, {"text": "Отклонить", "data": "sh:d:12:no"}]]}
    outcome = with_bot(tg, lambda bot: executor_for(bot).execute(NOTIFY_OWNER, payload))
    assert outcome.result == {"message_id": 4321}
    _, params = tg.calls[-1]
    assert params["chat_id"] == "42" and params["text"] == payload["text"] and "parse_mode" not in params
    assert params["disable_notification"] in ("true", "True")
    keyboard = json.loads(params["reply_markup"])["inline_keyboard"]
    assert keyboard == [[{"text": "Отправить", "callback_data": "sh:d:12:ok"},
                         {"text": "Отклонить", "callback_data": "sh:d:12:no"}]]
    assert json.loads(params["link_preview_options"])["is_disabled"] is True
    assert "business_connection_id" not in params


@pytest.mark.parametrize("mode, ok", [("ok", True), ("same", True), ("gone", True), ("locked", False), ("403", False)])
def test_notify_edit_treats_unchanged_and_missing_message_as_done(tg, mode, ok):
    tg.mode = mode
    outcome = with_bot(tg, lambda bot: executor_for(bot).execute(NOTIFY_EDIT, {"message_id": 700, "text": "итог"}))
    assert outcome.ok is ok
    method, params = tg.calls[-1]
    assert method == "editMessageText" and params["message_id"] == "700" and "reply_markup" not in params
    if not ok:
        assert outcome.retry_in is None


def test_refusal_classification():
    from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut

    for exc in (BadRequest("chat not found"), Forbidden("blocked"), InvalidToken("bad")):
        assert isinstance(shturman_bridge.refusal(exc), NotSent) and shturman_bridge.refusal(exc).replied is True
    assert shturman_bridge.refusal(RetryAfter(9)).retry_after == 9
    for exc in (TimedOut(), NetworkError("Bad Gateway"), RuntimeError("x"), asyncio.TimeoutError()):
        assert shturman_bridge.refusal(exc) is None

    import httpx

    def caused(cause):
        try:
            try:
                raise cause
            except Exception as inner:
                raise NetworkError("httpx error") from inner
        except NetworkError as outer:
            return outer

    never_left = shturman_bridge.refusal(caused(httpx.ConnectError("refused")))
    assert isinstance(never_left, NotSent) and never_left.replied is False
    assert shturman_bridge.refusal(caused(httpx.PoolTimeout("busy"))) is not None
    for cause in (httpx.ReadTimeout("slow"), httpx.WriteTimeout("slow"), httpx.ReadError("reset"),
                  httpx.RemoteProtocolError("closed")):
        assert shturman_bridge.refusal(caused(cause)) is None        # запрос мог уйти


# --- один исполнитель на процесс ---

def test_runtime_starts_once_and_follows_the_current_application(runtime, service_env):
    first = Application.builder().token(BOT_TOKEN).updater(None).build()
    second = Application.builder().token(BOT_TOKEN).updater(None).build()

    async def scenario():
        runtime.attach(first, Store())
        names = sorted(t.get_name() for t in asyncio.all_tasks() if t.get_name().startswith("shturman:"))
        executor = runtime.executor
        runtime.attach(second, Store())              # Hermes переподключился и построил новое приложение
        runtime.ensure_started()
        again = sorted(t.get_name() for t in asyncio.all_tasks() if t.get_name().startswith("shturman:"))
        same_executor = runtime.executor is executor
        current = runtime.application
        await asyncio.sleep(0.05)
        await runtime.stop()
        left = [t for t in asyncio.all_tasks() if t.get_name().startswith("shturman:") and not t.done()]
        return names, again, same_executor, current, left

    names, again, same_executor, current, left = asyncio.run(scenario())
    assert names == again == ["shturman:heartbeat", "shturman:ingest", "shturman:jobs-bot", "shturman:jobs-llm"]
    assert same_executor and current is second and left == []
    assert runtime.ensure_started() is False         # остановлен до следующей регистрации плагина


def test_plugin_reload_resumes_the_bridge_without_waiting_for_an_update(runtime, service_env):
    """Hermes перезагрузил плагин на ходу: выгрузка останавливает мост, новая регистрация возобновляет."""
    application = Application.builder().token(BOT_TOKEN).updater(None).build()

    async def scenario():
        runtime.attach(application, Store())
        await asyncio.sleep(0.02)
        runtime.shutdown()                               # ctx.on_unload
        await asyncio.sleep(0.02)
        stopped = runtime.running
        await asyncio.to_thread(runtime.configure, None)  # register() нового экземпляра — из другого потока
        await asyncio.sleep(0.05)
        resumed = runtime.running
        await runtime.stop()
        return stopped, resumed

    assert asyncio.run(scenario()) == (False, True)


def test_wiring_without_running_loop_starts_on_first_update(app, runtime, service_env):
    assert runtime.running is False                  # wire() вызван вне цикла событий — работа не запущена
    dispatch(app, runtime, {"update_id": 60, "business_message": business_message()},
             until=lambda: service_env.calls("POST", MESSAGE))
    assert len(service_env.calls("POST", MESSAGE)) == 1


def test_heartbeat_file_appears_while_running_and_disappears_on_stop(runtime, service_env):
    application = Application.builder().token(BOT_TOKEN).updater(None).build()
    store = Store()

    async def scenario():
        runtime.attach(application, store)
        for _ in range(200):
            if store.read("bridge"):
                break
            await asyncio.sleep(0.01)
        seen = store.read("bridge")
        await runtime.stop()
        return seen

    seen = asyncio.run(scenario())
    assert isinstance(seen.get("heartbeat_at"), int) and set(seen["counters"]) >= {"dropped", "jobs_done"}
    assert store.read("bridge") == {}


# --- второй круг: швы между плагином, Hermes и сервисом ---

@pytest.mark.parametrize("mode, retry_in", [("400", None), ("403", None), ("401", None), ("429", 8), ("500", 30)])
def test_notify_refused_by_telegram_is_final_only_when_telegram_said_no(tg, mode, retry_in):
    """«Текст слишком длинный», «бот заблокирован» — повтор дал бы тот же отказ. Повторяем,
    когда Telegram просит подождать или его ответ неясен."""
    tg.mode = mode
    outcome = with_bot(tg, lambda bot: executor_for(bot).execute(NOTIFY_OWNER, {"text": "карточка"}))
    assert not outcome.ok and outcome.retry_in == retry_in
    assert [m for m, _ in tg.calls].count("sendMessage") == 1


def test_long_card_with_emoji_fits_telegram_limit_on_the_wire(tg):
    from shturman_core.textlimits import utf16_len

    outcome = with_bot(tg, lambda bot: executor_for(bot).execute(NOTIFY_OWNER, {"text": "😀" * 3000}))
    assert outcome.ok and utf16_len(tg.calls[-1][1]["text"]) <= 4096


def test_schema_violation_through_the_real_hermes_llm_facade(monkeypatch):
    """Шов с Hermes: настоящий `PluginLlm`, подставлен только провайдер. Ответ модели, который
    не подходит под схему, доходит до сервиса целиком и без повторов."""
    plugin_llm = pytest.importorskip("agent.plugin_llm")
    import types

    monkeypatch.setattr(plugin_llm, "_resolve_task_ownership",
                        lambda plugin_id: (frozenset({"shturman_extract", "shturman_reply", "shturman_watch"}), frozenset()))
    seen = []
    answers = ['{"commitments": [{"message": "1", "what": "прислать смету", "лишнее": true}]}', "не JSON вовсе",
               '```json\n{"commitments": []}\n```']

    async def provider(**kw):
        seen.append(kw)
        message = types.SimpleNamespace(content=answers[len(seen) - 1])
        return "stub", "stub-model", types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)],
                                                           model="stub-model", usage=None)

    llm = plugin_llm.make_plugin_llm_for_test(
        plugin_id="shturman", policy=plugin_llm._TrustPolicy(plugin_id="shturman"), async_caller=provider)
    schema = {"type": "object", "additionalProperties": False, "required": ["commitments"], "properties": {
        "commitments": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                    "required": ["message", "what"],
                                                    "properties": {"message": {"type": "integer"}, "what": {"type": "string"}}}}}}
    payload = {"instructions": "Найди обязательства", "input": "Пришлю смету", "json_schema": schema,
               "schema_name": "commitments", "task": "shturman_extract", "max_tokens": 500}

    async def no_service(*args, **kwargs):
        return {}

    executor = Executor(no_service, llm=llm, bot=None, owner=lambda: {})
    violating = asyncio.run(executor.execute(LLM_STRUCTURED, payload))
    garbage = asyncio.run(executor.execute(LLM_STRUCTURED, payload))
    fenced = asyncio.run(executor.execute(LLM_STRUCTURED, payload))
    assert violating.ok and violating.result["schema_valid"] is False
    assert violating.result["parsed"] == {"commitments": [{"message": "1", "what": "прислать смету", "лишнее": True}]}
    assert violating.result["text"] == answers[0] and violating.result["model"] == "stub-model"
    assert garbage.ok and garbage.result["parsed"] is None and garbage.result["text"] == "не JSON вовсе"
    assert garbage.result["schema_valid"] is False
    assert fenced.ok and fenced.result == {"parsed": {"commitments": []}, "text": answers[2], "model": "stub-model",
                                           "schema_valid": True}
    assert len(seen) == 3                                      # по одному вызову модели на задание
    first = seen[0]
    assert first["task"] == "shturman_extract" and first["extra_body"] == {"response_format": {"type": "json_object"}}
    header = first["messages"][-1]["content"][0]["text"]
    assert "JSON schema:" in header and '"commitments"' in header and "Schema name: commitments" in header


def test_sending_switch_reaches_the_status_block(runtime, service_env):
    from shturman_core import bridge_stats

    service_env.replies[("GET", "/api/outbox/policy")] = (200, {"sending": False, "hard_daily_cap": 0, "policy": {}})
    application = Application.builder().token(BOT_TOKEN).updater(None).build()
    store = Store()

    async def scenario():
        runtime.attach(application, store)
        for _ in range(300):
            if store.read("bridge").get("sending") is False:
                break
            await asyncio.sleep(0.01)
        status = bridge_stats.status(store, configured=True)
        await runtime.stop()
        return status

    status = asyncio.run(scenario())
    assert status["sending"] is False and status["executor_running"] is True
    assert service_env.calls("PUT", "/api/outbox/policy") == []            # только чтение


def test_old_service_without_the_switch_leaves_the_flag_unknown(runtime, service_env):
    service_env.replies[("GET", "/api/outbox/policy")] = (200, {"policy": {"daily_cap": 400}})
    application = Application.builder().token(BOT_TOKEN).updater(None).build()

    async def scenario():
        runtime.attach(application, Store())
        for _ in range(200):
            if service_env.calls("GET", "/api/outbox/policy"):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        await runtime.stop()

    asyncio.run(scenario())
    assert runtime.stats.sending is None


def owner_requests(service):
    return [(r["method"], r["json"]) for r in service.requests if r["path"] == "/api/owner"]


def test_recovery_link_reaches_the_service_through_the_real_state_files(runtime, service_env):
    """Вход по ссылке восстановления (процесс дашборда) → отметка на диске → шлюз говорит сервису."""
    from shturman_core.auth import Auth
    from shturman_core.state import OWNER_UNBOUND

    application = Application.builder().token(BOT_TOKEN).updater(None).build()
    store = Store()

    async def scenario():
        runtime.attach(application, store)
        runtime.ingest.idle = 0.02
        for _ in range(300):
            if service_env.calls("PUT", "/api/owner"):
                break
            await asyncio.sleep(0.01)
        auth = Auth(Store())
        assert auth.redeem_activation(auth.issue_activation()) is True
        runtime.ingest._wake.set()
        for _ in range(300):
            if service_env.calls("DELETE", "/api/owner"):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        await runtime.stop()

    asyncio.run(scenario())
    assert owner_requests(service_env) == [("PUT", {"user_id": 42, "chat_id": 42}), ("DELETE", None)]
    assert store.read_strict(OWNER_UNBOUND) is None


def test_fresh_gateway_with_empty_state_does_not_unbind_the_services_owner(runtime, service_env):
    """Первая установка или потерянный каталог состояния: сервис своего владельца не теряет."""
    Store().delete("owner")
    application = Application.builder().token(BOT_TOKEN).updater(None).build()

    async def scenario():
        runtime.attach(application, Store())
        runtime.ingest.idle = 0.02
        await asyncio.sleep(0.3)
        await runtime.stop()

    asyncio.run(scenario())
    assert owner_requests(service_env) == []


def test_unreadable_owner_file_neither_unbinds_nor_loses_messages(app, runtime, service_env):
    """Сбой чтения файла владельца: сервису не говорят «владелец отвязан», сообщение не теряется."""
    owner_file = Path(Store().root) / "owner.json"
    owner_file.write_text("{ оборванная запись", encoding="utf-8")
    dispatch(app, runtime, {"update_id": 70, "business_message": business_message()},
             until=lambda: service_env.calls("POST", MESSAGE))
    assert len(service_env.calls("POST", MESSAGE)) == 1 and owner_requests(service_env) == []


def test_three_auxiliary_tasks_are_registered_with_hermes():
    import shturman_tools

    registered = []

    class Ctx:
        def register_auxiliary_task(self, key, **kwargs):
            registered.append((key, kwargs["display_name"], kwargs["defaults"]))

    assert shturman_tools.register_auxiliary_tasks(Ctx()) == ["shturman_extract", "shturman_reply", "shturman_watch"]
    assert all(name.startswith("Штурман") for _, name, _ in registered)
