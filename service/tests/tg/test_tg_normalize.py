"""Перевод объектов Telethon в словарь экспорта Telegram Desktop.

Сообщения собраны из настоящих объектов TL. Парные записи экспорта составлены по официальному
описанию формата (https://core.telegram.org/import-export), а не сняты с настоящей выгрузки.
"""

import dataclasses
from datetime import timedelta, timezone

import pytest
from telethon.tl import types

from shturman import store
from shturman.records import ChatRecord
from shturman.telegram_export import parse_message
from shturman.tg import normalize
from shturman.tg.normalize import chat_record, index_entities, message_record, text_entities

from tg_fakes import (C_NEWS, C_SUPER, CHANNEL, ENTITIES, G_FAMILY, GROUP, IVAN, MARIA, ME, SELF_ID,
                      SUPER, T0, U_BOT, U_IVAN, channel, msg, user)

ENT = index_entities(ENTITIES)
NOT_INCLUDED = "(File not included. Change data exporting settings to download.)"


def record(message, self_id=SELF_ID):
    return message_record(message, ENT, self_id=self_id)


def doc(*attrs, **flags):
    return types.MessageMediaDocument(
        document=types.Document(id=1, access_hash=1, file_reference=b"", date=T0, mime_type="x/y",
                                size=10, dc_id=2, attributes=list(attrs)), **flags)


# --- чаты ---

@pytest.mark.parametrize("entity,expected", [
    (ME, ("user", "saved_messages")),
    (U_IVAN, ("user", "personal_chat")),
    (U_BOT, ("user", "bot_chat")),
    (G_FAMILY, ("chat", "private_group")),
    (types.ChatForbidden(id=7, title="Закрытая"), ("chat", "private_group")),
    (C_SUPER, ("channel", "private_supergroup")),
    (channel(9, "Открытый чат", megagroup=True, username="open_chat"), ("channel", "public_supergroup")),
    (channel(9, "Закрытый канал", broadcast=True), ("channel", "private_channel")),
    (C_NEWS, ("channel", "public_channel")),
    (channel(9, "Имя из списка", broadcast=True,
             usernames=[types.Username("old", active=False), types.Username("fresh", active=True)]),
     ("channel", "public_channel")),
    (types.ChannelForbidden(id=9, access_hash=1, title="Ушли", megagroup=True), ("channel", "private_supergroup")),
])
def test_chat_types_use_export_vocabulary(entity, expected):
    chat = chat_record(entity, self_id=SELF_ID)
    assert (chat.peer_class, chat.type) == expected
    assert chat.type in normalize.CHAT_TYPES


def test_chat_record_carries_name_username_and_bot_flag():
    assert chat_record(U_IVAN, self_id=SELF_ID) == ChatRecord(
        "user", IVAN, "personal_chat", "Иван Петров", username="ivan_p", is_bot=False)
    assert chat_record(U_BOT, self_id=SELF_ID).is_bot is True
    assert chat_record(user(5, "", None, deleted=True), self_id=SELF_ID).name is None
    assert chat_record(types.UserEmpty(id=5), self_id=SELF_ID) is None


# --- разметка ---

def test_entity_offsets_are_utf16_code_units():
    text = "😀 жирный и https://example.org/doc конец"
    # эмодзи занимает две единицы UTF-16: «жирный» начинается с 3, а не с 2
    ents = [types.MessageEntityBold(offset=3, length=6),
            types.MessageEntityUrl(offset=12, length=23)]
    assert text_entities(text, ents) == [
        {"type": "bold", "text": "жирный"},
        {"type": "link", "text": "https://example.org/doc"},
    ]


def test_entity_types_and_extra_fields():
    text = "ссылка @ivan_p Иван код блок 😀 цитата +79990000000"
    ents = [
        types.MessageEntityTextUrl(offset=0, length=6, url="https://example.org"),
        types.MessageEntityMention(offset=7, length=7),
        types.MessageEntityMentionName(offset=15, length=4, user_id=IVAN),
        types.MessageEntityCode(offset=20, length=3),
        types.MessageEntityPre(offset=24, length=4, language="python"),
        types.MessageEntityCustomEmoji(offset=29, length=2, document_id=5368324170671202286),
        types.MessageEntityBlockquote(offset=32, length=6, collapsed=True),
        types.MessageEntityPhone(offset=39, length=12),
    ]
    assert text_entities(text, ents) == [
        {"type": "text_link", "text": "ссылка", "href": "https://example.org"},
        {"type": "mention", "text": "@ivan_p"},
        {"type": "mention_name", "text": "Иван", "user_id": IVAN},
        {"type": "code", "text": "код"},
        {"type": "pre", "text": "блок", "language": "python"},
        {"type": "custom_emoji", "text": "😀", "document_id": "5368324170671202286"},
        {"type": "blockquote", "text": "цитата", "collapsed": True},
        {"type": "phone", "text": "+79990000000"},
    ]


