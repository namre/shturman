"""«Отправить» под напоминанием исполнителю (Р-110, `brief/remind.py`): только нажатием владельца, тем же
согласованием и тем же отправщиком, что черновик; пока отправка выключена — кнопки нет и нажатие ничего не шлёт."""

from datetime import date

import pytest

from shturman.brief import core as _brief_buttons  # noqa: F401 — регистрирует кнопки «br»
from shturman.brief import remind

from outbox_helpers import (IVAN, add_chat, add_message, env, owner_messages, press, settle,  # noqa: F401
                            switch, texts)

WHAT = "прислать график работ"


@pytest.fixture(autouse=True)
def forget_state():
    yield
    remind.set_state(None)


async def promised(env, chat_id):
    """Иван обещал владельцу в чате аккаунта помощника; владелец принял."""
    mid = await add_message(env.conn, chat_id, 10, "Пришлю график работ завтра")
    owner_peer = await env.conn.fetchval("SELECT id FROM peers WHERE class = 'user' AND tg_id = 1000")
    if owner_peer is None:
        from shturman import store
        owner_peer = await store.ensure_peer(env.conn, "user", 1000, name="Евгений")
    ivan_peer = await env.conn.fetchval("SELECT id FROM peers WHERE class = 'user' AND tg_id = $1", IVAN)
    return await env.conn.fetchval(
        """INSERT INTO commitments (chat_id, source_message_id, debtor_peer_id, creditor_peer_id, direction, what,
                                    source_quote, due_expression, due_reason, due_date, status)
           VALUES ($1, $2, $3, $4, 'owed_to_owner', $5, 'Пришлю график работ завтра', 'завтра', 'ok', $6, 'open')
           RETURNING id""", chat_id, mid, ivan_peer, owner_peer, WHAT, date(2026, 10, 7))


async def card_of(env, commitment_id):
    remind.set_state(env.state)
    return (await press(env, f"sh:br:n:{commitment_id}"))["reply"]


def data_of(card, label):
    return next(b["data"] for row in card["buttons"] for b in row if b["text"] == label)


async def test_send_from_the_helper_account_only_by_the_owners_press(env):
    chat = await add_chat(env.conn, env.helper_acc)
    cid = await promised(env, chat)
    card = await card_of(env, cid)
    labels = [b["text"] for row in card["buttons"] for b in row]
    assert labels == ["Отправить", "Скопировать"] and "уйдёт от аккаунта помощника" in card["text"]
    assert env.tg.sent == []                                         # само ничего не уходит
    out = await press(env, data_of(card, "Отправить"))
    assert out["answer"].startswith("Принято, отправляю") and out["remove_buttons"] is True
    draft = await env.conn.fetchrow("SELECT * FROM outbox_drafts")
    assert (draft["origin"], draft["status"], draft["channel"]) == ("reminder", "approved", "session")
    assert await env.conn.fetchval("SELECT draft_id FROM reminders") == draft["id"]
    await settle(env)
    sent, = env.tg.sent
    assert sent["tg_id"] == IVAN and sent["account_id"] == env.helper_acc and WHAT in sent["text"]
    assert await env.conn.fetchval("SELECT status FROM outbox_drafts WHERE id = $1", draft["id"]) == "sent"
    # повторное нажатие — без второй отправки
    again = await press(env, data_of(card, "Отправить"))
    assert again["answer"] == "Это напоминание уже отправлено или отправляется."
    await settle(env)
    assert len(env.tg.sent) == 1
    detail = await env.conn.fetchval("SELECT detail FROM setup_audit WHERE action = 'remind.send'")
    assert detail == f"напоминание № {await env.conn.fetchval('SELECT id FROM reminders')}; черновик № {draft['id']}"
    assert f"Отправлено: Иван Петров (напоминание № {draft['id']})." in texts(await owner_messages(env.conn))


async def test_no_send_button_while_sending_is_off_and_a_stale_press_sends_nothing(env):
    chat = await add_chat(env.conn, env.helper_acc)
    cid = await promised(env, chat)
    card = await card_of(env, cid)
    send = data_of(card, "Отправить")
    switch(env, sending=False)
    fresh = await card_of(env, cid)
    assert [b["text"] for row in fresh["buttons"] for b in row] == ["Скопировать"]
    assert "Сервис ничего не отправляет" in fresh["text"]
    out = await press(env, send)                                     # старая кнопка
    assert out["answer"] == "Отправить сейчас нельзя: отправка выключена на сервере."
    await settle(env)
    assert env.tg.sent == [] and await env.conn.fetchval("SELECT count(*) FROM outbox_drafts") == 0


async def test_stop_and_a_foreign_press_send_nothing(env):
    chat = await add_chat(env.conn, env.helper_acc)
    cid = await promised(env, chat)
    send = data_of(await card_of(env, cid), "Отправить")
    assert (await press(env, send, user=999))["answer"] == "Кнопка недоступна."
    await env.conn.execute("UPDATE assistant_halt SET stopped = true WHERE id = 1")
    assert (await press(env, send))["answer"].startswith("Пока нельзя: ассистент остановлен")
    await settle(env)
    assert env.tg.sent == [] and await env.conn.fetchval("SELECT count(*) FROM outbox_drafts") == 0


async def test_a_pending_draft_in_the_chat_comes_first(env):
    chat = await add_chat(env.conn, env.helper_acc)
    cid = await promised(env, chat)
    send = data_of(await card_of(env, cid), "Отправить")
    r = await env.client.post("/api/outbox/drafts", json={"chat_id": chat, "text": "Добрый день, по графику: жду."})
    assert r.status_code in (200, 201, 202), r.text
    assert (await press(env, send))["answer"] == "В этом чате ждёт вашего решения другой черновик — сначала решите его."
    assert await env.conn.fetchval("SELECT count(*) FROM outbox_drafts WHERE origin = 'reminder'") == 0
