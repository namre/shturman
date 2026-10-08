"""Маршруты дашборда для сервиса переписки: проход для страниц владельца и состояние моста.

Нужен FastAPI — он приходит вместе с Hermes, поэтому в CI тест пропускается.
"""

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))

from shturman_core.pairing import Pairing  # noqa: E402
from shturman_core.state import Store  # noqa: E402

SECRET = "Zametka-Vladelca-7391-ne-dlya-zhurnala"
PREFIX = "/api/plugins/shturman"


@pytest.fixture(scope="module")
def plugin_api():
    # Как Hermes (hermes_cli/web_server_dashboard.py:854-864): модуль регистрируется по имени до выполнения.
    name = "hermes_dashboard_plugin_shturman"
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


@pytest.fixture
def web(plugin_api, tmp_path, monkeypatch):
    monkeypatch.setenv("SHTURMAN_STATE_DIR", str(tmp_path / "state"))
    app = FastAPI()
    app.include_router(plugin_api.router, prefix=PREFIX)
    with TestClient(app) as client:
        yield client


def test_allowed_request_passes_with_the_service_token_and_nothing_from_the_browser(web, service_env):
    service = service_env
    service.replies[("GET", "/api/chats")] = (200, {"chats": [{"id": 1, "title": "Пётр"}], "total": 1})
    response = web.get(f"{PREFIX}/service/chats?query=%D0%9F%D1%91%D1%82%D1%80&limit=5",
                       headers={"Authorization": "Bearer browser-session", "Cookie": "hermes_session=abc"})
    assert response.status_code == 200 and response.json()["chats"][0]["title"] == "Пётр"
    assert response.headers["cache-control"] == "no-store"
    sent = service.requests[-1]
    assert (sent["method"], sent["path"], sent["query"]) == ("GET", "/api/chats", "query=%D0%9F%D1%91%D1%82%D1%80&limit=5")
    assert sent["headers"]["authorization"] == f"Bearer {service.token}"      # токен подставил сервер
    assert "cookie" not in sent["headers"]
    assert service.token not in response.text and service.token not in str(response.headers)


@pytest.mark.parametrize("method, path", [
    ("PUT", "owner"), ("POST", "jobs/claim"), ("POST", "jobs/7/complete"), ("POST", "jobs/7/fail"),
    ("POST", "callbacks/telegram"), ("POST", "ingest/business/connection"), ("POST", "ingest/business/message"),
    ("POST", "ingest/business/deleted"), ("POST", "outbox/drafts"), ("POST", "processing/run"),
    ("GET", "status/%2e%2e/owner"), ("PUT", "status/%2e%2e/owner"), ("GET", "status/"), ("GET", ""),
    ("GET", "unknown"), ("POST", "status"), ("GET", "%2e%2e/mcp"), ("GET", "tg/login/a%2fb"),
    # страница настройки переписки: проход дашборда к ней не ведёт никаким написанием пути
    ("GET", "%2e%2e/shturman-setup/"), ("GET", "../shturman-setup/"), ("POST", "%2e%2e/shturman-setup/api/login"),
    ("GET", "status/%2e%2e/%2e%2e/shturman-setup/"), ("GET", "shturman-setup/"), ("GET", "%2fshturman-setup/"),
    ("GET", "setup"), ("POST", "setup/link"), ("POST", "setup-link"), ("POST", "setup/logout-all"),
    # вход в аккаунт Telegram, управление аккаунтами, выбор чатов, импорт выгрузки: с версии 0.0.6
    # только на странице настройки переписки — через дашборд не идёт ни QR, ни пароль, ни выгрузка
    ("POST", "tg/login"), ("GET", "tg/login/abc"), ("POST", "tg/login/abc/password"), ("POST", "tg/login/abc/cancel"),
    ("POST", "tg/accounts/7/logout"), ("POST", "tg/accounts/7/pause"), ("POST", "tg/accounts/7/resume"),
    ("POST", "tg/accounts/7/sync"), ("PUT", "tg/accounts/7/options"), ("GET", "tg/accounts/7/dialogs"),
    ("GET", "tg/accounts/7/sync"), ("POST", "imports"), ("GET", "imports"), ("GET", "imports/" + "a" * 32),
    ("DELETE", "imports/" + "a" * 32), ("GET", "imports/" + "a" * 32 + "/scan"), ("POST", "imports/" + "a" * 32 + "/run"),
])
def test_everything_outside_the_allowlist_is_refused_before_the_service(web, service_env, method, path):
    response = web.request(method, f"{PREFIX}/service/{path}", json={"user_id": 1, "chat_id": 1})
    assert response.status_code in (404, 405) and service_env.requests == []


