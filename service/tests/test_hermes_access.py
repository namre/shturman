"""Р-104: что токены, видимые ассистенту в Hermes, могут на сервере — перечень всех маршрутов приложения.

Тест перечисляет все маршруты сервиса и для каждого спрашивает сервер (`access.hermes_route`): открыт ли он токену
плагина Hermes и какого вида. Падает, если среди открытых появился изменяющий маршрут не вида ASK/BRIDGE, если
перечни ASK и BRIDGE изменились без правки этого файла (и таблицы прав в docs/service-reference.md), если запись перечня не
совпадает ни с одним настоящим маршрутом. Поведение — ниже: от имени токена Hermes ничего не меняется без нажатия
владельца, а закрытые маршруты отвечают 403 до обработчика.
"""

import importlib.util
import itertools
import re
from pathlib import Path

import pytest
from starlette.routing import Route

from shturman import access, bridge
from shturman.app import MODULES

from conftest import HERMES_AUTH, MCP_AUTH, OPERATOR_AUTH, OWNER

# Изменения, которые токену Hermes открыты только как просьба: карточка владельцу или черновик, ждущий нажатия.
EXPECTED_ASK = {
    ("POST", "/api/outbox/drafts"),
    ("POST", "/api/commitments/{id}/close"), ("POST", "/api/commitments/{id}/cancel"),
    ("POST", "/api/commitments/{id}/reopen"), ("POST", "/api/commitments/{id}/reschedule"),
    ("POST", "/api/people/{id}/aliases"),
    ("POST", "/api/projects"), ("POST", "/api/projects/{id}/chats"), ("POST", "/api/projects/{id}/archive"),
    ("POST", "/api/projects/{id}/sphere"),
    ("POST", "/api/spheres"), ("POST", "/api/spheres/{id}/chats"),
}
# Служебный обмен исполнителя заданий; каждый маршрут сам проверяет свои условия (свой бот, владелец).
EXPECTED_BRIDGE = {
    ("PUT", "/api/owner"), ("DELETE", "/api/owner"), ("POST", "/api/jobs/claim"),
    ("POST", "/api/jobs/{id}/complete"), ("POST", "/api/jobs/{id}/fail"), ("POST", "/api/callbacks/telegram"),
    ("POST", "/api/ingest/business/connection"), ("POST", "/api/ingest/business/message"),
    ("POST", "/api/ingest/business/deleted"),
}
# Чтение с текстом переписки — мерится и ограничивается (archive_guard.py).
EXPECTED_METERED = {
    ("GET", "/api/commitments"), ("GET", "/api/commitments/{id}"), ("GET", "/api/people"),
    ("GET", "/api/people/{id}"), ("GET", "/api/projects"), ("GET", "/api/projects/{id}"), ("GET", "/api/spheres"),
}

_CHOICES = ["close", "cancel", "reopen", "reschedule", "accept", "reject", "complete", "fail", "connection",
            "message", "deleted", "chats", "archive", "sphere", "abc", "a" * 32]


def _samples(path: str) -> list[tuple[str, str]]:
    """(образец пути, шаблон с {id}) для маршрута Starlette."""
    params = re.findall(r"\{([^}]+)\}", path)
    options = []
    for param in params:
        options.append(["7"] if param.endswith(":int") else _CHOICES)
    out = []
    for combo in itertools.product(*options):
        sample, template = path, path
        for param, value in zip(params, combo):
            sample = sample.replace("{" + param + "}", value, 1)
            template = template.replace("{" + param + "}", "{id}" if param.endswith(":int") else value, 1)
        out.append((sample, template))
    return out


async def _routes(make_client):
    client, state = await make_client(*MODULES)
    app = client._transport.app.inner
    found = []
    for route in app.routes:
        if not isinstance(route, Route) or not route.path.startswith("/api/"):
            continue
        for method in sorted((route.methods or set()) - {"HEAD"}):
            for sample, template in _samples(route.path):
                found.append((method, sample, template))
    return client, found


