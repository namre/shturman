"""Маршруты обработки: прогон, обязательства, люди — через поднятый сервис."""

from datetime import datetime, time, timedelta, timezone

from shturman import bridge
from shturman.processing import people, service

from conftest import MCP_AUTH
from proc_helpers import OWNER, account, chat, peer_id, press, say

IVAN = 2001
MODULES = ("shturman.api_core", "shturman.processing.service")


async def seed(conn):
    account_id = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    ivan_chat = await chat(conn, account_id, IVAN, "Иван Петров")
    # сообщения «сейчас»: маршрут считает границу от текущего времени
    now = datetime.now(timezone.utc) - timedelta(minutes=10)
    ids = await say(conn, ivan_chat, [
        (IVAN, "Иван Петров", "Пришлю смету по фасадам через 3 дня."),
        (OWNER, "Евгений Тестов", "Хорошо. Договор отправлю через две недели."),
    ], start=now)
    return account_id, ivan_chat, ids


def items():
    def item(message, quote, what, due):
        return {"message": message, "source_quote": quote, "what": what, "due_expression": due,
                "due_message": None, "recipient": None, "duplicate_of": None}
    return [item(1, "Пришлю смету по фасадам через 3 дня", "прислать смету по фасадам", "через 3 дня"),
            item(2, "Договор отправлю через две недели", "отправить договор", "через две недели")]


