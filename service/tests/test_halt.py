"""«Остановить всё» (Х2, Р-87): один флаг сильнее любых настроек.

Пока остановлено, каждая точка, где ассистент действует сам, отказывает: задачи ответа и вопросы,
черновики (и «только для просмотра»), подтверждения действий от внутреннего API, отправка (даже
включённая на сервере), наблюдатель. Триггеры базы — вторая линия. В момент остановки ждущее
отменяется; настройки не трогаются — после «Продолжить» всё как было. Внутренний API флаг не меняет.
"""

import json
import sys
from pathlib import Path

import asyncpg
import pytest

from shturman import authority, bridge, confirm, halt, jobs
from shturman.outbox import autoreply, drafts, policy
from shturman.replies import workflow

sys.path.insert(0, str(Path(__file__).resolve().parent / "outbox"))
from outbox_helpers import (OWNER, IVAN, add_chat, add_message, business, env, live, new_draft,  # noqa: E402,F401
                            owner_request, settle, take)
from test_outbox_autoreply import incoming, trusted_setup  # noqa: E402

POINTS = ["autoreply", "agent_draft", "prepare_draft", "confirmation", "send_check", "watcher",
          "question", "prepare_mode"]


async def stop(env, *, reading=False):
    return await halt.stop(env.state, via="page", reading=reading)


async def notes(conn) -> list[str]:
    rows = await conn.fetch("SELECT payload FROM jobs WHERE kind = $1 ORDER BY id", bridge.NOTIFY_OWNER)
    return [(r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]))["text"] for r in rows]


@pytest.mark.parametrize("point", POINTS)
async def test_every_point_where_the_assistant_acts_refuses_while_stopped(env, own_bot, point):
    chat = await add_chat(env.conn, env.helper_acc)
    if point in ("autoreply", "question", "prepare_mode"):
        await trusted_setup(env)
    if point == "prepare_mode":
        env.state.config = __import__("dataclasses").replace(env.state.config, sending=False, prepare_only=True)
    if point == "watcher":
        group = await add_chat(env.conn, env.helper_acc, 3001, cls="channel", type_="public_supergroup",
                               name="Стройка: чат")
        response = await owner_request(env, "POST", "/api/watch/rules", json={
            "name": "Сроки", "description": "Про сроки", "keywords": ["срок"], "chat_ids": [group]})
        assert response.status_code == 200, response.text
    await stop(env)
    jobs_before = await env.conn.fetchval("SELECT count(*) FROM jobs WHERE kind <> $1", bridge.NOTIFY_OWNER)

    if point in ("autoreply", "prepare_mode"):
        await incoming(env, chat)
        assert await env.conn.fetchval("SELECT count(*) FROM reply_tasks") == 0
    elif point == "question":
        # задача, заведённая до остановки, не задаёт вопрос после неё
        await env.conn.execute("UPDATE assistant_halt SET stopped = false")
        await incoming(env, chat)
        await env.conn.execute("UPDATE assistant_halt SET stopped = true")
        jobs_before += 1          # запрос к модели ушёл до остановки — по замыслу теста
        await take(env.conn, bridge.LLM_STRUCTURED, complete=dict(
            parsed=dict(outcome="ask_owner", question="Какой срок обещать?"), model="test"))
        task = await env.conn.fetchrow("SELECT status, error_code FROM reply_tasks")
        assert (task["status"], task["error_code"]) == ("cancelled", "owner_stopped")
        assert not [t for t in await notes(env.conn) if "Вопрос ассистента" in t]
    elif point == "agent_draft":
        response = await new_draft(env, chat)
        assert response.status_code == 409 and response.json()["reason"] == "assistant_stopped"
        assert "остановил ассистента" in response.json()["error"]
    elif point == "prepare_draft":
        tgt = await policy.target(env.conn, chat)
        msg = await add_message(env.conn, chat, 30, "Как дела?")
        with pytest.raises(drafts.Refused) as caught:
            await drafts.create_autoreply(env.conn, tgt, channel="session", text="Привет", trigger_message_id=msg,
                                          prepare_only=True)
        assert caught.value.decision.code == "assistant_stopped"
    elif point == "confirmation":
        response = await env.client.post("/api/outbox/trusted", json={"tg_user_id": IVAN})
        assert response.status_code == 423 and response.json()["code"] == "assistant_stopped"
        assert await env.conn.fetchval("SELECT count(*) FROM pending_actions") == 0
    elif point == "send_check":
        rules = await policy.load(env.conn, env.state.config)
        tgt = await policy.target(env.conn, chat)
        decision = await policy.check_send(env.conn, env.tg, rules, tgt, channel="session", text="Привет",
                                           text_hash="x")
        assert decision.code == "assistant_stopped"
    elif point == "watcher":
        group = await env.conn.fetchval("SELECT id FROM chats WHERE title = 'Стройка: чат'")
        message = await add_message(env.conn, group, 5, "Какой срок по договору?", sender=IVAN)
        await live(env, group, message, account_id=env.helper_acc)
        assert await env.conn.fetchval("SELECT count(*) FROM watch_hits") == 0
    # ни задания модели, ни отправки
    await settle(env)
    assert await env.conn.fetchval("SELECT count(*) FROM jobs WHERE kind <> $1", bridge.NOTIFY_OWNER) == jobs_before
    assert env.tg.sent == [] and await env.conn.fetchval(
        "SELECT count(*) FROM outbox_drafts WHERE status NOT IN ('rejected', 'failed')") == 0


