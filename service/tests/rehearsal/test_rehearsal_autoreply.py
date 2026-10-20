"""«Подготовить ответ» у доверенного собеседника, когда автоответ включён (Д-31, Р-105).

На рабочем экземпляре все нажатия «Подготовить ответ» у человека из списка доверенных при `/prepare on` (до Р-105) кончались
«Не получилось: сообщение удалено или чат исключён из архива» за 20 мс — до модели: репетиция считала,
что ответ готовит автоответ 2а, раз чат ему разрешён, хотя на сообщение старше его предела
(`max_age_seconds`) автоответ ничего не готовит. Теперь:
  * автоответ этого сообщения не готовит (старое, нет его задачи) — разовая репетиция готовится как обычно;
  * автоответ готовит или подготовил (его задача или черновик на это сообщение) — владелец узнаёт, где
    черновик, а не «удалено»;
  * не доверенный собеседник — как раньше;
  * незнакомый код итога — нейтральная фраза и запись в журнал сервиса, не «удалено».
"""

import dataclasses
import logging

import pytest

from shturman import bridge
from shturman.outbox import autoreply
from shturman.replies import workflow

from rehearsal_helpers import (ANNA, OWNER, PETR, answer, cards, chat, item, live, message, model_jobs,  # noqa: F401
                               now, rv)
from rehearsal_helpers import core, rs

GONE = "удален"


async def trusted_drafts_on(rv, *, can_answer: bool = True) -> int:
    """Автоответ у основного аккаунта включён, Анна — единственная доверенная. `can_answer` — автоответ может
    ответить сам: отправка включена, бизнес-подключение с правом отвечать есть. Черновики по списку выключены."""
    await rv.conn.execute("INSERT INTO outbox_accounts (account_id, autoreply_enabled) VALUES ($1, true)",
                          rv.owner_acc)
    await rv.conn.execute("INSERT INTO outbox_trusted (tg_user_id) VALUES ($1)", ANNA)
    if can_answer:
        await rv.conn.execute("INSERT INTO business_connections (id, account_id, can_reply, enabled, via) "
                              "VALUES ('bc-owner', $1, true, true, 'service')", rv.owner_acc)
    rv.state.config = dataclasses.replace(rv.state.config, sending=can_answer)
    rv.state.extras["outbox"].state.config = rv.state.config
    assert workflow.preparing(rv.state) is can_answer and not (await rs.load(rv.conn))["enabled"]
    return await chat(rv.conn, rv.owner_acc, ANNA, name="Анна Ким")


async def task_for(rv, chat_id: int, message_id: int, status: str = "generating") -> int:
    """Задача автоответа на это сообщение — так её записывает `workflow.register`."""
    return await rv.conn.fetchval(
        """INSERT INTO reply_tasks (account_id, chat_id, target_peer_id, target_tg_id, trigger_message_id,
                                    trigger_hash, policy_revision, nonce, status)
           SELECT c.account_id, c.id, c.peer_id, p.tg_id, $2, 'h', 'r', 'n', $3
           FROM chats c JOIN peers p ON p.id = c.peer_id WHERE c.id = $1 RETURNING id""",
        chat_id, message_id, status)


async def test_old_message_of_a_trusted_person_gets_a_rehearsal(rv):
    """Сообщение старше предела автоответа: автоответ его не готовит — «Подготовить ответ» готовит репетицию."""
    anna = await trusted_drafts_on(rv)
    max_age = (await autoreply.load(rv.conn))["max_age_seconds"]
    mid = await message(rv.conn, anna, 1, "Смету пришлёшь?", age=max_age + 600)
    tgt = await autoreply.policy.target(rv.conn, anna)
    assert (await autoreply.eligible(rv.conn, tgt, mid)).ok          # чат автоответу разрешён — как в найденном случае
    assert await core.why_not_once(rv.conn, mid) is None              # кнопка под уведомлением есть
    out = await core.request_once(rv.state, mid)
    assert out["ok"] and "Готовлю" in out["text"] and GONE not in out["text"]
    assert await autoreply.claim(rv.conn, rv.state, anna, mid) is None
    row = await rv.conn.fetchrow("SELECT * FROM rehearsal_items WHERE trigger_message_id = $1", mid)
    assert row["on_demand"] and row["state"] == "preparing" and row["reason"] is None
    (job,) = await model_jobs(rv.conn)
    await answer(rv.conn, job["id"], {"outcome": "reply", "text": "Пришлю в пятницу."})
    (card,) = await cards(rv.conn)
    assert "Пришлю в пятницу." in card["text"] and "По вашей просьбе" in card["text"]
    assert await rv.conn.fetchval("SELECT count(*) FROM reply_tasks") == 0


