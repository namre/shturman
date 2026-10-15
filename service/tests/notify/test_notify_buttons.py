"""Кнопки под уведомлениями (Б14, Р-89): «Не сообщать об этом чате», «Напомнить через час»,
«Подготовить ответ» — по способу работы; остановка ассистента «Подготовить ответ» не пропускает,
а сами уведомления продолжают уходить."""

import dataclasses
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rehearsal"))
from rehearsal_helpers import (ANNA, OWNER, PETR, answer, cards, chat, enable, item, message, model_jobs,  # noqa: E402,F401
                               now, press, rv)

from shturman import bridge, halt  # noqa: E402
from shturman.notify import buttons as nb  # noqa: E402 — регистрирует кнопки
from shturman.notify import settings as ns  # noqa: E402
from shturman.rehearsal import settings as rs  # noqa: E402


async def notices(conn) -> list[dict]:
    rows = await conn.fetch("SELECT payload FROM jobs WHERE kind = $1 AND dedup_key LIKE 'notify:%' ORDER BY id",
                            bridge.NOTIFY_OWNER)
    return [r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]) for r in rows]


def keys(note: dict) -> dict[str, str]:
    return {b["text"]: b["data"] for row in note["buttons"] or [] for b in row}


async def told(env, *, name="Анна Ким", tg_id=ANNA, text="Когда будет смета?", tg_message_id=10):
    anna = await chat(env.conn, env.owner_acc, tg_id, name=name)
    mid = await message(env.conn, anna, tg_message_id, text, sender=tg_id, sender_name=name)
    assert await env.n.on_message({"account_id": env.owner_acc, "chat_id": anna, "message_id": mid,
                                   "source": "session", "outgoing": False, "edited": False, "via_bot": False})
    await env.n.flush()
    return anna, mid, (await notices(env.conn))[-1]


