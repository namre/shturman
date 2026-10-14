"""Репетиция ничего не отправляет — ни сразу, ни потом (Р-85).

Проверяется худший случай: главный выключатель отправки включён, подготовка без доставки (`/prepare on`)
включена, у основного аккаунта есть бизнес-подключение с правом отвечать, подставной шлюз Telegram готов
«отправить» и от основного аккаунта, отправщик черновиков работает в фоне. Карточка репетиции, все
три оценки, повторное нажатие, новые сообщения после переключений — и ни черновика, ни задачи ответа, ни
задания отправки, ни одного обращения к отправке Telegram.
"""

import dataclasses

from shturman import authority
from shturman.executor import owner_chat
from shturman.outbox import drafts

from rehearsal_helpers import (ANNA, OWNER, answer, cards, chat, enable, live, message, model_jobs,  # noqa: F401
                               nothing_sent, press, rv)


async def _command(rv, text):
    with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.command"):
        return await owner_chat.handle_command(rv.conn, rv.state, text, OWNER)


async def _card_for(rv, number, text):
    anna = await chat(rv.conn, rv.owner_acc, ANNA, name="Анна Ким")
    mid = await message(rv.conn, anna, number, text)
    _, item_id = await live(rv, rv.owner_acc, anna, mid)
    job = (await model_jobs(rv.conn))[-1]
    await answer(rv.conn, job["id"], {"outcome": "reply", "text": "Да, всё в силе."})
    return item_id, (await cards(rv.conn))[-1]


async def test_rehearsal_card_never_becomes_deliverable_whatever_is_switched(rv):
    # худший случай: всё, что нужно для отправки, на месте — и для основного аккаунта тоже
    await rv.conn.execute("INSERT INTO business_connections (id, account_id, can_reply, enabled, via) "
                          "VALUES ('bc-owner', $1, true, true, 'service')", rv.owner_acc)
    rv.tg.sendable.update({rv.owner_acc, rv.helper_acc})
    await enable(rv.conn)

    item_id, card = await _card_for(rv, 1, "Встреча завтра в силе?")
    await nothing_sent(rv)

    # владелец включает подготовку без доставки, а на сервере включают отправку
    await _command(rv, "/prepare on")
    rv.state.config = dataclasses.replace(rv.state.config, sending=True, prepare_only=True)
    outbox = rv.state.extras["outbox"]
    outbox.state.config = rv.state.config
    for row in card["buttons"]:
        await press(rv.conn, row[0]["data"])         # все три оценки по очереди
        await press(rv.conn, row[0]["data"])         # и повторно
    await drafts.sweep(outbox)
    outbox.kick()
    await rv.state.events.drain()
    await nothing_sent(rv)
    assert (await rv.conn.fetchval("SELECT rating FROM rehearsal_items WHERE id = $1", item_id)) == "bad"

    # новое сообщение уже при включённой отправке — снова только карточка
    _, second = await _card_for(rv, 2, "А во сколько?")
    assert [[b["text"] for b in r] for r in second["buttons"]] == [["Годится"], ["Почти — поправил бы"], ["Не годится"]]
    await drafts.sweep(outbox)
    await nothing_sent(rv)

    # и выключение подготовки потом ничего «не отпускает»
    await _command(rv, "/prepare off")
    rv.state.config = dataclasses.replace(rv.state.config, prepare_only=False)
    outbox.state.config = rv.state.config
    await drafts.sweep(outbox)
    await nothing_sent(rv)


async def test_nothing_in_the_rehearsal_row_could_be_sent(rv):
    """В базе нет текста предложенного ответа: из строки репетиции нечего доставлять."""
    await enable(rv.conn)
    item_id, _ = await _card_for(rv, 1, "Пришлёшь смету?")
    columns = {r["column_name"] for r in await rv.conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'rehearsal_items'")}
    assert not columns & {"text", "proposal", "draft_id", "task_id", "channel", "business_connection_id"}
    row = dict(await rv.conn.fetchrow("SELECT * FROM rehearsal_items WHERE id = $1", item_id))
    assert "Да, всё в силе" not in repr(row)
    assert await rv.conn.fetchval("SELECT count(*) FROM jobs WHERE payload::text LIKE '%Да, всё в силе%' "
                                  "AND handler IS DISTINCT FROM 'rehearsal.card'") == 0
