"""Приложение «Штурман», договорённости (Р-96): список «В команде» со «Скрыть», дата обещания у каждой
договорённости, «Не так» с причиной."""

import json

from setup_fakes import ORIGIN, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from test_miniapp import ready, signed  # noqa: F401
from test_pack4a_app import _today, seed  # noqa: E402

from shturman.processing import commitments  # noqa: E402


async def test_team_list_hide_and_back(stand, conn):
    s = await ready(stand, conn)
    today = _today(s)
    ids = await seed(conn, today)
    await conn.execute("UPDATE commitments SET status = 'team' WHERE id = $1", ids["others"])
    assert (await s.app.sign_in(signed(query_id="team-1"))).status_code == 200
    body = (await s.app.get("/commitments")).json()
    team, = body["team"]
    assert (team["id"], team["what"], team["who"], team["to"]) == (ids["others"], "между ними", "Пётр Смета", "Анна Ким")
    assert team["promised"] == today.isoformat() or team["promised"] < today.isoformat()
    assert [r["code"] for r in body["reasons"]][:2] == ["not_promise", "wrong_person"]
    # у каждой договорённости своих списков — дата обещания
    assert all(i["promised"] for groups in body["lists"].values() for items in groups.values() for i in items)
    assert body["waiting"] == 1                                # «В команде» не ждёт решения владельца

    stale = await s.app.post(f"/commitments/{ids['others']}/hide", {"v": "0" * 24})
    assert stale.status_code == 409
    mine = await s.app.post(f"/commitments/{ids['overdue_mine']}/hide", {"v": team["v"]})
    assert mine.status_code == 409                            # «Скрыть» — только у «В команде»
    got = await s.app.post(f"/commitments/{ids['others']}/hide", {"v": team["v"]})
    assert got.status_code == 200 and got.json()["undo"] is True
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", ids["others"]) == "rejected"
    assert (await s.app.get("/commitments")).json()["team"] == []
    back = await s.app.post(f"/commitments/{ids['others']}/reopen", {})
    assert back.status_code == 200
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", ids["others"]) == "team"
    log = (await commitments.get_commitment(conn, ids["others"], with_events=True))["events"]
    assert [(e["actor"], e["action"], e["to"]) for e in log][-2:] == [("owner", "hidden", "rejected"),
                                                                       ("owner", "unhidden", "team")]


async def test_wrong_with_reason_is_journaled(stand, conn):
    s = await ready(stand, conn)
    today = _today(s)
    ids = await seed(conn, today)
    assert (await s.app.sign_in(signed(query_id="why-1"))).status_code == 200
    body = (await s.app.get("/commitments")).json()
    item = next(i for i in body["lists"]["owner_owes"]["overdue"] if i["id"] == ids["overdue_mine"])
    bad = await s.app.post(f"/commitments/{item['id']}/wrong", {"v": item["v"], "reason": "потому что"})
    assert bad.status_code == 400
    got = await s.app.post(f"/commitments/{item['id']}/wrong", {"v": item["v"], "reason": "wrong_due"})
    assert got.status_code == 200
    log = (await commitments.get_commitment(conn, item["id"], with_events=True))["events"]
    assert (log[-1]["action"], log[-1]["details"]) == ("marked_wrong", {"via": "app", "reason": "wrong_due"})
    audit = json.dumps([dict(r) for r in await conn.fetch("SELECT detail FROM setup_audit")], ensure_ascii=False)
    assert "причина: wrong_due" in audit and "смет" not in audit
