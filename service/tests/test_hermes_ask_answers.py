"""Д-53 (внешняя ревизия, SEC-01): ответы на просьбы ассистента (маршруты вида ASK) не несут содержимого.

Каждый маршрут вида ASK из `access.HERMES_ROUTES` вызывается токеном Hermes во всех ветках, которые удалось
построить: «ждёт подтверждения» (202), «ничего не изменилось», отказы 4xx (нет такого, имя занято, чат не в
проекте), — при открытой и при закрытой выдаче архива. В базе — метки в названиях чатов и проектов, именах людей,
описаниях, другом названии проекта, сфере и формулировке договорённости. Ни одна метка не должна оказаться в
ответе, если её не было в самом запросе. При закрытой выдаче — 423 `archive_closed` до обработчика: ни карточки
владельцу, ни ответа маршрута.
"""

import itertools
import json
import re
from datetime import datetime, timezone

import pytest

from shturman import access, authority, bridge, store
from shturman.app import MODULES

from conftest import HERMES_AUTH, OWNER

MARKERS = ("Секретчат", "Секретчеловек", "Секретпроект", "Секретописание", "Секретпсевдоним", "Секретсфера",
           "Секретдоговор", "Секретархив")
MISSING = 987654321


async def _seed(conn) -> dict:
    from shturman.processing import commitments, people, projects
    from shturman.records import ChatRecord, MessageRecord

    account = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    chat, _ = await store.ensure_chat(conn, account, ChatRecord("user", 2001, "personal_chat", "Чат Секретчат"))
    other, _ = await store.ensure_chat(conn, account, ChatRecord("user", 2002, "personal_chat", "Чат Секретчат-2"))
    for n, (c, tg) in enumerate(((chat, 2001), (other, 2002)), 1):
        rec = MessageRecord(tg_message_id=n, sent_at=datetime(2026, 10, 6, 11, tzinfo=timezone.utc), kind="message",
                            sender_class="user", sender_tg_id=tg, sender_name=f"Иван Секретчеловек {n}",
                            text="Пришлю смету", entities=None, reply_to_tg_id=None, forwarded_from=None,
                            edited_at=None, media_type=None, media_path=None, service_action=None)
        await store.upsert_messages(conn, [(c, rec)], source="session", owner_tg_id=OWNER)
    peer = await conn.fetchval("SELECT id FROM peers WHERE tg_id = 2001")
    person = await people.ensure_person_for_peer(conn, peer, propose=False)
    pending_person = await people.ensure_person_for_peer(
        conn, await conn.fetchval("SELECT id FROM peers WHERE tg_id = 2002"), propose=False)
    await people.confirm_person(conn, person)
    ids = {"chat": chat, "other": other, "person": person, "pending_person": pending_person}
    for status, title in (("active", "Проект Секретпроект"), ("archived", "Проект Секретархив")):
        pid = await conn.fetchval(
            """INSERT INTO projects (title, title_norm, description, status, origin)
               VALUES ($1, $2, 'Секретописание', $3, 'owner') RETURNING id""", title, projects.norm(title), status)
        await conn.execute("INSERT INTO project_chats (project_id, chat_id, origin) VALUES ($1, $2, 'owner')", pid, chat)
        ids[status] = pid
    await conn.execute("INSERT INTO project_aliases (project_id, alias, alias_norm) VALUES ($1, $2, $3)",
                       ids["active"], "Секретпсевдоним", projects.norm("Секретпсевдоним"))
    ids["sphere"] = await conn.fetchval(
        """INSERT INTO spheres (title, title_norm, description) VALUES ('Сфера Секретсфера', 'сфера секретсфера',
           'Секретописание') RETURNING id""")
    await conn.execute("INSERT INTO sphere_chats (sphere_id, account_id, peer_class, tg_id) VALUES ($1, $2, 'user', 2001)",
                       ids["sphere"], account)
    await conn.execute("UPDATE projects SET sphere_id = $1 WHERE id = $2", ids["sphere"], ids["active"])
    message = await conn.fetchval("SELECT id FROM messages WHERE chat_id = $1", chat)
    for key in ("open", "done"):
        item = await conn.fetchval(
            """INSERT INTO commitments (chat_id, source_message_id, direction, what, source_quote)
               VALUES ($1, $2, 'owed_to_owner', 'Секретдоговор: прислать смету', 'Пришлю смету') RETURNING id""",
            chat, message)
        with authority.owner_context(OWNER):
            assert (await commitments.accept(conn, item))["ok"]
            if key == "done":
                assert (await commitments.close(conn, item))["ok"]
        ids[key] = item
    return ids


