"""Инструменты агента: что уходит в сервис, что возвращается агенту и куда они дотянуться не могут."""

import pytest

from shturman_core import service_routes, tools
from shturman_core.service_client import ServiceClient


@pytest.fixture
def client(service) -> ServiceClient:
    return ServiceClient(service.url, service.token, allow=service_routes.TOOLS)


def last(service):
    sent = service.requests[-1]
    return sent["method"], sent["path"], sent["query"], sent["json"]


def test_draft_is_created_and_the_answer_says_nothing_was_sent(service, client):
    service.replies[("POST", "/api/outbox/drafts")] = (200, {
        "draft_id": 12, "status": "pending", "channel": "business", "chat_id": 7,
        "text": "Буду в 15:00", "expires_at": "2026-10-06T12:00:00+00:00", "text_changed": False})
    out = tools.run("shturman_draft_message", client,
                    {"chat_id": 7, "text": "Буду в 15:00", "reply_to_message_id": "31", "channel": "business"})
    assert last(service) == ("POST", "/api/outbox/drafts", "",
                             {"chat_id": 7, "text": "Буду в 15:00", "reply_to_message_id": 31, "channel": "business"})
    assert out["ok"] is True and out["sent"] is False and out["draft_id"] == 12 and out["status"] == "pending"
    assert "не отправлено" in out["note"]
    assert "text" not in out


def test_draft_refusal_is_explained_to_the_agent(service, client):
    service.replies[("POST", "/api/outbox/drafts")] = (
        409, {"error": "В этот чат черновики запрещены владельцем.", "reason": "drafting_denied"})
    out = tools.run("shturman_draft_message", client, {"chat_id": 7, "text": "привет"})
    assert out == {"ok": False, "error": "В этот чат черновики запрещены владельцем.", "reason": "drafting_denied"}
    service.replies[("POST", "/api/outbox/drafts")] = (
        429, {"error": "слишком часто", "reason": "limit_drafts", "retry_after": 600})
    assert tools.run("shturman_draft_message", client, {"chat_id": 7, "text": "привет"})["retry_after"] == 600


@pytest.mark.parametrize("args", [
    {}, {"chat_id": 7}, {"text": "привет"}, {"chat_id": 0, "text": "п"}, {"chat_id": -5, "text": "п"},
    {"chat_id": True, "text": "п"}, {"chat_id": "семь", "text": "п"}, {"chat_id": 7, "text": "   "},
    {"chat_id": 7, "text": 5}, {"chat_id": 7, "text": "п", "channel": "sms"},
    {"chat_id": 7, "text": "x" * 20_001}, {"chat_id": 7, "text": "п", "reply_to_message_id": "abc"},
])
def test_bad_draft_arguments_never_reach_the_service(service, client, args):
    out = tools.run("shturman_draft_message", client, args)
    assert out["ok"] is False and out["reason"] == "bad_args" and service.requests == []


def test_commitments_list_and_card(service, client):
    service.replies[("GET", "/api/commitments")] = (200, {"view": "overdue", "commitments": [{"id": 3}]})
    out = tools.run("shturman_commitments", client,
                    {"view": "overdue", "direction": "owner_owes", "person_id": 4, "limit": 500})
    assert out == {"view": "overdue", "commitments": [{"id": 3}], "ok": True}
    assert last(service)[:3] == ("GET", "/api/commitments", "view=overdue&direction=owner_owes&person_id=4&limit=100")
    tools.run("shturman_commitments", client, {})
    assert last(service)[2] == "view=open&limit=50"
    tools.run("shturman_commitments", client, {"commitment_id": 9})
    assert last(service)[:2] == ("GET", "/api/commitments/9")
    assert tools.run("shturman_commitments", client, {"view": "everything"})["reason"] == "bad_args"


def test_commitment_update_actions(service, client):
    for action in ("close", "cancel", "reopen"):
        tools.run("shturman_commitment_update", client, {"commitment_id": 9, "action": action})
        assert last(service) == ("POST", f"/api/commitments/9/{action}", "", {})
    tools.run("shturman_commitment_update", client, {"commitment_id": 9, "action": "reschedule", "due": "в пятницу"})
    assert last(service) == ("POST", "/api/commitments/9/reschedule", "", {"due": "в пятницу"})
    before = len(service.requests)
    for args in ({"commitment_id": 9, "action": "reschedule"}, {"commitment_id": 9},
                 {"commitment_id": 9, "action": "accept"}, {"commitment_id": 9, "action": "reject"},
                 {"commitment_id": 9, "action": "../../owner"}, {"action": "close"}):
        assert tools.run("shturman_commitment_update", client, args)["reason"] == "bad_args"
    assert len(service.requests) == before        # принять и отклонить предложение агент не может


