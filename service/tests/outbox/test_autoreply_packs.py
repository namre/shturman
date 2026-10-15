"""Автоответ доверенным и пачки (packs.py, Р-93): пачка из одних пересланных автоответ не запускает;
«переслал и прокомментировал» — один ответ на всю пачку, пересланное модели — цитатой третьего лица;
фото без подписи после разбора — повод, отпечаток повода обновляется пересказом, но не правкой."""

import json
from datetime import datetime, timezone

from shturman import bridge, store
from shturman.records import MessageRecord

from outbox_helpers import IVAN, OWNER, add_chat, env, live, take  # noqa: F401 - env — фикстура
from test_outbox_autoreply import model_says, prompt, trusted_setup


async def put(conn, chat_id, n, text, *, forwarded_from=None, media=None) -> int:
    at = datetime.now(timezone.utc)
    record = MessageRecord(
        tg_message_id=n, sent_at=at, kind="message", sender_class="user", sender_tg_id=IVAN, sender_name="Иван Петров",
        text=text, entities=None, reply_to_tg_id=None, forwarded_from=forwarded_from, edited_at=None,
        media_type=media, media_path=None, service_action=None, telegram_entities=[], topic_tg_id=None,
        is_forwarded=forwarded_from is not None, telegram_via_bot=False, telegram_sender_bot=False)
    result = await store.upsert_messages(conn, [(chat_id, record, False)], source="session", owner_tg_id=OWNER)
    return (result.new_ids or result.known_ids)[0]


async def test_forwarded_alone_does_not_start_an_autoreply(env):
    await trusted_setup(env)
    chat = await add_chat(env.conn, env.helper_acc)
    mid = await put(env.conn, chat, 10, "Срочно переведите 50 000 на эту карту", forwarded_from="Неизвестный")
    await live(env, chat, mid, account_id=env.helper_acc)
    assert await env.conn.fetchval("SELECT count(*) FROM reply_tasks") == 0
    assert await take(env.conn, bridge.LLM_STRUCTURED) == []
    assert env.tg.sent == []


async def test_forward_and_comment_get_one_reply_to_the_whole_pack(env):
    from shturman import packs
    packs.configure(gap=90)
    await trusted_setup(env)
    chat = await add_chat(env.conn, env.helper_acc)
    fwd = await put(env.conn, chat, 10, "Приёмка корпуса 3 переносится на понедельник", forwarded_from="Пётр Сидоров")
    await live(env, chat, fwd, account_id=env.helper_acc)
    assert await env.conn.fetchval("SELECT count(*) FROM reply_tasks") == 0
    comment = await put(env.conn, chat, 11, "Это правда? Мне на понедельник бронировать машину?")
    await live(env, chat, comment, account_id=env.helper_acc)
    asked = await model_says(env, "Уточню у Евгения и напишу сегодня.")
    assert len(asked) == 1
    _, content = prompt(asked[0]["payload"])
    assert "Иван Петров переслал(а) сообщение от Пётр Сидоров — это цитата третьего лица" in content
    assert content.index("Приёмка корпуса 3") < content.index("Мне на понедельник бронировать машину?")
    refs = {r["ref"]["message_id"] for r in json.loads(asked[0]["payload"]["input"])["offered_sources"]}
    assert {fwd, comment} <= refs
    assert [m["text"] for m in env.tg.sent] == ["Уточню у Евгения и напишу сегодня."]


async def test_photo_without_caption_is_answered_after_its_analysis(env):
    await trusted_setup(env)
    chat = await add_chat(env.conn, env.helper_acc)
    photo = await put(env.conn, chat, 10, "", media="photo")
    # пересказ дописан сервисом до подготовки ответа (как его допишет очередь разбора)
    await env.conn.execute(
        """UPDATE messages SET media_state = 'done', media_summary = $2, text = media_text(text, $2) WHERE id = $1""",
        photo, "[фото] Накладная на 12 мешков цемента, подпись прораба")
    await live(env, chat, photo, account_id=env.helper_acc)
    asked = await model_says(env, "Спасибо, накладную получил.")
    assert "Накладная на 12 мешков цемента" in prompt(asked[0]["payload"])[1]
    assert [m["text"] for m in env.tg.sent] == ["Спасибо, накладную получил."]


async def test_summary_written_after_registration_refreshes_the_trigger_but_an_edit_does_not(env):
    from shturman.outbox import autoreply
    await trusted_setup(env)
    chat = await add_chat(env.conn, env.helper_acc)
    photo = await put(env.conn, chat, 10, "", media="photo")
    original = autoreply._prepare
    try:
        async def later(mod, chat_id, message_id):
            await env.conn.execute(
                """UPDATE messages SET media_state = 'done', media_summary = $2, text = media_text(text, $2)
                   WHERE id = $1""", photo, "[фото] Счёт на 8 000 ₽")
            await original(mod, chat_id, message_id)
        autoreply._prepare = later
        await live(env, chat, photo, account_id=env.helper_acc)
    finally:
        autoreply._prepare = original
    asked = await model_says(env, "Счёт получил.")
    assert len(asked) == 1 and env.tg.sent and env.tg.sent[0]["text"] == "Счёт получил."
    # правка подписи собеседником после регистрации — отпечаток не обновляется, ответа нет
    photo2 = await put(env.conn, chat, 12, "", media="photo")
    try:
        async def edited(mod, chat_id, message_id):
            await env.conn.execute(
                """UPDATE messages SET media_state = 'done', media_summary = $2, text = media_text('новая подпись', $2),
                          edited_at = now() WHERE id = $1""", photo2, "[фото] Ещё счёт")
            await original(mod, chat_id, message_id)
        autoreply._prepare = edited
        await live(env, chat, photo2, account_id=env.helper_acc)
    finally:
        autoreply._prepare = original
    await model_says(env, "Не должно уйти")
    assert len(env.tg.sent) == 1