@pytest.mark.parametrize("cls,name", [
    (types.MessageEntityHashtag, "hashtag"), (types.MessageEntityBotCommand, "bot_command"),
    (types.MessageEntityEmail, "email"), (types.MessageEntityItalic, "italic"),
    (types.MessageEntityCashtag, "cashtag"), (types.MessageEntityUnderline, "underline"),
    (types.MessageEntityStrike, "strikethrough"), (types.MessageEntityBankCard, "bank_card"),
    (types.MessageEntitySpoiler, "spoiler"), (types.MessageEntityUnknown, "unknown"),
])
def test_simple_entity_names(cls, name):
    assert text_entities("слово", [cls(offset=0, length=5)]) == [{"type": name, "text": "слово"}]


def test_broken_entity_is_skipped_not_stored_wrong():
    text = "a😀b"
    ents = [types.MessageEntityBold(offset=2, length=1),    # режет эмодзи пополам
            types.MessageEntityBold(offset=3, length=50),   # выходит за текст
            types.MessageEntityItalic(offset=3, length=1)]
    assert text_entities(text, ents) == [{"type": "italic", "text": "b"}]
    assert text_entities("без разметки", None) is None


# --- вложения ---

@pytest.mark.parametrize("media,expected", [
    (None, None),
    (types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1)), "photo"),
    (doc(types.DocumentAttributeFilename("смета.pdf")), "file"),
    (doc(types.DocumentAttributeAudio(duration=7, voice=True)), "voice_message"),
    (doc(types.DocumentAttributeAudio(duration=180, title="Песня")), "audio_file"),
    (doc(types.DocumentAttributeVideo(duration=5, w=240, h=240, round_message=True)), "video_message"),
    (doc(types.DocumentAttributeVideo(duration=60, w=1280, h=720)), "video_file"),
    (doc(types.DocumentAttributeVideo(duration=3, w=320, h=240), types.DocumentAttributeAnimated()), "animation"),
    (doc(types.DocumentAttributeImageSize(512, 512),
         types.DocumentAttributeSticker(alt="👍", stickerset=types.InputStickerSetEmpty())), "sticker"),
    (doc(voice=True), "voice_message"),
    (doc(round=True), "video_message"),
    (types.MessageMediaGeo(geo=types.GeoPointEmpty()), None),
    (types.MessageMediaContact(phone_number="1", first_name="И", last_name="П", vcard="", user_id=1), None),
    (types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1)), None),
])
def test_media_types_use_export_vocabulary(media, expected):
    assert normalize.media_type(media) == expected


# --- служебные сообщения ---

@pytest.mark.parametrize("action,expected", [
    (types.MessageActionPhoneCall(call_id=1, duration=42), "phone_call"),
    (types.MessageActionChatAddUser(users=[MARIA]), "invite_members"),
    (types.MessageActionChatDeleteUser(user_id=MARIA), "remove_members"),
    (types.MessageActionChatJoinedByLink(inviter_id=IVAN), "join_group_by_link"),
    (types.MessageActionChatCreate(title="Семья", users=[IVAN]), "create_group"),
    (types.MessageActionChatEditTitle(title="Новая"), "edit_group_title"),
    (types.MessageActionPinMessage(), "pin_message"),
    (types.MessageActionChannelCreate(title="Канал"), "create_channel"),
    (types.MessageActionChatMigrateTo(channel_id=SUPER), "migrate_to_supergroup"),
    (types.MessageActionContactSignUp(), "joined_telegram"),
    (types.MessageActionHistoryClear(), "clear_history"),
    # действие, которого нет в таблице, получает имя из названия класса
    (types.MessageActionPaymentRefunded(peer=types.PeerUser(1), currency="RUB", total_amount=1,
                                        charge=types.PaymentCharge(id="1", provider_charge_id="1")),
     "payment_refunded"),
])
def test_service_actions_use_export_vocabulary(action, expected):
    assert normalize.service_action(action) == expected


def test_service_message_is_archived_as_service_with_actor():
    service = types.MessageService(id=4, peer_id=types.PeerChat(GROUP), date=T0,
                                   from_id=types.PeerUser(IVAN), action=types.MessageActionChatAddUser([MARIA]))
    r = record(service)
    assert (r.kind, r.service_action, r.text, r.entities, r.media_type) == \
           ("service", "invite_members", "", None, None)
    assert (r.sender_class, r.sender_tg_id, r.sender_name) == ("user", IVAN, "Иван Петров")


# --- отправитель, направление, ответ, пересылка ---

