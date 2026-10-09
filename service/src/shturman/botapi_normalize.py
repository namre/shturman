"""Сообщение Bot API → запись архива в словаре экспорта Telegram Desktop.

Плагин в Hermes пересылает обновления бизнес-режима как есть: объект `Message` в том виде,
в каком его отдаёт Bot API (результат `to_dict()` библиотеки бота). Здесь он приводится к тем
же значениям, что даёт разбор экспорта (`telegram_export.parse_message`): одно и то же
сообщение, пришедшее из экспорта и от бизнес-бота, должно лечь в одну строку архива и не
породить ложную «правку».

Сверено на 2026-10-06:
  * Bot API 10.3 — https://core.telegram.org/bots/api (поля Message, MessageEntity,
    MessageOrigin, BusinessConnection);
  * формат экспорта — https://core.telegram.org/import-export и имена значений, которые пишет
    Telegram Desktop (export_output_json.cpp, export_data_types.cpp, ветка dev, f23c378).
    Исходный код Telegram Desktop (GPL-3.0) использован только как справочник имён и правил,
    фрагменты из него не заимствованы.

На настоящих данных НЕ проверено (см. также «Что ещё не проверено» в docs/service.md):
совпадают ли идентификаторы сообщений в бизнес-потоке и в экспорте; совпадает ли текст
знак в знак; в каком порядке приходят вложенные фрагменты разметки; имена собеседников
(экспорт пишет имя так, как его видит аккаунт владельца, Bot API — так, как его видит бот).

Только личные чаты: бизнес-режим работает в переписке один на один. Файлы вложений не
скачиваются — `media_path` всегда пуст.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from .records import ChatRecord, MessageRecord

MAX_ID = 2**63 - 1          # идентификаторы хранятся в bigint
MAX_UNIXTIME = 2**33        # 2242 год: всё, что дальше, — мусор, а не время сообщения
# У Telegram предел текста — 4096 знаков; запас оставлен на будущие изменения предела.
MAX_TEXT_CHARS = 16384


class NormalizeError(ValueError):
    """Объект не похож на сообщение Bot API из личного чата. Текст — для владельца."""


class SkipMessage(NormalizeError):
    """Сообщение настоящее, но в архив не идёт (например, у него ещё нет идентификатора)."""


# Вид фрагмента разметки: Bot API → экспорт. Имена экспорта — из SerializeText в Telegram Desktop.
# Чего Bot API не различает: bank_card (приходит как обычный текст).
ENTITY_TYPES = {
    "mention": "mention",
    "hashtag": "hashtag",
    "cashtag": "cashtag",
    "bot_command": "bot_command",
    "url": "link",
    "email": "email",
    "phone_number": "phone",
    "bold": "bold",
    "italic": "italic",
    "underline": "underline",
    "strikethrough": "strikethrough",
    "spoiler": "spoiler",
    "blockquote": "blockquote",
    "expandable_blockquote": "blockquote",   # в экспорте — blockquote с collapsed: true
    "code": "code",
    "pre": "pre",
    "text_link": "text_link",
    "text_mention": "mention_name",
    "custom_emoji": "custom_emoji",
    "date_time": "unknown",                  # экспорт пишет форматированную дату как unknown
}

# Вложение: поле Bot API → media_type экспорта. Порядок важен: при анимации Bot API для
# совместимости заполняет ещё и document, при «живом фото» — ещё и photo.
# Фото и обычный файл в экспорте идут без media_type; разбор экспорта называет их photo и file.
_MEDIA = (
    ("sticker", "sticker"),
    ("video_note", "video_message"),
    ("voice", "voice_message"),
    ("animation", "animation"),
    ("video", "video_file"),
    ("audio", "audio_file"),
    ("photo", "photo"),
    ("live_photo", "photo"),
    ("document", "file"),
)

# Служебное сообщение: поле Bot API → action экспорта. Только то, что бывает в личном чате.
# Помеченное «не сверено» в экспорте найдено не было — имя взято ближайшее по смыслу.
_SERVICE = (
    ("pinned_message", "pin_message"),
    ("message_auto_delete_timer_changed", "set_messages_ttl"),
    ("successful_payment", "send_payment"),
    ("refunded_payment", "refunded_payment"),
    ("proximity_alert_triggered", "proximity_reached"),
    ("web_app_data", "send_webview_data"),
    ("connected_website", "allow_sending_messages"),
    ("passport_data", "send_passport_values"),
    ("users_shared", "requested_peer"),
    ("chat_shared", "requested_peer"),
    ("chat_background_set", "set_chat_wallpaper"),
    ("gift", "send_star_gift"),
    ("unique_gift", "send_star_gift"),          # не сверено
    ("gift_upgrade_sent", "send_star_gift"),    # не сверено
    ("checklist_tasks_done", "todo_completions"),
    ("checklist_tasks_added", "todo_append_tasks"),
    ("poll_option_added", "poll_append_answer"),
    ("poll_option_deleted", "poll_delete_answer"),
    ("paid_message_price_changed", "paid_messages_price_change"),
    ("managed_bot_created", "managed_bot_created"),
)


@dataclass
class Normalized:
    chat: ChatRecord
    record: MessageRecord
    # Сообщение отправлено не человеком с клавиатуры: через встроенного бота (via_bot)
    # или ботом от имени аккаунта в бизнес-режиме (sender_business_bot).
    via_bot: bool
    by_business_bot: bool


def clean(value: Any) -> str | None:
    """Строка, которую примет Postgres: без нулевых байтов и без половинок суррогатных пар."""
    if not isinstance(value, str):
        return None
    if "\x00" in value:
        value = value.replace("\x00", "")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        value = value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return value


def _dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value if isinstance(value, int) else None


def _id(value: Any, what: str) -> int:
    number = _int(value)
    if number is None or not 0 < number <= MAX_ID:
        raise NormalizeError(f"поле {what}: нужен положительный идентификатор")
    return number


def _time(value: Any, what: str, *, required: bool) -> datetime | None:
    if isinstance(value, float) and math.isfinite(value):
        value = int(value)
    number = _int(value)
    if number is None:
        if required or value is not None:
            raise NormalizeError(f"поле {what}: нужно время в секундах Unix")
        return None
    if not 0 < number < MAX_UNIXTIME:
        raise NormalizeError(f"поле {what}: время вне допустимых пределов")
    return datetime.fromtimestamp(number, tz=timezone.utc)


def user_name(user: dict[str, Any] | None) -> str | None:
    """Имя так, как его пишет экспорт: «имя фамилия»; у удалённого аккаунта имени нет."""
    if not user:
        return None
    parts = [clean(user.get("first_name")) or "", clean(user.get("last_name")) or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or None


def text_entities(text: str | None, entities: Any) -> list[dict[str, Any]] | None:
    """Разметка Bot API → фрагменты `text_entities` экспорта без «plain».

    Смещения Bot API — в кодовых единицах UTF-16. Экспорт режет текст на непересекающиеся
    куски: фрагмент, который начинается внутри уже взятого, пропускается (так вложенная
    разметка превращается в один внешний фрагмент). Здесь то же правило.
    """
    if not isinstance(text, str) or not text or not isinstance(entities, list):
        return None
    raw = text.encode("utf-16-le", "surrogatepass")
    size = len(raw) // 2
    out: list[dict[str, Any]] = []
    offset = 0
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        start, length = _int(entity.get("offset")), _int(entity.get("length"))
        if start is None or length is None or start < offset or length <= 0 or start + length > size:
            continue
        try:
            part = raw[2 * start: 2 * (start + length)].decode("utf-16-le")
        except UnicodeDecodeError:
            continue  # граница попала в середину суррогатной пары
        kind = entity.get("type")
        item: dict[str, Any] = {
            "type": ENTITY_TYPES.get(kind, "unknown") if isinstance(kind, str) else "unknown",
            "text": clean(part) or "",
        }
        if kind == "text_link":
            item["href"] = clean(entity.get("url")) or ""
        elif kind == "text_mention":
            user_id = _int((_dict(entity.get("user")) or {}).get("id"))
            if user_id is not None and 0 < user_id <= MAX_ID:
                item["user_id"] = user_id
        elif kind == "custom_emoji":
            item["document_id"] = clean(entity.get("custom_emoji_id")) or ""
        elif kind == "pre":
            item["language"] = clean(entity.get("language")) or ""
        elif kind in ("blockquote", "expandable_blockquote"):
            item["collapsed"] = kind == "expandable_blockquote"
        out.append(item)
        offset = start + length
    return out or None


def forwarded_from(origin: Any) -> str | None:
    """Источник пересылки одной строкой — как `forwarded_from` в экспорте."""
    origin = _dict(origin)
    if origin is None:
        return None
    kind = origin.get("type")
    if kind == "user":
        return user_name(_dict(origin.get("sender_user")))
    if kind == "hidden_user":
        return (clean(origin.get("sender_user_name")) or "").strip() or None
    if kind == "chat":
        return (clean((_dict(origin.get("sender_chat")) or {}).get("title")) or "").strip() or None
    if kind == "channel":
        return (clean((_dict(origin.get("chat")) or {}).get("title")) or "").strip() or None
    return None


def chat_record(chat: Any, *, partner: dict[str, Any] | None = None) -> ChatRecord:
    """Личный чат Bot API → чат архива. `partner` — пользователь-собеседник, если он известен
    из самого сообщения: только по нему видно, что собеседник — бот."""
    chat = _dict(chat)
    if chat is None:
        raise NormalizeError("поле chat: нужен объект чата")
    if chat.get("type") != "private":
        raise NormalizeError("принимаются только личные чаты: бизнес-режим работает один на один")
    tg_id = _id(chat.get("id"), "chat.id")
    is_bot = partner.get("is_bot") if partner and isinstance(partner.get("is_bot"), bool) else None
    username = (clean(chat.get("username")) or "").lstrip("@") or None
    return ChatRecord(
        peer_class="user", tg_id=tg_id,
        type="bot_chat" if is_bot else "personal_chat",
        name=user_name(chat), username=username, is_bot=is_bot,
    )


def seen_by(chat: ChatRecord, owner_tg_id: int) -> ChatRecord:
    """Чат владельца с самим собой в экспорте называется saved_messages."""
    if chat.tg_id == owner_tg_id:
        return replace(chat, type="saved_messages", is_bot=False)
    return chat


def _media_type(message: dict[str, Any]) -> str | None:
    for key, name in _MEDIA:
        if message.get(key):
            return name
    return None


def _voice_ref(message: dict[str, Any]) -> dict[str, Any]:
    """Сведения о вложении для скачивания через getFile своего бота: голосовое и «кружок» —
    расшифровка (voice/), фото и документ — разбор (media/). У остальных вложений — ничего."""
    for key in ("voice", "video_note"):
        media = _dict(message.get(key))
        if media is None:
            continue
        file_id = media.get("file_id")
        duration = _int(media.get("duration"))
        return {
            "media_ref": file_id if isinstance(file_id, str) and 0 < len(file_id) <= 300 else None,
            "media_duration": duration if duration is not None and 0 <= duration < 10**7 else None,
        }
    photo = message.get("photo")
    if isinstance(photo, list):
        # самый большой размер: Bot API присылает их по возрастанию
        sizes = [p for p in photo if isinstance(p, dict) and isinstance(p.get("file_id"), str)]
        if sizes:
            best = sizes[-1]
            return {"media_ref": best["file_id"] if 0 < len(best["file_id"]) <= 300 else None,
                    "media_mime": "image/jpeg", "media_size": _size(best.get("file_size"))}
    document = _dict(message.get("document"))
    if document is not None:
        file_id, name, mime = document.get("file_id"), document.get("file_name"), document.get("mime_type")
        return {
            "media_ref": file_id if isinstance(file_id, str) and 0 < len(file_id) <= 300 else None,
            "media_name": name[:255] if isinstance(name, str) and name else None,
            "media_mime": mime[:100] if isinstance(mime, str) and mime else None,
            "media_size": _size(document.get("file_size")),
        }
    return {}


def _size(value: Any) -> int | None:
    size = _int(value)
    return size if size is not None and 0 <= size < 10**13 else None


def _service_action(message: dict[str, Any]) -> str | None:
    allowed = _dict(message.get("write_access_allowed"))
    if allowed is not None:
        if allowed.get("from_attachment_menu") is True:
            return "attach_menu_bot_allowed"
        if allowed.get("from_request") is True:
            return "web_app_bot_allowed"
        return "allow_sending_messages"
    for key, action in _SERVICE:
        if message.get(key) not in (None, False):
            return action
    return None


def normalize_message(message: Any) -> Normalized:
    """Сообщение Bot API из личного чата → чат и запись архива.

    Бросает NormalizeError, если объект не разобрать, и SkipMessage, если сообщение
    в архив не идёт. Направление (исходящее ли) здесь не определяется: его знает тот, кому
    известен владелец.
    """
    message = _dict(message)
    if message is None:
        raise NormalizeError("поле message: нужен объект сообщения")

    sender = _dict(message.get("from"))
    sender_id = _id(sender.get("id"), "from.id") if sender is not None else None
    raw_chat = _dict(message.get("chat"))
    partner = sender if sender is not None and raw_chat is not None and sender_id == _int(raw_chat.get("id")) else None
    chat = chat_record(raw_chat, partner=partner)

    message_id = _int(message.get("message_id"))
    if message_id == 0:
        # Bot API: 0 — сообщение ещё не отправлено (отложено сервером) или живёт только на экране.
        raise SkipMessage("у сообщения ещё нет идентификатора")
    message_id = _id(message.get("message_id"), "message_id")
    sent_at = _time(message.get("date"), "date", required=True)
    edited_at = _time(message.get("edit_date"), "edit_date", required=False)

    if isinstance(message.get("text"), str):
        raw_text, raw_entities = message["text"], message.get("entities")
    elif isinstance(message.get("caption"), str):
        raw_text, raw_entities = message["caption"], message.get("caption_entities")
    else:
        raw_text, raw_entities = "", None
    if len(raw_text) > MAX_TEXT_CHARS:
        raise NormalizeError("текст сообщения длиннее, чем бывает в Telegram")

    action = _service_action(message)
    reply = _dict(message.get("reply_to_message"))
    reply_id = _int(reply.get("message_id")) if reply is not None else None
    if reply_id is not None and not 0 < reply_id <= MAX_ID:
        reply_id = None

    record = MessageRecord(
        tg_message_id=message_id,
        sent_at=sent_at,
        kind="service" if action else "message",
        sender_class="user" if sender_id is not None else None,
        sender_tg_id=sender_id,
        sender_name=user_name(sender),
        text=clean(raw_text) or "",
        entities=text_entities(raw_text, raw_entities),
        # Ответ на сообщение из другого чата (external_reply) не записывается: в архиве ссылка
        # «ответ на» понимается внутри того же чата.
        reply_to_tg_id=reply_id,
        forwarded_from=forwarded_from(message.get("forward_origin")),
        edited_at=edited_at,
        media_type=None if action else _media_type(message),
        media_path=None,
        service_action=action,
        **({} if action else _voice_ref(message)),
    )
    by_business_bot = _dict(message.get("sender_business_bot")) is not None
    return Normalized(
        chat=chat, record=record,
        via_bot=_dict(message.get("via_bot")) is not None or by_business_bot,
        by_business_bot=by_business_bot,
    )