async def test_database_is_the_second_line(env, own_bot):
    chat = await add_chat(env.conn, env.helper_acc)
    msg = await add_message(env.conn, chat, 11, "Вопрос")
    await stop(env)
    for sql, args in (
        ("""INSERT INTO reply_tasks (account_id, chat_id, target_peer_id, target_tg_id, trigger_message_id,
                trigger_hash, policy_revision, nonce)
            SELECT c.account_id, c.id, c.peer_id, $2, $3, 'h', 'r', 'n' FROM chats c WHERE c.id = $1""",
         (chat, IVAN, msg)),
        ("INSERT INTO pending_actions (kind, summary, payload, nonce, expires_at) "
         "VALUES ('outbox.trusted_add', 'x', '{}', 'n', now() + interval '1 hour')", ()),
        ("INSERT INTO jobs (kind, payload) VALUES ('business.send', '{}')", ()),
        ("INSERT INTO watch_hits (rule_id, message_id, chat_id, content_hash, matched, status) "
         "VALUES (1, $1, $2, 'h', '{}', 'checking')", (msg, chat)),
    ):
        with pytest.raises(asyncpg.PostgresError) as caught:
            await env.conn.execute(sql, *args)
        assert halt.is_halt_error(caught.value), sql
    # уведомление владельцу и карточка «Продолжить» — можно
    await bridge.notify_owner(env.conn, "Вам написали")
    await env.conn.execute("INSERT INTO pending_actions (kind, summary, payload, nonce, expires_at) "
                           "VALUES ('halt.resume', 'x', '{}', 'n2', now() + interval '1 hour')")


async def test_stop_cancels_what_waits_and_keeps_settings(env, own_bot, approvals):
    await trusted_setup(env)
    autoreply_before = await env.conn.fetchval("SELECT value FROM settings WHERE key = 'outbox.autoreply'")
    trusted_before = await env.conn.fetchval("SELECT count(*) FROM outbox_trusted")
    chat = await add_chat(env.conn, env.helper_acc)
    await incoming(env, chat)
    await take(env.conn, bridge.LLM_STRUCTURED, complete=dict(
        parsed=dict(outcome="ask_owner", question="Какой срок обещать?"), model="test"))
    other = await add_chat(env.conn, env.helper_acc, 2002, name="Мария")
    response = await new_draft(env, other)
    assert response.status_code == 200, response.text
    action = approvals.waiting(await env.client.post("/api/outbox/trusted", json={"tg_user_id": 2002}))
    await env.conn.execute("INSERT INTO executor_state (key, value) VALUES ('prompt', '{\"kind\": \"reply\"}')")

    result = await stop(env)
    assert result["counts"] == {"questions": 1, "tasks": 0, "drafts": 1, "actions": 1, "rehearsals": 0, "triage": 0}
    assert await env.conn.fetchval("SELECT status FROM reply_tasks") == "cancelled"
    assert await env.conn.fetchval("SELECT status FROM outbox_drafts") == "rejected"
    assert await approvals.status(action) == "rejected"
    assert await env.conn.fetchval("SELECT count(*) FROM executor_state WHERE key = 'prompt'") == 0
    # настройки не тронуты: после «Продолжить» вернётся ровно то же
    assert await env.conn.fetchval("SELECT value FROM settings WHERE key = 'outbox.autoreply'") == autoreply_before
    assert await env.conn.fetchval("SELECT count(*) FROM outbox_trusted") == trusted_before
    text = halt.stopped_message(result, env.state.config)
    assert "1 вопрос ассистента" in text and "1 черновик" in text and "1 подтверждение" in text
    assert "/resume" in text
    # нажатие «Да» под отклонённой карточкой ничего не делает
    assert (await approvals.press(action))["answer"] == "Действие уже недоступно."
    log = await env.conn.fetch("SELECT action, detail FROM setup_audit WHERE action = 'halt.stop'")
    assert len(log) == 1 and "откуда: page" in log[0]["detail"] and "вопросов: 1" in log[0]["detail"]


