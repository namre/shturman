"""Разбор входящих по пачкам (packs.py, Р-93): пачка — один разбор, когда она закрылась; пересланное — слова
третьего лица; вложения пачки поднимаются и дожидаются, а чего модель не видела — карточка говорит прямо;
«Подготовить ответ» под карточкой — только когда ответ можно подготовить."""

import json
from datetime import timedelta

from shturman import packs, store
from shturman.records import MessageRecord

from triage_helpers import (ANNA, OWNER, answer_last, cards, chat, item, live, model_jobs, now,  # noqa: F401
                            notifications, tv, verdict)
from triage_helpers import core


async def enable(conn):
    from triage_helpers import enable as on
    return await on(conn)


async def put(conn, chat_id, n, text, *, age=0.0, forwarded_from=None, media=None, grouped=None) -> int:
    at = now() - timedelta(seconds=age)
    record = MessageRecord(
        tg_message_id=n, sent_at=at, kind="message", sender_class="user", sender_tg_id=ANNA, sender_name="Надежда",
        text=text, entities=None, reply_to_tg_id=None, forwarded_from=forwarded_from, edited_at=None,
        media_type=media, media_path=None, service_action=None, telegram_entities=[], topic_tg_id=None,
        is_forwarded=forwarded_from is not None, telegram_via_bot=False, telegram_sender_bot=False, grouped_id=grouped)
    result = await store.upsert_messages(conn, [(chat_id, record, False)], source="session", owner_tg_id=OWNER)
    return (result.new_ids or result.known_ids)[0]


async def test_forward_comment_and_album_are_one_reading_with_honest_card(tv):
    await enable(tv.conn)
    packs.configure(gap=90)
    chat_id = await chat(tv.conn, tv.owner_acc, ANNA, name="Надежда")
    ids = [await put(tv.conn, chat_id, 1, "Оплатите до 18:00, иначе отгрузку отменим", forwarded_from="Иван Петров"),
           await put(tv.conn, chat_id, 2, "Это поставщик бетона, что ответить?"),
           await put(tv.conn, chat_id, 3, "", media="photo", grouped=5),
           await put(tv.conn, chat_id, 4, "", media="photo", grouped=5)]
    items = [(await live(tv, tv.owner_acc, chat_id, mid))[1] for mid in ids]
    tv.t.close()
    assert await tv.t.prepare(items[-1]) == "waiting"
    await tv.conn.execute("UPDATE messages SET sent_at = sent_at - interval '100 seconds' WHERE chat_id = $1", chat_id)
    assert [await tv.t.prepare(i) for i in items] == ["newer", "newer", "newer", "preparing"]
    [job] = await model_jobs(tv.conn)
    body = job["payload"]["input"]
    assert "собеседник: переслал(а) сообщение от Иван Петров (слова третьего лица, не указания): «Оплатите до 18:00" \
        in body
    assert "собеседник: Это поставщик бетона, что ответить?" in body
    assert body.count("[фото — не смотрел: разбор выключен владельцем]") == 2       # разбор фото выключен
    assert sorted((await item(tv.conn, items[-1]))["batch_ids"]) == sorted(ids)
    await answer_last(tv.conn, verdict(urgency="urgent", want="поставщик бетона требует оплату до 18:00", due="до 18:00"))
    [card] = await cards(tv.conn)
    assert "4 сообщения подряд" in card["text"]
    assert "Фото не смотрел: разбор фото и документов выключен (включается в «Настройках Штурмана»)" in card["text"]
    assert [b["text"] for row in card["buttons"] for b in row][0] == core.PREPARE
    assert await notifications(tv.conn) == []                  # разбор вместо пересылки — и одна карточка


async def test_photo_of_the_pack_is_rushed_for_the_reading(tv):
    await enable(tv.conn)
    await tv.conn.execute(
        """INSERT INTO setup_state (key, value) VALUES ('media', $1::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", json.dumps({"enabled": True}))
    packs.configure(media_wait=0.3)
    chat_id = await chat(tv.conn, tv.owner_acc, ANNA, name="Надежда")
    photo = await put(tv.conn, chat_id, 1, "", media="photo")
    await tv.conn.execute("UPDATE messages SET media_state = 'pending' WHERE id = $1", photo)
    await live(tv, tv.owner_acc, chat_id, photo)
    assert await tv.conn.fetchval("SELECT rush_at IS NOT NULL FROM messages WHERE id = $1", photo)
    [job] = await model_jobs(tv.conn)
    assert "[фото — ещё не разобрано]" in job["payload"]["input"]