async def test_old_message_with_a_dead_autoreply_task_gets_a_rehearsal(rv):
    """Задача автоответа на сообщение была, но кончилась без черновика (снята, истекла) — готовим репетицию."""
    anna = await trusted_drafts_on(rv)
    mid = await message(rv.conn, anna, 1, "Смету пришлёшь?", age=3600)
    await task_for(rv, anna, mid, status="cancelled")
    assert await autoreply.claim(rv.conn, rv.state, anna, mid) is None
    out = await core.request_once(rv.state, mid)
    assert out["ok"] and "Готовлю" in out["text"]
    assert len(await model_jobs(rv.conn)) == 1


async def test_fresh_message_whose_autoreply_is_working_says_so(rv):
    """Свежее сообщение, задача автоответа в работе: понятный ответ — черновик придёт; не «удалено»."""
    anna = await trusted_drafts_on(rv)
    mid = await message(rv.conn, anna, 1, "Ты где?")
    task = await task_for(rv, anna, mid)
    found = await autoreply.claim(rv.conn, rv.state, anna, mid)
    assert found is not None and found.task["id"] == task and found.draft is None
    out = await core.request_once(rv.state, mid)
    assert out["ok"] and out["answer"] == "Ответ готовит автоответ."
    assert "уже готовит автоответ" in out["text"] and "ответит собеседнику сам" in out["text"]
    assert GONE not in out["text"] and "Не получилось" not in out["text"]
    assert await model_jobs(rv.conn) == [] and await rv.conn.fetchval("SELECT count(*) FROM rehearsal_items") == 0


async def test_autoreply_draft_is_pointed_to(rv):
    """Автоответ уже подготовил черновик на это сообщение: владелец узнаёт его номер и где он."""
    from shturman.notify import buttons as nb
    anna = await trusted_drafts_on(rv)
    mid = await message(rv.conn, anna, 1, "Ты где?")
    task = await task_for(rv, anna, mid, status="drafting")
    draft = await autoreply.drafts.create(rv.conn, rv.state, chat_id=anna, text="Скоро буду.", channel="business",
                                          reply_to_message_id=mid, task_id=task)
    await rv.conn.execute("UPDATE reply_tasks SET status = 'draft_ready', draft_id = $2 WHERE id = $1",
                          task, draft["draft_id"])
    out = await core.request_once(rv.state, mid)
    assert out["ok"] and f"черновик № {draft['draft_id']} — ждёт вашего решения" in out["text"]
    assert "«Согласования»" in out["text"] and GONE not in out["text"]
    # то же через кнопку под уведомлением: ответ бота, а не «Не получилось»
    pressed = await nb.prepare_reply(rv.conn, mid, personal=True)
    assert pressed["answer"] == "Ответ готовит автоответ." and pressed["reply"]["text"] == out["text"]
    # то же, когда задача давно истекла по времени, а черновик остался: он и есть ответ автоответа
    await rv.conn.execute("UPDATE reply_tasks SET status = 'expired' WHERE id = $1", task)
    again = await core.request_once(rv.state, mid)
    assert f"черновик № {draft['draft_id']}" in again["text"]
    assert await model_jobs(rv.conn) == []


async def test_background_rehearsal_still_steps_aside_for_a_fresh_trusted_message(rv):
    """Автоматическая репетиция по списку: свежее сообщение доверенного готовит автоответ — второй ответ
    не готовится (до записи задачи — по тем же правилам, что автоответ); код — `autoreply`, не `gone`."""
    anna = await trusted_drafts_on(rv)
    await rs.save(rv.conn, {**await rs.load(rv.conn), "enabled": True, "people": [ANNA], "pause_seconds": 0})
    mid = await message(rv.conn, anna, 1, "Ты где?")
    assert await core.handled_by_autoreply(rv.conn, rv.state, anna, mid)
    # строка репетиции, заведённая до записи задачи автоответа, закрывается кодом «autoreply»
    item_id = await rv.conn.fetchval(
        """INSERT INTO rehearsal_items (account_id, chat_id, trigger_message_id, peer_tg_id, nonce)
           VALUES ($1, $2, $3, $4, 'n') RETURNING id""", rv.owner_acc, anna, mid, ANNA)
    assert await rv.mod.prepare(item_id) == "autoreply"
    assert (await item(rv.conn, item_id))["reason"] == "autoreply"


async def test_not_trusted_person_is_as_before(rv):
    await trusted_drafts_on(rv)
    petr = await chat(rv.conn, rv.owner_acc, PETR, name="Пётр")
    mid = await message(rv.conn, petr, 1, "Ты где?", sender=PETR, sender_name="Пётр")
    assert await autoreply.claim(rv.conn, rv.state, petr, mid) is None
    out = await core.request_once(rv.state, mid)
    assert out["ok"] and "Готовлю" in out["text"]
    assert len(await model_jobs(rv.conn)) == 1


