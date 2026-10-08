"""Перевод объектов Telethon в записи архива.

Архив говорит на словаре экспорта Telegram Desktop (JSON): типы чатов, вид разметки
(`text_entities`), значения `media_type`, названия служебных действий. Сообщение, пришедшее
из сессии, должно лечь в ту же строку, что и то же сообщение из экспорта, и не создать ложной
«правки», поэтому текст берётся дословно (`message.message`), а всё остальное приводится к
значениям экспорта. Формат экспорта используется как факт (https://core.telegram.org/import-export);
код Telegram Desktop сюда не переносился.

Функции чистые: сети и клиента им не нужно. Имена отправителей и источников пересылки берутся
из сущностей, пришедших вместе с сообщением (`users`/`chats` ответа или обновления).

Разбор вложений и пересылок написан по образцу:
# Основано на j2h4u/mcp-telegram (MIT; форк sparfenyuk/mcp-telegram, MIT),
#   src/mcp_telegram/telethon_media.py, src/mcp_telegram/messages/telegram_adapter.py@1acce79
# Основано на chigwell/telegram-mcp (Apache-2.0), telegram_mcp/tools/messages.py@c4f9b23
# Преобразование коротких обновлений — по Telethon 1.45.0 (MIT), telethon/events/newmessage.py@7e0bf14
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from telethon.tl import types

from ..records import ChatRecord, MessageRecord

PeerKey = tuple[str, int]
Entities = Mapping[PeerKey, Any]

# Виды чатов, как их называет экспорт.
PERSONAL_TYPES = ("personal_chat",)
GROUP_TYPES = ("private_group", "private_supergroup")
CHAT_TYPES = (
    "saved_messages", "personal_chat", "bot_chat", "private_group",
    "private_supergroup", "public_supergroup", "private_channel", "public_channel",
)

# Разметка: класс Telethon → значение `type` во фрагменте `text_entities`.
_ENTITY_TYPES = {
    "MessageEntityUnknown": "unknown",
    "MessageEntityMention": "mention",
    "MessageEntityHashtag": "hashtag",
    "MessageEntityBotCommand": "bot_command",
    "MessageEntityUrl": "link",
    "MessageEntityEmail": "email",
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityCode": "code",
    "MessageEntityPre": "pre",
    "MessageEntityTextUrl": "text_link",
    "MessageEntityMentionName": "mention_name",
    "InputMessageEntityMentionName": "mention_name",
    "MessageEntityPhone": "phone",
    "MessageEntityCashtag": "cashtag",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strikethrough",
    "MessageEntityBankCard": "bank_card",
    "MessageEntitySpoiler": "spoiler",
    "MessageEntityCustomEmoji": "custom_emoji",
    "MessageEntityBlockquote": "blockquote",
}

# Служебные действия: класс Telethon → значение `action` в экспорте.
_ACTIONS = {
    "MessageActionChatCreate": "create_group",
    "MessageActionChatEditTitle": "edit_group_title",
    "MessageActionChatEditPhoto": "edit_group_photo",
    "MessageActionChatDeletePhoto": "delete_group_photo",
    "MessageActionChatAddUser": "invite_members",
    "MessageActionChatDeleteUser": "remove_members",
    "MessageActionChatJoinedByLink": "join_group_by_link",
    "MessageActionChatJoinedByRequest": "join_group_by_request",
    "MessageActionChannelCreate": "create_channel",
    "MessageActionChatMigrateTo": "migrate_to_supergroup",
    "MessageActionChannelMigrateFrom": "migrate_from_group",
    "MessageActionPinMessage": "pin_message",
    "MessageActionHistoryClear": "clear_history",
    "MessageActionGameScore": "score_in_game",
    "MessageActionPaymentSent": "send_payment",
    "MessageActionPhoneCall": "phone_call",
    "MessageActionScreenshotTaken": "take_screenshot",
    "MessageActionCustomAction": "custom_action",
    "MessageActionSecureValuesSent": "send_passport_values",
    "MessageActionContactSignUp": "joined_telegram",
    "MessageActionGeoProximityReached": "proximity_reached",
    "MessageActionGroupCall": "group_call",
    "MessageActionInviteToGroupCall": "invite_to_group_call",
    "MessageActionSetMessagesTTL": "set_messages_ttl",
    "MessageActionGroupCallScheduled": "group_call_scheduled",
    "MessageActionSetChatTheme": "edit_chat_theme",
    "MessageActionWebViewDataSent": "send_webview_data",
    "MessageActionWebViewDataSentMe": "send_webview_data",
    "MessageActionPaymentRefunded": "refunded_payment",
    "MessageActionStarGift": "send_star_gift",
    "MessageActionStarGiftUnique": "send_star_gift",
    "MessageActionPaidMessagesPrice": "paid_messages_price_change",
    "MessageActionGiftPremium": "send_premium_gift",
    "MessageActionTopicCreate": "topic_created",
    "MessageActionTopicEdit": "topic_edit",
    "MessageActionSuggestProfilePhoto": "suggest_profile_photo",
    "MessageActionRequestedPeer": "requested_peer",
    "MessageActionSetChatWallPaper": "set_chat_wallpaper",
    "MessageActionGiftCode": "gift_code_prize",
    "MessageActionGiveawayLaunch": "giveaway_launch",
    "MessageActionGiveawayResults": "giveaway_results",
    "MessageActionBoostApply": "boost_apply",
}

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _snake(name: str, prefix: str) -> str:
    if name.startswith(prefix):
        name = name[len(prefix):]
    return _CAMEL.sub("_", name).lower() or "unknown"


# --- собеседники ---

def peer_key(peer: Any) -> PeerKey | None:
    """(класс, идентификатор) по объекту Peer*; None — если это не собеседник."""
    if isinstance(peer, types.PeerUser):
        return "user", int(peer.user_id)
    if isinstance(peer, types.PeerChat):
        return "chat", int(peer.chat_id)
    if isinstance(peer, types.PeerChannel):
        return "channel", int(peer.channel_id)
    return None


def to_peer(key: PeerKey) -> Any:
    cls, tg_id = key
    if cls == "user":
        return types.PeerUser(tg_id)
    if cls == "chat":
        return types.PeerChat(tg_id)
    if cls == "channel":
        return types.PeerChannel(tg_id)
    raise ValueError(f"неизвестный класс собеседника: {cls!r}")


def entity_key(entity: Any) -> PeerKey | None:
    if isinstance(entity, (types.User, types.UserEmpty)):
        return "user", int(entity.id)
    if isinstance(entity, (types.Chat, types.ChatForbidden, types.ChatEmpty)):
        return "chat", int(entity.id)
    if isinstance(entity, (types.Channel, types.ChannelForbidden)):
        return "channel", int(entity.id)
    return None


def index_entities(*groups: Iterable[Any] | None) -> dict[PeerKey, Any]:
    """Собирает сущности ответа или обновления в словарь по (классу, идентификатору)."""
    out: dict[PeerKey, Any] = {}
    for group in groups:
        for entity in group or ():
            key = entity_key(entity)
            if key is not None:
                out[key] = entity
    return out


def display_name(entity: Any) -> str | None:
    """Имя, как его показывает Telegram: «Имя Фамилия» у людей, название у групп и каналов."""
    if isinstance(entity, types.User):
        name = " ".join(p for p in (entity.first_name, entity.last_name) if p).strip()
        return name or None
    title = getattr(entity, "title", None)
    return title or None if isinstance(title, str) else None


def username_of(entity: Any) -> str | None:
    username = getattr(entity, "username", None)
    if username:
        return username
    for item in getattr(entity, "usernames", None) or ():
        if getattr(item, "active", False) and getattr(item, "username", None):
            return item.username
    return None


def chat_record(entity: Any, *, self_id: int) -> ChatRecord | None:
    """Чат в словаре экспорта. None — сущность не годится в чат (пустая или неизвестная)."""
    if isinstance(entity, types.User):
        if entity.id == self_id or entity.is_self:
            kind = "saved_messages"
        elif entity.bot:
            kind = "bot_chat"
        else:
            kind = "personal_chat"
        return ChatRecord("user", int(entity.id), kind, display_name(entity),
                          username=username_of(entity), is_bot=bool(entity.bot))
    if isinstance(entity, (types.Chat, types.ChatForbidden)):
        return ChatRecord("chat", int(entity.id), "private_group", display_name(entity))
    if isinstance(entity, types.Channel):
        public = username_of(entity) is not None
        if entity.megagroup or entity.gigagroup:
            kind = "public_supergroup" if public else "private_supergroup"
        else:
            kind = "public_channel" if public else "private_channel"
        return ChatRecord("channel", int(entity.id), kind, display_name(entity),
                          username=username_of(entity))
    if isinstance(entity, types.ChannelForbidden):
        kind = "private_supergroup" if entity.megagroup else "private_channel"
        return ChatRecord("channel", int(entity.id), kind, display_name(entity))
    return None


# --- разметка ---

def _utf16_slice(units: bytes, offset: int, length: int) -> str | None:
    """Вырезает фрагмент по смещениям Telegram: они считаются в кодовых единицах UTF-16.

    Срез строки Python по этим числам ошибается на каждом символе вне основной плоскости
    (эмодзи занимает две единицы и один индекс). None — смещения разрезают символ пополам
    или выходят за текст: такую разметку лучше пропустить, чем сохранить неверной.
    """
    if offset < 0 or length <= 0:
        return None
    chunk = units[offset * 2:(offset + length) * 2]
    if len(chunk) != length * 2:
        return None
    try:
        return chunk.decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def text_entities(text: str, entities: Iterable[Any] | None) -> list[dict[str, Any]] | None:
    """Разметка в виде фрагментов `text_entities` экспорта — без фрагментов типа plain.

    Экспорт режет текст на куски подряд; архив хранит только размеченные куски (так же делает
    разбор экспорта). Куски экспорта не пересекаются: фрагмент, который начинается внутри уже
    взятого, пропускается — вложенная разметка превращается в один внешний фрагмент. То же
    правило у разбора сообщений бизнес-бота (`botapi_normalize.text_entities`).
    """
    if not text or not entities:
        return None
    units = text.encode("utf-16-le", errors="surrogatepass")
    out: list[dict[str, Any]] = []
    taken = 0
    for ent in entities:
        name = type(ent).__name__
        start, length = int(getattr(ent, "offset", -1)), int(getattr(ent, "length", 0))
        if start < taken:
            continue
        fragment = _utf16_slice(units, start, length)
        if fragment is None:
            continue
        taken = start + length
        item: dict[str, Any] = {"type": _ENTITY_TYPES.get(name) or _snake(name, "MessageEntity"),
                                "text": fragment}
        if name == "MessageEntityTextUrl" and ent.url:
            item["href"] = ent.url
        elif name in ("MessageEntityMentionName", "InputMessageEntityMentionName"):
            user_id = getattr(ent, "user_id", None)
            if isinstance(user_id, int):
                item["user_id"] = user_id
        elif name == "MessageEntityPre":
            item["language"] = ent.language or ""
        elif name == "MessageEntityCustomEmoji":
            item["document_id"] = str(ent.document_id)
        elif name == "MessageEntityBlockquote":
            item["collapsed"] = bool(ent.collapsed)
        out.append(item)
    return out or None


def addressing_entities(text: str, entities: Iterable[Any] | None) -> list[dict[str, Any]]:
    """Полная разметка TL, включая вложенные цитаты и код; смещения — UTF-16.

    Вид разметки экспорта выше намеренно остаётся прежним. Здесь нельзя сворачивать
    вложенность: упоминание внутри жирной цитаты не становится обращением.
    """
    units = text.encode("utf-16-le", errors="surrogatepass")
    out = []
    for ent in entities or ():
        offset, length = int(getattr(ent, "offset", -1)), int(getattr(ent, "length", 0))
        fragment = _utf16_slice(units, offset, length)
        if fragment is None:
            continue
        name = type(ent).__name__
        item = {"type": _ENTITY_TYPES.get(name) or _snake(name, "MessageEntity"),
                "offset": offset, "length": length, "text": fragment}
        if name in ("MessageEntityMentionName", "InputMessageEntityMentionName"):
            uid = getattr(ent, "user_id", None)
            if isinstance(uid, types.InputUser):
                uid = uid.user_id
            if isinstance(uid, int) and not isinstance(uid, bool) and uid > 0:
                item["user_id"] = uid
        if name == "MessageEntityTextUrl":
            item["href"] = ent.url
        out.append(item)
    return out


def topic_id(message: Any) -> int | None:
    """Корень темы форума; обычный ответ вне форума темой не является."""
    reply = getattr(message, "reply_to", None)
    if not isinstance(reply, types.MessageReplyHeader) or not reply.forum_topic \
            or reply.reply_to_peer_id is not None:
        return None
    value = reply.reply_to_top_id or reply.reply_to_msg_id
    return int(value) if value is not None and value > 0 else None


# --- вложения и служебные действия ---

def media_type(media: Any) -> str | None:
    """Вид вложения в словаре экспорта: photo, file, sticker, animation, video_message,
    video_file, voice_message, audio_file. Остальное (место, контакт, опрос, превью ссылки)
    экспорт описывает отдельными полями без `media_type` — для них None."""
    if media is None or isinstance(media, types.MessageMediaEmpty):
        return None
    if isinstance(media, types.MessageMediaPhoto):
        return "photo"
    if not isinstance(media, types.MessageMediaDocument):
        return None
    attrs = list(getattr(media.document, "attributes", None) or ())

    def first(cls: type) -> Any:
        return next((a for a in attrs if isinstance(a, cls)), None)

    audio = first(types.DocumentAttributeAudio)
    video = first(types.DocumentAttributeVideo)
    if media.voice or (audio is not None and audio.voice):
        return "voice_message"
    if first(types.DocumentAttributeSticker) is not None:
        return "sticker"
    if media.round or (video is not None and video.round_message):
        return "video_message"
    if first(types.DocumentAttributeAnimated) is not None:
        return "animation"
    if video is not None or media.video:
        return "video_file"
    if audio is not None:
        return "audio_file"
    return "file"


def media_duration(media: Any) -> int | None:
    """Длительность голосового или «кружка» в секундах — для очереди расшифровки (voice/)."""
    if not isinstance(media, types.MessageMediaDocument) or media.document is None:
        return None
    for attr in getattr(media.document, "attributes", None) or ():
        if isinstance(attr, (types.DocumentAttributeAudio, types.DocumentAttributeVideo)):
            value = getattr(attr, "duration", None)
            if isinstance(value, (int, float)) and value >= 0:
                return int(round(value))
    return None


def service_action(action: Any) -> str | None:
    if action is None or isinstance(action, types.MessageActionEmpty):
        return None
    name = type(action).__name__
    if name == "MessageActionBotAllowed":
        # Экспорт различает три случая одного действия.
        if action.attach_menu:
            return "attach_menu_bot_allowed"
        if action.from_request:
            return "web_app_bot_allowed"
        return "allow_sending_messages"
    return _ACTIONS.get(name) or _snake(name, "MessageAction")


# --- сообщения ---

def is_outgoing(message: Any, *, self_id: int) -> bool:
    """Исходящее ли сообщение — по флагу самого сообщения, а не по сравнению отправителя.

    В «Избранном» Telegram флаг не ставит; свои непересланные записи там считаются исходящими,
    как их показывают официальные клиенты."""
    if message.out:
        return True
    return peer_key(message.peer_id) == ("user", self_id) and message.fwd_from is None


def _sender(message: Any, chat: PeerKey | None, self_id: int) -> PeerKey | None:
    explicit = peer_key(message.from_id)
    if explicit is not None:
        return explicit
    if chat is None:
        return None
    if chat[0] == "user":
        return ("user", self_id) if message.out else chat
    if chat[0] == "channel":
        return chat  # запись канала или анонимный администратор группы
    return None


def _forwarded_from(message: Any, entities: Entities) -> str | None:
    fwd = message.fwd_from
    if fwd is None:
        return None
    if fwd.from_name:  # автор скрыл профиль: есть только имя
        return fwd.from_name
    key = peer_key(fwd.from_id)
    if key is not None and key in entities:
        return display_name(entities[key])
    return None


def _reply_to(message: Any) -> int | None:
    reply = message.reply_to
    if not isinstance(reply, types.MessageReplyHeader):
        return None  # ответ на историю и прочее
    if reply.reply_to_peer_id is not None:
        return None  # ответ на сообщение из другого чата: его номер к этому чату не относится
    value = reply.reply_to_msg_id
    return int(value) if value is not None else None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def message_record(message: Any, entities: Entities, *, self_id: int) -> MessageRecord | None:
    """Запись архива по сообщению Telethon (`Message` или `MessageService`).

    None — пустое сообщение (`MessageEmpty`) или объект без номера и времени.
    """
    if not isinstance(message, (types.Message, types.MessageService)):
        return None
    if message.id is None or message.date is None:
        return None
    chat = peer_key(message.peer_id)
    sender = _sender(message, chat, self_id)
    sender_entity = entities.get(sender) if sender is not None else None
    service = isinstance(message, types.MessageService)
    text = "" if service else (message.message or "")
    return MessageRecord(
        tg_message_id=int(message.id),
        sent_at=_aware(message.date),
        kind="service" if service else "message",
        sender_class=sender[0] if sender else None,
        sender_tg_id=sender[1] if sender else None,
        sender_name=display_name(sender_entity) if sender_entity is not None else None,
        text=text,
        entities=None if service else text_entities(text, message.entities),
        # У служебного сообщения «ответ» — это ссылка на предмет действия (что закрепили, за что
        # заплатили); экспорт и бизнес-бот пишут её отдельными полями, а не как ответ.
        reply_to_tg_id=None if service else _reply_to(message),
        forwarded_from=_forwarded_from(message, entities),
        # Скрытая правка (реакция, кнопки) правкой не считается — см. hidden_edit_at().
        edited_at=None if message.edit_hide else _aware(message.edit_date),
        media_type=None if service else media_type(message.media),
        media_path=None,  # файлы в этом срезе не скачиваются
        service_action=service_action(message.action) if service else None,
        telegram_entities=[] if service else addressing_entities(text, message.entities),
        topic_tg_id=None if service else topic_id(message),
        is_forwarded=message.fwd_from is not None,
        telegram_via_bot=getattr(message, "via_bot_id", None) is not None,
        telegram_sender_bot=bool(sender_entity.bot) if isinstance(sender_entity, types.User) else None,
        media_duration=None if service else media_duration(message.media),
    )


def hidden_edit_at(message: Any) -> datetime | None:
    """Время «скрытой правки»: Telegram меняет `edit_date` и ставит `edit_hide`, когда у
    сообщения сменились реакции или кнопки, а текст остался прежним. Отметку «изменено» такое
    сообщение не получает. Но если его текст при этом отличается от сохранённого, значит,
    настоящую правку сервис пропустил, и время скрытой правки — лучшее, что о ней известно;
    сравнение с архивом делает тот, кто пишет (`sync.promote_hidden_edits`)."""
    if isinstance(message, types.Message) and message.edit_hide:
        return _aware(message.edit_date)
    return None


def message_from_update(update: Any, *, self_id: int) -> Any:
    """Достаёт сообщение из обновления о новом или изменённом сообщении.

    Личные чаты и обычные группы Telegram часто присылает «коротким» обновлением без объекта
    сообщения — из него собирается обычный `Message`. None — в обновлении сообщения нет.
    """
    if isinstance(update, types.UpdateShortMessage):
        return types.Message(
            id=update.id, peer_id=types.PeerUser(update.user_id),
            from_id=types.PeerUser(self_id if update.out else update.user_id),
            date=update.date, message=update.message, out=update.out,
            mentioned=update.mentioned, media_unread=update.media_unread, silent=update.silent,
            fwd_from=update.fwd_from, via_bot_id=update.via_bot_id, reply_to=update.reply_to,
            entities=update.entities, ttl_period=update.ttl_period,
        )
    if isinstance(update, types.UpdateShortChatMessage):
        return types.Message(
            id=update.id, peer_id=types.PeerChat(update.chat_id),
            from_id=types.PeerUser(self_id if update.out else update.from_id),
            date=update.date, message=update.message, out=update.out,
            mentioned=update.mentioned, media_unread=update.media_unread, silent=update.silent,
            fwd_from=update.fwd_from, via_bot_id=update.via_bot_id, reply_to=update.reply_to,
            entities=update.entities, ttl_period=update.ttl_period,
        )
    message = getattr(update, "message", None)
    if isinstance(message, (types.Message, types.MessageService)):
        return message
    return None

