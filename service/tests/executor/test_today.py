"""Команда /today и кнопка «Сегодня» бота согласований (Р-94): договорённости на сегодня и просроченные —
«Я обещал» и «Мне обещали» коротким списком; кнопка «Открыть «Штурман»» сразу на экран «Договорённости»."""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from shturman import authority, store
from shturman.processing import commitments
from shturman.records import ChatRecord, MessageRecord
from shturman.setup_page import approvals
from exec_fakes import OWNER_USER, bind, rig  # noqa: F401 — фикстура

OWNER = OWNER_USER["id"]
ANNA = 5001


async def ask(rig, text: str = "/today", *, press: bool = False) -> dict:
    before = len(rig.tg.sent())
    if press:
        rig.tg.press(text)
    else:
        rig.tg.text(text)
    await rig.bot.poll_once()
    sent = rig.tg.sent()[before:]
    assert sent, text
    return sent[-1]


def labels(message: dict) -> list[list[tuple[str, str]]]:
    rows = (message.get("reply_markup") or {}).get("inline_keyboard") or []
    return [[(b["text"], b.get("callback_data") or "web_app:" + b["web_app"]["url"]) for b in row] for row in rows]


async def seed(conn, today) -> dict:
    account = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    me = await store.ensure_peer(conn, "user", OWNER, name="Евгений")
    chat, _ = await store.ensure_chat(conn, account, ChatRecord("user", ANNA, "personal_chat", "Анна Ким"))
    anna = await conn.fetchval("SELECT id FROM peers WHERE class = 'user' AND tg_id = $1", ANNA)
    now = datetime.now(timezone.utc)
    rows = [(chat, MessageRecord(tg_message_id=n + 1, sent_at=now - timedelta(hours=n + 1), kind="message",
                                 sender_class="user", sender_tg_id=who, sender_name=None, text=text, entities=None,
                                 reply_to_tg_id=None, forwarded_from=None, edited_at=None, media_type=None,
                                 media_path=None, service_action=None), who == OWNER)
            for n, (who, text) in enumerate([(OWNER, "Смету пришлю"), (ANNA, "Договор подпишу сегодня"),
                                              (ANNA, "Акт на неделе"), (ANNA, "Скрытое"), (ANNA, "Ещё одно")])]
    await store.upsert_messages(conn, rows, source="session", owner_tg_id=OWNER)
    msgs = [r["id"] for r in await conn.fetch("SELECT id FROM messages WHERE chat_id = $1 ORDER BY tg_message_id", chat)]

    async def promise(msg, debtor, creditor, direction, what, due, accept=True):
        cid = await conn.fetchval(
            """INSERT INTO commitments (chat_id, source_message_id, debtor_peer_id, creditor_peer_id, direction, what,
                                        source_quote, due_reason, due_date)
               VALUES ($1, $2, $3, $4, $5, $6, 'цитата', 'ok', $7) RETURNING id""",
            chat, msg, debtor, creditor, direction, what, due)
        if accept:
            with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.callback"):
                await commitments.accept(conn, cid, actor="owner")
        return cid

    out = {
        "late": await promise(msgs[0], me, anna, "owner_owes", "прислать смету по складу", today - timedelta(days=3)),
        "today": await promise(msgs[1], anna, me, "owed_to_owner", "подписать договор", today),
        "week": await promise(msgs[2], anna, me, "owed_to_owner", "прислать акт", today + timedelta(days=2)),
        "hidden": await promise(msgs[3], anna, me, "owed_to_owner", "скрытое защитой", today),
        "proposed": await promise(msgs[4], anna, me, "owed_to_owner", "ещё не подтверждено", today, accept=False),
    }
    await conn.execute("UPDATE messages SET agent_visible = false WHERE id = $1", msgs[3])
    return out


@pytest.mark.asyncio
async def test_today_lists_today_and_overdue_by_direction(rig):
    await bind(rig)
    today = approvals.today_of(rig.state.config)
    await seed(rig.conn, today)
    message = await ask(rig)
    text = message["text"]
    assert text.startswith("Сегодня, ")
    assert "Я обещал\n• Анна Ким — прислать смету по складу (просрочено на 3 дня)" in text
    assert "Мне обещали\n• Анна Ким — подписать договор (сегодня)" in text
    for absent in ("прислать акт", "скрытое защитой", "ещё не подтверждено", "цитата", "{"):
        assert absent not in text, absent
    assert "Ещё 1 договорённость ждёт подтверждения «Верно / Не так» — в разделе «Согласования»." in text
    # без внешнего адреса приложения нет — и кнопки тоже
    assert "приложении" not in text
    assert all(not label.startswith("web_app:") for row in labels(message) for _, label in row)
    # кнопка меню «Сегодня» — тот же ответ
    pressed = await ask(rig, "sh:oc:today", press=True)
    assert pressed["text"] == text


@pytest.mark.asyncio
async def test_today_opens_the_app_on_commitments_and_says_when_empty(rig):
    await bind(rig)
    rig.state.config = dataclasses.replace(rig.state.config, setup_origin="https://shturman.example:8443")
    message = await ask(rig)
    assert "На сегодня договорённостей нет, просроченных тоже." in message["text"]
    assert "в приложении «Штурман», раздел «Договорённости»" in message["text"]
    assert labels(message)[0] == [("Открыть «Штурман»",
                                   "web_app:https://shturman.example:8443/shturman-setup/app/?open=commitments")]
    # «Сегодня» — в главном меню, данные прежних кнопок не менялись
    menu = await ask(rig, "/menu")
    assert ("Сегодня", "sh:oc:today") in labels(menu)[0]
    assert labels(menu)[0][0] == ("Сейчас", "sh:oc:status")