async def test_every_route_open_to_the_hermes_token_is_read_ask_or_bridge(make_client):
    _, found = await _routes(make_client)
    assert len(found) > 100
    ask, bridge_, metered, reads = set(), set(), set(), set()
    for method, sample, template in found:
        route = access.hermes_route(method, sample)
        if route is None:
            continue
        kind, meter = route
        if method == "GET":
            assert kind == access.READ, (method, template)
            reads.add((method, template))
            if meter:
                metered.add((method, template))
        else:
            # Новый изменяющий маршрут, открытый токену ассистента, — только просьбой или служебным обменом.
            assert kind in (access.ASK, access.BRIDGE), (method, template, kind)
            (ask if kind == access.ASK else bridge_).add((method, template))
    assert ask == EXPECTED_ASK
    assert bridge_ == EXPECTED_BRIDGE
    assert metered == EXPECTED_METERED
    # Каждая запись перечня совпадает хотя бы с одним настоящим маршрутом.
    for method, pattern, _kind, _meter in access.HERMES_ROUTES:
        assert any(m == method and pattern.fullmatch(s) for m, s, _ in found), (method, pattern.pattern)


async def test_owner_decisions_are_closed_to_the_hermes_token(make_client):
    """Отменить ждущее подтверждение, сузить уведомления, исключить чат, принять договорённость, правила
    отправки, страницы памяти — 403 до обработчика, ничего не меняется."""
    client, _ = await make_client(*MODULES)
    closed = [("POST", "/api/confirmations/1/cancel"), ("GET", "/api/confirmations"), ("PUT", "/api/notify"),
              ("PUT", "/api/rehearsal"), ("PUT", "/api/chats/1/excluded"), ("POST", "/api/commitments/1/accept"),
              ("POST", "/api/commitments/1/reject"), ("PUT", "/api/outbox/policy"), ("POST", "/api/outbox/trusted"),
              ("DELETE", "/api/outbox/trusted"), ("POST", "/api/watch/rules"), ("PUT", "/api/pages/1/owner-block"),
              ("POST", "/api/facts/1/retract"), ("PUT", "/api/owner/profile/owner-block"), ("GET", "/api/chats"),
              ("GET", "/api/outbox/drafts"), ("GET", "/api/pages"), ("GET", "/api/facts"), ("POST", "/api/ops/consents"),
              ("POST", "/api/processing/run"), ("DELETE", "/api/people/1/aliases"), ("POST", "/api/people/merge"),
              ("PUT", "/api/spheres/1"), ("DELETE", "/api/spheres/1"), ("POST", "/api/tg/login"),
              ("POST", "/api/outbox/drafts/1/cancel"), ("GET", "/api/watch/hits")]
    for method, path in closed:
        response = await client.request(method, path, headers=HERMES_AUTH, json={})
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["code"] == "hermes_forbidden"
    # Тот же маршрут оператору открыт (сам ответ — дело маршрута, но не 403 токена).
    assert (await client.get("/api/confirmations", headers=OPERATOR_AUTH)).status_code == 200
    # Токен архива во внутренний API не пускает вовсе.
    assert (await client.get("/api/status", headers=MCP_AUTH)).status_code == 401


async def _commitment(conn, status="open"):
    from shturman import store
    from shturman.processing import commitments
    from shturman.records import ChatRecord, MessageRecord
    from datetime import datetime, timezone

    account = await store.ensure_account(conn, OWNER, "Владелец")
    chat, _ = await store.ensure_chat(conn, account, ChatRecord("user", 2001, "personal_chat", "Иван Петров"))
    rec = MessageRecord(tg_message_id=1, sent_at=datetime(2026, 10, 6, 11, tzinfo=timezone.utc), kind="message",
                        sender_class="user", sender_tg_id=2001, sender_name="Иван Петров", text="Пришлю смету",
                        entities=None, reply_to_tg_id=None, forwarded_from=None, edited_at=None, media_type=None,
                        media_path=None, service_action=None)
    await store.upsert_messages(conn, [(chat, rec)], source="session", owner_tg_id=OWNER)
    message = await conn.fetchval("SELECT id FROM messages WHERE chat_id = $1", chat)
    item = await conn.fetchval(
        """INSERT INTO commitments (chat_id, source_message_id, direction, what, source_quote)
           VALUES ($1, $2, 'owed_to_owner', 'Прислать смету', 'Пришлю смету') RETURNING id""", chat, message)
    if status == "open":
        from shturman import authority
        with authority.owner_context(OWNER):
            assert (await commitments.accept(conn, item))["ok"]
    return item


