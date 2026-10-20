"""Черновик ответа ничего не отправляет сам — ни сразу, ни потом (Р-85, Р-105).

Худший случай: главный выключатель отправки включён, у основного аккаунта есть бизнес-подключение с правом
отвечать, подставной шлюз Telegram готов «отправить» и от основного аккаунта, отправщик работает. Карточка,
все её кнопки, кроме «Отправить», повторные нажатия, новые сообщения — и ни согласованного черновика, ни
задачи ответа, ни задания отправки, ни одного обращения к отправке Telegram. «Отправить» — единственный путь:
одно задание бизнес-бота после нажатия владельца; основной аккаунт не отправляет ничего никогда.
"""

import dataclasses

from shturman import bridge
from shturman.outbox import drafts

from rehearsal_helpers import (ANNA, OWNER, answer, cards, chat, enable, live, message, model_jobs,  # noqa: F401
                               nothing_sent, press, rv)


async def _card_for(rv, number, text):
    anna = await chat(rv.conn, rv.owner_acc, ANNA, name="Анна Ким")
    mid = await message(rv.conn, anna, number, text)
    _, item_id = await live(rv, rv.owner_acc, anna, mid)
    job = (await model_jobs(rv.conn))[-1]
    await answer(rv.conn, job["id"], {"outcome": "reply", "text": "Да, всё в силе."})
    return item_id, (await cards(rv.conn))[-1]


def _data(card):
    return [b["data"] for row in card["buttons"] for b in row if b.get("data")]


async def test_only_the_owners_send_press_delivers_and_only_through_the_business_bot(rv):
    await rv.conn.execute("INSERT INTO business_connections (id, account_id, can_reply, enabled, via) "
                          "VALUES ('bc-owner', $1, true, true, 'service')", rv.owner_acc)
    rv.tg.sendable.update({rv.owner_acc, rv.helper_acc})
    rv.state.config = dataclasses.replace(rv.state.config, sending=True)
    outbox = rv.state.extras["outbox"]
    outbox.state.config = rv.state.config
    await enable(rv.conn)

    item_id, card = await _card_for(rv, 1, "Встреча завтра в силе?")
    send = [d for d in _data(card) if d.startswith("sh:ob:s:")]
    assert len(send) == 1
    await nothing_sent(rv)
    # всё, кроме «Отправить», — по два раза: ничего не уходит
    for data in _data(card):
        if not data.startswith(("sh:ob:s:", "sh:vr:")):
            await press(rv.conn, data)
            await press(rv.conn, data)
    await drafts.sweep(outbox)
    outbox.kick()
    await nothing_sent(rv)
    assert (await rv.conn.fetchval("SELECT rating FROM rehearsal_items WHERE id = $1", item_id)) == "bad"
    # «Отправить» под отклонённым черновиком больше ничего не делает
    await press(rv.conn, send[0])
    await drafts.settle(outbox)
    await nothing_sent(rv)

    # новый черновик: «Отправить» — одно задание бизнес-бота, и только оно
    _, second = await _card_for(rv, 2, "А во сколько?")
    send = [d for d in _data(second) if d.startswith("sh:ob:s:")]
    out = await press(rv.conn, send[0])
    assert out["answer"] == "Принято, отправляю."
    await press(rv.conn, send[0])                       # второе нажатие — не вторая отправка
    await drafts.settle(outbox)
    assert await rv.conn.fetchval("SELECT count(*) FROM jobs WHERE kind = $1", bridge.BUSINESS_SEND) == 1
    assert rv.tg.sent == [] and rv.tg.calls == 0       # основной аккаунт не отправил ничего
    assert await rv.conn.fetchval("SELECT count(*) FROM reply_tasks") == 0


async def test_without_sending_there_is_no_send_button_and_the_proposal_is_not_in_the_row(rv):
    """Отправка выключена: «Отправить» нет. Текст предложения — только в черновике сервиса, не в строке
    подготовки и не в заданиях, кроме самой карточки. Всё остальное для «Отправить» на месте (бот согласований
    в бизнес-режиме с правом отвечать, свежее входящее) — не хватает только выключателя (якорь
    `suggest_send_needs_switch`)."""
    await enable(rv.conn)
    await rv.conn.execute("INSERT INTO business_connections (id, account_id, can_reply, enabled, via) "
                          "VALUES ('bc-owner', $1, true, true, 'service')", rv.owner_acc)
    item_id, card = await _card_for(rv, 1, "Пришлёшь смету?")
    assert not [d for d in _data(card) if d.startswith("sh:ob:")]
    columns = {r["column_name"] for r in await rv.conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'rehearsal_items'")}
    assert not columns & {"text", "proposal", "task_id", "channel", "business_connection_id"}
    row = dict(await rv.conn.fetchrow("SELECT * FROM rehearsal_items WHERE id = $1", item_id))
    assert "Да, всё в силе" not in repr(row)
    assert await rv.conn.fetchval("SELECT count(*) FROM jobs WHERE payload::text LIKE '%Да, всё в силе%' "
                                  "AND handler IS DISTINCT FROM 'rehearsal.card'") == 0
    await nothing_sent(rv)


async def test_reply_draft_never_replaces_a_waiting_assistant_draft(rv):
    """Черновик ассистента владелец попросил сам (или он привязан к задаче ответа): черновик сервиса его не
    заменяет. Карточка приходит с текстом, но без кнопок доставки; черновик ассистента ждёт как ждал."""
    await enable(rv.conn)
    anna = await chat(rv.conn, rv.owner_acc, ANNA, name="Анна Ким")
    agent = await rv.conn.fetchval(
        """INSERT INTO outbox_drafts (account_id, chat_id, channel, text, text_hash, origin, nonce, status, expires_at)
           VALUES ($1, $2, 'business', 'Буду в десять.', 'h', 'agent', 'agent-nonce-01', 'pending',
                   now() + interval '1 hour') RETURNING id""", rv.owner_acc, anna)
    mid = await message(rv.conn, anna, 3, "Во сколько встречаемся?")
    await live(rv, rv.owner_acc, anna, mid)
    job = (await model_jobs(rv.conn))[-1]
    await answer(rv.conn, job["id"], {"outcome": "reply", "text": "Да, всё в силе."})
    card = (await cards(rv.conn))[-1]
    assert "Да, всё в силе." in card["text"]
    assert not [d for d in _data(card) if d.startswith("sh:ob:")]
    from shturman import words
    assert words.AGENT_DRAFT_WAITING in card["text"]               # почему кнопок доставки нет
    assert await rv.conn.fetchval("SELECT status FROM outbox_drafts WHERE id = $1", agent) == "pending"
    assert await rv.conn.fetchval("SELECT count(*) FROM outbox_drafts WHERE origin = 'suggest'") == 0
