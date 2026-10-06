"""Перечни разрешённых маршрутов: что кому можно и что нельзя никому из этой роли."""

import re
from pathlib import Path

import pytest

from shturman_core.service_routes import BRIDGE, TOOLS, UI, allowed

# Всё, что меняет правила отправки, доверенных, автоответ, наблюдателя, исключения чатов,
# аккаунты Telegram и импорт, — действия владельца в интерфейсе. Агенту они недоступны.
OWNER_ONLY = [
    ("PUT", "/api/outbox/policy"),
    ("PUT", "/api/outbox/chats/7"),
    ("PUT", "/api/outbox/autoreply"),
    ("POST", "/api/outbox/trusted"),
    ("DELETE", "/api/outbox/trusted"),
    ("POST", "/api/outbox/drafts/7/cancel"),
    ("POST", "/api/watch/rules"),
    ("PUT", "/api/watch/rules/7"),
    ("DELETE", "/api/watch/rules/7"),
    ("PUT", "/api/chats/7/excluded"),
    ("POST", "/api/tg/login"),
    ("POST", "/api/tg/login/abc/password"),
    ("POST", "/api/tg/accounts/7/logout"),
    ("POST", "/api/tg/accounts/7/pause"),
    ("POST", "/api/tg/accounts/7/resume"),
    ("PUT", "/api/tg/accounts/7/options"),
    ("POST", "/api/tg/accounts/7/sync"),
    ("POST", "/api/imports"),
    ("POST", "/api/imports/" + "a" * 32 + "/run"),
    ("DELETE", "/api/imports/" + "a" * 32),
    ("POST", "/api/commitments/7/accept"),
    ("POST", "/api/commitments/7/reject"),
    ("POST", "/api/people/merge"),
    ("POST", "/api/people/7/split"),
    ("DELETE", "/api/people/7/aliases"),
    ("POST", "/api/people/proposals/7/reject"),
    ("POST", "/api/processing/run"),
]
INTERNAL = [
    ("PUT", "/api/owner"),
    ("POST", "/api/jobs/claim"),
    ("POST", "/api/jobs/7/complete"),
    ("POST", "/api/jobs/7/fail"),
    ("POST", "/api/callbacks/telegram"),
    ("POST", "/api/ingest/business/connection"),
    ("POST", "/api/ingest/business/message"),
    ("POST", "/api/ingest/business/deleted"),
]


@pytest.mark.parametrize("method, path", OWNER_ONLY + INTERNAL)
def test_agent_tools_cannot_reach_owner_or_internal_routes(method, path):
    assert not allowed(TOOLS, method, path)


@pytest.mark.parametrize("method, path", [
    ("POST", "/api/outbox/drafts"),
    ("GET", "/api/commitments"),
    ("GET", "/api/commitments/7"),
    ("POST", "/api/commitments/7/close"),
    ("POST", "/api/commitments/7/cancel"),
    ("POST", "/api/commitments/7/reopen"),
    ("POST", "/api/commitments/7/reschedule"),
    ("GET", "/api/people"),
    ("GET", "/api/people/7"),
    ("POST", "/api/people/7/aliases"),
])
def test_agent_tools_reach_exactly_their_routes(method, path):
    assert allowed(TOOLS, method, path)


def test_agent_tool_list_has_nothing_else():
    assert len(TOOLS) == 7


@pytest.mark.parametrize("method, path", INTERNAL + [
    ("POST", "/mcp"), ("GET", "/health"), ("POST", "/api/outbox/drafts"), ("POST", "/api/processing/run"),
])
def test_owner_ui_cannot_reach_internal_routes(method, path):
    assert not allowed(UI, method, path)


