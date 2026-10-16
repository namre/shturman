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
import re
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
    "login.code": "Вход по коду из бота",
    "login.code_sent": "Запрошен код входа",
    "login.failed": "Неудачная попытка входа",
    "login.code_locked": "Вход по коду закрыт после неверных попыток",
    # Вход по коду из бота (code_login.py, Р-88): «Это не я» и включение и выключение. Ни кодов, ни адресов.
    "login.code_denied": "«Это не я» под кодом входа: ждущие коды погашены",
    "login.code_setting": "Изменён вход по коду из бота",
    "logout": "Выход",
    "logout.all": "Выход на всех устройствах",
    "setup.scenario": "Выбран способ подключения",
    "setup.media": "Изменён разбор фото и документов",
    "bot.token": "Токен бота согласований сохранён",
    "bot.token_removed": "Токен бота согласований убран",
    "bot.bind_link": "Выдана ссылка привязки владельца к боту",
    # Бот согласований в одно нажатие через бот-менеджер посредника (bot_setup.py, Р-77).
    "bot.managed_start": "Начато создание бота согласований в одно нажатие",
    "bot.managed_cancel": "Создание бота в одно нажатие отменено",
    "bot.managed": "Бот согласований в одно нажатие",
    "bot.bind_refused": "Ссылку привязки открыл не создатель бота — ссылки привязки отменены",
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
    # раскладки списка чатов (Р-82): «Включить все» / «Выключить все» в разделе — номер раздела и число
    "tg.bulk_on": "Включено чтение всех чатов раздела",
    "tg.bulk_off": "Выключено чтение всех чатов раздела",
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
    # Сферы (setup_page/spheres.py, Р-82). В подробностях — номера сфер, папок и число чатов, без названий.
    "sphere.create": "Заведена сфера",
    "sphere.update": "Изменена сфера",
    "sphere.delete": "Сфера удалена (снята с чатов и проектов)",
    "sphere.chats": "Изменены чаты сферы",
    "sphere.folder": "Сфера и папка Telegram",
    "sphere.project": "Изменена сфера проекта",
    # Mini App бота согласований (miniapp.py): вход по данным Telegram и решения кнопками.
    # В подробностях — вид и номер пункта и подпись кнопки; ни имён, ни текстов.
    "app.login": "Вход в приложение «Штурман» в Telegram",
    "app.login_failed": "Неудачная попытка входа в приложение в Telegram",
    "app.logout": "Выход из приложения в Telegram",
    "app.decide": "Решение в приложении в Telegram",
    # Экран «Чаты» приложения (Р-80): то же, что tg.sync_on / tg.sync_off / chat.exclude / chat.include
    # со страницы, но из приложения. В подробностях — номер чата в Telegram и глубина, без названий.
    "app.sync_on": "Из приложения в Telegram: включено чтение чата",
    "app.sync_off": "Из приложения в Telegram: выключено чтение чата",
    "app.chat_exclude": "Из приложения в Telegram: чат исключён из архива, сохранённое осталось",
    "app.chat_include": "Из приложения в Telegram: чат возвращён в архив",
    # Раскладки и сферы в приложении (Р-82): массово — только выключить; изменения сфер — с пометкой.
    "app.bulk_off": "Из приложения в Telegram: выключено чтение всех чатов раздела",
    "app.bulk_on": "Из приложения в Telegram: массовое включение чтения",
    "app.sphere_create": "Из приложения в Telegram: заведена сфера",
    "app.sphere_update": "Из приложения в Telegram: изменена сфера",
    "app.sphere_delete": "Из приложения в Telegram: сфера удалена (снята с чатов и проектов)",
    "app.sphere_chats": "Из приложения в Telegram: изменены чаты сферы",
    "app.sphere_folder": "Из приложения в Telegram: сфера и папка Telegram",
    # Уведомления, наблюдатель, повтор после сбоя (notify.py, Р-84). В подробностях — коды и числа:
    # «от кого» (all/people/spheres/off), сколько людей, номера сфер, номера правил; без имён и слов.
    "notify.settings": "Изменены уведомления о новых сообщениях",
    "app.notify": "Из приложения в Telegram: изменены уведомления о новых сообщениях",
    "watch.create": "Заведено правило наблюдателя",
    "watch.update": "Изменено правило наблюдателя",
    "watch.delete": "Удалено правило наблюдателя вместе с его журналом",
    "retry.failed": "Повтор того, что не получилось из-за сбоя модели",
    "commitments.recheck": "Перепроверка договорённостей по новым правилам (Р-96)",
    # Репетиция ответов на основном аккаунте (rehearsal/, Р-85). В подробностях — включена ли, сколько
    # людей, предел и пауза, откуда изменено; у оценки — номер карточки и код оценки. Ни имён, ни
    # номеров людей, ни текста сообщения и предложенного ответа.
    "rehearsal.settings": "Изменена репетиция ответов",
    "rehearsal.rate": "Оценка ответа в репетиции",
    # Свой вариант владельца после «Почти» (Р-89): номер карточки и число знаков, без текста.
    "rehearsal.variant": "Записан ваш вариант ответа в репетиции",
    # «Остановить всё» и «Продолжить» (halt.py, Р-87): откуда и что отменено — числами.
    "halt.stop": "Ассистент остановлен",
    "halt.stop_again": "Повторная остановка ассистента",
    "halt.resume_request": "Запрошено «Продолжить» — ждёт нажатия в боте",
    "halt.resume": "Ассистент продолжил работу — подтверждено в боте",
    # Ответ на вопрос ассистента из приложения (П1): номер задачи и число знаков, без текста.
    "app.answer": "Из приложения в Telegram: ответ на вопрос ассистента",
    # «Не сообщать об этом чате» под уведомлением в боте (Р-89): номер чата.
    "notify.mute": "Не сообщать об этом чате (кнопка в боте)",
    # Разбор входящих (triage/, Р-92): включение — только нажатием в боте; правила — числами, без имён и
    # номеров людей; «Не важно» — номер карточки и ступень (normal → lower и т. п.).
    "triage.enable_request": "Запрошено включение разбора входящих — ждёт нажатия в боте",
    "triage.enable": "Разбор входящих включён — подтверждено в боте",
    "triage.disable": "Разбор входящих выключен",
    "triage.settings": "Изменены правила разбора входящих",
    "triage.feedback": "«Не важно» под карточкой разбора входящих",
    # Экран «Договорённости» приложения (Р-94): номер договорённости и новая дата, без формулировок и имён.
    "app.commitment_done": "Из приложения в Telegram: договорённость отмечена выполненной",
    "app.commitment_move": "Из приложения в Telegram: срок договорённости перенесён",
    "app.commitment_wrong": "Из приложения в Telegram: договорённость убрана как записанная неверно",
    "app.commitment_reopen": "Из приложения в Telegram: договорённость возвращена в список",
    "app.commitment_hide": "Из приложения в Telegram: обещание скрыто из «В команде»",
    # «История» (Р-95): возврат изменения и отзыв разрешения. В подробностях — номер записи или разрешения,
    # вид и откуда; без имён, названий чатов и запросов.
    "history.revert": "«Вернуть как было» — изменение возвращено",
    "history.revert_request": "«Вернуть как было» — ждёт нажатия в боте (возврат расширяет)",
    "grant.revoke": "Отозвано разрешение",
    "app.history_revert": "Из приложения в Telegram: «Вернуть как было»",
    "app.revoke": "Из приложения в Telegram: отозвано разрешение",
    # «Ответы и доверие» (Р-95): что изменено — кодом (trusted_add, drafting_deny, group_on…) и числа.
    "replies.change": "Изменены настройки ответов и доверия",
    "app.replies": "Из приложения в Telegram: изменены настройки ответов и доверия (сужение)",
    "app.replies_request": "Из приложения в Telegram: просьба расширить ответы и доверие — ждёт нажатия в боте",
    # Утренняя сводка и обзор недели (Р-95): время и вкл/выкл, откуда.
    "summaries.schedule": "Изменено расписание сводок",
}