def test_commitment_refusal_from_service(service, client):
    service.replies[("POST", "/api/commitments/9/close")] = (
        409, {"ok": False, "code": "bad_status", "error": "обязательство уже закрыто"})
    out = tools.run("shturman_commitment_update", client, {"commitment_id": 9, "action": "close"})
    assert out == {"ok": False, "error": "обязательство уже закрыто", "reason": "bad_status"}


def test_people_search_card_and_alias(service, client):
    tools.run("shturman_people", client, {"action": "search", "query": "Петров", "chat_id": 7})
    method, path, query, _ = last(service)
    assert (method, path) == ("GET", "/api/people") and "chat_id=7" in query and "limit=20" in query
    tools.run("shturman_people", client, {"action": "card", "person_id": 4})
    assert last(service)[:2] == ("GET", "/api/people/4")
    tools.run("shturman_people", client, {"action": "add_alias", "person_id": 4, "alias": "Петрович"})
    assert last(service) == ("POST", "/api/people/4/aliases", "", {"alias": "Петрович"})
    before = len(service.requests)
    for args in ({"action": "merge"}, {"action": "card"}, {"action": "add_alias", "person_id": 4},
                 {"action": "add_alias", "person_id": 4, "alias": "x" * 121}, {}):
        assert tools.run("shturman_people", client, args)["reason"] == "bad_args"
    assert len(service.requests) == before


def test_answers_are_cleaned_of_hidden_characters(service, client):
    service.replies[("GET", "/api/people/4")] = (200, {
        "display_name": "Пётр‮​\x07 Иванов", "aliases": ["a⁦b"], "note": "строка\nвторая\tx"})
    out = tools.run("shturman_people", client, {"action": "card", "person_id": 4})
    assert out["display_name"] == "Пётр Иванов" and out["aliases"] == ["ab"] and out["note"] == "строка\nвторая\tx"


def test_service_absent_or_down(service, client):
    assert tools.run("shturman_commitments", None, {}) == {
        "ok": False, "error": "Сервис переписки не подключён.", "reason": "not_configured"}
    service.replies[("GET", "/api/commitments")] = (500, {"error": "сбой"})
    assert tools.run("shturman_commitments", client, {})["reason"] == "unavailable"
    assert tools.run("shturman_commitments", client, "не объект")["reason"] == "bad_args"
    assert tools.run("shturman_send_now", client, {})["ok"] is False


class Spy:
    """Клиент, который только записывает, куда инструмент пошёл бы."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    def request(self, method, path, **kwargs):
        self.seen.append((method, path))
        return {}


def test_every_tool_call_stays_inside_the_tool_allowlist():
    spy = Spy()
    calls = [
        ("shturman_draft_message", {"chat_id": 1, "text": "т"}),
        ("shturman_commitments", {}), ("shturman_commitments", {"commitment_id": 2}),
        *[("shturman_commitment_update", {"commitment_id": 2, "action": a, "due": "завтра"})
          for a in tools.COMMITMENT_ACTIONS],
        ("shturman_people", {"action": "search", "query": "а"}),
        ("shturman_people", {"action": "card", "person_id": 3}),
        ("shturman_people", {"action": "add_alias", "person_id": 3, "alias": "а"}),
    ]
    for name, args in calls:
        assert tools.run(name, spy, args)["ok"] is True
    assert len(spy.seen) == len(calls)
    assert all(service_routes.allowed(service_routes.TOOLS, m, p) for m, p in spy.seen)


def test_even_a_buggy_tool_cannot_leave_the_allowlist(service, client, monkeypatch):
    def rogue(client, args):
        return client.request("PUT", "/api/outbox/autoreply", json_body={"account_id": 1, "enabled": True})

    monkeypatch.setitem(tools.HANDLERS, "shturman_people", rogue)
    out = tools.run("shturman_people", client, {})
    assert out["ok"] is False and out["reason"] == "not_allowed" and service.requests == []


def test_schemas_and_toolsets_are_consistent():
    assert set(tools.SCHEMAS) == set(tools.HANDLERS) == set(tools.TOOLSETS)
    for name, schema in tools.SCHEMAS.items():
        assert schema["name"] == name and schema["parameters"]["type"] == "object"
        assert schema["parameters"]["additionalProperties"] is False
        assert all(key.isascii() for key in schema["parameters"]["properties"])     # имена параметров — английские
    assert "НИЧЕГО НЕ ОТПРАВЛЯЕТ" in tools.SCHEMAS["shturman_draft_message"]["description"]
    # В группе «только чтение» нет ни одного инструмента, который что-то меняет или создаёт.
    assert [n for n, t in tools.TOOLSETS.items() if t == tools.TOOLSET_READ] == ["shturman_commitments"]
    # Имя MCP-сервера архива группой инструментов не занято: иначе она заслонила бы его.
    assert "shturman" not in tools.TOOLSETS.values()
