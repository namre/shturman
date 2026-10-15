"""Страница и приложение «Штурман», пакет 2: «Остановить всё» / «Продолжить» (Р-87), ответ на вопрос
ассистента из приложения (П1), вкладка «Репетиция» (Р-89), кнопка «Открыть настройки в браузере» (П4)."""

import json

import pytest

from setup_fakes import API, ORIGIN, bind_owner, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from test_miniapp import App, MODULES, ready, signed  # noqa: F401
import exec_fakes  # noqa: E402

from shturman import authority, bridge, halt, store  # noqa: E402
from shturman.records import ChatRecord  # noqa: E402
from shturman.replies import owner as replies_owner  # noqa: E402
from shturman.rehearsal import settings as rs  # noqa: E402

OWNER = exec_fakes.OWNER_USER["id"]
ANNA = 5001


async def bot_messages(s) -> list[dict]:
    """Что ушло владельцу в бот: сообщения сервиса — через очередь заданий своего бота."""
    await exec_fakes.until(lambda: True)
    rows = await s.conn.fetch("SELECT payload FROM jobs WHERE kind = $1 ORDER BY id", bridge.NOTIFY_OWNER)
    return [r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]) for r in rows]


async def test_page_stops_at_once_and_resume_is_only_a_card_in_the_bot(stand, conn):
    s = await ready(stand, conn)
    stopped = await s.page.post("/halt", {"reading": False})
    assert stopped.status_code == 200 and stopped.json()["stopped"] is True and stopped.json()["via"] == "page"
    state = (await s.page.get("/state")).json()
    assert state["halt"]["stopped"] is True and state["halt"]["since"]
    assert any("вопросы ассистента" in line for line in state["halt"]["resumes"])
    await exec_fakes.until(lambda: _told(s, "⏸ Ассистент остановлен"))
    # «Продолжить» со страницы — только карточка в боте; остановка остаётся
    asked = await s.page.post("/halt/resume", {"reading": False})
    assert asked.status_code == 202 and asked.json()["status"] == "pending_confirmation"
    async with s.state.pool.acquire() as c:
        assert await halt.is_stopped(c)
        assert await c.fetchval("SELECT kind FROM pending_actions WHERE status = 'pending'") == "halt.resume"
    assert (await s.page.get("/halt")).json()["resume_pending"]["action_id"] == asked.json()["action_id"]
    log = [r["action"] for r in await conn.fetch("SELECT action FROM setup_audit ORDER BY id")]
    assert "halt.stop" in log and "halt.resume_request" in log and "halt.resume" not in log
    # внутренний API флаг не меняет ни в одну сторону
    for path in ("/api/halt", "/api/halt/resume", "/shturman-setup/api/halt"):
        response = await s.api.post(path, json={})
        assert response.status_code in (401, 403, 404, 421), path
    async with s.state.pool.acquire() as c:
        assert await halt.is_stopped(c)


async def _told(s, start: str) -> bool:
    return any((p.get("text") or "").startswith(start) for p in await bot_messages(s))


async def test_app_stops_and_cannot_resume_without_the_bot(stand, conn):
    s = await ready(stand, conn, setup_origin="https://shturman.example:8443")
    assert (await s.app.sign_in(signed(query_id="halt-1"))).status_code == 200
    listed = (await s.app.get("/approvals")).json()
    assert listed["halt"]["stopped"] is False
    assert listed["setup_url"] == "https://shturman.example:8443/shturman-setup/"
    stopped = await s.app.post("/halt", {"reading": False})
    assert stopped.status_code == 200 and stopped.json()["via"] == "app"
    asked = await s.app.post("/halt/resume", {})
    assert asked.status_code == 202
    action = asked.json()["action_id"]
    # в «Согласованиях» приложения карточка без кнопок, и нажать её отсюда нельзя
    items = (await s.app.get("/approvals")).json()["items"]
    (card,) = [i for i in items if i["kind"] == "action" and i["id"] == action]
    assert card["actions"] == [] and "только кнопкой" in card["note"]
    nonce = await conn.fetchval("SELECT nonce FROM pending_actions WHERE id = $1", action)
    refused = await s.app.post("/approvals/decide", {"ref": f"sh:cf:y:{action}:{nonce}"})
    assert refused.status_code == 409
    async with s.state.pool.acquire() as c:
        assert await halt.is_stopped(c)
    # сообщение после остановки — с кнопкой приложения, которую ставит сам исполнитель
    (message,) = [p for p in await bot_messages(s) if p["text"].startswith("⏸ Ассистент остановлен")]
    assert {"text": "Открыть «Штурман»", "app": True} in [b for row in message["buttons"] for b in row]
    # нажатие в самом боте — снимает
    with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.callback"):
        done = await bridge.dispatch_callback(conn, f"sh:cf:y:{action}:{nonce}", OWNER)
    assert done["answer"] == "Сделано."
    async with s.state.pool.acquire() as c:
        assert not await halt.is_stopped(c)


