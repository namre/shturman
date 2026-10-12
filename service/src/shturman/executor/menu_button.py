"""Кнопка «Штурман» в чате владельца с ботом согласований: открывает Mini App.

Сервис ставит её сам (`setChatMenuButton` с `chat_id` личного чата владельца) — у @BotFather
ничего настраивать не нужно, и кнопка есть только у владельца: остальным бот кнопку не меняет.

Когда кнопка стоит:
  * у сервиса свой бот и он знает, кто он (`Bot.identity`);
  * владелец привязан к этому боту (`binding.bound_owner`);
  * у страницы настройки есть внешний адрес https (`config.setup_external`). Mini App живёт на
    том же адресе: `<адрес страницы>/shturman-setup/app/`. Telegram открывает приложения только по
    https. Без внешнего адреса (режим без Hermes, вход только через туннель SSH) кнопки нет.

Что делается, когда что-то меняется (новый адрес страницы, другой владелец, владелец отвязан):
прежняя кнопка в прежнем чате возвращается к обычной (`MenuButtonDefault`), новая ставится в
новом. Что стоит сейчас, записано в `executor_state.menu_button` — повторно Telegram не
спрашиваем, пока ничего не изменилось. Сверка идёт при запуске бота, после привязки владельца и
раз в `CHECK_EVERY` секунд при опросе.

Сбой Telegram не останавливает бота: состояние не записывается, следующая сверка повторит.
"""

from __future__ import annotations

import logging
from typing import Any

from ..setup_page import APP_PATH
from . import binding
from .botapi import BotApi, BotApiError

logger = logging.getLogger("shturman.executor.bot")

TEXT = "Штурман"
STATE_KEY = "menu_button"
CHECK_EVERY = 600.0          # секунд между сверками при опросе

# Состояние для `GET /api/executor/status` и строки `miniapp` в ./ops/doctor.sh.
SET, NO_ADDRESS, NO_OWNER, FAILED = "set", "no_address", "no_owner", "failed"


def app_url(config: Any) -> str | None:
    """Адрес Mini App либо None, если у страницы настройки нет внешнего адреса https."""
    origin = config.setup_external
    return origin + APP_PATH if isinstance(origin, str) and origin.startswith("https://") else None


def button(url: str) -> dict[str, Any]:
    return {"type": "web_app", "text": TEXT, "web_app": {"url": url}}


async def sync(state: Any, api: BotApi, bot_id: int | None) -> str:
    """Приводит кнопку меню к нужному виду. Возвращает состояние (`SET`, `NO_ADDRESS`, `NO_OWNER`,
    `FAILED`)."""
    if bot_id is None:
        return FAILED
    async with state.pool.acquire() as conn:
        owner = await binding.bound_owner(conn, bot_id)
        saved = await binding.get_state(conn, STATE_KEY) or {}
    url = app_url(state.config)
    want = {"bot_id": bot_id, "chat_id": owner["chat_id"], "url": url} if owner and url else \
        {"bot_id": bot_id, "chat_id": None, "url": None}
    # Запись о кнопке другого бота не трогаем: его токена у нас больше нет.
    have = saved if saved.get("bot_id") == bot_id else {"bot_id": bot_id, "chat_id": None, "url": None}
    status = SET if want["url"] else (NO_OWNER if url else NO_ADDRESS)
    if have == want:
        return status
    try:
        if have.get("chat_id") and have["chat_id"] != want["chat_id"]:
            await api.set_chat_menu_button(int(have["chat_id"]), None)      # прежнему чату — обычная кнопка
        if want["url"]:
            await api.set_chat_menu_button(int(want["chat_id"]), button(want["url"]))
    except BotApiError as exc:
        logger.warning("бот согласований: кнопка меню не поставлена (%s)", exc)
        return FAILED
    async with state.pool.acquire() as conn:
        await binding.put_state(conn, STATE_KEY, want)
    if want["url"]:
        logger.info("бот согласований: кнопка «%s» в чате владельца открывает приложение", TEXT)
    return status
