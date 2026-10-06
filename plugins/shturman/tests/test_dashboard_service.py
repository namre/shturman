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

PASSWORD = "Obl@chnyj-Parol-2FA-7391"
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
])
def test_everything_outside_the_allowlist_is_refused_before_the_service(web, service_env, method, path):
    response = web.request(method, f"{PREFIX}/service/{path}", json={"user_id": 1, "chat_id": 1})
    assert response.status_code in (404, 405) and service_env.requests == []


def test_password_passes_through_once_and_leaves_no_trace_in_logs(web, service_env, caplog):
    service = service_env
    service.replies[("POST", "/api/tg/login/abc/password")] = (200, {"state": "done"})
    with caplog.at_level(logging.DEBUG):
        response = web.post(f"{PREFIX}/service/tg/login/abc/password", json={"password": PASSWORD})
    assert response.status_code == 200 and response.json() == {"state": "done"}
    assert service.requests[-1]["json"] == {"password": PASSWORD}
    assert service.requests[-1]["headers"]["content-type"] == "application/json"
    assert PASSWORD not in caplog.text and service.token not in caplog.text


def test_service_outage_with_a_password_in_flight_logs_nothing_secret(web, monkeypatch, caplog):
    monkeypatch.setenv("SHTURMAN_SERVICE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SHTURMAN_API_TOKEN", "t" * 40)
    with caplog.at_level(logging.DEBUG):
        response = web.post(f"{PREFIX}/service/tg/login/abc/password", json={"password": PASSWORD})
    assert response.status_code == 502 and response.json() == {"detail": "Сервис переписки недоступен."}
    assert PASSWORD not in caplog.text and "t" * 40 not in caplog.text
    for record in caplog.records:
        assert record.exc_info is None           # без трассировок: в них могли бы оказаться данные запроса


def test_service_errors_are_passed_to_the_page_as_they_are(web, service_env):
    service_env.replies[("PUT", "/api/chats/7/excluded")] = (
        409, {"error": "идёт импорт экспорта — измените исключения после его окончания", "code": "import_running"})
    response = web.put(f"{PREFIX}/service/chats/7/excluded", json={"excluded": True})
    assert response.status_code == 409 and response.json()["code"] == "import_running"
    service_env.replies[("GET", "/api/imports/" + "a" * 32 + "/scan")] = (202, {"state": "scanning"})
    assert web.get(f"{PREFIX}/service/imports/{'a' * 32}/scan?wait=1").status_code == 202


def test_wrong_service_token_is_not_shown_as_the_owners_session_expiring(web, service_env, monkeypatch):
    monkeypatch.setenv("SHTURMAN_API_TOKEN", "w" * 40)
    response = web.get(f"{PREFIX}/service/status")
    assert response.status_code == 502 and "токен" in response.json()["detail"]


def test_without_service_the_passage_is_closed_politely(web, service):
    response = web.get(f"{PREFIX}/service/status")
    assert response.status_code == 503 and service.requests == []


def test_upload_is_streamed_to_the_service_byte_for_byte(web, service_env):
    service = service_env
    service.replies[("POST", "/api/imports")] = (201, {"import_id": "a" * 32, "size_bytes": 3_000_000, "state": "uploaded"})
    chunk = bytes(range(256)) * 400                      # 102 400 байт

    def body():
        for _ in range(30):
            yield chunk

    response = web.post(f"{PREFIX}/service/imports", content=body(), headers={"Content-Type": "application/json"})
    assert response.status_code == 201 and response.json()["import_id"] == "a" * 32
    assert service.requests[-1]["raw"] == chunk * 30

    exact = b'{"about": "result.json"}' * 1000
    web.post(f"{PREFIX}/service/imports", content=exact, headers={"Content-Type": "application/json"})
    sent = service.requests[-1]
    assert sent["raw"] == exact and sent["headers"]["content-length"] == str(len(exact))


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