def test_telegram_password_never_travels_through_the_dashboard(web, service_env, caplog):
    """Облачный пароль Telegram вводится только на странице настройки переписки. Если его всё же
    отправят в проход дашборда, запрос отклоняется до сервиса и в журнал не попадает."""
    with caplog.at_level(logging.DEBUG):
        response = web.post(f"{PREFIX}/service/tg/login/abc/password", json={"password": SECRET})
    assert response.status_code == 404 and service_env.requests == []
    assert SECRET not in caplog.text and SECRET not in response.text


def test_request_body_passes_through_once_and_leaves_no_trace_in_logs(web, service_env, caplog):
    """Тело запроса (здесь — заметки владельца на странице памяти) передаётся как есть и не пишется в журнал."""
    service = service_env
    service.replies[("PUT", "/api/pages/7/owner-block")] = (200, {"ok": True})
    with caplog.at_level(logging.DEBUG):
        response = web.put(f"{PREFIX}/service/pages/7/owner-block", json={"text": SECRET})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert service.requests[-1]["json"] == {"text": SECRET}
    assert service.requests[-1]["headers"]["content-type"] == "application/json"
    assert SECRET not in caplog.text and service.token not in caplog.text


def test_service_outage_with_a_request_in_flight_logs_nothing_secret(web, monkeypatch, caplog):
    monkeypatch.setenv("SHTURMAN_SERVICE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SHTURMAN_API_TOKEN", "t" * 40)
    with caplog.at_level(logging.DEBUG):
        response = web.put(f"{PREFIX}/service/pages/7/owner-block", json={"text": SECRET})
    assert response.status_code == 502 and response.json() == {"detail": "Сервис переписки недоступен."}
    assert SECRET not in caplog.text and "t" * 40 not in caplog.text
    for record in caplog.records:
        assert record.exc_info is None           # без трассировок: в них могли бы оказаться данные запроса


def test_service_errors_are_passed_to_the_page_as_they_are(web, service_env):
    service_env.replies[("PUT", "/api/chats/7/excluded")] = (
        409, {"error": "идёт импорт экспорта — измените исключения после его окончания", "code": "import_running"})
    response = web.put(f"{PREFIX}/service/chats/7/excluded", json={"excluded": True})
    assert response.status_code == 409 and response.json()["code"] == "import_running"
    service_env.replies[("POST", "/api/pages/build")] = (202, {"state": "building"})
    assert web.post(f"{PREFIX}/service/pages/build?wait=1", json={}).status_code == 202


def test_waiting_for_the_owner_reaches_the_page_as_it_is(web, service_env):
    """У сервиса свой бот согласований: действие не применено, странице — тот же 202 и то же тело."""
    waiting = {"status": "pending_confirmation", "action_id": 7, "expires_at": "2026-10-07T12:00:00+00:00",
               "summary": "Добавить в доверенные: Иван Петров (идентификатор Telegram 2001).",
               "note": "Ждёт вашего подтверждения в боте согласований."}
    service_env.replies[("POST", "/api/outbox/trusted")] = (202, waiting)
    response = web.post(f"{PREFIX}/service/outbox/trusted", json={"tg_user_id": 2001})
    assert response.status_code == 202 and response.json() == waiting
    # страница может посмотреть ждущие действия и отменить, но не подтвердить
    service_env.replies[("GET", "/api/confirmations")] = (200, {"required": True, "pending": [{"id": 7}]})
    assert web.get(f"{PREFIX}/service/confirmations").json()["pending"] == [{"id": 7}]
    service_env.replies[("GET", "/api/confirmations/7")] = (200, {"id": 7, "status": "pending"})
    assert web.get(f"{PREFIX}/service/confirmations/7").json()["status"] == "pending"
    service_env.replies[("POST", "/api/confirmations/7/cancel")] = (200, {"ok": True})
    assert web.post(f"{PREFIX}/service/confirmations/7/cancel").json() == {"ok": True}
    sent = len(service_env.requests)
    for path in ("confirmations/7/confirm", "confirmations/7/apply", "callbacks/telegram"):
        assert web.post(f"{PREFIX}/service/{path}", json={}).status_code in (404, 405)
    assert len(service_env.requests) == sent                    # до сервиса не дошло