def test_sender_in_private_chat_comes_from_direction_not_from_id():
    incoming = record(msg(1, ("user", IVAN), "привет"))
    outgoing = record(msg(2, ("user", IVAN), "и вам", out=True))
    assert (incoming.sender_tg_id, incoming.sender_name) == (IVAN, "Иван Петров")
    assert (outgoing.sender_tg_id, outgoing.sender_name) == (SELF_ID, "Евгений Тестов")
    assert normalize.is_outgoing(msg(2, ("user", IVAN), "и вам", out=True), self_id=SELF_ID) is True
    assert normalize.is_outgoing(msg(1, ("user", IVAN), "привет"), self_id=SELF_ID) is False


def test_saved_messages_own_notes_are_outgoing_forwards_are_not():
    note = msg(1, ("user", SELF_ID), "заметка")
    forwarded = msg(2, ("user", SELF_ID), "чужое", fwd_from=types.MessageFwdHeader(date=T0, from_id=types.PeerUser(IVAN)))
    assert normalize.is_outgoing(note, self_id=SELF_ID) is True
    assert normalize.is_outgoing(forwarded, self_id=SELF_ID) is False
    assert record(forwarded).forwarded_from == "Иван Петров"


def test_channel_post_and_group_message_senders():
    post = record(msg(1, ("channel", CHANNEL), "новость", post=True))
    assert (post.sender_class, post.sender_tg_id, post.sender_name) == ("channel", CHANNEL, "Стройка: новости")
    in_group = record(msg(10, ("chat", GROUP), "купи хлеба", sender=MARIA))
    assert (in_group.sender_class, in_group.sender_tg_id, in_group.sender_name) == ("user", MARIA, "Мария")


def test_reply_forward_edit_and_bot_fields():
    m = msg(3, ("user", IVAN), "см. выше",
            reply_to=types.MessageReplyHeader(reply_to_msg_id=2),
            fwd_from=types.MessageFwdHeader(date=T0, from_name="Скрытый Автор"),
            edit_date=T0 + timedelta(hours=1))
    r = record(m)
    assert (r.reply_to_tg_id, r.forwarded_from, r.edited_at) == (2, "Скрытый Автор", T0 + timedelta(hours=1))
    assert r.media_path is None
    from_channel = msg(4, ("user", IVAN), "", fwd_from=types.MessageFwdHeader(date=T0, from_id=types.PeerChannel(CHANNEL)))
    assert record(from_channel).forwarded_from == "Стройка: новости"
    # ответ на сообщение из другого чата: его номер к этому чату не относится
    cross = msg(5, ("user", IVAN), "о том посте", reply_to=types.MessageReplyHeader(
        reply_to_msg_id=77, reply_to_peer_id=types.PeerChannel(CHANNEL)))
    assert record(cross).reply_to_tg_id is None
    story = msg(6, ("user", IVAN), "на историю", reply_to=types.MessageReplyStoryHeader(
        peer=types.PeerUser(IVAN), story_id=1))
    assert record(story).reply_to_tg_id is None


def test_empty_message_gives_no_record():
    assert record(types.MessageEmpty(id=9, peer_id=None)) is None


def test_short_updates_become_ordinary_messages():
    short = types.UpdateShortMessage(id=7, user_id=IVAN, message="коротко", pts=1, pts_count=1, date=T0, out=True)
    m = normalize.message_from_update(short, self_id=SELF_ID)
    r = record(m)
    assert normalize.peer_key(m.peer_id) == ("user", IVAN) and normalize.is_outgoing(m, self_id=SELF_ID)
    assert (r.tg_message_id, r.text, r.sender_tg_id) == (7, "коротко", SELF_ID)
    in_chat = types.UpdateShortChatMessage(id=8, from_id=MARIA, chat_id=GROUP, message="в группе",
                                           pts=1, pts_count=1, date=T0)
    m = normalize.message_from_update(in_chat, self_id=SELF_ID)
    assert normalize.peer_key(m.peer_id) == ("chat", GROUP) and record(m).sender_tg_id == MARIA
    assert normalize.message_from_update(types.UpdateNewMessage(types.MessageEmpty(1, None), 1, 1),
                                         self_id=SELF_ID) is None


# --- одно и то же сообщение из экспорта и из сессии ---

def export_base(mid, text, **extra):
    at = T0 + timedelta(minutes=mid)
    raw = {"id": mid, "type": "message", "date": at.strftime("%Y-%m-%dT%H:%M:%S"),
           "date_unixtime": str(int(at.timestamp())), "from": "Иван Петров", "from_id": f"user{IVAN}",
           "text": text, "text_entities": [{"type": "plain", "text": text}] if text else []}
    raw.update(extra)
    return raw


FORMATTED = "Договор 😀 лежит https://example.org/doc, посмотрите"

