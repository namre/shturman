"""Потоковое чтение экспорта Telegram Desktop (result.json).

Поддерживаются оба вида файла: полный экспорт аккаунта (чаты лежат в `chats.list`
и `left_chats.list`) и экспорт одного чата (поля чата лежат в корне).

Файл читается событиями, целиком в память не загружается: полный экспорт может
весить сотни мегабайт. Схема: https://core.telegram.org/import-export
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, BinaryIO, Iterator

import ijson

from .records import MessageRecord

# Типы чатов экспорта → класс сущности Telegram.
_CHAT_CLASS = {
    "saved_messages": "user",
    "personal_chat": "user",
    "bot_chat": "user",
    "private_group": "chat",
    "private_supergroup": "channel",
    "public_supergroup": "channel",
    "private_channel": "channel",
    "public_channel": "channel",
}

_CHAT_BASES = ("", "chats.list.item", "left_chats.list.item")
_PEER_REF = re.compile(r"^(user|chat|channel)(\d+)$")
_MEDIA_KEYS = ("photo", "file")
_NOT_INCLUDED = "(File not included"


class ExportFormatError(ValueError):
    """Файл не похож на экспорт Telegram Desktop в формате JSON."""


@dataclass
class ExportChat:
    tg_id: int
    type: str
    name: str | None

    @property
    def peer_class(self) -> str:
        try:
            return _CHAT_CLASS[self.type]
        except KeyError:
            raise ExportFormatError(f"неизвестный тип чата в экспорте: {self.type!r}") from None


# Запись сообщения общая для всех источников; прежнее имя оставлено для читаемости разбора.
ExportMessage = MessageRecord


@dataclass
class ExportOwner:
    tg_user_id: int
    name: str | None


@dataclass
class _ChatState:
    base: str
    fields: dict[str, Any] = field(default_factory=dict)
    announced: bool = False

    def chat(self) -> ExportChat:
        if "id" not in self.fields or "type" not in self.fields:
            raise ExportFormatError(
                "в экспорте сообщения идут раньше описания чата — такой файл не поддерживается"
            )
        return ExportChat(
            tg_id=int(self.fields["id"]),
            type=str(self.fields["type"]),
            name=self.fields.get("name"),
        )


def _ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _peer_ref(value: Any) -> tuple[str | None, int | None]:
    if not isinstance(value, str):
        return None, None
    m = _PEER_REF.match(value)
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def _flatten_text(value: Any) -> str:
    """Текст в экспорте — строка либо список из строк и фрагментов с разметкой."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


def _entities(raw: dict[str, Any]) -> list[dict[str, Any]] | None:
    ents = raw.get("text_entities")
    if not isinstance(ents, list):
        return None
    marked = [e for e in ents if isinstance(e, dict) and e.get("type") not in (None, "plain")]
    return marked or None


def _media(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    media_type = raw.get("media_type")
    path = None
    for key in _MEDIA_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value:
            if media_type is None:
                media_type = "photo" if key == "photo" else "file"
            if not value.startswith(_NOT_INCLUDED):
                path = value
            break
    return media_type, path


def parse_message(raw: dict[str, Any]) -> ExportMessage | None:
    """Превращает сообщение экспорта в запись архива. None — запись без идентификатора или времени."""
    if "id" not in raw:
        return None
    sent_at = _ts(raw.get("date_unixtime"))
    if sent_at is None:
        return None

    kind = "service" if raw.get("type") == "service" else "message"
    if kind == "service":
        sender_class, sender_id = _peer_ref(raw.get("actor_id"))
        sender_name = raw.get("actor")
    else:
        sender_class, sender_id = _peer_ref(raw.get("from_id"))
        sender_name = raw.get("from")

    media_type, media_path = _media(raw)
    reply_to = raw.get("reply_to_message_id")

    return ExportMessage(
        tg_message_id=int(raw["id"]),
        sent_at=sent_at,
        kind=kind,
        sender_class=sender_class,
        sender_tg_id=sender_id,
        sender_name=sender_name if isinstance(sender_name, str) else None,
        text=_flatten_text(raw.get("text")),
        entities=_entities(raw),
        reply_to_tg_id=int(reply_to) if isinstance(reply_to, int) else None,
        forwarded_from=raw.get("forwarded_from") if isinstance(raw.get("forwarded_from"), str) else None,
        edited_at=_ts(raw.get("edited_unixtime")),
        media_type=media_type if isinstance(media_type, str) else None,
        media_path=media_path,
        service_action=raw.get("action") if kind == "service" else None,
    )


def iter_export(fp: BinaryIO) -> Iterator[tuple[str, Any, Any]]:
    """Читает экспорт потоком.

    Выдаёт кортежи:
      ("owner", ExportOwner, None)        — владелец экспорта (только в полном экспорте);
      ("chat", ExportChat, None)          — начало чата, до его сообщений;
      ("message", ExportChat, ExportMessage).
    """
    state: _ChatState | None = None
    builder: ijson.ObjectBuilder | None = None
    builder_prefix: str | None = None
    builder_kind: str | None = None
    seen_anything = False

    for prefix, event, value in ijson.parse(fp, use_float=True):
        if builder is not None:
            builder.event(event, value)
            if event == "end_map" and prefix == builder_prefix:
                obj = builder.value
                kind, builder, builder_prefix = builder_kind, None, None
                if kind == "owner":
                    if isinstance(obj.get("user_id"), int):
                        name = " ".join(
                            p for p in (obj.get("first_name"), obj.get("last_name")) if p
                        ).strip()
                        yield "owner", ExportOwner(obj["user_id"], name or None), None
                else:
                    assert state is not None
                    chat = state.chat()
                    if not state.announced:
                        state.announced = True
                        yield "chat", chat, None
                    msg = parse_message(obj)
                    if msg is not None:
                        yield "message", chat, msg
            continue

        if event == "start_map":
            if prefix == "personal_information":
                builder, builder_prefix, builder_kind = ijson.ObjectBuilder(), prefix, "owner"
                builder.event(event, value)
                continue
            if prefix in _CHAT_BASES:
                state = _ChatState(base=prefix)
                continue
            if state is not None and prefix == _join(state.base, "messages.item"):
                seen_anything = True
                builder, builder_prefix, builder_kind = ijson.ObjectBuilder(), prefix, "message"
                builder.event(event, value)
                continue

        if state is not None:
            for key in ("name", "type", "id"):
                if prefix == _join(state.base, key) and event in ("string", "number", "integer", "null"):
                    state.fields[key] = value
                    seen_anything = True
            if event == "end_map" and prefix == state.base:
                # Чат закончился. Пустой чат с известным заголовком тоже объявляем.
                if not state.announced and "id" in state.fields and "type" in state.fields:
                    yield "chat", state.chat(), None
                state = None

    if not seen_anything:
        raise ExportFormatError(
            "в файле не найдено ни одного чата — нужен result.json из экспорта Telegram Desktop в формате JSON"
        )


def _join(base: str, key: str) -> str:
    return f"{base}.{key}" if base else key