async def test_queued_business_send_is_withdrawn_and_never_goes(env, own_bot):
    chat = await add_chat(env.conn, env.owner_acc)
    await business(env.conn, env.owner_acc)
    await env.conn.execute("UPDATE business_connections SET via = 'service'")
    await add_message(env.conn, chat, 40, "Пришлите смету")
    draft = (await new_draft(env, chat)).json()
    row = await env.conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1", draft["draft_id"])
    with authority.owner_context(OWNER, chat_id=OWNER, action="test.telegram.callback"):
        async with env.conn.transaction():
            await drafts.on_button(env.conn, f"s:{row['id']}:{row['nonce']}", OWNER)
    await drafts.deliver(env.mod, row["id"])
    job = await env.conn.fetchrow("SELECT id, status FROM jobs WHERE kind = 'business.send'")
    if job is None:
        pytest.skip("отправка бизнес-ботом в этом окружении не ставится")
    assert job["status"] == "queued"
    await stop(env)
    assert await env.conn.fetchval("SELECT status FROM jobs WHERE id = $1", job["id"]) == "failed"
    assert await env.conn.fetchval("SELECT error_code FROM outbox_drafts WHERE id = $1", row["id"]) == "owner_stopped"


async def test_resume_needs_a_press_in_the_bot_and_not_the_app(env, own_bot, approvals):
    await stop(env)
    # ни внутренний API, ни владелец со страницы (setup) не снимают флаг напрямую
    for path in ("/api/halt", "/api/halt/resume", "/api/stop", "/api/resume"):
        assert (await env.client.post(path, json={})).status_code == 404
    with authority.setup_context(1, action="test"):
        with pytest.raises(confirm.Refused):
            await halt.resume(env.conn, reading=False, via="page")
    assert await halt.is_stopped(env.conn)
    with authority.setup_context(1, action="test"):
        out = await halt.request_resume(env.conn, env.state, via="page", reading=False)
    assert out["status"] == "pending_confirmation" and await halt.is_stopped(env.conn)
    action = out["action_id"]
    card = await approvals.card(action)
    assert "Продолжить работу ассистента" in card and "Снова заработает" in card
    # в приложении карточка видна без кнопок, а нажатие оттуда не принимается
    (listed,) = [a for a in await confirm.awaiting_owner(env.conn) if a["id"] == action]
    assert listed["buttons"] == [] and "только кнопкой" in listed["note"]
    nonce = await env.conn.fetchval("SELECT nonce FROM pending_actions WHERE id = $1", action)
    with authority.miniapp_context(OWNER, chat_id=OWNER):
        refused = await bridge.dispatch_callback(env.conn, f"sh:cf:y:{action}:{nonce}", OWNER)
    assert "только в боте" in refused["answer"] and await halt.is_stopped(env.conn)
    done = await approvals.press(action)
    assert done["answer"] == "Сделано." and not await halt.is_stopped(env.conn)
    assert await env.conn.fetchval("SELECT count(*) FROM setup_audit WHERE action = 'halt.resume'") == 1
    # после «Продолжить» всё как было: автоответ снова готовит
    await trusted_setup(env)
    chat = await add_chat(env.conn, env.helper_acc)
    await incoming(env, chat)
    assert await env.conn.fetchval("SELECT count(*) FROM reply_tasks") == 1


async def test_status_shows_stopped_and_agent_cannot_stop_or_resume(env, own_bot):
    status = (await env.client.get("/api/status")).json()["stopped"]
    assert status == {"stopped": False, "since": None, "via": None, "reading_paused": 0}
    await stop(env)
    status = (await env.client.get("/api/status")).json()["stopped"]
    assert status["stopped"] is True and status["via"] == "page" and status["since"]
    for method in ("PUT", "POST", "DELETE"):
        assert (await env.client.request(method, "/api/halt")).status_code in (404, 405)


async def test_owner_answer_after_stop_is_closed_in_words(env, own_bot):
    from shturman.replies import owner
    await trusted_setup(env)
    chat = await add_chat(env.conn, env.helper_acc)
    await incoming(env, chat)
    await take(env.conn, bridge.LLM_STRUCTURED, complete=dict(
        parsed=dict(outcome="ask_owner", question="Какой срок обещать?"), model="test"))
    task = await workflow.get(env.conn, await env.conn.fetchval("SELECT id FROM reply_tasks"))
    await stop(env)
    with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.reply"):
        async with env.conn.transaction():
            out = await owner.answer(env.conn, env.state, task["id"], "В пятницу.", OWNER, nonce=task["nonce"])
    assert out["ok"] is False and out["text"] == owner.CLOSED
    assert jobs  # модуль очереди на месте
    assert autoreply  # и автоответа