def _requests(ids: dict) -> list[tuple[str, str, dict]]:
    """(шаблон маршрута, путь, тело) — по нескольку веток на каждый маршрут вида ASK."""
    out = [
        ("/api/outbox/drafts", "/api/outbox/drafts", {"chat_id": ids["chat"], "text": "Добрый день"}),
        ("/api/outbox/drafts", "/api/outbox/drafts", {"chat_id": MISSING, "text": "Добрый день"}),
        ("/api/people/{id}/aliases", f"/api/people/{ids['person']}/aliases", {"alias": "Ваня"}),
        ("/api/people/{id}/aliases", f"/api/people/{ids['pending_person']}/aliases", {"alias": "Петя"}),
        ("/api/people/{id}/aliases", f"/api/people/{MISSING}/aliases", {"alias": "Ваня"}),
        ("/api/projects", "/api/projects", {"title": "Зонд", "chat_ids": [ids["chat"], ids["other"]]}),
        ("/api/projects", "/api/projects", {"title": "Проект Секретпроект"}),               # такой уже есть
        ("/api/projects", "/api/projects", {"title": "Секретпсевдоним"}),                   # имя занято другим
        ("/api/projects", "/api/projects", {"title": "Зонд-2", "aliases": ["Проект Секретархив"]}),
        ("/api/projects", "/api/projects", {"title": "Зонд-3", "chat_ids": [MISSING]}),
        ("/api/spheres", "/api/spheres", {"title": "Работа"}),
        ("/api/spheres", "/api/spheres", {"title": "Сфера Секретсфера"}),
        ("/api/spheres/{id}/chats", f"/api/spheres/{ids['sphere']}/chats", {"add": [ids["other"]]}),
        ("/api/spheres/{id}/chats", f"/api/spheres/{ids['sphere']}/chats", {"remove": [ids["chat"]]}),
        ("/api/spheres/{id}/chats", f"/api/spheres/{ids['sphere']}/chats", {"add": [MISSING]}),
        ("/api/spheres/{id}/chats", f"/api/spheres/{MISSING}/chats", {"add": [ids["chat"]]}),
    ]
    for action in ("close", "cancel", "reopen", "reschedule"):
        body = {"due": "к пятнице"} if action == "reschedule" else {}
        for key in ("open", "done"):
            out.append((f"/api/commitments/{{id}}/{action}", f"/api/commitments/{ids[key]}/{action}", body))
        out.append((f"/api/commitments/{{id}}/{action}", f"/api/commitments/{MISSING}/{action}", body))
    for key in ("active", "archived"):
        pid = ids[key]
        out += [
            ("/api/projects/{id}/chats", f"/api/projects/{pid}/chats", {"add": [ids["other"]]}),
            ("/api/projects/{id}/chats", f"/api/projects/{pid}/chats", {"add": [ids["chat"]]}),      # ничего нового
            ("/api/projects/{id}/chats", f"/api/projects/{pid}/chats", {"remove": [MISSING]}),        # не из проекта
            ("/api/projects/{id}/chats", f"/api/projects/{pid}/chats", {"remove": [ids["chat"]]}),
            ("/api/projects/{id}/archive", f"/api/projects/{pid}/archive", {}),
            ("/api/projects/{id}/sphere", f"/api/projects/{pid}/sphere", {"sphere_id": ids["sphere"]}),
            ("/api/projects/{id}/sphere", f"/api/projects/{pid}/sphere", {"sphere_id": None}),
        ]
    out += [
        ("/api/projects/{id}/chats", f"/api/projects/{MISSING}/chats", {"add": [ids["chat"]]}),
        ("/api/projects/{id}/archive", f"/api/projects/{MISSING}/archive", {}),
        ("/api/projects/{id}/sphere", f"/api/projects/{MISSING}/sphere", {"sphere_id": None}),
    ]
    return out


def _ask_templates() -> set[str]:
    """Все маршруты вида ASK перечня сервера — с раскрытыми вариантами действий."""
    out = set()
    for method, pattern, kind, _meter in access.HERMES_ROUTES:
        if kind != access.ASK:
            continue
        assert method == "POST", pattern.pattern
        text = pattern.pattern.replace("[0-9]{1,19}", "{id}")
        alternatives = re.findall(r"\(([^()]*)\)", text)
        for combo in itertools.product(*[a.split("|") for a in alternatives]) if alternatives else [()]:
            sample = text
            for alt, choice in zip(alternatives, combo):
                sample = sample.replace(f"({alt})", choice, 1)
            out.add(sample)
    return out


