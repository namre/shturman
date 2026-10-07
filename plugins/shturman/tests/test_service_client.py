"""Клиент сервиса переписки — против маленького настоящего HTTP-сервера."""

import socket
import time

import pytest

from shturman_core import service_client, service_routes
from shturman_core.service_client import (
    NotAllowed, ServiceClient, ServiceError, ServiceUnavailable, normalize_base_url,
)


def client(service, routes=service_routes.BRIDGE, **kwargs) -> ServiceClient:
    return ServiceClient(service.url, service.token, allow=routes, **kwargs)


def test_request_carries_token_json_and_query(service):
    service.replies[("POST", "/api/jobs/claim")] = (200, {"jobs": [{"id": 1}]})
    out = client(service).request("POST", "/api/jobs/claim", json_body={"kinds": ["llm.text"], "имя": "Пётр"})
    assert out == {"jobs": [{"id": 1}]}
    sent = service.requests[-1]
    assert sent["headers"]["authorization"] == f"Bearer {service.token}"
    assert sent["headers"]["content-type"] == "application/json"
    assert sent["json"] == {"kinds": ["llm.text"], "имя": "Пётр"}

    tools = client(service, service_routes.TOOLS)
    tools.request("GET", "/api/commitments", query={"view": "today", "limit": 5, "person_id": None, "x": ""})
    assert service.requests[-1]["query"] == "view=today&limit=5"


def test_call_returns_status_code(service):
    service.replies[("GET", "/api/status")] = (202, {"state": "scanning"})
    assert client(service).call("GET", "/api/status") == (202, {"state": "scanning"})


def test_waiting_for_the_owner_is_never_mistaken_for_done(service):
    """Сервис со своим ботом согласований не применил действие: тот, кто читает только тело
    ответа (инструмент агента), должен увидеть «не выполнено», а не данные карточки."""
    waiting = {"status": "pending_confirmation", "action_id": 7, "expires_at": "2026-10-07T12:00:00+00:00",
               "summary": "Принять предложенное обязательство № 9 (Иван Петров → вам; срок 09.10.2026).",
               "note": "Ждёт вашего подтверждения в боте согласований."}
    service.replies[("POST", "/api/commitments/9/close")] = (202, waiting)
    tools = client(service, service_routes.TOOLS)
    assert tools.call("POST", "/api/commitments/9/close", json_body={}) == (202, waiting)   # как есть
    assert service_client.is_pending(202, waiting) and not service_client.is_pending(200, waiting)
    out = tools.request("POST", "/api/commitments/9/close", json_body={})
    assert out == {"ok": False, "applied": False, "status": "pending_confirmation", "action_id": 7,
                   "expires_at": "2026-10-07T12:00:00+00:00", "note": service_client.PENDING_NOTE}
    assert "НЕ выполнено" in out["note"] and "Иван Петров" not in str(out)
    # обычный ответ 202 («ещё считается») остаётся как был
    service.replies[("GET", "/api/commitments")] = (202, {"state": "scanning"})
    assert tools.request("GET", "/api/commitments") == {"state": "scanning"}
    assert not service_client.is_pending(202, {"state": "scanning"})


def test_route_outside_allowlist_never_reaches_the_network(service):
    with pytest.raises(NotAllowed) as caught:
        client(service, service_routes.TOOLS).request("PUT", "/api/outbox/policy", json_body={"x": 1})
    assert caught.value.code == "not_allowed"
    assert service.requests == []


def test_refusal_keeps_status_code_and_message(service):
    service.replies[("POST", "/api/ingest/business/message")] = (
        409, {"error": "неизвестное бизнес-подключение", "code": "unknown_connection"})
    with pytest.raises(ServiceError) as caught:
        client(service).request("POST", "/api/ingest/business/message", json_body={"message": {}})
    error = caught.value
    assert (error.status, error.code, error.message) == (409, "unknown_connection", "неизвестное бизнес-подключение")
    assert not isinstance(error, ServiceUnavailable)