def test_wrong_service_token_is_not_shown_as_the_owners_session_expiring(web, service_env, monkeypatch):
    monkeypatch.setenv("SHTURMAN_API_TOKEN", "w" * 40)
    response = web.get(f"{PREFIX}/service/status")
    assert response.status_code == 502 and "токен" in response.json()["detail"]


def test_without_service_the_passage_is_closed_politely(web, service):
    response = web.get(f"{PREFIX}/service/status")
    assert response.status_code == 503 and service.requests == []


def test_a_large_body_is_streamed_to_the_service_byte_for_byte(web, service_env):
    """Выгрузка Telegram Desktop через дашборд больше не загружается (только на странице настройки
    переписки), но проход по-прежнему передаёт тело потоком и без изменений — здесь на маршруте,
    который в проходе остался."""
    service = service_env
    service.replies[("PUT", "/api/pages/7/owner-block")] = (200, {"ok": True})
    chunk = bytes(range(256)) * 400                      # 102 400 байт

    def body():
        for _ in range(30):
            yield chunk

    response = web.put(f"{PREFIX}/service/pages/7/owner-block", content=body(), headers={"Content-Type": "application/json"})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert service.requests[-1]["raw"] == chunk * 30

    exact = b'{"text": "owner note"}' * 1000
    web.put(f"{PREFIX}/service/pages/7/owner-block", content=exact, headers={"Content-Type": "application/json"})
    sent = service.requests[-1]
    assert sent["raw"] == exact and sent["headers"]["content-length"] == str(len(exact))


def test_an_export_upload_through_the_dashboard_is_refused_before_the_service(web, service_env):
    response = web.post(f"{PREFIX}/service/imports", content=b'{"about": "result.json"}' * 100,
                        headers={"Content-Type": "application/json"})
    assert response.status_code == 404 and service_env.requests == []


@pytest.mark.parametrize("query", ["a=" + "x" * 2100, "a=%20ok&b=к"])
def test_strange_query_strings(web, service_env, query):
    response = web.get(f"{PREFIX}/service/chats?{query}")
    assert response.status_code in (200, 400)
    if response.status_code == 400:
        assert service_env.requests == []


def test_state_reports_the_bridge_in_numbers(web, service_env):
    state = web.get(f"{PREFIX}/state").json()
    assert state["service"]["configured"] is True and state["service"]["executor_running"] is False
    assert state["service"]["counters"]["forwarded_messages"] == 0
    assert state["service"]["counters"]["not_stored_disabled"] == 0
    # признаки для страницы: «отправка выключена» и «бизнес-подключение выключено»; пока неизвестны
    assert state["service"]["sending"] is None and state["service"]["business_disabled"] is None
    assert "persona" in state and "pairing" in state     # прежнее содержимое на месте
    assert service_env.requests == []                    # состояние не ходит в сервис


def test_state_without_service(web):
    assert web.get(f"{PREFIX}/state").json()["service"]["configured"] is False


def test_confirming_the_owner_tells_the_service(web, service_env):
    store = Store()
    started = Pairing(store).start()
    assert Pairing(store).try_bind(started["code"], user_id=42, chat_id=42, name="Иван") == "accepted"
    response = web.post(f"{PREFIX}/pairing/confirm")
    assert response.status_code == 200 and response.json()["owner"]["user_id"] == 42
    assert service_env.calls("PUT", "/api/owner")[0]["json"] == {"user_id": 42, "chat_id": 42}


def test_confirming_the_owner_works_without_the_service(web, monkeypatch):
    monkeypatch.setenv("SHTURMAN_SERVICE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SHTURMAN_API_TOKEN", "t" * 40)
    store = Store()
    started = Pairing(store).start()
    Pairing(store).try_bind(started["code"], user_id=42, chat_id=42, name="Иван")
    assert web.post(f"{PREFIX}/pairing/confirm").json()["owner"]["user_id"] == 42


# --- шаг мастера «Переписка»: состояние страницы настройки переписки ---------------------------

