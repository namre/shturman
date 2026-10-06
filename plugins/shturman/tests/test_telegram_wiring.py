"""Обработчики Telegram на настоящей библиотеке python-telegram-bot, без сети.

В CI библиотеки нет (она приходит вместе с Hermes), поэтому там тест пропускается.
Локально: запускать интерпретатором, в котором установлен Hermes.
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

telegram = pytest.importorskip("telegram")
from telegram.ext import Application, CallbackContext  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shturman_telegram  # noqa: E402
from shturman_core.pairing import Pairing  # noqa: E402
from shturman_core.state import Store  # noqa: E402

OWNER = {"id": 42, "is_bot": False, "first_name": "Иван", "last_name": "Иванов", "username": "ivan"}
STRANGER = {"id": 99, "is_bot": False, "first_name": "Пётр"}


def message(text, user=OWNER, mid=1):
    return {"message_id": mid, "date": int(time.time()), "text": text,
            "chat": {"id": user["id"], "type": "private"}, "from": user}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SHTURMAN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(shturman_telegram, "business_plugin_enabled", lambda: False)
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    monkeypatch.setattr(telegram.Message, "reply_text", fake_reply)
    application = Application.builder().token("1234567890:" + "A" * 35).updater(None).build()
    shturman_telegram.wire(application, None)
    return application, replies


def dispatch(bundle, payload):
    app = bundle[0] if isinstance(bundle, tuple) else bundle
    """Как диспетчер библиотеки: в каждой группе сообщение получает первый подошедший обработчик."""
    update = telegram.Update.de_json(payload, app.bot)
    handled = []

    async def go():
        for group in sorted(app.handlers):
            for handler in app.handlers[group]:
                check = handler.check_update(update)
                if check is None or check is False:
                    continue
                context = CallbackContext.from_update(update, app)
                await handler.handle_update(update, app, check, context)
                handled.append((group, handler.callback.__name__))
                break

    asyncio.run(go())
    return handled


def test_ordinary_message_is_left_to_hermes(app):
    handled = dispatch(app, {"update_id": 1, "message": message("привет")})
    assert [h for h in handled if h[0] == 0] == []       # в группе ядра мы ничего не перехватили
    assert app[1] == []


def test_start_with_token_binds_owner(app):
    started = Pairing(Store()).start()
    handled = dispatch(app, {"update_id": 2, "message": message(f"/start {started['token']}")})
    assert (0, "on_pairing_message") in handled
    owner = Store().read("owner")
    assert (owner["user_id"], owner["chat_id"], owner["name"], owner["username"]) == \
        (42, 42, "Иван Иванов", "ivan")
    assert app[1] == [shturman_telegram.REPLIES["bound"]]
    # окно закрылось — следующее сообщение снова идёт в Hermes
    assert [h for h in dispatch(app, {"update_id": 3, "message": message("привет", mid=2)}) if h[0] == 0] == []


def test_bare_start_during_pairing_gets_a_hint_not_the_stock_reply(app):
    Pairing(Store()).start()
    handled = dispatch(app, {"update_id": 4, "message": message("/start", user=STRANGER)})
    assert (0, "on_pairing_message") in handled
    assert app[1] == [shturman_telegram.REPLIES["hint"]]
    assert Store().read("owner") == {}


def test_typed_code_binds(app):
    started = Pairing(Store()).start()
    dispatch(app, {"update_id": 5, "message": message(started["code"])})
    assert Store().read("owner")["user_id"] == 42


def test_business_message_never_reaches_core_without_business_plugin(app):
    payload = {"update_id": 6, "business_message": dict(message("здравствуйте", user=STRANGER),
                                                        business_connection_id="bc1")}
    handled = dispatch(app, payload)
    assert (0, "drop_business_message") in handled
    assert app[1] == []


def test_business_message_is_not_treated_as_pairing(app):
    started = Pairing(Store()).start()
    payload = {"update_id": 7, "business_message": dict(message(f"/start {started['token']}", user=STRANGER),
                                                        business_connection_id="bc1")}
    handled = dispatch(app, payload)
    assert (0, "on_pairing_message") not in handled
    assert Store().read("owner") == {}


def test_guard_is_off_when_business_plugin_is_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SHTURMAN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(shturman_telegram, "business_plugin_enabled", lambda: True)
    application = Application.builder().token("1234567890:" + "A" * 35).updater(None).build()
    shturman_telegram.wire(application, None)
    names = [h.callback.__name__ for h in application.handlers.get(0, [])]
    assert "drop_business_message" not in names and "on_pairing_message" in names


def test_business_connection_is_recorded(app):
    payload = {"update_id": 8, "business_connection": {
        "id": "bc1", "user": OWNER, "user_chat_id": 42, "date": int(time.time()),
        "is_enabled": True, "rights": {"can_reply": True},
    }}
    handled = dispatch(app, payload)
    assert (shturman_telegram.OBSERVER_GROUP, "observe") in handled
    state = Store().read("business")
    assert state["connected"] is True and state["can_reply"] is True and state["user_id"] == 42
