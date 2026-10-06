"""Обработчики Telegram, которые плагин добавляет к боту Hermes.

Hermes подключает их раньше собственных, а Telegram-библиотека отдаёт сообщение первому
подошедшему обработчику в группе. Поэтому каждый обработчик здесь ограничен узким условием:

  1. Привязка владельца — только пока в мастере открыто окно привязки.
  2. Защита бизнес-режима — пока плагин бизнес-режима не загружен: без него ядро Hermes
     приняло бы сообщения собеседников владельца за его команды (issue hermes-agent #127430).
  3. Наблюдение за подключением бизнес-режима — отдельная группа, сообщений не перехватывает.
"""

from __future__ import annotations

import logging
import time

from shturman_core.pairing import Pairing
from shturman_core.state import Store

logger = logging.getLogger("shturman.telegram")

BUSINESS_PLUGIN = "telegram-business"
OBSERVER_GROUP = 73   # любая группа, где нет обработчиков ядра (0 и 99) и плагина бизнес-режима (-1)

REPLIES = {
    "accepted": "Принято. Вернитесь в мастер настройки и подтвердите там, что это вы.",
    "wrong_code": "Код не подошёл. Сверьте его с тем, что показывает мастер настройки.",
    "hint": "Идёт настройка. Чтобы привязать бота, нажмите «Открыть бота» в мастере настройки "
            "или отправьте сюда код из мастера.",
    "expired": "Время привязки вышло. Начните привязку в мастере настройки заново.",
    "cancelled": "Слишком много неверных кодов. Начните привязку в мастере настройки заново.",
}

_ACTIVE_CACHE_SECONDS = 5.0
_active_cache: tuple[float, bool] = (0.0, False)


def reply_for(result: str, had_candidate: bool) -> str | None:
    if result == "accepted":
        return REPLIES["accepted"]
    if result == "wrong":
        return REPLIES["wrong_code"] if had_candidate else REPLIES["hint"]
    return REPLIES.get(result)


def business_plugin_active() -> bool:
    """Загружен ли плагин бизнес-режима на самом деле — включён и поднялся без ошибки.

    Смотрим не в настройки, а в то, что загрузил Hermes: плагин, записанный включённым,
    мог не подняться. Любая неясность трактуется как «не загружен» — защита остаётся.
    """
    global _active_cache
    now = time.monotonic()
    if now - _active_cache[0] < _ACTIVE_CACHE_SECONDS:
        return _active_cache[1]
    active = False
    try:
        from hermes_cli.plugins import get_plugin_manager

        for plugin in get_plugin_manager().list_plugins():
            if plugin.get("name") == BUSINESS_PLUGIN and plugin.get("enabled") and not plugin.get("error"):
                active = True
                break
    except Exception:
        active = False
    _active_cache = (now, active)
    return active


def wire(application, adapter) -> None:
    from telegram import Update
    from telegram.ext import MessageHandler, TypeHandler, filters

    store = Store()

    # --- 1. защита бизнес-режима: ставится первой и не зависит от остального ---
    # Обработчик есть всегда; пропускать ли сообщение дальше, решается на каждое сообщение.

    class _BusinessUnguarded(filters.UpdateFilter):
        def filter(self, update) -> bool:
            return not business_plugin_active()

    async def drop_business_message(update, context) -> None:
        return None

    application.add_handler(MessageHandler(
        filters.UpdateType.BUSINESS_MESSAGES & _BusinessUnguarded(), drop_business_message,
    ))

    # --- 2. привязка владельца ---
    try:
        _wire_pairing(application, store, MessageHandler, filters)
    except Exception:
        logger.warning("shturman: обработчик привязки не подключён", exc_info=True)

    # --- 3. наблюдение за подключением бизнес-режима ---

    async def observe(update, context) -> None:
        connection = getattr(update, "business_connection", None)
        if connection is None:
            return
        try:
            owner_id = store.read("owner").get("user_id")
            user_id = getattr(getattr(connection, "user", None), "id", None)
            if owner_id is None or user_id != owner_id:
                return               # бота мог подключить к себе кто угодно — чужие подключения не учитываем
            rights = getattr(connection, "rights", None)
            store.write("business", {
                "connected": bool(getattr(connection, "is_enabled", False)),
                "can_reply": bool(getattr(rights, "can_reply", False)) if rights is not None
                else bool(getattr(connection, "can_reply", False)),
                "user_id": user_id,
                "updated_at": int(time.time()),
            })
        except Exception:
            logger.warning("shturman: не удалось записать состояние бизнес-режима", exc_info=True)

    application.add_handler(TypeHandler(Update, observe), group=OBSERVER_GROUP)


def _wire_pairing(application, store, MessageHandler, filters) -> None:
    from shturman_core.pairing import extract_candidate

    pairing = Pairing(store)

    class _PairingOpen(filters.MessageFilter):
        def filter(self, message) -> bool:
            return pairing.is_pending()

    async def on_pairing_message(update, context) -> None:
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None or getattr(user, "is_bot", False):
            return
        text = message.text or ""
        result = pairing.try_bind(
            text, user_id=user.id, chat_id=chat.id,
            name=getattr(user, "full_name", "") or "", username=getattr(user, "username", "") or "",
        )
        if result == "accepted":
            logger.info("shturman: получено значение привязки, ждём подтверждения в мастере")
        reply = reply_for(result, extract_candidate(text) is not None)
        if reply:
            try:
                await message.reply_text(reply)
            except Exception:
                logger.warning("shturman: не удалось ответить при привязке", exc_info=True)

    application.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE & filters.TEXT & _PairingOpen(),
        on_pairing_message,
    ))
