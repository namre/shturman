"""Запись сообщения в словаре архива — общая для всех источников.

Словарь значений (`media_type`, `service_action`, вид `entities`) — как в экспорте
Telegram Desktop: под него написана схема, и к нему приводят сообщения остальные источники.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

SOURCES = ("import", "business", "session")
PEER_CLASSES = ("user", "chat", "channel")


@dataclass
class MessageRecord:
    tg_message_id: int
    sent_at: datetime
    kind: str  # message | service
    sender_class: str | None
    sender_tg_id: int | None
    sender_name: str | None
    text: str
    entities: list[dict[str, Any]] | None
    reply_to_tg_id: int | None
    forwarded_from: str | None
    edited_at: datetime | None
    media_type: str | None
    media_path: str | None
    service_action: str | None
    # Отдельно от совместимой с экспортом разметки: исходные позиции и вложенность
    # нужны для безопасного распознавания адресата. None — источник этого не сообщил.
    telegram_entities: list[dict[str, Any]] | None = None
    topic_tg_id: int | None = None
    is_forwarded: bool | None = None
    telegram_via_bot: bool | None = None
    telegram_sender_bot: bool | None = None
    # Голосовые и «кружки»: длительность в секундах и file_id Bot API (только бизнес-режим) —
    # чтобы потом скачать файл и расшифровать (voice/).
    media_duration: int | None = None
    media_ref: str | None = None
    media_name: str | None = None      # имя файла документа
    media_mime: str | None = None
    media_size: int | None = None      # байт


@dataclass
class ChatRecord:
    """Чат с точки зрения аккаунта. `type` — как в экспорте: personal_chat, private_group и т.д."""

    peer_class: str
    tg_id: int
    type: str
    name: str | None
    username: str | None = None
    is_bot: bool | None = None

