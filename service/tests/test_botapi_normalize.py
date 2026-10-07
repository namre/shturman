"""Сообщение Bot API приводится к тем же значениям, что и то же сообщение из экспорта.

Пары «экспорт — Bot API» составлены по описаниям форматов, а не сняты с настоящего аккаунта:
тесты проверяют, что разбор двух форматов сходится, но не то, что Telegram действительно
отдаёт одно и то же сообщение именно так.
"""

from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from shturman.botapi_normalize import (
    NormalizeError, SkipMessage, chat_record, clean, forwarded_from, normalize_message, seen_by,
    text_entities,
)
from shturman.telegram_export import parse_message

from conftest import IVAN, OWNER, msg

T = 1789200000
IVAN_USER = {"id": IVAN, "is_bot": False, "first_name": "Иван", "last_name": "Петров", "username": "ivan_p"}
OWNER_USER = {"id": OWNER, "is_bot": False, "first_name": "Евгений", "last_name": "Тестов"}
IVAN_CHAT = {"id": IVAN, "type": "private", "first_name": "Иван", "last_name": "Петров", "username": "ivan_p"}


def bot(mid, text=None, *, sender=IVAN_USER, ts=T, **extra):
    m = {"message_id": mid, "date": ts, "chat": IVAN_CHAT, "from": sender, "business_connection_id": "bc1"}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return m


def units(s: str) -> int:
    """Длина в кодовых единицах UTF-16 — так считает Bot API."""
    return len(s.encode("utf-16-le")) // 2


def ent(text: str, fragment: str, kind: str, **extra):
    start = text.index(fragment)
    return {"type": kind, "offset": units(text[:start]), "length": units(fragment), **extra}


def same(export_raw, bot_raw):
    """Запись из экспорта и запись из Bot API совпадают во всём, кроме пути к файлу."""
    a, b = asdict(parse_message(export_raw)), asdict(normalize_message(bot_raw).record)
    a.pop("media_path"), b.pop("media_path")
    assert a == b
    return normalize_message(bot_raw)


def test_plain_text_matches_export():
    n = same(msg(1, T, IVAN, "Иван Петров", "Добрый день!"), bot(1, "Добрый день!"))
    assert n.record.sent_at == datetime.fromtimestamp(T, tz=timezone.utc)
    assert n.record.entities is None and n.record.media_path is None
    assert (n.chat.peer_class, n.chat.tg_id, n.chat.type, n.chat.name, n.chat.username) == (
        "user", IVAN, "personal_chat", "Иван Петров", "ivan_p")


def test_entity_offsets_are_utf16_and_names_follow_export():
    text = "😀 Смета: https://example.org/doc, пишите @ivan_p или на a@b.ru, тел. +79990001122 #фасад /start $USD"
    pairs = [("https://example.org/doc", "url", "link"), ("@ivan_p", "mention", "mention"),
             ("a@b.ru", "email", "email"), ("+79990001122", "phone_number", "phone"),
             ("#фасад", "hashtag", "hashtag"), ("/start", "bot_command", "bot_command"),
             ("$USD", "cashtag", "cashtag")]
    entities = [ent(text, frag, kind) for frag, kind, _ in pairs]
    assert entities[0]["offset"] == text.index("https") + 1   # эмодзи занимает две единицы, а не одну
    export_entities, rest = [], text
    for frag, _, name in pairs:
        before, rest = rest.split(frag, 1)
        export_entities += [{"type": "plain", "text": before}, {"type": name, "text": frag}]
    n = same(msg(2, T, IVAN, "Иван Петров", text, text_entities=export_entities), bot(2, text, entities=entities))
    assert [e["type"] for e in n.record.entities] == [name for _, _, name in pairs]