# Записи, по которым видно, кто и как получил доступ и что сменил в самом доступе. Хранятся
# отдельно от обычных и показываются на странице отдельным списком. Неудачные попытки входа сюда
# не входят намеренно: их может слать посторонний, и они не должны вытеснять остальное.
IMPORTANT = frozenset({
    "login.link", "login.code", "logout.all", "setup.scenario", "setup.media",
    # вход по коду: кто сказал «Это не я» и кто включал и выключал вход по коду — из вида не убрать
    "login.code_denied", "login.code_setting",
    # остановка ассистента и её снятие — главный выключатель владельца
    "halt.stop", "halt.stop_again", "halt.resume_request", "halt.resume",
    "bot.token", "bot.token_removed", "bot.bind_link", "bot.managed_start", "bot.bind_refused",
    "tg.keys", "tg.keys_removed", "tg.login", "tg.login_done", "tg.logout", "tg.forget",
    "llm.save", "llm.removed", "llm.chatgpt_start", "llm.chatgpt", "llm.chatgpt_removed",
    # заметки владельца ассистент читает как его собственные слова: их правку не вытеснить из вида
    "memory.owner_block", "memory.profile_block", "memory.owner_fact_accept",
    # вход в приложение и решения в нём — то же, что вход на страницу и нажатие в боте
    "app.login", "app.decide", "app.answer",
    # выбор чатов из приложения: войти в него может и держатель токена бота согласований (Р-80) —
    # потоком мелких действий эти записи из вида не убрать
    "app.sync_on", "app.sync_off", "app.chat_exclude", "app.chat_include", "app.bulk_off", "app.bulk_on",
    # сферы из приложения: в шагах 2–4 от них будет зависеть обработка и модель — из вида не убрать
    "app.sphere_create", "app.sphere_update", "app.sphere_delete", "app.sphere_chats", "app.sphere_folder",
    # что и о ком уходит в чат бота согласований; повтор снова отдаёт данные провайдеру модели
    "notify.settings", "app.notify", "retry.failed", "commitments.recheck",
    # чьи личные сообщения уходят модели ради репетиции ответов — из вида не убрать
    "rehearsal.settings",
    # разбор входящих отдаёт переписку провайдеру модели — его включение и выключение из вида не убрать
    "triage.enable_request", "triage.enable", "triage.disable", "triage.settings",
    # договорённости из приложения: войти в него может и держатель токена бота согласований (Р-80, Р-94) —
    # закрытые и перенесённые договорённости из вида не убрать
    "app.commitment_done", "app.commitment_move", "app.commitment_wrong", "app.commitment_reopen",
    "app.commitment_hide",
    # кто расширял доверие и возвращал изменения — в том числе из приложения (Р-95)
    "history.revert", "history.revert_request", "app.history_revert", "app.revoke",
    "replies.change", "app.replies", "app.replies_request",
})


