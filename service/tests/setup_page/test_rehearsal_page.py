"""Раздел «Репетиция ответов» на странице «Переписка» и карточка в настоящем исполнителе бота (Р-85).

  * со страницы владелец включает репетицию и выбирает людей сразу, без карточки в боте; в журнале —
    признаки и числа, без имён и номеров людей;
  * карточка уходит через бот согласований с тремя кнопками оценки; нажатие сохраняет оценку и
    отмечает выбранную кнопку — сообщение собеседнику при этом не уходит.
"""

import json

from setup_fakes import ORIGIN, bind_owner, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
import exec_fakes  # noqa: E402

from shturman import bridge, store  # noqa: E402
from shturman.notify import settings as ns  # noqa: E402
from shturman.records import ChatRecord, MessageRecord  # noqa: E402
from shturman.rehearsal import settings as rs  # noqa: E402

from test_miniapp import OWNER, TOKEN  # noqa: E402

MODULES = ("shturman.api_core", "shturman.executor.service", "shturman.ingest_api", "shturman.tg.service",
           "shturman.outbox.service", "shturman.notify.service", "shturman.rehearsal.service",
           "shturman.setup_page.service")
ANNA = 2001


async def ready(stand, conn):
    s = await stand(modules=MODULES, bot_token=TOKEN)
    await s.page.login(conn)
    await bind_owner(s)
    s.acc = await store.ensure_account(conn, OWNER["id"], "Владелец", "owner")
    return s


async def test_page_turns_rehearsal_on_at_once_and_logs_only_numbers(stand, conn):
    s = await ready(stand, conn)
    body = (await s.page.get("/rehearsal")).json()
    assert body["settings"] == {"enabled": False, "people": [], "daily_cap": 30, "pause_seconds": 5}
    assert body["own_bot"] is True and body["main_account"] is True and body["sending"] is False
    assert body["stats"]["shown"] == 0 and body["limits"]["daily_cap"] == [1, 300]
    await store.ensure_chat(conn, s.acc, ChatRecord("user", ANNA, "personal_chat", "Анна Ким"))

    r = await s.page.put("/rehearsal", {"enabled": True, "people": [ANNA], "daily_cap": 20, "pause_seconds": 10})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["settings"]["enabled"] is True and got["people"] == [{"tg_id": ANNA, "name": "Анна Ким", "username": None}]
    assert await conn.fetchval("SELECT count(*) FROM pending_actions") == 0          # сам владелец — без карточки
    r = await s.page.put("/rehearsal", {"people": [OWNER["id"]]})
    assert r.status_code == 400 and "собственный аккаунт" in r.json()["error"]
    r = await s.page.put("/rehearsal", {"enabled": False})
    assert r.status_code == 200 and (await rs.load(conn))["enabled"] is False
    rows = [(r["action"], r["detail"]) for r in await conn.fetch(
        "SELECT action, detail FROM setup_audit WHERE action = 'rehearsal.settings' ORDER BY id")]
    assert rows == [("rehearsal.settings", "включена: да; людей: 1; в сутки: 20; пауза: 10 с; через: страница"),
                    ("rehearsal.settings", "включена: нет; людей: 1; в сутки: 20; пауза: 10 с; через: страница")]
    dumped = json.dumps(rows, ensure_ascii=False)
    assert "Анна" not in dumped and str(ANNA) not in dumped
    # запись — в списке важных: потоком мелких действий её не вытеснить
    from shturman.setup_page import audit
    assert "rehearsal.settings" in audit.IMPORTANT and "rehearsal.rate" not in audit.IMPORTANT


async def test_card_goes_through_the_approvals_bot_and_rating_marks_the_button(stand, conn):
    s = await ready(stand, conn)
    await ns.save(conn, {**await ns.load(conn), "quiet": False})
    await rs.save(conn, {**rs.DEFAULTS, "enabled": True, "people": [ANNA], "pause_seconds": 0})
    from shturman.outbox import autoreply
    await autoreply.update(conn, {"debounce_seconds": 0})
    anna, _ = await store.ensure_chat(conn, s.acc, ChatRecord("user", ANNA, "personal_chat", "Анна Ким"))
    from datetime import datetime, timezone
    record = MessageRecord(
        tg_message_id=7, sent_at=datetime.now(timezone.utc), kind="message", sender_class="user", sender_tg_id=ANNA,
        sender_name="Анна Ким", text="Завтра в десять удобно?", entities=None, reply_to_tg_id=None,
        forwarded_from=None, edited_at=None, media_type=None, media_path=None, service_action=None,
        telegram_entities=[], topic_tg_id=None, is_forwarded=False, telegram_via_bot=False,
        telegram_sender_bot=False)
    result = await store.upsert_messages(conn, [(anna, record, False)], source="session", owner_tg_id=OWNER["id"])
    mid = result.new_ids[0]
    item_id = await s.state.extras["rehearsal"].on_message(
        {"account_id": s.acc, "chat_id": anna, "message_id": mid, "source": "session", "outgoing": False,
         "edited": False, "via_bot": False})
    job = await conn.fetchval("SELECT id FROM jobs WHERE handler = 'rehearsal.reply'")
    await conn.execute("UPDATE jobs SET status = 'running' WHERE id = $1", job)
    await bridge.deliver_result(conn, job, {"parsed": {"outcome": "reply", "text": "Да, в десять удобно."}})

    def card():
        return [m for m in s.telegram.sent() if m["text"].startswith("✍️ Черновик ответа")]
    [sent] = await exec_fakes.until(card)
    assert "Да, в десять удобно." in sent["text"] and "Завтра в десять удобно?" in sent["text"]
    buttons = exec_fakes.buttons_of(sent)
    # Р-105: отправка выключена — «Отправить» нет; свой ответ голосом и «Не годится»
    assert list(buttons) == ["🎙 Наговорить свой", "Не годится"]
    keyboard = sent["reply_markup"]["inline_keyboard"]
    assert keyboard[0] == [{"text": "Скопировать", "copy_text": {"text": "Да, в десять удобно."}}]
    # разговор — свёрнутой цитатой (Р-103), ответ — моноширинным блоком; разметки в тексте нет
    assert "parse_mode" not in sent and [e["type"] for e in sent["entities"]] == ["expandable_blockquote", "pre"]
    message_id = s.telegram.last_message_id()
    s.telegram.press(buttons["Не годится"], message_id=message_id, markup=sent["reply_markup"])
    await exec_fakes.until(lambda: conn.fetchval("SELECT rating = 'bad' FROM rehearsal_items WHERE id = $1", item_id))
    # собеседнику — ничего: ни от бота в бизнес-режиме, ни согласованным черновиком
    assert s.telegram.sent(business=True) == []
    assert await conn.fetchval("SELECT status FROM outbox_drafts WHERE origin = 'suggest'") == "rejected"
    assert await conn.fetchval("SELECT count(*) FROM outbox_drafts WHERE origin <> 'suggest'") == 0
    stats = (await s.page.get("/rehearsal")).json()["stats"]
    assert (stats["shown"], stats["bad"], stats["unrated"]) == (1, 1, 0)