def test_entities_with_extra_fields_match_export():
    text = "жирный курсив подчёркнутый зачёркнутый скрытый код блок ссылка Мария 🙂 цитата свёрнутая"
    spec = [
        ("жирный", "bold", {}, "bold", {}),
        ("курсив", "italic", {}, "italic", {}),
        ("подчёркнутый", "underline", {}, "underline", {}),
        ("зачёркнутый", "strikethrough", {}, "strikethrough", {}),
        ("скрытый", "spoiler", {}, "spoiler", {}),
        ("код", "code", {}, "code", {}),
        ("блок", "pre", {"language": "python"}, "pre", {"language": "python"}),
        ("ссылка", "text_link", {"url": "https://example.org/"}, "text_link", {"href": "https://example.org/"}),
        ("Мария", "text_mention", {"user": {"id": 2002, "is_bot": False, "first_name": "Мария"}},
         "mention_name", {"user_id": 2002}),
        ("🙂", "custom_emoji", {"custom_emoji_id": "5368324170671202286"},
         "custom_emoji", {"document_id": "5368324170671202286"}),
        ("цитата", "blockquote", {}, "blockquote", {"collapsed": False}),
        ("свёрнутая", "expandable_blockquote", {}, "blockquote", {"collapsed": True}),
    ]
    entities = [ent(text, frag, kind, **extra) for frag, kind, extra, _, _ in spec]
    export_entities = []
    for i, (frag, _, _, name, extra) in enumerate(spec):
        if i:
            export_entities.append({"type": "plain", "text": " "})
        export_entities.append({"type": name, "text": frag, **extra})
    same(msg(3, T, IVAN, "Иван Петров", text, text_entities=export_entities), bot(3, text, entities=entities))


def test_nested_and_broken_entities_follow_export_rule():
    text = "важное слово и хвост"
    entities = [
        ent(text, "важное слово", "bold"),
        ent(text, "слово", "italic"),                       # внутри уже взятого — экспорт его не пишет
        {"type": "bold", "offset": 50, "length": 5},        # за пределами текста
        {"type": "bold", "offset": "x", "length": 2},
        "мусор",
        ent(text, "хвост", "date_time", unix_time=T),       # экспорт называет это unknown
    ]
    assert text_entities(text, entities) == [
        {"type": "bold", "text": "важное слово"}, {"type": "unknown", "text": "хвост"}]
    assert text_entities(text, None) is None and text_entities("", entities) is None
    # граница посреди суррогатной пары — такой фрагмент пропускается, а не ломает запись
    assert text_entities("😀", [{"type": "bold", "offset": 1, "length": 1}]) is None


def test_reply_forward_and_edit_match_export():
    forwards = [
        ({"type": "user", "date": T - 9, "sender_user": {"id": 2002, "is_bot": False, "first_name": "Мария",
                                                          "last_name": "Соколова"}}, "Мария Соколова"),
        ({"type": "hidden_user", "date": T - 9, "sender_user_name": "Скрытый Автор"}, "Скрытый Автор"),
        ({"type": "channel", "date": T - 9, "message_id": 5,
          "chat": {"id": -1004001, "type": "channel", "title": "Стройка: новости"}}, "Стройка: новости"),
        ({"type": "chat", "date": T - 9,
          "sender_chat": {"id": -1003001, "type": "supergroup", "title": "Подрядчики"}}, "Подрядчики"),
    ]
    for origin, name in forwards:
        same(
            msg(7, T, IVAN, "Иван Петров", "к понедельнику", reply_to_message_id=3, forwarded_from=name,
                edited_unixtime=str(T + 3600)),
            bot(7, "к понедельнику", reply_to_message=bot(3, "исходное"), forward_origin=origin,
                edit_date=T + 3600),
        )
    assert forwarded_from({"type": "user", "sender_user": {"id": 5, "first_name": ""}}) is None  # удалённый аккаунт
    assert forwarded_from({"type": "что-то новое"}) is None and forwarded_from("x") is None
    # ответ на сообщение из другого чата ссылкой внутри чата не становится
    assert normalize_message(bot(8, "ок", external_reply={"message_id": 77})).record.reply_to_tg_id is None