PUBLIC = "https://assistant.example.com"
SETUP = "https://assistant.example.com:8443"            # то же имя, другой порт — так по умолчанию
PAGE = SETUP + "/shturman-setup/"
SETUP_NOTHING = {"enabled": True, "origin": SETUP, "reason": None, "origin_set": True, "tg_keys": False,
                 "accounts": 0, "own_bot": False, "owner_bound": False, "business_connected": False,
                 "own_model": False}
SETUP_EVERYTHING = {"enabled": True, "origin": SETUP, "reason": None, "origin_set": True, "tg_keys": True,
                    "accounts": 2, "own_bot": True, "owner_bound": True, "business_connected": True,
                    "own_model": True}


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setenv("HERMES_DASHBOARD_PUBLIC_URL", PUBLIC)


def test_correspondence_with_a_service_of_the_previous_version(web, service_env, public):
    """Сервис 0.0.5 объекта setup не отдаёт: мастер открывается и говорит «обновите экземпляр»."""
    service_env.replies[("GET", "/api/status")] = (200, {"messages": 1200, "chats": 14, "own_bot": False})
    out = web.get(f"{PREFIX}/correspondence").json()
    assert out == {"state": "outdated", "url": None, "setup": None, "archive": {"messages": 1200, "chats": 14}}


def test_correspondence_with_a_service_built_before_the_separate_address(web, service_env, public):
    """Объект setup есть, полей origin и reason нет: та сборка отдавала страницу на адресе дашборда.
    Мастер говорит «обновите» и ссылку не даёт — в том числе на адрес дашборда."""
    before = {k: v for k, v in SETUP_NOTHING.items() if k not in ("origin", "reason")}
    service_env.replies[("GET", "/api/status")] = (200, {"messages": 5, "chats": 1, "setup": before})
    response = web.get(f"{PREFIX}/correspondence")
    assert response.json() == {"state": "outdated", "url": None, "setup": None, "archive": {"messages": 5, "chats": 1}}
    assert "shturman-setup" not in response.text


def test_correspondence_when_nothing_is_configured(web, service_env, public):
    service_env.replies[("GET", "/api/status")] = (200, {"messages": 0, "chats": 0, "setup": SETUP_NOTHING})
    out = web.get(f"{PREFIX}/correspondence").json()
    assert out == {"state": "ok", "url": PAGE,
                   "setup": {"tg_keys": False, "business_connected": False, "accounts": 0},
                   "archive": {"messages": 0, "chats": 0}}


def test_correspondence_when_everything_is_configured(web, service_env, public):
    service_env.replies[("GET", "/api/status")] = (
        200, {"messages": 300000, "chats": 87, "setup": SETUP_EVERYTHING})
    out = web.get(f"{PREFIX}/correspondence").json()
    assert out["state"] == "ok" and out["url"] == PAGE
    assert out["setup"] == {"tg_keys": True, "business_connected": True, "accounts": 2}
    assert out["archive"] == {"messages": 300000, "chats": 87}


def test_correspondence_when_the_page_shares_the_dashboard_address(web, service_env, public):
    """Адрес страницы совпал с адресом дашборда: сервис страницу отключил, мастер кнопку не показывает."""
    same = dict(SETUP_NOTHING, enabled=False, origin=None, reason="same_origin")
    service_env.replies[("GET", "/api/status")] = (200, {"messages": 0, "chats": 0, "setup": same})
    out = web.get(f"{PREFIX}/correspondence").json()
    assert out["state"] == "same_origin" and out["url"] is None and out["setup"]["accounts"] == 0


def test_correspondence_never_links_to_the_dashboard_address(web, service_env, public):
    """Даже если сервис прислал адрес дашборда как адрес страницы, ссылки на него не будет."""
    for origin in (PUBLIC, PUBLIC + "/", "https://ASSISTANT.example.com:443"):
        service_env.replies[("GET", "/api/status")] = (200, {"setup": dict(SETUP_NOTHING, origin=origin)})
        response = web.get(f"{PREFIX}/correspondence")
        assert response.json()["state"] == "same_origin" and response.json()["url"] is None
        assert "shturman-setup" not in response.text