async def test_app_answer_goes_the_same_way_as_the_command(stand, conn, monkeypatch):
    s = await ready(stand, conn)
    account = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    chat, _ = await store.ensure_chat(conn, account, ChatRecord("user", ANNA, "personal_chat", "Анна Ким"))
    from datetime import datetime, timezone
    from shturman.records import MessageRecord
    record = MessageRecord(tg_message_id=10, sent_at=datetime.now(timezone.utc), kind="message", sender_class="user",
                           sender_tg_id=ANNA, sender_name="Анна Ким", text="Когда смета?", entities=None,
                           reply_to_tg_id=None, forwarded_from=None, edited_at=None, media_type=None, media_path=None,
                           service_action=None)
    mid = (await store.upsert_messages(conn, [(chat, record, False)], source="session", owner_tg_id=OWNER)).new_ids[0]
    task = await conn.fetchval(
        """INSERT INTO reply_tasks (account_id, chat_id, target_peer_id, target_tg_id, trigger_message_id, trigger_hash,
                                    policy_revision, status, owner_question, nonce, decision_expires_at)
           SELECT c.account_id, c.id, c.peer_id, $2, $3, 'h', 'r', 'waiting_owner', 'Какой срок обещать?',
                  'task-nonce-0001', now() + interval '10 minutes'
           FROM chats c WHERE c.id = $1 RETURNING id""", chat, ANNA, mid)
    calls = []

    async def answer(conn_, state, task_id, text, user_id, *, nonce=None, window=False):
        from shturman import authority as a
        principal = a.get_owner_principal()
        calls.append((task_id, text, user_id, nonce, window, principal.source, principal.action))
        return {"ok": True, "code": "ok", "text": replies_owner.THANKS}

    monkeypatch.setattr(replies_owner, "answer", answer)
    assert (await s.app.sign_in(signed(query_id="answer-1"))).status_code == 200
    items = (await s.app.get("/approvals")).json()["items"]
    (item,) = [i for i in items if i["kind"] == "reply"]
    assert item["answer"]["ref"] == f"sh:rp:a:{task}:task-nonce-0001" and item["answer"]["question"]
    assert "Ответить" not in [a["label"] for a in item["actions"]]        # в приложении это поле, не кнопка
    assert [a["label"] for a in item["actions"]] == ["Не отвечать"]
    done = await s.app.post("/approvals/answer", {"ref": item["answer"]["ref"], "text": "В пятницу."})
    assert done.status_code == 200 and done.json()["answer"] == replies_owner.THANKS
    assert calls == [(task, "В пятницу.", OWNER, "task-nonce-0001", True, "telegram", "telegram.miniapp")]
    # кнопки, которой нет в списке, — нет; текст ответа в журнал не попадает
    bad = await s.app.post("/approvals/answer", {"ref": f"sh:rp:a:{task}:other-nonce-01", "text": "x"})
    assert bad.status_code == 409 and bad.json()["error"] == replies_owner.CLOSED
    detail = await conn.fetchval("SELECT detail FROM setup_audit WHERE action = 'app.answer' AND outcome = 'ok'")
    assert detail == f"вопрос № {task}; знаков: 10" and "пятниц" not in detail


async def test_app_rehearsal_tab_widens_at_once_but_writes_journal_and_tells_the_owner(stand, conn):
    s = await ready(stand, conn)
    await conn.execute("""INSERT INTO setup_state (key, value) VALUES ('scenario', '{"scenario": "own"}')
                          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""")
    account = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    await store.ensure_chat(conn, account, ChatRecord("user", ANNA, "personal_chat", "Анна Ким"))
    assert (await s.app.sign_in(signed(query_id="rh-1"))).status_code == 200
    view = (await s.app.get("/rehearsal")).json()
    assert view["available"] is True and view["settings"]["enabled"] is False
    found = (await s.app.get("/rehearsal/people", params={"q": "анна"})).json()["items"]
    assert [p["tg_id"] for p in found] == [ANNA]
    out = await s.app.put("/rehearsal", {"enabled": True, "people": [ANNA]})
    assert out.status_code == 200 and out.json()["settings"] == {"enabled": True, "people": [ANNA], "daily_cap": 30}
    assert (await rs.load(conn))["enabled"] is True
    log = await conn.fetchval("SELECT detail FROM setup_audit WHERE action = 'rehearsal.settings'")
    assert "через: приложение" in log and "Анна" not in log
    told = [p["text"] for p in await bot_messages(s) if p["text"].startswith("В приложении изменена репетиция")]
    assert told and "добавлен в список: Анна Ким" in told[0] and "/rehearsal remove 5001" in told[0] \
        and "/rehearsal off" in told[0]
    # предел и паузу из приложения не меняют
    assert (await s.app.put("/rehearsal", {"daily_cap": 300})).status_code == 400
    # внутренний API (роль дашборда) не меняется: расширение там по-прежнему ждёт нажатия в боте —
    # tests/rehearsal/test_rehearsal_api.py


async def test_app_rehearsal_tab_explains_staff_mode(stand, conn):
    s = await ready(stand, conn)
    await conn.execute("""INSERT INTO setup_state (key, value) VALUES ('scenario', '{"scenario": "staff"}')
                          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""")
    assert (await s.app.sign_in(signed(query_id="rh-2"))).status_code == 200
    view = (await s.app.get("/rehearsal")).json()
    assert view["available"] is False and "Через ваш аккаунт" in view["unavailable_text"]
    assert (await s.app.put("/rehearsal", {"enabled": True})).status_code == 409


@pytest.mark.parametrize("method,path", [("POST", "/halt"), ("POST", "/halt/resume"), ("PUT", "/rehearsal"),
                                         ("GET", "/rehearsal"), ("POST", "/approvals/answer")])
async def test_new_app_routes_need_the_app_session(stand, conn, method, path):
    s = await ready(stand, conn)
    assert (await s.app.send(method, path, {} if method != "GET" else None)).status_code == 401
    # и ключ страницы их не открывает
    other = App(s.api._transport, ORIGIN)
    other.key = s.page.key
    assert (await other.send(method, path, {} if method != "GET" else None)).status_code == 401
    async with s.state.pool.acquire() as c:
        assert not await halt.is_stopped(c)
