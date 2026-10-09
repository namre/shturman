"""Журнал действий со страницы настройки: когда, что, итог.

Действия со страницы применяются сразу, без карточки в боте, поэтому каждое изменяющее действие
оставляет здесь запись, а последние записи видны на самой странице.

Записи двух родов. Важные — входы, смена ключей и бота, привязка владельца, вход в аккаунты
Telegram и выход из них, смена модели — хранятся отдельно от обычных: у каждого рода свой предел
(`KEEP_IMPORTANT`, `KEEP`). Тот, кто получил доступ к странице и хочет замести следы, тысячами
обычных действий (включить и выключить чат) вытеснит обычные записи, но не запись о своём входе
и не запись о смене ключей. На странице важные записи показаны отдельным списком — по той же
причине: из вида их потоком мелких действий тоже не убрать.

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
KEEP = 2000             # столько последних обычных записей хранится
KEEP_IMPORTANT = 2000   # и столько же важных — отдельно: обычные их не вытесняют
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
    "setup.scenario": "Выбран способ подключения",
    "setup.media": "Изменён разбор фото и документов",
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
    "tg.forget": "Аккаунт Telegram удалён из архива",
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
    "llm.chatgpt_start": "Начат вход через ChatGPT",
    "llm.chatgpt": "Подключена подписка ChatGPT",
    "llm.chatgpt_model": "Выбрана модель подписки ChatGPT",
    "llm.chatgpt_removed": "Подписка ChatGPT отключена",
    "memory.owner_block": "Изменены ваши заметки на странице памяти",
    "memory.page_accept": "Заведена страница памяти о человеке",
    "memory.page_reject": "Страницу памяти о человеке решено не заводить",
    "memory.commitment_accept": "Договорённость из переписки принята",
    "memory.commitment_reject": "Договорённость из переписки отклонена",
    "memory.project_create": "Заведён проект",
    "memory.project_chats": "Изменены чаты проекта",
    "memory.project_archive": "Проект перенесён в архив",
    "memory.project_accept": "Заведён предложенный проект",
    "memory.project_reject": "Предложенный проект решено не заводить",
    "memory.fact_retract": "Факт отмечен как неверный",
    "memory.owner_fact_accept": "Факт о вас принят в профиль",
    "memory.owner_fact_reject": "Факт о вас отклонён",
    "memory.profile_block": "Изменены ваши правила и указания ассистенту",
}


# Записи, по которым видно, кто и как получил доступ и что сменил в самом доступе. Хранятся
# отдельно от обычных и показываются на странице отдельным списком. Неудачные попытки входа сюда
# не входят намеренно: их может слать посторонний, и они не должны вытеснять остальное.
IMPORTANT = frozenset({
    "login.link", "login.code", "logout.all", "setup.scenario", "setup.media",
    "bot.token", "bot.token_removed", "bot.bind_link",
    "tg.keys", "tg.keys_removed", "tg.login", "tg.login_done", "tg.logout", "tg.forget",
    "llm.save", "llm.removed", "llm.chatgpt_start", "llm.chatgpt", "llm.chatgpt_removed",
    # заметки владельца ассистент читает как его собственные слова: их правку не вытеснить из вида
    "memory.owner_block", "memory.profile_block", "memory.owner_fact_accept",
})


# Как называется действие, если оно не состоялось (итог не «ok»).
NOT_DONE = {
    "bot.token": "Токен бота согласований не сохранён",
    "llm.save": "Своя модель сервиса не сохранена",
    "llm.chatgpt": "Подписка ChatGPT не подключена",
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


def _row(r: Any) -> dict[str, Any]:
    title = (NOT_DONE.get(r["action"]) if r["outcome"] != OK else None) or ACTIONS.get(r["action"], r["action"])
    return {"at": r["at"].isoformat(), "action": r["action"], "title": title, "outcome": r["outcome"],
            "detail": r["detail"]}


async def recent(conn: asyncpg.Connection, limit: int = 30) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT at, action, outcome, detail FROM setup_audit ORDER BY id DESC LIMIT $1", limit)
    return [_row(r) for r in rows]


async def recent_important(conn: asyncpg.Connection, limit: int = 10) -> list[dict[str, Any]]:
    """Последние важные записи — сколько бы обычных ни было сделано после них."""
    rows = await conn.fetch(
        """SELECT at, action, outcome, detail FROM setup_audit WHERE action = ANY($1::text[])
           ORDER BY id DESC LIMIT $2""", sorted(IMPORTANT), limit)
    return [_row(r) for r in rows]


async def trim(conn: asyncpg.Connection) -> None:
    """Оставляет последние `KEEP` обычных записей и последние `KEEP_IMPORTANT` важных — порознь."""
    important = sorted(IMPORTANT)
    for keep, mine in ((KEEP, False), (KEEP_IMPORTANT, True)):
        await conn.execute(
            """DELETE FROM setup_audit
               WHERE (action = ANY($1::text[])) = $2
                 AND id <= COALESCE((SELECT id FROM setup_audit WHERE (action = ANY($1::text[])) = $2
                                     ORDER BY id DESC OFFSET $3 LIMIT 1), 0)""",
            important, mine, keep)
