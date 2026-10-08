"""Фото и документы из переписки: что в них — коротким текстом в архиве (docs/media.md).

Вложение скачивается тем же путём, каким пришло сообщение (сессия аккаунта по номеру
сообщения, свой бот по file_id), либо берётся из загруженной выгрузки Telegram Desktop
(files.py). Из документа сервис сам достаёт текст (extract.py); фото и сканы показывает модели
картинками. Модель — та же, что у обработки (задание llm.structured): Hermes или своя модель
сервиса, в том числе по подписке ChatGPT. Итог дописывается в текст сообщения (миграция 0028).

Разбор включает владелец на странице настройки переписки: файлы уходят провайдеру модели.
"""

from __future__ import annotations

from typing import Any

MEDIA_TYPES = ("photo", "file")
ENABLED_KEY = "media.enabled"          # settings: {"enabled": bool, "by": ..., "at": ...}


async def enabled(conn: Any) -> bool:
    """Включён ли разбор вложений (решение владельца на странице настройки)."""
    value = await conn.fetchval("SELECT value FROM settings WHERE key = $1", ENABLED_KEY)
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except ValueError:
            return False
    return isinstance(value, dict) and value.get("enabled") is True