@pytest.mark.parametrize("fields,export_extra", [
    ({"voice": {"file_id": "v", "duration": 7}},
     {"media_type": "voice_message", "file": "chats/chat_01/voice_messages/audio_1.ogg"}),
    ({"video_note": {"file_id": "v", "length": 240, "duration": 5}},
     {"media_type": "video_message", "file": "(File not included. Change data exporting settings to download.)"}),
    ({"video": {"file_id": "v", "width": 1, "height": 1, "duration": 5}, "caption": "Обход объекта"},
     {"media_type": "video_file", "file": "video.mp4"}),
    ({"audio": {"file_id": "a", "duration": 5}}, {"media_type": "audio_file", "file": "a.mp3"}),
    ({"sticker": {"file_id": "s", "type": "regular", "width": 512, "height": 512, "emoji": "👍"}},
     {"media_type": "sticker", "file": "s.webp", "sticker_emoji": "👍"}),
    ({"animation": {"file_id": "g", "width": 1, "height": 1, "duration": 1},
      "document": {"file_id": "g"}}, {"media_type": "animation", "file": "g.mp4"}),
    ({"photo": [{"file_id": "p", "width": 90, "height": 90}], "caption": "Фото объекта"},
     {"photo": "(File not included. Change data exporting settings to download.)"}),
    ({"document": {"file_id": "d", "file_name": "smeta.xlsx"}, "caption": "Смета"}, {"file": "files/smeta.xlsx"}),
])
def test_media_kinds_match_export(fields, export_extra):
    caption = fields.get("caption", "")
    n = same(msg(5, T, IVAN, "Иван Петров", caption, **export_extra), bot(5, **fields))
    assert n.record.media_type is not None and n.record.media_path is None


def test_caption_entities_are_used_for_media():
    caption = "Смета: https://example.org/s"
    n = same(
        msg(6, T, IVAN, "Иван Петров", caption, photo="p.jpg", text_entities=[
            {"type": "plain", "text": "Смета: "}, {"type": "link", "text": "https://example.org/s"}]),
        bot(6, photo=[{"file_id": "p"}], caption=caption,
            caption_entities=[ent(caption, "https://example.org/s", "url")]),
    )
    assert n.record.media_type == "photo"


def test_service_messages_use_export_action_names():
    export = {"id": 9, "type": "service", "date": "x", "date_unixtime": str(T), "actor": "Иван Петров",
              "actor_id": f"user{IVAN}", "action": "pin_message", "message_id": 3, "text": "", "text_entities": []}
    n = same(export, bot(9, pinned_message=bot(3, "важное")))
    assert (n.record.kind, n.record.service_action, n.record.media_type) == ("service", "pin_message", None)
    cases = [
        ({"message_auto_delete_timer_changed": {"message_auto_delete_time": 86400}}, "set_messages_ttl"),
        ({"write_access_allowed": {"from_attachment_menu": True}}, "attach_menu_bot_allowed"),
        ({"write_access_allowed": {"from_request": True}}, "web_app_bot_allowed"),
        ({"write_access_allowed": {}}, "allow_sending_messages"),
        ({"successful_payment": {"currency": "XTR", "total_amount": 1}}, "send_payment"),
        ({"checklist_tasks_done": {"marked_as_done_task_ids": [1]}}, "todo_completions"),
    ]
    for fields, action in cases:
        assert normalize_message(bot(10, **fields)).record.service_action == action