def test_correspondence_without_a_page_address(web, service_env, public):
    """Адрес страницы не задан: кнопки нет, страница открывается через туннель, состояние видно."""
    none = dict(SETUP_NOTHING, origin=None, reason="no_origin", tg_keys=True, accounts=1)
    service_env.replies[("GET", "/api/status")] = (200, {"messages": 40, "chats": 2, "setup": none})
    out = web.get(f"{PREFIX}/correspondence").json()
    assert out == {"state": "no_origin", "url": None,
                   "setup": {"tg_keys": True, "business_connected": False, "accounts": 1},
                   "archive": {"messages": 40, "chats": 2}}


@pytest.mark.parametrize("origin", [
    "http://assistant.example.com:8443", "javascript:alert(1)", "https://assistant.example.com:8443/evil",
    "https://user@assistant.example.com:8443", "https://assistant.example.com:8443\"><script>", 8443,
])
def test_correspondence_puts_only_a_checked_address_into_the_link(web, service_env, public, origin):
    service_env.replies[("GET", "/api/status")] = (200, {"setup": dict(SETUP_NOTHING, origin=origin)})
    response = web.get(f"{PREFIX}/correspondence")
    assert response.json()["url"] is None and response.json()["state"] == "no_origin"
    assert "script" not in response.text and "evil" not in response.text and "javascript" not in response.text


def test_correspondence_asks_the_service_one_read_only_question_and_leaks_nothing(web, service_env, public):
    link = "/shturman-setup/#" + "k" * 43
    service_env.replies[("GET", "/api/status")] = (
        200, {"messages": 5, "chats": 1, "setup": dict(SETUP_NOTHING, login_link=link, own_bot="да", tg_keys="да")})
    response = web.get(f"{PREFIX}/correspondence", headers={"Cookie": "hermes_session=abc"})
    assert response.status_code == 200
    assert [(r["method"], r["path"]) for r in service_env.requests] == [("GET", "/api/status")]
    assert "cookie" not in service_env.requests[0]["headers"]
    assert service_env.token not in response.text and "k" * 43 not in response.text
    assert response.json()["setup"]["tg_keys"] is None           # строка вместо признака — не признак
    assert "own_bot" not in response.json()["setup"]             # бот согласований в сводку не входит
    # Мастер только спрашивает: записать что-либо этим адресом нельзя.
    for method in ("POST", "PUT", "DELETE"):
        assert web.request(method, f"{PREFIX}/correspondence").status_code == 405


def test_correspondence_without_the_service(web, public):
    out = web.get(f"{PREFIX}/correspondence").json()
    assert out["state"] == "no_service" and out["setup"] is None and out["url"] is None


def test_correspondence_when_the_service_does_not_answer(web, monkeypatch, public, caplog):
    monkeypatch.setenv("SHTURMAN_SERVICE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SHTURMAN_API_TOKEN", "t" * 40)
    with caplog.at_level(logging.DEBUG):
        out = web.get(f"{PREFIX}/correspondence").json()
    assert out["state"] == "unreachable" and out["setup"] is None and out["url"] is None
    assert "t" * 40 not in caplog.text


def test_correspondence_when_the_service_rejects_the_plugin_token(web, service_env, public):
    service_env.token = "другой" * 8
    assert web.get(f"{PREFIX}/correspondence").json()["state"] == "unreachable"


def test_correspondence_in_emergency_mode_still_gives_the_page_address(web, service_env, monkeypatch):
    """Аварийный режим: дашборд запущен без внешнего адреса, а у страницы настройки адрес свой —
    он от дашборда не зависит, и ссылка остаётся."""
    monkeypatch.delenv("HERMES_DASHBOARD_PUBLIC_URL", raising=False)
    service_env.replies[("GET", "/api/status")] = (200, {"setup": SETUP_NOTHING})
    out = web.get(f"{PREFIX}/correspondence").json()
    assert out["state"] == "ok" and out["url"] == PAGE


def test_state_still_does_not_call_the_service_and_knows_nothing_of_the_plugin_to_install(web, service_env):
    state = web.get(f"{PREFIX}/state").json()
    assert state["business"] == {"connected": False, "can_reply": False, "updated_at": None}
    assert service_env.requests == []
    assert web.post(f"{PREFIX}/mark", json={"key": "correspondence_seen"}).status_code == 200
    assert web.post(f"{PREFIX}/mark", json={"key": "business_skipped"}).status_code == 200      # прежняя отметка
