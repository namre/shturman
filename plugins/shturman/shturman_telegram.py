"""Обработчики Telegram, которые плагин добавляет к боту Hermes.

Hermes подключает их раньше собственных, а Telegram-библиотека отдаёт сообщение первому
подошедшему обработчику в группе. Поэтому каждый обработчик здесь ограничен узким условием:

  1. Привязка владельца — только пока в мастере открыто окно привязки.
  2. Защита бизнес-режима — только пока не включён плагин бизнес-режима: без него ядро Hermes
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
    "bound": "Готово, бот привязан к вам. Вернитесь в мастер настройки — он продолжит сам.",
    "wrong_code": "Код не подошёл. Сверьте его с тем, что показывает мастер настройки.",
    "hint": "Идёт настройка. Чтобы привязать бота, нажмите «Открыть бота» в мастере настройки "
            "или отправьте сюда код из мастера.",
    "expired": "Время привязки вышло. Начните привязку в мастере настройки заново.",
    "cancelled": "Слишком много неверных кодов. Начните привязку в мастере настройки заново.",
}


def reply_for(result: str, had_candidate: bool) -> str | None:
    if result == "bound":
        return REPLIES["bound"]
    if result == "wrong":
        return REPLIES["wrong_code"] if had_candidate else REPLIES["hint"]
    return REPLIES.get(result)


def business_plugin_enabled() -> bool:
    try:
        from hermes_cli.config import load_config

        plugins = (load_config() or {}).get("plugins") or {}
        enabled = set(plugins.get("enabled") or [])
        disabled = set(plugins.get("disabled") or [])
        return BUSINESS_PLUGIN in enabled and BUSINESS_PLUGIN not in disabled
    except Exception:
        return False


def wire(application, adapter) -> None:
    from telegram import Update
    from telegram.ext import MessageHandler, TypeHandler, filters

    from shturman_core.pairing import extract_candidate

    store = Store()
    pairing = Pairing(store)

    # --- 1. привязка владельца ---

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
        if result == "bound":
            logger.info("shturman: владелец привязан")
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

    # --- 2. защита бизнес-режима ---

    if not business_plugin_enabled():
        async def drop_business_message(update, context) -> None:
            return None

        application.add_handler(MessageHandler(
            filters.UpdateType.BUSINESS_MESSAGES, drop_business_message,
        ))
        logger.info(
            "shturman: плагин бизнес-режима не включён — сообщения бизнес-режима до ядра не доходят")

    # --- 3. наблюдение за подключением ---

    async def observe(update, context) -> None:
        connection = getattr(update, "business_connection", None)
        if connection is None:
            return
        try:
            rights = getattr(connection, "rights", None)
            store.write("business", {
                "connected": bool(getattr(connection, "is_enabled", False)),
                "can_reply": bool(getattr(rights, "can_reply", False)) if rights is not None
                else bool(getattr(connection, "can_reply", False)),
                "user_id": getattr(getattr(connection, "user", None), "id", None),
                "updated_at": int(time.time()),
            })
        except Exception:
            logger.warning("shturman: не удалось записать состояние бизнес-режима", exc_info=True)

    application.add_handler(TypeHandler(Update, observe), group=OBSERVER_GROUP)