PAIRS = {
    "plain": (
        export_base(1, "Добрый день! Пришлю смету к пятнице."),
        msg(1, ("user", IVAN), "Добрый день! Пришлю смету к пятнице."),
    ),
    "outgoing": (
        export_base(2, "Хорошо, жду.", **{"from": "Евгений Тестов", "from_id": f"user{SELF_ID}"}),
        msg(2, ("user", IVAN), "Хорошо, жду.", out=True),
    ),
    "formatted_reply_edited": (
        export_base(3, ["Договор 😀 лежит ", {"type": "link", "text": "https://example.org/doc"}, ", посмотрите"],
                    text_entities=[{"type": "plain", "text": "Договор 😀 лежит "},
                                   {"type": "link", "text": "https://example.org/doc"},
                                   {"type": "plain", "text": ", посмотрите"}],
                    reply_to_message_id=2, edited="2026-09-12T11:03:00",
                    edited_unixtime=str(int((T0 + timedelta(minutes=63)).timestamp()))),
        msg(3, ("user", IVAN), FORMATTED, entities=[types.MessageEntityUrl(offset=17, length=23)],
            reply_to=types.MessageReplyHeader(reply_to_msg_id=2), edit_date=T0 + timedelta(minutes=63)),
    ),
    "forwarded": (
        export_base(4, "Купи хлеба", forwarded_from="Мария"),
        msg(4, ("user", IVAN), "Купи хлеба",
            fwd_from=types.MessageFwdHeader(date=T0, from_id=types.PeerUser(MARIA))),
    ),
    "voice": (
        export_base(5, "", media_type="voice_message", file=NOT_INCLUDED, mime_type="audio/ogg", duration_seconds=7),
        msg(5, ("user", IVAN), "", media=doc(types.DocumentAttributeAudio(duration=7, voice=True), voice=True)),
    ),
    "photo_with_caption": (
        export_base(6, "Фото объекта", photo=NOT_INCLUDED, width=1280, height=720),
        msg(6, ("user", IVAN), "Фото объекта", media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1))),
    ),
    "document": (
        export_base(7, "", file=NOT_INCLUDED, mime_type="application/pdf"),
        msg(7, ("user", IVAN), "", media=doc(types.DocumentAttributeFilename("смета.pdf"))),
    ),
    "service_call": (
        {"id": 8, "type": "service", "date": "2026-09-12T10:08:00",
         "date_unixtime": str(int((T0 + timedelta(minutes=8)).timestamp())),
         "actor": "Иван Петров", "actor_id": f"user{IVAN}", "action": "phone_call",
         "duration_seconds": 42, "text": "", "text_entities": []},
        types.MessageService(id=8, peer_id=types.PeerUser(IVAN), date=T0 + timedelta(minutes=8),
                             action=types.MessageActionPhoneCall(call_id=1, duration=42)),
    ),
}


@pytest.mark.parametrize("name", sorted(PAIRS))
def test_session_record_equals_export_record(name):
    raw, message = PAIRS[name]
    from_export, from_session = parse_message(raw), record(message)
    assert dataclasses.asdict(from_session) == dataclasses.asdict(from_export)
    assert from_session.sent_at.tzinfo == timezone.utc


async def test_export_then_session_is_one_row_without_false_edit(conn):
    account_id = await store.ensure_account(conn, SELF_ID, "Владелец")
    chat_id, _ = await store.ensure_chat(conn, account_id, ChatRecord("user", IVAN, "personal_chat", "Иван Петров"))
    exported = [(chat_id, parse_message(raw)) for raw, _ in PAIRS.values()]
    live = [(chat_id, record(m), normalize.is_outgoing(m, self_id=SELF_ID)) for _, m in PAIRS.values()]
    first = await store.upsert_messages(conn, exported, source="import", owner_tg_id=SELF_ID)
    second = await store.upsert_messages(conn, live, source="session", owner_tg_id=SELF_ID)
    assert (first.new, second.new, second.known, second.versions) == (len(PAIRS), 0, len(PAIRS), 0)
    assert await conn.fetchval("SELECT count(*) FROM messages") == len(PAIRS)
    assert await conn.fetchval("SELECT count(*) FROM message_versions") == 0
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE sources = ARRAY['import', 'session']") == len(PAIRS)
    assert await conn.fetchval("SELECT is_outgoing FROM messages WHERE tg_message_id = 2") is True
    # и в обратном порядке: сначала сессия, потом тот же экспорт
    await conn.execute("DELETE FROM messages")
    await store.upsert_messages(conn, live, source="session", owner_tg_id=SELF_ID)
    again = await store.upsert_messages(conn, exported, source="import", owner_tg_id=SELF_ID)
    assert (again.new, again.versions) == (0, 0)
    assert await conn.fetchval("SELECT count(*) FROM message_versions") == 0