# Как называется действие, если оно не состоялось (итог не «ok»).
NOT_DONE = {
    "app.decide": "Решение в приложении не принято: кнопки уже нет",
    "app.sync_on": "Из приложения в Telegram: чтение чата не включено",
    "app.bulk_on": "Из приложения в Telegram: массовое включение отклонено — только на странице",
    "app.notify": "Из приложения в Telegram: расширение уведомлений отклонено — только на странице",
    "app.commitment_done": "Из приложения в Telegram: договорённость не отмечена — она изменилась или её нет",
    "app.commitment_move": "Из приложения в Telegram: срок договорённости не перенесён",
    "app.commitment_wrong": "Из приложения в Telegram: договорённость не убрана — она изменилась или её нет",
    "app.commitment_reopen": "Из приложения в Telegram: договорённость не возвращена",
    "app.commitment_hide": "Из приложения в Telegram: обещание не скрыто — оно изменилось или его нет",
    "bot.token": "Токен бота согласований не сохранён",
    "bot.managed": "Бот согласований в одно нажатие не получен",
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


async def write_conn(conn: asyncpg.Connection, action: str, outcome: str = OK, detail: str = "") -> None:
    """То же, что `write`, но тем соединением, что уже есть, — в его транзакции (нажатия в боте:
    запись появляется вместе с самим действием или не появляется вовсе)."""
    if action not in ACTIONS:
        raise KeyError(f"неизвестный вид действия для журнала: {action}")
    await conn.execute("INSERT INTO setup_audit (action, outcome, detail) VALUES ($1, $2, $3)",
                       action, outcome, detail[:DETAIL_LIMIT])


# Коды причин в подробностях — словами (С5, Р-86). В базе код остаётся как есть (по нему ищет тот,
# кто обслуживает сервер); на странице владелец видит слова. Незнакомый код словом не заменить —
# тогда пишется, где искать подробности, а не сам код.
CODE_WORDS = {
    # вход через ChatGPT (executor/siwc.py)
    "wrong_state": "адрес из другой, устаревшей попытки входа",
    "no_attempt": "попытка входа уже закончилась",
    "not_callback": "это не тот адрес, что открывается после входа",
    "no_params": "в адресе нет нужных данных",
    "no_code": "в адресе нет кода входа",
    "code_rejected": "OpenAI не принял код входа",
    "denied": "OpenAI отказал в доступе",
    "expired": "срок истёк",
    "client_mismatch": "адрес от другого подключения",
    "no_models": "подписке не доступна ни одна модель",
    # проверка своей модели (apply.py)
    "blocked_address": "адрес во внутренней сети сервера — так нельзя",
    "blocked_base_url": "адрес во внутренней сети сервера — так нельзя",
    "bad_base_url": "адрес API записан неверно",
    "bad_address": "адрес записан неверно",
    "not_https": "адрес не https",
    "dns_failed": "сервер не нашёл такого адреса",
    "timeout": "провайдер не ответил вовремя",
    "bad_response": "по адресу ответил не API модели",
    "http_401": "провайдер не принял ключ",
    "http_402": "на счёте у провайдера нет средств",
    "http_403": "провайдер отказал ключу в доступе",
    "http_404": "провайдер не знает такой модели или адреса",
    "http_400": "провайдер отклонил пробный запрос",
    "http_422": "провайдер отклонил пробный запрос",
    "http_429": "провайдер ограничил запросы",
    "proxy_needs_socksio": "для прокси не хватает пакета на сервере",
    "bad_proxy_url": "адрес прокси на сервере записан неверно",
    "no_connection": "нет связи",
    "too_many_requests": "слишком много запросов, нужно подождать",
    # вход в приложение «Штурман» (initdata.py, miniapp.py)
    "replayed": "те же данные входа уже использованы",
    "not_owner": "приложение открыл не привязанный владелец",
    "bad_signature": "данные входа не подписаны вашим ботом",
    "malformed": "данные входа повреждены",
    "from_future": "время в данных входа впереди часов сервера",
    "no_bot": "бот согласований не настроен",
    "bot_conflict": "ботом пользуется ещё одна программа",
    # бот в одно нажатие (bot_setup.py)
    "wrong_bot": "не тот бот",
    "not_pending": "создание уже закончилось",
}
_CODE = re.compile(r"\b(?:http_\d{3}|[a-z][a-z0-9]*_[a-z0-9_]+|replayed|denied|expired|malformed|timeout)\b")


def human_detail(detail: str | None) -> str:
    """Подробности записи для страницы: коды причин заменены словами."""
    if not detail:
        return detail or ""
    return _CODE.sub(lambda m: CODE_WORDS.get(m.group(0), "подробности — в журнале сервиса"), detail)


def _row(r: Any) -> dict[str, Any]:
    title = (NOT_DONE.get(r["action"]) if r["outcome"] != OK else None) or ACTIONS.get(r["action"], r["action"])
    return {"at": r["at"].isoformat(), "action": r["action"], "title": title, "outcome": r["outcome"],
            "detail": human_detail(r["detail"])}


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
