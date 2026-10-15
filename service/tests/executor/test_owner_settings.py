"""Настройки ответов и доверия в боте кнопками, без номеров и JSON (Р-95, находка Б3).

Через настоящий опрос бота согласований с подставным Telegram: «Выбрать человека» из последних собеседников,
«Убрать», «Отозвать» под /sources и /mcpaccess, «Добавить обращение» ответом на просьбу. Нажатие владельца в
самом боте — его решение: применяется сразу, тем же путём, что страница, со строкой «Истории».
"""

import pytest

from shturman import history, store
from shturman.records import ChatRecord
from exec_fakes import OWNER_USER, STRANGER_USER, bind, buttons_of, rig  # noqa: F401 — rig — фикстура

ANNA, BORIS = 5001, 5002


async def ask(rig, text: str) -> dict:
    before = len(rig.tg.sent())
    rig.tg.text(text)
    await rig.bot.poll_once()
    sent = rig.tg.sent()[before:]
    assert sent, text
    return sent[-1]


async def press(rig, message: dict, label: str, *, user: dict = OWNER_USER) -> dict | None:
    """Нажать кнопку под сообщением; вернуть правку этого сообщения (если была)."""
    data = buttons_of(message)[label]
    before = len(rig.tg.calls("editMessageText"))
    rig.tg.press(data, message_id=777, markup=message.get("reply_markup"), user=user)
    await rig.bot.poll_once()
    edits = rig.tg.calls("editMessageText")[before:]
    return edits[-1] if edits else None


async def people(rig):
    acc = await store.ensure_account(rig.conn, OWNER_USER["id"], "Владелец", "owner")
    anna, _ = await store.ensure_chat(rig.conn, acc, ChatRecord("user", ANNA, "personal_chat", "Анна Ким"))
    boris, _ = await store.ensure_chat(rig.conn, acc, ChatRecord("user", BORIS, "personal_chat", "Борис Ли"))
    return acc, anna, boris


@pytest.mark.asyncio
async def test_trust_picks_a_person_by_button_and_trusted_list_removes(rig):
    await bind(rig)
    await people(rig)
    picker = await ask(rig, "/trust")
    assert picker["text"].startswith("Кого сделать доверенным?")
    assert set(buttons_of(picker)) == {"Анна Ким", "Борис Ли"}
    assert all(d.startswith("sh:hx:ta:") for d in buttons_of(picker).values())
    edit = await press(rig, picker, "Анна Ким")
    assert edit["text"].startswith("Анна Ким — в доверенных")
    assert await rig.conn.fetchval("SELECT count(*) FROM outbox_trusted WHERE tg_user_id = $1", ANNA) == 1
    assert (await history.entries(rig.conn))[0]["via"] == "бот, кнопка"
    listed = await ask(rig, "/trusted")
    assert "Анна Ким" in listed["text"] and "Убрать: Анна Ким" in buttons_of(listed)
    edit = await press(rig, listed, "Убрать: Анна Ким")
    assert edit["text"].startswith("Доверенные люди: пока никого")
    assert await rig.conn.fetchval("SELECT count(*) FROM outbox_trusted") == 0
    # чужое нажатие — нет
    again = await ask(rig, "/trust")
    await press(rig, again, "Борис Ли", user=STRANGER_USER)
    assert await rig.conn.fetchval("SELECT count(*) FROM outbox_trusted") == 0


@pytest.mark.asyncio
async def test_sources_and_mcp_have_revoke_buttons(rig):
    await bind(rig)
    _, anna, _ = await people(rig)
    grant = await rig.conn.fetchval(
        """INSERT INTO source_grants (task_id, target_chat_id, kind, request, mode, owner_id, expires_at)
           VALUES (NULL, $1, 'chat', '{"query": "смета"}', 'read', $2, now() + interval '30 days') RETURNING id""",
        anna, OWNER_USER["id"])
    listed = await ask(rig, "/sources")
    label = f"Отозвать № {grant}: «Анна Ким»"
    assert label in buttons_of(listed) and "Отозвать — кнопкой ниже" in listed["text"]
    edit = await press(rig, listed, label)
    assert "сейчас ничего не разрешено" in edit["text"]
    assert await rig.conn.fetchval("SELECT revoked_at IS NOT NULL FROM source_grants WHERE id = $1", grant)
    await rig.conn.execute("INSERT INTO remote_mcp_clients (id, name, redirect_uris) VALUES ('c1', 'Claude', '[]')")
    mcp = await rig.conn.fetchval("""INSERT INTO remote_mcp_grants (client_id, owner_id, resource, expires_at)
                                     VALUES ('c1', $1, 'r', now() + interval '30 days') RETURNING id""", OWNER_USER["id"])
    listed = await ask(rig, "/mcpaccess")
    edit = await press(rig, listed, "Отозвать: «Claude»")
    assert "сейчас ни одной" in edit["text"]
    assert await rig.conn.fetchval("SELECT revoked_at IS NOT NULL FROM remote_mcp_grants WHERE id = $1", mcp)


@pytest.mark.asyncio
async def test_addressing_by_reply_and_drafting_by_chat_button(rig):
    await bind(rig)
    _, anna, _ = await people(rig)
    screen = await ask(rig, "/addressing")
    assert "нет" in screen["text"] and "Добавить обращение" in buttons_of(screen)
    rig.tg.press(buttons_of(screen)["Добавить обращение"], message_id=777, markup=screen["reply_markup"])
    await rig.bot.poll_once()
    asked = rig.tg.sent()[-1]
    assert asked["text"].startswith("Напишите обращение") and asked["reply_markup"]["force_reply"] is True
    reply = await ask(rig, "Штурман")
    assert reply["text"].startswith("Добавлено обращение «Штурман»") and "Убрать «Штурман»" in buttons_of(reply)
    from shturman.outbox import scopes
    assert (await scopes.addressing(rig.conn))["aliases"] == ["Штурман"]
    edit = await press(rig, reply, "Убрать «Штурман»")
    assert "Обращения к ассистенту в группах: нет" in edit["text"]
    picker = await ask(rig, "/drafting")
    assert picker["text"].startswith("Для какого чата") and "Анна Ким" in buttons_of(picker)
    choice = await press(rig, picker, "Анна Ким")
    assert "сейчас — как обычно" in choice["text"]
    choice_message = {"reply_markup": choice["reply_markup"]}
    done = await press(rig, choice_message, "Нельзя")
    assert "сейчас — нельзя" in done["text"]
    assert await rig.conn.fetchval("SELECT drafting FROM outbox_chats WHERE chat_id = $1", anna) == "deny"
    # JSON остался запасным путём
    text = (await ask(rig, '/addressing {"aliases": ["Ассистент"]}'))["text"]
    assert "«Ассистент»" in text and "Вернуть: /addressing" in text