@pytest.mark.parametrize("method, path", [
    (m, p) for m, p in OWNER_ONLY if p != "/api/processing/run"
] + [
    ("GET", "/api/status"), ("GET", "/api/embeddings/status"), ("GET", "/api/processing/status"),
    ("GET", "/api/chats"), ("GET", "/api/imports"), ("GET", "/api/imports/" + "a" * 32),
    ("GET", "/api/imports/" + "a" * 32 + "/scan"), ("GET", "/api/tg/accounts"), ("GET", "/api/tg/login/abc"),
    ("POST", "/api/tg/login/abc/cancel"), ("GET", "/api/tg/accounts/7/dialogs"), ("GET", "/api/tg/accounts/7/sync"),
    ("GET", "/api/outbox/drafts"), ("GET", "/api/outbox/policy"), ("GET", "/api/outbox/autoreply"),
    ("GET", "/api/outbox/trusted"), ("GET", "/api/watch/rules"), ("GET", "/api/watch/hits"),
    ("GET", "/api/commitments"), ("GET", "/api/commitments/7"), ("POST", "/api/commitments/7/close"),
    ("GET", "/api/people"), ("GET", "/api/people/proposals"), ("GET", "/api/people/7"),
    ("POST", "/api/people/7/aliases"),
])
def test_owner_ui_reaches_what_its_pages_need(method, path):
    assert allowed(UI, method, path)


@pytest.mark.parametrize("method, path", INTERNAL + [("GET", "/api/status")])
def test_bridge_reaches_its_routes(method, path):
    assert allowed(BRIDGE, method, path)


@pytest.mark.parametrize("method, path", OWNER_ONLY + [("POST", "/api/outbox/drafts"), ("GET", "/api/commitments")])
def test_bridge_reaches_nothing_else(method, path):
    assert not allowed(BRIDGE, method, path)


@pytest.mark.parametrize("path", [
    "/api/status/", "/api/status/../owner", "/api//status", "/api/status?x=1", "/api/status#x",
    "/API/STATUS", "api/status", "/api/commitments/7/close/../accept", "/api/commitments/-7",
    "/api/commitments/7 ", "/api/commitments/７", "/api/imports/" + "A" * 32, "/api/imports/" + "a" * 31,
    "/api/tg/login/a/b/password", "/api/people/7/aliases\n", "/api/status\x00", "", "/",
])
def test_path_tricks_do_not_pass(path):
    for routes in (BRIDGE, TOOLS, UI):
        for method in ("GET", "POST", "PUT", "DELETE"):
            assert not allowed(routes, method, path)


def test_method_must_match_and_types_are_checked():
    assert allowed(UI, "get", "/api/status")
    assert not allowed(UI, "POST", "/api/status")
    assert not allowed(UI, "PATCH", "/api/status") and not allowed(UI, "HEAD", "/api/status")
    assert not allowed(UI, "GET", None) and not allowed(UI, None, "/api/status")


# --- сверка с кодом сервиса: перечни не должны разойтись с настоящими маршрутами ---

_SERVICE = Path(__file__).resolve().parents[3] / "service" / "src" / "shturman"
_ROUTE = re.compile(r"""Route\(\s*f?["']([^"']+)["']\s*,[^\n]*?methods=\[([^\]]+)\]""")
_SAMPLES = {"{import_id}": "a" * 32, "{login_id}": "abc", "{action}": "close", "{account}": "/api/tg/accounts/7"}
# Маршруты сервиса, которые из плагина не вызывает никто: создание черновика доступно только
# агенту, запуск обработки и MCP — не через плагин.
_NOBODY = {("POST", "/api/processing/run"), ("POST", "/mcp")}


def _service_routes() -> list[tuple[str, str]]:
    found = []
    for source in sorted(_SERVICE.rglob("*.py")):
        for path, methods in _ROUTE.findall(source.read_text(encoding="utf-8")):
            for placeholder, sample in _SAMPLES.items():
                path = path.replace(placeholder, sample)
            path = re.sub(r"\{\w+:int\}", "7", path)
            for method in re.findall(r"[A-Z]+", methods):
                found.append((method, path))
    return found


@pytest.mark.skipif(not _SERVICE.is_dir(), reason="код сервиса рядом не лежит")
def test_every_service_route_is_assigned_and_every_pattern_is_real():
    real = _service_routes()
    assert len(real) > 50                                   # разбор действительно что-то нашёл
    unassigned = [(m, p) for m, p in real
                  if (m, p) not in _NOBODY and not any(allowed(r, m, p) for r in (BRIDGE, TOOLS, UI))]
    assert unassigned == []                                 # новый маршрут сервиса нужно явно отнести к роли
    for routes in (BRIDGE, TOOLS, UI):
        for method, pattern in routes:
            assert any(m == method and pattern.fullmatch(p) for m, p in real), (method, pattern.pattern)