def test_bot_flags_and_chat_kinds():
    plain = normalize_message(bot(1, "привет"))
    assert (plain.via_bot, plain.by_business_bot, plain.chat.is_bot) == (False, False, False)
    inline = normalize_message(bot(2, "картинка", via_bot={"id": 7, "is_bot": True, "first_name": "gif"}))
    assert (inline.via_bot, inline.by_business_bot) == (True, False)
    ours = normalize_message(bot(3, "Спасибо, получил.", sender=OWNER_USER,
                                 sender_business_bot={"id": 8, "is_bot": True, "first_name": "Штурман"}))
    assert (ours.via_bot, ours.by_business_bot) == (True, True)
    # по исходящему сообщению не видно, бот ли собеседник
    assert (ours.chat.is_bot, ours.chat.type) == (None, "personal_chat")

    helper = {"id": 5005, "is_bot": True, "first_name": "Помощник", "username": "helper_bot"}
    with_bot = normalize_message({"message_id": 4, "date": T, "from": helper, "text": "готово",
                                  "chat": {"id": 5005, "type": "private", "first_name": "Помощник"}})
    assert (with_bot.chat.type, with_bot.chat.is_bot) == ("bot_chat", True)

    own = chat_record({"id": OWNER, "type": "private", "first_name": "Евгений"})
    assert seen_by(own, OWNER).type == "saved_messages" and seen_by(plain.chat, OWNER).type == "personal_chat"
    nameless = chat_record({"id": 77, "type": "private", "first_name": ""})   # удалённый аккаунт
    assert nameless.name is None and nameless.username is None


def test_message_without_id_is_skipped_not_stored():
    with pytest.raises(SkipMessage):
        normalize_message(bot(0, "ещё не отправлено"))


@pytest.mark.parametrize("broken", [
    None, [], "строка", 5,
    {},
    {"message_id": 1, "date": T},                                              # нет чата
    bot(1, "в группе", chat={"id": -1003001, "type": "supergroup", "title": "Подрядчики"}),
    bot(1, "в группе", chat={"id": -3001, "type": "group", "title": "Семья"}),
    bot(1, "канал", chat={"id": -1004001, "type": "channel", "title": "Новости"}),
    bot(1, "в группе", chat={"id": 3001, "type": "group", "title": "Семья"}),
    bot(1, "без вида чата", chat={"id": 2001}),
    bot(1, "x", chat={"id": "2001", "type": "private"}),
    bot(1, "x", chat={"id": 2 ** 70, "type": "private"}),
    bot("1", "x"), bot(None, "x"), bot(-5, "x"), bot(True, "x"), bot(2 ** 64, "x"),
    bot(1, "x", ts=None), bot(1, "x", ts="вчера"), bot(1, "x", ts=-1), bot(1, "x", ts=10 ** 15),
    bot(1, "x", ts=float("nan")),
    bot(1, "x", edit_date="потом"),
    bot(1, "x", sender={"id": "abc"}), bot(1, "x", sender={}),
    bot(1, "я" * 20000),
])
def test_malformed_message_is_rejected_with_russian_text(broken):
    with pytest.raises(NormalizeError) as err:
        normalize_message(broken)
    assert any("а" <= ch <= "я" for ch in str(err.value))


def test_odd_but_harmless_shapes_do_not_break():
    odd = bot(1, "текст", entities="не список", reply_to_message="x", forward_origin=5, photo="да",
              via_bot="бот", edit_date=None, caption=5)
    n = normalize_message(odd)
    assert (n.record.text, n.record.entities, n.record.reply_to_tg_id, n.record.forwarded_from) == (
        "текст", None, None, None)
    assert normalize_message(bot(2, sender=None)).record.sender_tg_id is None
    assert normalize_message(bot(3, "x", ts=float(T))).record.sent_at == datetime.fromtimestamp(T, tz=timezone.utc)


def test_strings_are_safe_for_the_database():
    assert clean("a\x00b") == "ab" and clean(5) is None
    lone = clean("до \ud83d после")
    lone.encode("utf-8")
    n = normalize_message(bot(1, "нуль\x00 и \ud83d половинка", sender={"id": IVAN, "first_name": "Ив\x00ан"}))
    n.record.text.encode("utf-8")
    assert "\x00" not in n.record.text and n.record.sender_name == "Иван"
