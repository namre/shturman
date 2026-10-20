"""Кто включает черновики ответов (бывшая репетиция, Р-85, Р-105): внутренний API (ассистент) — включение и расширение ждут нажатия владельца
в боте согласований, выключение и сужение — сразу; команда /drafts (прежняя /rehearsal) в боте — решение самого владельца;
в `GET /api/status` — только числа; журнал действий — без имён и текста."""

import json

from shturman import authority, bridge
from shturman.executor import owner_chat

from rehearsal_helpers import ANNA, OWNER, PETR, chat, enable, rs, rv  # noqa: F401


async def _told(conn) -> list[str]:
    rows = await conn.fetch("SELECT payload FROM jobs WHERE kind = $1 AND dedup_key IS NULL ORDER BY id",
                            bridge.NOTIFY_OWNER)
    return [(r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]))["text"] for r in rows]


async def test_enabling_and_adding_people_wait_for_the_owner(rv, approvals):
    await chat(rv.conn, rv.owner_acc, ANNA, name="Анна Ким")

    async def send():
        return await rv.client.put("/api/rehearsal", json={"enabled": True, "people": [ANNA]})

    async def read():
        s = await rs.load(rv.conn)
        return s["enabled"], s["people"]

    await approvals.gate(send, read, before=(False, []), after=(True, [ANNA]))
    card = await approvals.card((await rv.conn.fetchval("SELECT max(id) FROM pending_actions")))
    assert "включить черновики ответов" in card and "Анна Ким (идентификатор Telegram 2001)" in card
    assert "ничего не отправляется" in card and "уйдут модели" in card
    # ещё один человек — снова расширение
    action = approvals.waiting(await rv.client.put("/api/rehearsal", json={"people": [ANNA, PETR]}))
    assert (await rs.load(rv.conn))["people"] == [ANNA]
    await approvals.press(action, yes=False)
    rows = [r["detail"] for r in await rv.conn.fetch(
        "SELECT detail FROM setup_audit WHERE action = 'rehearsal.settings' ORDER BY id")]
    # применено нажатием в боте (в тестах — с пометкой тестового пути); номеров и имён людей нет
    assert len(rows) == 1 and rows[0].startswith("включена: да; людей: 1; в сутки: 30; пауза: 5 с; через: бот")


async def test_disabling_and_narrowing_apply_at_once_and_the_owner_is_told(rv, approvals):
    await enable(rv.conn, people=(ANNA, PETR), daily_cap=50)
    r = await rv.client.put("/api/rehearsal", json={"people": [ANNA], "daily_cap": 10})
    assert r.status_code == 200, r.text
    s = (await rs.load(rv.conn))
    assert (s["people"], s["daily_cap"]) == ([ANNA], 10)
    r = await rv.client.put("/api/rehearsal", json={"enabled": False})
    assert r.status_code == 200 and (await rs.load(rv.conn))["enabled"] is False
    assert await approvals.pending() == 0
    told = await _told(rv.conn)
    assert len(told) == 2 and all(t.startswith("Изменены черновики ответов") for t in told)
    assert "Анна" not in "".join(told)                       # имён в сообщении нет — только число


async def test_mixed_request_narrows_now_and_widening_waits(rv, approvals):
    await enable(rv.conn, people=(ANNA,), daily_cap=50)
    r = await rv.client.put("/api/rehearsal", json={"daily_cap": 5, "people": [ANNA, PETR]})
    action = approvals.waiting(r)
    assert r.json()["applied_now"] == {"daily_cap": 5}
    s = await rs.load(rv.conn)
    assert (s["daily_cap"], s["people"]) == (5, [ANNA])
    await approvals.press(action)
    assert (await rs.load(rv.conn))["people"] == [ANNA, PETR]


async def test_without_own_bot_widening_is_refused_and_narrowing_still_works(rv):
    await enable(rv.conn, people=(ANNA,))
    bridge.set_builtin(())
    r = await rv.client.put("/api/rehearsal", json={"people": [ANNA, PETR]})
    assert r.status_code == 409 and r.json()["code"] == "owner_unknown"
    assert (await rv.client.put("/api/rehearsal", json={"enabled": False})).status_code == 200


async def test_api_refuses_bots_own_account_and_bad_values(rv):
    for body, words in (({"people": [OWNER]}, "собственный аккаунт"), ({"people": [777000]}, "служебный"),
                        ({"daily_cap": 0}, "от 1 до 300"), ({"people": ["anna"]}, "числовых ID"),
                        ({"send": True}, "нет таких настроек")):
        r = await rv.client.put("/api/rehearsal", json=body)
        assert r.status_code == 400 and words in r.json()["error"], (body, r.text)


async def test_status_and_get_carry_only_numbers(rv):
    await enable(rv.conn, people=(ANNA, PETR))
    status = (await rv.client.get("/api/status")).json()
    fields = {k: v for k, v in status.items() if k.startswith("rehearsal_")}
    assert fields == {"rehearsal_enabled": True, "rehearsal_people": 2, "rehearsal_shown_7d": 0,
                      "rehearsal_good_7d": 0, "rehearsal_almost_7d": 0, "rehearsal_bad_7d": 0,
                      "rehearsal_unrated_7d": 0, "rehearsal_limited_7d": 0, "rehearsal_failed_7d": 0}
    body = (await rv.client.get("/api/rehearsal")).json()
    assert body["own_bot"] is True and body["main_account"] is True and body["settings"]["people"] == [ANNA, PETR]
    assert set(body["stats"]) >= {"shown", "good", "almost", "bad", "unrated", "reply", "decline", "days"}


async def _command(conn, state, text):
    with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.command"):
        return await owner_chat.handle_command(conn, state, text, OWNER)


async def test_bot_command_is_the_owners_decision_and_applies_at_once(rv):
    await chat(rv.conn, rv.owner_acc, ANNA, name="Анна Ким")
    out = await _command(rv.conn, rv.state, "/drafts on")
    assert "черновики ответов сами: готовятся" in out["text"] and "В списке пока никого" in out["text"]
    await _command(rv.conn, rv.state, f"/rehearsal add {ANNA}")      # прежнее имя команды тоже работает
    s = await rs.load(rv.conn)
    assert s["enabled"] is True and s["people"] == [ANNA]
    assert "сам ничего не отправляет" in (await _command(rv.conn, rv.state, "/drafts"))["text"].lower()
    assert "Черновики ответов: сами — готовятся" in (await _command(rv.conn, rv.state, "/status"))["text"]
    await _command(rv.conn, rv.state, f"/rehearsal remove {ANNA}")
    await _command(rv.conn, rv.state, "/drafts off")
    s = await rs.load(rv.conn)
    assert s["enabled"] is False and s["people"] == []
    assert await rv.conn.fetchval("SELECT count(*) FROM pending_actions") == 0
    details = [r["detail"] for r in await rv.conn.fetch(
        "SELECT detail FROM setup_audit WHERE action = 'rehearsal.settings' ORDER BY id")]
    assert len(details) == 4 and all(d.endswith("через: бот, команда") for d in details)
    assert str(ANNA) not in "".join(details)                 # номеров людей в журнале нет
    try:
        await _command(rv.conn, rv.state, "/drafts send")
    except ValueError as exc:
        assert "/drafts on или off" in str(exc)
    else:
        raise AssertionError("неизвестный вид команды должен получить подсказку формата")


async def test_bot_command_without_verified_owner_does_nothing(rv):
    import pytest
    with pytest.raises(PermissionError):
        await owner_chat.handle_command(rv.conn, rv.state, "/rehearsal on", OWNER)
    assert (await rs.load(rv.conn))["enabled"] is False