@pytest.mark.parametrize("action, body", [("close", {}), ("cancel", {}), ("reschedule", {"due": "через неделю"})])
async def test_hermes_cannot_close_cancel_or_move_an_approved_commitment_without_a_press(
        make_client, conn, own_bot, approvals, action, body):
    item = await _commitment(conn)
    client, _ = await make_client(*MODULES)
    await bridge.set_owner(conn, OWNER, OWNER)
    read = lambda: conn.fetchval("SELECT status || coalesce(due_date::text, '') FROM commitments WHERE id = $1", item)
    before = await read()
    response = await client.post(f"/api/commitments/{item}/{action}", headers=HERMES_AUTH, json=body)
    action_id = approvals.waiting(response)
    assert await read() == before                         # без нажатия — ничего
    card = await approvals.card(action_id)
    assert "Ассистент просит" in card and f"№ {item}" in card
    done = await approvals.press(action_id)
    assert done["answer"] == "Сделано.", done
    assert await read() != before                         # нажатие владельца — применилось


async def test_without_own_bot_the_hermes_request_is_refused_not_applied(make_client, conn):
    item = await _commitment(conn)
    client, _ = await make_client(*MODULES)
    await bridge.set_owner(conn, OWNER, OWNER)
    response = await client.post(f"/api/commitments/{item}/close", headers=HERMES_AUTH, json={})
    assert response.status_code == 409 and response.json()["code"] == "owner_unknown"
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", item) == "open"


async def test_operator_token_keeps_the_owner_tools(make_client, conn):
    """Оператор (`shturman call`) по-прежнему закрывает принятую договорённость сразу — он доверенная сторона."""
    item = await _commitment(conn)
    client, _ = await make_client(*MODULES)
    response = await client.post(f"/api/commitments/{item}/close", json={})
    assert response.status_code == 200, response.text
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", item) == "done"


async def test_hermes_alias_to_a_confirmed_person_waits_for_the_owner(make_client, conn, own_bot, approvals):
    from shturman.processing import people
    item = await _commitment(conn)
    client, _ = await make_client(*MODULES)
    await bridge.set_owner(conn, OWNER, OWNER)
    peer = await conn.fetchval("SELECT id FROM peers WHERE tg_id = 2001")
    person = await people.ensure_person_for_peer(conn, peer, propose=False)
    await people.confirm_person(conn, person)
    count = lambda: conn.fetchval("SELECT count(*) FROM person_aliases WHERE person_id = $1", person)
    before = await count()
    approvals.waiting(await client.post(f"/api/people/{person}/aliases", headers=HERMES_AUTH, json={"alias": "Ваня"}))
    assert await count() == before
    del item


def test_plugin_routes_are_all_open_on_the_server():
    """Каждый маршрут, который плагин Hermes вызывает (его перечни BRIDGE, TOOLS, UI), сервер этому токену
    открывает — иначе плагин получал бы 403. И наоборот: перечень сервера не шире того, что зовёт плагин."""
    path = Path(__file__).resolve().parents[2] / "plugins" / "shturman" / "shturman_core" / "service_routes.py"
    if not path.is_file():
        pytest.skip("кода плагина рядом нет")
    spec = importlib.util.spec_from_file_location("plugin_routes", path)
    routes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routes)
    samples = []
    for method, pattern, _kind, _meter in access.HERMES_ROUTES:
        text = pattern.pattern.replace("[0-9]{1,19}", "7")
        alternatives = re.findall(r"\(([^()]*)\)", text)
        for combo in itertools.product(*[a.split("|") for a in alternatives]) if alternatives else [()]:
            sample = text
            for alt, choice in zip(alternatives, combo):
                sample = sample.replace(f"({alt})", choice, 1)
            samples.append((method, sample))
    plugin = [(m, s) for m, s in samples
              if any(routes.allowed(r, m, s) for r in (routes.BRIDGE, routes.TOOLS, routes.UI))]
    for method, pattern in routes.BRIDGE + routes.TOOLS + routes.UI:
        assert any(m == method and pattern.fullmatch(s) for m, s in samples), (method, pattern.pattern)
    assert set(plugin) == set(samples)
