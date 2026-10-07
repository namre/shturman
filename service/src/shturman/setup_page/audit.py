"""Журнал действий со страницы настройки: когда, что, итог.

Действия со страницы применяются сразу, без карточки в боте, поэтому каждое изменяющее действие
оставляет здесь запись, а последние записи видны на самой странице.

Чего в журнале нет и быть не должно: значений токенов, ключей, кодов, паролей, ссылок входа и
привязки, QR-ссылок, текстов переписки, названий чатов и имён людей. Поле `detail` собирает код
из чисел, ролей и признаков — значения из запроса в него не подставляются.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

logger = logging.getLogger("shturman.setup")

OK, REFUSED, FAILED = "ok", "refused", "failed"
KEEP = 2000           # столько последних записей хранится
DETAIL_LIMIT = 300

# Вид действия → как он называется на странице. Новое действие дописывается сюда же: запись
# с незнакомым видом не делается (тест проверяет, что все виды из маршрутов здесь есть).
ACTIONS = {
    "login.link": "Вход по ссылке",
    "login.code": "Вход по коду от бота",
    "login.code_sent": "Код входа отправлен в бота",
    "login.failed": "Неудачная попытка входа",
    "login.code_locked": "Вход по коду закрыт после неверных попыток",
    "logout": "Выход",
    "logout.all": "Выход на всех устройствах",
    "bot.token": "Токен бота согласований сохранён",
    "bot.token_removed": "Токен бота согласований убран",
    "bot.bind_link": "Выдана ссылка привязки владельца к боту",
    "tg.keys": "Ключи приложения Telegram сохранены",
    "tg.keys_removed": "Ключи приложения Telegram убраны",
    "tg.login": "Начат вход в аккаунт Telegram",
    "tg.login_done": "Аккаунт Telegram подключён",
    "tg.login_cancel": "Вход в аккаунт Telegram отменён",
    "tg.pause": "Аккаунт Telegram поставлен на паузу",
    "tg.resume": "Аккаунт Telegram снят с паузы",
    "tg.logout": "Выход из аккаунта Telegram",
    "tg.options": "Изменены настройки аккаунта Telegram",
    "tg.sync_on": "Включено чтение чатов",
    "tg.sync_off": "Выключено чтение чатов",
    "chat.exclude": "Чат исключён из архива",
    "chat.include": "Чат возвращён в архив",
    "chat.purge": "Сообщения исключённого чата стёрты",
    "import.upload": "Загружена выгрузка Telegram Desktop",
    "import.run": "Запущен импорт выгрузки",
    "import.delete": "Загруженная выгрузка удалена",
    "llm.save": "Своя модель сервиса сохранена",
    "llm.removed": "Своя модель сервиса убрана",
}


async def write(pool: asyncpg.Pool, action: str, outcome: str = OK, detail: str = "") -> None:
    """Пишет запись. Сбой журнала не отменяет уже сделанного действия, но виден в журнале сервиса."""
    if action not in ACTIONS:
        raise KeyError(f"неизвестный вид действия для журнала: {action}")
    try:
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO setup_audit (action, outcome, detail) VALUES ($1, $2, $3)",
                               action, outcome, detail[:DETAIL_LIMIT])
    except Exception as exc:  # noqa: BLE001
        logger.error("журнал страницы настройки: запись не сделана (%s)", type(exc).__name__)


async def recent(conn: asyncpg.Connection, limit: int = 30) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT at, action, outcome, detail FROM setup_audit ORDER BY id DESC LIMIT $1", limit)
    return [{"at": r["at"].isoformat(), "action": r["action"], "title": ACTIONS.get(r["action"], r["action"]),
             "outcome": r["outcome"], "detail": r["detail"]} for r in rows]


async def trim(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "DELETE FROM setup_audit WHERE id <= (SELECT max(id) FROM setup_audit) - $1", KEEP)