def test_outbox_refusal_reason_becomes_code(service):
    service.replies[("POST", "/api/outbox/drafts")] = (
        429, {"error": "такой текст уже отправлялся", "reason": "duplicate_text", "retry_after": 600})
    with pytest.raises(ServiceError) as caught:
        client(service, service_routes.TOOLS).request("POST", "/api/outbox/drafts", json_body={})
    assert caught.value.code == "duplicate_text" and caught.value.payload["retry_after"] == 600


@pytest.mark.parametrize("status", [500, 503])
def test_server_error_means_unavailable(service, status):
    service.replies[("GET", "/api/status")] = (status, {"error": "архив занят"})
    with pytest.raises(ServiceUnavailable) as caught:
        client(service).request("GET", "/api/status")
    assert caught.value.status == status


def test_wrong_token_means_unavailable_and_is_not_echoed(service):
    wrong = ServiceClient(service.url, "w" * 40, allow=service_routes.BRIDGE)
    with pytest.raises(ServiceUnavailable) as caught:
        wrong.request("GET", "/api/status")
    assert caught.value.code == "unauthorized"
    assert "w" * 40 not in str(caught.value) and "w" * 40 not in repr(wrong)


def test_no_listener_means_unavailable_without_token_in_text():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    lonely = ServiceClient(f"http://127.0.0.1:{port}", "s" * 40, allow=service_routes.BRIDGE, timeout=1)
    with pytest.raises(ServiceUnavailable) as caught:
        lonely.request("GET", "/api/status")
    assert "s" * 40 not in str(caught.value)
    assert caught.value.__cause__ is None        # в цепочке причин — заголовки запроса с токеном


def test_slow_service_times_out_quickly(service):
    def slow(record):
        time.sleep(1.5)
        return 200, {"ok": True}

    service.replies[("GET", "/api/status")] = slow
    started = time.monotonic()
    with pytest.raises(ServiceUnavailable):
        client(service, timeout=0.3).request("GET", "/api/status")
    assert time.monotonic() - started < 1.2


def test_redirect_is_not_followed(service):
    service.replies[("GET", "/api/status")] = (302, ({"Location": service.url + "/api/owner"}, {}))
    with pytest.raises(ServiceUnavailable):
        client(service).request("GET", "/api/status")
    assert [r["path"] for r in service.requests] == ["/api/status"]


def test_proxy_from_environment_is_ignored(service, monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert client(service).request("GET", "/api/status") == {"ok": True}


def test_non_json_answer_is_unavailable(service):
    service.replies[("GET", "/api/status")] = (200, b"<html>")
    with pytest.raises(ServiceUnavailable):
        client(service).request("GET", "/api/status")


def test_without_token_the_service_is_simply_absent(monkeypatch):
    assert service_client.configured() is False
    assert ServiceClient.from_env(service_routes.BRIDGE) is None
    monkeypatch.setenv("SHTURMAN_API_TOKEN", "x" * 40)
    made = ServiceClient.from_env(service_routes.BRIDGE)
    assert made is not None and made.base_url == service_client.DEFAULT_URL
    monkeypatch.setenv("SHTURMAN_SERVICE_URL", "ftp://example")
    assert service_client.configured() is False and ServiceClient.from_env(service_routes.BRIDGE) is None


@pytest.mark.parametrize("raw, expected", [
    ("", "http://127.0.0.1:8765"),
    ("http://127.0.0.1:8765/", "http://127.0.0.1:8765"),
    ("http://shturman:8765", "http://shturman:8765"),
    ("https://service.internal", "https://service.internal"),
    ("http://[::1]:8765", "http://[::1]:8765"),
    ("http://user:pass@127.0.0.1:8765", ""),
    ("http://127.0.0.1:8765/api", ""),
    ("http://127.0.0.1:8765/?x=1", ""),
    ("file:///etc/passwd", ""),
    ("127.0.0.1:8765", ""),
    ("http://127.0.0.1:notaport", ""),
])
def test_base_url_is_normalized_or_rejected(raw, expected):
    assert normalize_base_url(raw) == expected