def _leaks(response, body: dict) -> list[str]:
    sent = json.dumps(body, ensure_ascii=False)
    try:
        got = json.dumps(response.json(), ensure_ascii=False)
    except ValueError:
        got = response.text
    return [m for m in MARKERS if m not in sent and (m in got or m in response.text)]


def test_every_ask_route_is_exercised():
    assert {template for template, _p, _b in _requests({k: 1 for k in (
        "chat", "other", "person", "pending_person", "active", "archived", "sphere", "open", "done")})} \
        == _ask_templates()


@pytest.mark.parametrize("with_bot", [True, False], ids=["own_bot", "no_own_bot"])
async def test_ask_answers_carry_no_private_content(make_client, conn, with_bot):
    bridge.set_builtin({bridge.NOTIFY_OWNER, bridge.NOTIFY_EDIT} if with_bot else ())
    try:
        ids = await _seed(conn)
        client, _ = await make_client(*MODULES)
        await bridge.set_owner(conn, OWNER, OWNER)
        statuses = set()
        for template, path, body in _requests(ids):
            response = await client.post(path, headers=HERMES_AUTH, json=body)
            statuses.add(response.status_code)
            assert response.status_code < 500, (path, body, response.text)
            assert not _leaks(response, body), (path, body, response.status_code, response.text)
            data = response.json()
            assert "summary" not in data and "project" not in data and "person" not in data, (path, data)
            if response.status_code == 202:
                assert set(data) == {"status", "action_id", "expires_at", "note"}, data
            elif template != "/api/outbox/drafts":
                # «ничего не изменилось» для ассистента выглядит как «нет такого»
                assert response.status_code != 200, (path, data)
            if response.status_code == 404 and template != "/api/outbox/drafts":
                assert data == {"error": access.UNAVAILABLE_TEXT, "code": "not_found"}, (path, data)
            # карточки не копятся: иначе дальше всё упёрлось бы в предел ждущих (429) до обработчика
            await conn.execute("UPDATE pending_actions SET status = 'expired' WHERE status = 'pending'")
        # Ветки действительно пройдены: ожидание владельца и отказы (со своим ботом); без бота — отказ до обработчика.
        if with_bot:
            assert {202, 404} <= statuses and 429 not in statuses, statuses
        else:
            assert 202 not in statuses and 409 in statuses, statuses
        # Владелец видит карточки целиком — с именами и формулировкой.
        if with_bot:
            cards = " ".join(r["text"] for r in await conn.fetch(
                "SELECT payload->>'text' AS text FROM jobs WHERE kind = $1", bridge.NOTIFY_OWNER))
            assert "Секретдоговор" in cards and "Секретчат" in cards
    finally:
        bridge.set_builtin(())


async def test_remove_of_a_chat_not_in_the_project_is_refused(make_client, conn):
    """Убрать из проекта чат, которого в нём нет, — отказ, а не «ничего не изменилось» с проектом целиком."""
    ids = await _seed(conn)
    client, _ = await make_client(*MODULES)
    response = await client.post(f"/api/projects/{ids['active']}/chats", json={"remove": [MISSING]})
    assert response.status_code == 404 and response.json()["code"] == "bad_chat", response.text
    # Оператору «ничего не изменилось» по-прежнему отдаёт проект: он доверенная сторона.
    same = await client.post(f"/api/projects/{ids['active']}/chats", json={"add": [ids["chat"]]})
    assert same.status_code == 200 and same.json()["project"]["id"] == ids["active"]


async def test_with_the_archive_closed_ask_routes_answer_423_before_the_handler(make_client, conn, own_bot):
    ids = await _seed(conn)
    client, _ = await make_client(*MODULES)
    await bridge.set_owner(conn, OWNER, OWNER)
    await conn.execute("UPDATE archive_guard SET state = 'closed', reason = 'owner' WHERE id = 1")
    jobs_before = await conn.fetchval("SELECT count(*) FROM jobs")
    requests = _requests(ids)
    for _template, path, body in requests:
        response = await client.post(path, headers=HERMES_AUTH, json=body)
        assert response.status_code == 423 and response.json()["code"] == "archive_closed", (path, response.text)
        assert not _leaks(response, body)
    assert await conn.fetchval("SELECT count(*) FROM pending_actions") == 0
    assert await conn.fetchval("SELECT count(*) FROM outbox_drafts") == 0
    assert await conn.fetchval("SELECT count(*) FROM jobs") == jobs_before
    assert await conn.fetchval(
        "SELECT count(*) FROM archive_disclosures WHERE caller = 'hermes' AND refused = 'archive_closed'") == len(requests)
    # Оператору закрытая выдача архива ничего не закрывает.
    assert (await client.post(f"/api/projects/{ids['archived']}/archive", json={})).status_code == 200