async def test_processing_and_commitment_routes(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    _, ivan_chat, ids = await seed(conn)

    assert (await client.post("/api/processing/run", headers=MCP_AUTH)).status_code == 401
    assert (await client.post("/api/processing/run", json={"since": "вчера"})).status_code == 400
    assert (await client.post("/api/processing/run", json={"limit": 0})).status_code == 400
    assert (await client.post("/api/processing/run", json={"limit": "5"})).status_code == 400

    planned = (await client.post("/api/processing/run", json={"limit": 10})).json()
    assert (planned["status"], planned["planned"], planned["messages"]["new"]) == ("planned", 1, 2)
    again = (await client.post("/api/processing/run")).json()          # пустое тело допустимо
    assert (again["status"], again["planned"], again["pending"]) == ("already_running", 0, 1)

    # исполнитель забирает задание и возвращает ответ через общий API
    job = (await client.post("/api/jobs/claim", json={"kinds": ["llm.structured"]})).json()["jobs"][0]
    done = await client.post(f"/api/jobs/{job['id']}/complete",
                             json={"result": {"parsed": {"commitments": items()}, "text": "", "model": "m"}})
    assert done.status_code == 200

    status = (await client.get("/api/processing/status")).json()
    assert status["last_run"]["status"] == "done" and status["counts"]["proposed"] == 2
    assert status["last_run"]["stats"]["results"] == {"proposed": 2}
    assert status["state"]["watermark"] == max(ids)
    assert "смет" not in str(status).lower()                         # в состоянии только счётчики

    proposed = (await client.get("/api/commitments", params={"view": "proposed"})).json()["commitments"]
    assert [c["what"] for c in proposed] == ["прислать смету по фасадам", "отправить договор"]
    smeta, dogovor = proposed[0]["id"], proposed[1]["id"]
    assert (await client.get("/api/commitments", params={"view": "nope"})).status_code == 400
    assert (await client.get("/api/commitments", params={"person_id": "x"})).status_code == 400
    assert (await client.get("/api/commitments", params={"person_id": 999})).status_code == 404

    # Numeric IDs in an HTTP callback cannot impersonate the private control-bot receipt.
    pressed = await client.post("/api/callbacks/telegram", json={"data": f"sh:cm:a:{smeta}", "from_user_id": OWNER})
    assert pressed.status_code == 403 and pressed.json()["code"] == "own_bot"
    assert (await press(conn, f"sh:cm:a:{smeta}"))["answer"] == "Принято."
    action = approvals.waiting(await client.post(f"/api/commitments/{dogovor}/accept"))
    assert (await approvals.press(action))["answer"] == "Сделано."
    assert (await client.get(f"/api/commitments/{dogovor}")).json()["status"] == "open"

    one = (await client.get(f"/api/commitments/{smeta}")).json()
    assert one["source_quote"] == "Пришлю смету по фасадам через 3 дня" and one["source"]["message_id"] == ids[0]
    assert [e["action"] for e in one["events"]] == ["proposed", "accepted"]
    assert (await client.get("/api/commitments/999")).status_code == 404

    opened = (await client.get("/api/commitments")).json()
    assert opened["view"] == "open" and len(opened["commitments"]) == 2
    mine = (await client.get("/api/commitments", params={"direction": "owner_owes"})).json()["commitments"]
    assert [c["id"] for c in mine] == [dogovor]
    ivan_person = await people.person_for_peer(conn, await peer_id(conn, IVAN))
    by_person = (await client.get("/api/commitments", params={"person_id": ivan_person, "chat_id": ivan_chat})).json()
    assert len(by_person["commitments"]) == 2

    moved = await client.post(f"/api/commitments/{dogovor}/reschedule", json={"due": "через месяц"})
    action = approvals.waiting(moved)
    assert (await approvals.press(action))["answer"] == "Сделано."
    assert (await client.get(f"/api/commitments/{dogovor}")).json()["due_expression"] == "через месяц"
    vague = await client.post(f"/api/commitments/{dogovor}/reschedule", json={"due": "на днях"})
    assert vague.status_code == 422 and vague.json()["code"] == "vague"
    assert (await client.post(f"/api/commitments/{dogovor}/reschedule", json={})).status_code == 400

    assert (await client.post(f"/api/commitments/{smeta}/close")).json()["commitment"]["status"] == "done"
    assert (await client.post(f"/api/commitments/{smeta}/cancel")).status_code == 409
    assert (await client.post(f"/api/commitments/{smeta}/reopen")).json()["commitment"]["status"] == "open"
    assert (await client.post(f"/api/commitments/{smeta}/cancel")).json()["commitment"]["status"] == "cancelled"
    assert (await client.post(f"/api/commitments/{smeta}/explode")).status_code == 404
    assert (await client.post("/api/commitments/999/close")).status_code == 404
    closed = (await client.get("/api/commitments", params={"view": "closed"})).json()["commitments"]
    assert [c["id"] for c in closed] == [smeta]


async def test_people_routes(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    account_id = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    await chat(conn, account_id, IVAN, "Иван Петров")
    await chat(conn, account_id, 2020, "Ivan Petrov")
    first = await people.ensure_person_for_peer(conn, await peer_id(conn, IVAN))
    second = await people.ensure_person_for_peer(conn, await peer_id(conn, 2020))

    listed = (await client.get("/api/people")).json()["people"]
    assert {p["id"] for p in listed} == {first, second}
    found = (await client.get("/api/people", params={"query": "Ивану Петрову"})).json()["people"]
    assert {p["id"] for p in found} == {first, second} and found[0]["match"] == "ambiguous"
    assert (await client.get("/api/people", params={"query": "Сидорову"})).json()["people"] == []

    person = (await client.get(f"/api/people/{first}")).json()
    assert person["display_name"] == "Иван Петров" and person["peers"][0]["tg_id"] == IVAN
    assert (await client.get("/api/people/999")).status_code == 404

    added = await client.post(f"/api/people/{first}/aliases", json={"alias": "Иван Иванович"})
    action = approvals.waiting(added)
    assert (await approvals.press(action))["answer"] == "Сделано."
    assert (await client.get(f"/api/people/{first}")).json()["confirmed"] is True
    assert (await client.post(f"/api/people/{first}/aliases", json={"alias": "  "})).status_code == 400
    assert (await client.post("/api/people/999/aliases", json={"alias": "Кто-то"})).status_code == 404
    found = (await client.get("/api/people", params={"query": "Иванычу"})).json()["people"]
    assert [p["id"] for p in found] == [first]
    removed = await client.request("DELETE", f"/api/people/{first}/aliases", json={"alias": "Иван Иванович"})
    assert removed.json()["removed"] == 1

    proposals = (await client.get("/api/people/proposals")).json()["proposals"]
    assert [(p["person"]["id"], p["other"]["id"]) for p in proposals] == [(second, first)]
    merged = await client.post("/api/people/merge", json={"proposal_id": proposals[0]["id"]})
    action = approvals.waiting(merged)
    assert (await approvals.press(action))["answer"] == "Сделано."
    merged_person = (await client.get(f"/api/people/{first}")).json()
    assert merged_person["id"] == first and len(merged_person["peers"]) == 2
    assert (await client.get(f"/api/people/{second}")).json()["id"] == first      # старый идентификатор жив
    assert (await client.get("/api/people/proposals")).json()["proposals"] == []
    assert (await client.post("/api/people/merge", json={"source_id": first, "target_id": first})).status_code == 409
    assert (await client.post("/api/people/merge", json={"source_id": "a", "target_id": 1})).status_code == 400

    split = await client.post(f"/api/people/{first}/split", json={"peer_id": await peer_id(conn, 2020)})
    action = approvals.waiting(split)
    assert (await approvals.press(action))["answer"] == "Сделано."
    assert len((await client.get(f"/api/people/{first}")).json()["peers"]) == 1
    impossible = approvals.waiting(await client.post(
        f"/api/people/{first}/split", json={"peer_id": await peer_id(conn, IVAN)}))
    assert (await approvals.press(impossible))["answer"] == "Не получилось."
    assert await approvals.status(impossible) == "failed"
    assert len((await client.get(f"/api/people/{first}")).json()["peers"]) == 1

    # отклонение предложения
    await chat(conn, account_id, 2030, "Иван Петров")
    third = await people.ensure_person_for_peer(conn, await peer_id(conn, 2030))
    pending = (await client.get("/api/people/proposals")).json()["proposals"]
    assert {p["person"]["id"] for p in pending} == {third} and len(pending) == 2
    rejected = await client.post(f"/api/people/proposals/{pending[0]['id']}/reject")
    assert rejected.json() == {"ok": True, "status": "rejected", "changed": True}
    assert (await client.post("/api/people/proposals/999/reject")).status_code == 404
    assert len((await client.get("/api/people/proposals")).json()["proposals"]) == 1


def test_nightly_time_is_configurable(monkeypatch):

    class Cfg:
        nightly_at = None

    monkeypatch.delenv("SHTURMAN_NIGHTLY_AT", raising=False)
    assert service.nightly_time(Cfg()) == time(3, 30)
    monkeypatch.setenv("SHTURMAN_NIGHTLY_AT", "04:15")
    assert service.nightly_time(Cfg()) == time(4, 15)
    monkeypatch.setenv("SHTURMAN_NIGHTLY_AT", "поздно")
    assert service.nightly_time(Cfg()) == time(3, 30)
    Cfg.nightly_at = "02:00"
    assert service.nightly_time(Cfg()) == time(2, 0)