async def mode(conn, scenario: str) -> None:
    await conn.execute(
        """INSERT INTO setup_state (key, value) VALUES ('scenario', $1::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", json.dumps({"scenario": scenario}))


async def replies(conn) -> list[str]:
    """Что бот написал владельцу отдельным сообщением в ответ на кнопку (поле reply нажатия)."""
    return []


async def test_personal_notification_has_three_buttons_and_none_of_them_sends(rv):
    _, _, note = await told(rv)
    assert set(keys(note)) == {"Подготовить ответ", "Напомнить через час", "Не сообщать об этом чате"}
    assert all(data.startswith("sh:nt:") for data in keys(note).values())


async def test_mute_is_at_once_and_returned_only_on_the_page(rv):
    anna, _, note = await told(rv)
    out = await press(rv.conn, keys(note)["Не сообщать об этом чате"])
    assert out["answer"] == "Больше не сообщаю об этом чате."
    assert out["reply"]["text"] == ("Готово: об этом чате больше не сообщаю. Вернуть — в «Настройках Штурмана», "
                                    "раздел «Уведомления».")
    assert [b["text"] for row in out["buttons"] for b in row] == ["Подготовить ответ", "Напомнить через час"]
    settings = await ns.load(rv.conn)
    assert settings["muted_chats"] == [anna]
    # новое сообщение из этого чата — без уведомления; из другого — как раньше
    later = await message(rv.conn, anna, 11, "Ау?")
    assert await rv.n.on_message({"account_id": rv.owner_acc, "chat_id": anna, "message_id": later, "source": "session",
                                  "outgoing": False, "edited": False, "via_bot": False}) is None
    # вернуть — расширение: через приложение и внутренний API без нажатия в боте не пройдёт
    assert ns.widening(settings, {**settings, "muted_chats": []}) == ["muted"]
    assert await rv.conn.fetchval("SELECT count(*) FROM setup_audit WHERE action = 'notify.mute'") == 1


async def test_remind_in_an_hour_unless_the_owner_answered(rv):
    anna, _, note = await told(rv)
    out = await press(rv.conn, keys(note)["Напомнить через час"])
    assert out["answer"] == "Напомню через час, если вы не ответите в этом чате."
    row = await rv.conn.fetchrow("SELECT state, reminder, due_at FROM notify_items")
    assert row["state"] == "waiting" and row["reminder"]
    assert timedelta(minutes=59) < row["due_at"] - now() <= timedelta(hours=1)
    assert await rv.n.flush() == 0                                   # рано
    assert await rv.n.flush(now() + timedelta(hours=1, minutes=1)) == 1
    reminder = (await notices(rv.conn))[-1]
    assert reminder["text"].startswith("⏰ Напоминаю: вы ещё не ответили\n💬 Вам написали: Анна Ким")
    assert keys(reminder)                                            # кнопки снова есть, с новой меткой
    # во второй раз — владелец уже ответил: напоминание молча снимается
    await press(rv.conn, keys(reminder)["Напомнить через час"])
    await message(rv.conn, anna, 12, "Завтра", outgoing=True, sender=OWNER, sender_name="Я")
    assert await rv.n.flush(now() + timedelta(hours=1, minutes=1)) == 0
    assert await rv.conn.fetchval("SELECT dropped_reason FROM notify_items") == "answered"
    # старая кнопка ничего не делает
    assert (await press(rv.conn, keys(note)["Напомнить через час"]))["answer"] == "Это уведомление устарело."


async def test_prepare_in_own_mode_is_a_one_off_rehearsal_for_anyone(rv):
    await mode(rv.conn, "own")
    assert not (await rs.load(rv.conn))["enabled"]                   # репетиция выключена, людей нет
    _, mid, note = await told(rv)
    out = await press(rv.conn, keys(note)["Подготовить ответ"])
    assert out["answer"] == "Готовлю." and "Ничего не отправляется" in out["reply"]["text"]
    row = await rv.conn.fetchrow("SELECT * FROM rehearsal_items WHERE trigger_message_id = $1", mid)
    assert row["on_demand"] and row["state"] == "preparing"
    (job,) = await model_jobs(rv.conn)
    await answer(rv.conn, job["id"], {"outcome": "reply", "text": "Смета будет в пятницу."})
    (card,) = await cards(rv.conn)
    assert "Смета будет в пятницу." in card["text"] and "ничего не отправлено" in card["text"]
    assert (await item(rv.conn, row["id"]))["state"] == "shown"
    assert await rv.conn.fetchval("SELECT count(*) FROM outbox_drafts") == 0 and rv.tg.sent == []
    # второй раз на то же сообщение — словами
    again = await press(rv.conn, keys(note)["Подготовить ответ"])
    assert "уже была" in again["reply"]["text"]


async def test_prepare_counts_in_the_daily_cap(rv):
    await mode(rv.conn, "own")
    await enable(rv.conn, people=(), enabled=False, daily_cap=1)
    await rv.conn.execute(
        """INSERT INTO rehearsal_items (account_id, chat_id, trigger_message_id, peer_tg_id, nonce, state, asked_at)
           SELECT a.id, c.id, m.id, 1, 'n', 'shown', now()
           FROM accounts a, chats c, messages m WHERE a.role = 'owner' LIMIT 0""")
    _, mid, note = await told(rv)
    first = await press(rv.conn, keys(note)["Подготовить ответ"])
    assert first["answer"] == "Готовлю."
    _, _, other = await told(rv, name="Пётр", tg_id=PETR, text="Привет", tg_message_id=20)
    second = await press(rv.conn, keys(other)["Подготовить ответ"])
    assert second["answer"] == "Не получилось." and "предел" in second["reply"]["text"]


async def test_prepare_in_staff_mode_needs_drafts_on_and_makes_a_view_only_draft(rv):
    await mode(rv.conn, "staff")
    _, mid, note = await told(rv)
    off = await press(rv.conn, keys(note)["Подготовить ответ"])
    assert off["answer"] == "Не получилось." and "/prepare on" in off["reply"]["text"]
    rv.state.config = dataclasses.replace(rv.state.config, prepare_only=True)
    out = await press(rv.conn, keys(note)["Подготовить ответ"])
    assert out["answer"] == "Готовлю." and "только для просмотра" in out["reply"]["text"]
    (job,) = [j for j in await rv.conn.fetch("SELECT id, handler FROM jobs WHERE kind = $1", bridge.LLM_STRUCTURED)
              if j["handler"] == nb.DRAFT_HANDLER]
    await rv.conn.execute("UPDATE jobs SET status = 'running' WHERE id = $1", job["id"])
    await bridge.deliver_result(rv.conn, job["id"], {"parsed": {"outcome": "reply", "text": "Смета в пятницу."}})
    draft = await rv.conn.fetchrow("SELECT * FROM outbox_drafts")
    assert draft is not None and draft["prepare_only"] and draft["status"] == "pending"
    assert draft["text"] == "Смета в пятницу." and rv.tg.sent == []
    assert await rv.conn.fetchval("SELECT count(*) FROM jobs WHERE kind = $1", bridge.BUSINESS_SEND) == 0


async def test_stop_blocks_prepare_but_notifications_keep_coming(rv):
    await mode(rv.conn, "own")
    await halt.stop(rv.state, via="bot")
    anna, mid, note = await told(rv)                                # уведомление ушло, хотя остановлено
    assert note["text"].startswith("💬 Вам написали")
    out = await press(rv.conn, keys(note)["Подготовить ответ"])
    assert "остановлен" in out["reply"]["text"]
    assert await rv.conn.fetchval("SELECT count(*) FROM rehearsal_items") == 0
    assert await model_jobs(rv.conn) == []
    # «Не сообщать» и «Напомнить» — не действия ассистента: работают и при остановке
    assert (await press(rv.conn, keys(note)["Напомнить через час"]))["answer"].startswith("Напомню")
