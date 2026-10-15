"""«Ответить» на вопрос ассистента (П1, Б4): кнопка в боте и поле в приложении — тот же путь, что
команда `/replytask N answer текст`, и никаких новых прав."""

import json
import sys
from pathlib import Path

import pytest

from shturman import authority, bridge
from shturman.replies import owner, workflow

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outbox"))
from outbox_helpers import OWNER, IVAN, add_chat, env, owner_messages, press, settle, take  # noqa: E402,F401
from test_outbox_autoreply import incoming, model_says, trusted_setup  # noqa: E402


async def asked(env):
    """Ассистент задал вопрос владельцу: задача ждёт его, карточка ушла в бот."""
    await trusted_setup(env)
    chat = await add_chat(env.conn, env.helper_acc)
    await incoming(env, chat)
    await take(env.conn, bridge.LLM_STRUCTURED, complete=dict(
        parsed=dict(outcome="ask_owner", question="Какой срок обещать?"), model="test"))
    task = await workflow.get(env.conn, await env.conn.fetchval("SELECT id FROM reply_tasks"))
    assert task["status"] == "waiting_owner"
    (card,) = [p for p in await notes(env.conn) if "Вопрос ассистента" in p["text"]]
    return task, card


async def notes(conn) -> list[dict]:
    """Сообщения владельцу, вставшие в очередь своего бота согласований (исполнитель builtin)."""
    rows = await conn.fetch("SELECT payload FROM jobs WHERE kind = $1 ORDER BY id", bridge.NOTIFY_OWNER)
    return [r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]) for r in rows]


async def as_owner(env, fn, *args, **kw):
    with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.reply"):
        async with env.conn.transaction():
            return await fn(*args, **kw)


async def test_card_has_answer_and_skip_buttons_and_deadline(env, own_bot):
    task, card = await asked(env)
    assert "Нажмите «Ответить»" in card["text"] and "/replytask" in card["text"]
    deadline = task["decision_expires_at"]
    assert f"до {deadline.astimezone(workflow._owner_zone()):%H:%M}" in card["text"]
    data = {b["text"]: b["data"] for row in card["buttons"] for b in row}
    assert data == {"Ответить": f"sh:rp:a:{task['id']}:{task['nonce']}",
                    "Не отвечать": f"sh:rp:n:{task['id']}:{task['nonce']}"}   # прежняя кнопка — как была


async def test_answer_button_asks_for_text_and_changes_nothing_by_itself(env, own_bot):
    task, card = await asked(env)
    ref = next(b["data"] for row in card["buttons"] for b in row if b["text"] == "Ответить")
    out = await press(env, ref)
    assert out["answer"] == "Напишите ответ следующим сообщением."
    prompt = out["prompt"]
    assert (prompt["kind"], prompt["id"], prompt["nonce"]) == ("reply", task["id"], task["nonce"])
    assert "одним сообщением" in prompt["text"] and "Не отвечать" in prompt["text"]
    assert (await workflow.get(env.conn, task["id"]))["status"] == "waiting_owner"
    # не владелец — кнопка недоступна
    assert (await press(env, ref, user=IVAN))["answer"] == "Кнопка недоступна."


async def test_answer_by_button_is_the_same_path_as_the_command(env, own_bot):
    task, _ = await asked(env)
    out = await as_owner(env, owner.answer, env.conn, env.state, task["id"], "  В пятницу.  ", OWNER,
                         nonce=task["nonce"], window=True)
    assert out == {"ok": True, "code": "ok", "text": owner.THANKS}
    after = await workflow.get(env.conn, task["id"])
    assert (after["status"], after["owner_answer"], after["requires_approval"], after["owner_resume_via"],
            after["owner_resume_by"]) == ("generating", "В пятницу.", True, "telegram", OWNER)
    # модель видит уточнение владельца — как после команды
    (job,) = await take(env.conn, bridge.LLM_STRUCTURED)
    payload = job["payload"] if isinstance(job["payload"], dict) else json.loads(job["payload"])
    assert json.loads(payload["input"])["owner_clarification"] == "В пятницу."
    await bridge.deliver_result(env.conn, job["id"], {"parsed": {"outcome": "reply", "text": "Смета в пятницу."}})
    await settle(env)
    draft = await env.conn.fetchrow("SELECT * FROM outbox_drafts")
    assert draft["status"] == "pending" and env.tg.sent == []          # отправка — только «Отправить» владельца
    # второй ответ — словами, ничего не меняет
    again = await as_owner(env, owner.answer, env.conn, env.state, task["id"], "Ещё раз", OWNER,
                           nonce=task["nonce"], window=True)
    assert again["ok"] is False and again["text"] in (owner.ANSWERED, owner.CLOSED)


@pytest.mark.parametrize("case", ["stranger", "group", "no_principal", "closed", "late", "stale_nonce", "empty", "long"])
async def test_answer_is_refused_in_words(env, own_bot, case):
    task, _ = await asked(env)
    text, nonce, user = "В пятницу.", task["nonce"], OWNER
    context = authority.owner_context(OWNER, chat_id=OWNER, action="telegram.reply")
    if case == "stranger":
        context, user = authority.owner_context(IVAN, chat_id=IVAN, action="telegram.reply"), IVAN
    elif case == "group":
        context = authority.owner_context(OWNER, chat_id=-100500, action="telegram.reply")
    elif case == "closed":
        await workflow.stop(env.conn, task["id"], "expired", "expired")
    elif case == "late":
        await env.conn.execute("UPDATE reply_tasks SET decision_expires_at = now() - interval '1 second' WHERE id = $1",
                               task["id"])
    elif case == "stale_nonce":
        nonce = "other-nonce-000"
    elif case == "empty":
        text = "   "
    elif case == "long":
        text = "а" * 2001
    if case == "no_principal":
        out = await owner.answer(env.conn, env.state, task["id"], text, user, nonce=nonce, window=True)
    else:
        with context:
            async with env.conn.transaction():
                out = await owner.answer(env.conn, env.state, task["id"], text, user, nonce=nonce, window=True)
    expected = {"stranger": "not_owner", "group": "not_owner", "no_principal": "not_owner", "closed": "closed",
                "late": "late", "stale_nonce": "closed", "empty": "empty", "long": "too_long"}[case]
    assert out["ok"] is False and out["code"] == expected
    if case == "closed":
        assert out["text"] == "Этот вопрос уже закрыт — ассистент не дождался ответа."
    current = await workflow.get(env.conn, task["id"])
    assert current["owner_answer"] is None and current["owner_resume_at"] is None


async def test_command_keeps_working_and_tells_in_words(env, own_bot):
    task, _ = await asked(env)
    out = await as_owner(env, owner.handle_command, env.conn, env.state, f"/replytask {task['id']} answer В пятницу.",
                         OWNER)
    assert "ассистент продолжает" in out["text"]
    late = await as_owner(env, owner.handle_command, env.conn, env.state, f"/replytask {task['id']} answer Ещё", OWNER)
    assert "уже" in late["text"]