async def test_unknown_outcome_is_not_called_deleted_and_is_logged(rv, monkeypatch, caplog):
    anna = await chat(rv.conn, rv.owner_acc, ANNA, name="Анна Ким")
    mid = await message(rv.conn, anna, 1, "Ты где?")

    async def odd(item_id):
        return "something_new"

    monkeypatch.setattr(rv.mod, "prepare", odd)
    with caplog.at_level(logging.WARNING, logger="shturman.rehearsal"):
        out = await core.request_once(rv.state, mid)
    assert not out["ok"] and GONE not in out["text"] and "something_new" in out["text"]
    assert "журнал сервиса" in out["text"]
    assert any("something_new" in r.getMessage() for r in caplog.records)
    # «удалено» — только об удалённом
    assert GONE in await core.outcome_text(rv.conn, rv.state, anna, mid, "gone")


async def test_on_demand_card_that_will_not_come_is_explained(rv):
    """Владельцу ответили «Готовлю…», а пока модель думала, сообщение изменили: бот говорит почему — и
    это не «удалено»."""
    anna = await chat(rv.conn, rv.owner_acc, ANNA, name="Анна Ким")
    mid = await message(rv.conn, anna, 1, "Ты где?")
    assert (await core.request_once(rv.state, mid))["ok"]
    (job,) = await model_jobs(rv.conn)
    await rv.conn.execute("UPDATE messages SET edited_at = now() WHERE id = $1", mid)
    await answer(rv.conn, job["id"], {"outcome": "reply", "text": "Скоро."})
    row = await rv.conn.fetchrow("SELECT * FROM rehearsal_items WHERE trigger_message_id = $1", mid)
    assert (row["state"], row["reason"]) == ("skipped", "edited") and await cards(rv.conn) == []
    told = await rv.conn.fetch("SELECT payload->>'text' AS t FROM jobs WHERE kind = $1 AND payload->>'text' LIKE $2",
                               bridge.NOTIFY_OWNER, "Черновика ответа не будет.%")
    assert [r["t"] for r in told] == ["Черновика ответа не будет. Не получилось: " + core.WORDS["edited"]]


@pytest.mark.parametrize("working", [True, False])
async def test_staff_mode_draft_does_not_replace_the_autoreply_one(rv, working):
    """Способ «Через бота и помощника»: «Подготовить ответ» не заменяет черновик автоответа своим, когда
    автоответ отвечает на это сообщение; когда не отвечает (старое) — черновик готовится, как раньше."""
    import json

    from shturman.notify import buttons as nb
    await rv.conn.execute(
        """INSERT INTO setup_state (key, value) VALUES ('scenario', $1::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", json.dumps({"scenario": "staff"}))
    anna = await trusted_drafts_on(rv)
    mid = await message(rv.conn, anna, 1, "Ты где?", age=0 if working else 3600)
    if working:
        await task_for(rv, anna, mid)
    out = await nb.prepare_reply(rv.conn, mid, personal=True)
    drafting = len(await model_jobs(rv.conn))
    if working:
        assert out["answer"] == "Ответ готовит автоответ." and "уже готовит автоответ" in out["reply"]["text"]
        assert drafting == 0
    else:
        # тот же «Черновик ответа», что в способе «Через ваш аккаунт» (Р-105)
        assert out["answer"] == "Готовлю." and "Готовлю черновик ответа" in out["reply"]["text"] and drafting == 1


async def test_trusted_person_gets_a_draft_when_the_autoreply_cannot_answer(rv):
    """Р-105: доверенный с включённым автоответом, а отправка выключена — автоответ задачу не заводит,
    черновик ответа приходит сам (как человеку из списка). Доверенный без включённого автоответа — ничего:
    его сообщения модели владелец не отдавал."""
    anna = await trusted_drafts_on(rv, can_answer=False)
    mid = await message(rv.conn, anna, 1, "Ты где?")
    from rehearsal_helpers import payload
    await autoreply.on_message(rv.state.extras["outbox"], payload(rv.owner_acc, anna, mid))
    assert await rv.conn.fetchval("SELECT count(*) FROM reply_tasks") == 0
    _, item_id = await live(rv, rv.owner_acc, anna, mid)
    assert item_id is not None
    (job,) = await model_jobs(rv.conn)
    await answer(rv.conn, job["id"], {"outcome": "reply", "text": "Буду через час."})
    (card,) = await cards(rv.conn)
    assert "Буду через час." in card["text"] and "Черновик ответа" in card["text"]
    # автоответ для аккаунта выключили — доверенный больше черновиков сам не получает
    await rv.conn.execute("UPDATE outbox_accounts SET autoreply_enabled = false")
    later = await message(rv.conn, anna, 2, "Ау?")
    _, none = await live(rv, rv.owner_acc, anna, later)
    assert none is None