async def test_existing_and_missing_records_answer_alike_without_a_card(make_client, conn, own_bot):
    """Пробы в один бит (ревизия PR #71): если просьба не ставит карточку владельцу, ответ ассистенту не зависит
    от того, есть ли запись, в каком она состоянии и исключён ли чат."""
    ids = await _seed(conn)
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", ids["other"])
    await conn.execute("INSERT INTO project_chats (project_id, chat_id, origin) VALUES ($1, $2, 'owner')",
                       ids["active"], ids["other"])
    client, _ = await make_client(*MODULES)
    await bridge.set_owner(conn, OWNER, OWNER)

    async def answer(path, body):
        r = await client.post(path, headers=HERMES_AUTH, json=body)
        return r.status_code, r.json()

    pairs = [
        # вид запроса проверяется до базы
        ((f"/api/people/{ids['person']}/aliases", {"alias": "123"}), (f"/api/people/{MISSING}/aliases", {"alias": "123"})),
        ((f"/api/projects/{ids['active']}/chats", {}), (f"/api/projects/{MISSING}/chats", {})),
        # «ничего не изменилось» — как «нет такого»
        ((f"/api/projects/{ids['active']}/chats", {"add": [ids["chat"]]}), (f"/api/projects/{MISSING}/chats", {"add": [ids["chat"]]})),
        ((f"/api/projects/{ids['archived']}/archive", {}), (f"/api/projects/{MISSING}/archive", {})),
        ((f"/api/projects/{ids['active']}/sphere", {"sphere_id": ids["sphere"]}),
         (f"/api/projects/{MISSING}/sphere", {"sphere_id": ids["sphere"]})),
        # исключённый чат в проекте — как чужой
        ((f"/api/projects/{ids['active']}/chats", {"remove": [ids["other"]]}),
         (f"/api/projects/{ids['active']}/chats", {"remove": [MISSING]})),
        # черновик: исключённый чат — как несуществующий
        (("/api/outbox/drafts", {"chat_id": ids["other"], "text": "Добрый день"}),
         ("/api/outbox/drafts", {"chat_id": MISSING, "text": "Добрый день"})),
    ]
    for existing, missing in pairs:
        assert await answer(*existing) == await answer(*missing), existing
    assert await conn.fetchval("SELECT count(*) FROM pending_actions") == 0

    # Без бота согласований — один отказ до обработчика, есть запись или нет.
    bridge.set_builtin(())
    assert await answer(f"/api/projects/{ids['active']}/archive", {}) == await answer(f"/api/projects/{MISSING}/archive", {})
    assert (await answer(f"/api/projects/{MISSING}/archive", {}))[1]["code"] == "owner_unknown"


async def test_draft_answer_is_the_allowlist(make_client, conn, own_bot, config):
    """Черновик создан (отправка включена): ассистенту — номер, состояние, канал, чат, срок; ни текста, ни номеров
    Telegram, ни опор."""
    import dataclasses

    from shturman.records import ChatRecord, MessageRecord

    cfg = dataclasses.replace(config, sending=True, send_daily_hard_cap=1000)
    client, _ = await make_client(*MODULES, cfg=cfg)
    await bridge.set_owner(conn, OWNER, OWNER)
    account = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    chat, _ = await store.ensure_chat(conn, account, ChatRecord("user", 2001, "personal_chat", "Чат Секретчат"))
    rec = MessageRecord(tg_message_id=41, sent_at=datetime.now(timezone.utc), kind="message", sender_class="user",
                        sender_tg_id=2001, sender_name="Иван Секретчеловек", text="Когда смета?", entities=None,
                        reply_to_tg_id=None, forwarded_from=None, edited_at=None, media_type=None, media_path=None,
                        service_action=None)
    await store.upsert_messages(conn, [(chat, rec)], source="session", owner_tg_id=OWNER)
    await conn.execute("""INSERT INTO business_connections (id, account_id, can_reply, enabled, via)
                          VALUES ('bc-1', $1, true, true, 'service')""", account)
    message = await conn.fetchval("SELECT id FROM messages WHERE chat_id = $1", chat)
    body = {"chat_id": chat, "text": "Добрый день, смету пришлю в пятницу.", "reply_to_message_id": message}
    response = await client.post("/api/outbox/drafts", headers=HERMES_AUTH, json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["draft_id"] and data["status"] == "pending" and data["chat_id"] == chat
    assert set(data) <= set(access.DRAFT_KEYS), data
    assert "text" not in data and "reply_to_tg_id" not in data and not _leaks(response, body)
