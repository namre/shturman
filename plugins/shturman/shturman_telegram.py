"""Обработчики Telegram, которые плагин добавляет к боту Hermes.

Hermes подключает их раньше собственных, а Telegram-библиотека отдаёт сообщение первому
подошедшему обработчику в группе. Поэтому каждый обработчик здесь ограничен узким условием:

  1. Привязка владельца — только пока в мастере открыто окно привязки.
  2. Защита бизнес-режима — пока плагин бизнес-режима не загружен: без него ядро Hermes
     приняло бы сообщения собеседников владельца за его команды (issue hermes-agent #127430).
     Сообщения бизнес-режима без текста (фото, голосовые, файлы) не пропускаются к ядру никогда:
     плагин бизнес-режима их не берёт, а обработчик медиа в ядре подходит и к ним.
  3. Наблюдение за подключением бизнес-режима — отдельная группа, сообщений не перехватывает.
  4. Молчание до привязки — пока у бота нет владельца и не задан список разрешённых
     пользователей. Без этого стоковый Hermes отвечает любому написавшему английским
     сообщением с кодом привязки и командой для терминала.
  5. Запись бизнес-сообщений в архив — отдельная группа, которая идёт раньше всех остальных;
     обновление только кладётся в очередь пересылки в сервис переписки.
  6. Кнопки сервиса переписки (данные начинаются с «sh:») — нажатие владельца передаётся сервису.

Пункты 5 и 6 работают, только когда сервис переписки подключён; без него они ничего не делают.

Если у сервиса включён свой бот согласований (`shturman_core/own_bot.py`), пункты 5 и 6 сервису
ничего не передают: бизнес-поток и нажатия он получает через своего бота. На нажатие старой
карточки владелец получает короткое пояснение. Пункты 1–4 от этого не зависят: защита
бизнес-режима перехватывает сообщения собеседников так же, как без своего бота.

Порядок, от которого зависит запись в архив (python-telegram-bot 22.8, Hermes 0.21.5):
  * группы обработчиков выполняются по возрастанию номера, в каждой срабатывает первый подошедший;
  * защита (п. 2) стоит в группе 0 и ничего не останавливает: она просто занимает место
    обработчика ядра в своей группе, остальные группы обновление получают;
  * плагин бизнес-режима ставит перехват правки черновика в группу −1 и в этом случае
    останавливает дальнейшую обработку (ApplicationHandlerStop);
  * поэтому запись в архив стоит в группе −73: она выполняется до защиты и до плагина
    бизнес-режима и не зависит от того, кто из них сработает.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from shturman_core.pairing import Pairing
from shturman_core.state import Store
from shturman_core.textlimits import cut_utf16

logger = logging.getLogger("shturman.telegram")

BUSINESS_PLUGIN = "telegram-business"
OBSERVER_GROUP = 73   # любая группа, где нет обработчиков ядра (0 и 99) и плагина бизнес-режима (-1)
ARCHIVE_GROUP = -73   # раньше всех: запись в архив не должна зависеть от остальных обработчиков
SERVICE_BUTTON = r"^sh:"   # кнопки сервиса; префиксы ядра (ea: sc: cl: cp: mp: mm: mc: gt:) и bd: не задеты

REPLIES = {
    "accepted": "Принято. Вернитесь в мастер настройки и подтвердите там, что это вы.",
    "wrong_code": "Код не подошёл. Сверьте его с тем, что показывает мастер настройки.",
    "hint": "Идёт настройка. Чтобы привязать бота, нажмите «Открыть бота» в мастере настройки "
            "или отправьте сюда код из мастера.",
    "expired": "Время привязки вышло. Начните привязку в мастере настройки заново.",
    "cancelled": "Слишком много неверных кодов. Начните привязку в мастере настройки заново.",
    "unbound": "Этот бот ещё не настроен. Если вы его владелец, откройте мастер настройки "
               "и нажмите там «Открыть бота».",
    "service_down": "Сервис переписки недоступен, попробуйте позже",
    "button_refused": "Кнопка недоступна.",
    "own_bot": "Теперь согласования приходят в отдельного бота",
}

UNBOUND_REPLY_INTERVAL = 3600      # одному чату подсказку не чаще раза в час
_ALLOWLIST_ENV = (
    "TELEGRAM_ALLOWED_USERS", "TELEGRAM_ALLOW_ALL_USERS",
    "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS",
)
_unbound_replied: dict[int, float] = {}


def instance_unbound(store: Store) -> bool:
    """У бота нет владельца, и никто не задал список разрешённых пользователей вручную.

    Если список задан (например, экземпляр настраивали до появления мастера), не вмешиваемся:
    кому отвечать, решает Hermes.
    """
    if store.read("owner").get("chat_id"):
        return False
    return not any((os.environ.get(name) or "").strip() for name in _ALLOWLIST_ENV)


def should_reply_unbound(chat_id: int, now: float) -> bool:
    last = _unbound_replied.get(chat_id)
    if last is not None and now - last < UNBOUND_REPLY_INTERVAL:
        return False
    if len(_unbound_replied) > 1000:
        _unbound_replied.clear()
    _unbound_replied[chat_id] = now
    return True

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

    # Без текста — всегда: плагин бизнес-режима берёт только текстовые сообщения, а обработчик
    # медиа в ядре Hermes (plugins/platforms/telegram/adapter.py:2933-2937) подходит и к фото,
    # голосовым и файлам из бизнес-чатов.
    application.add_handler(MessageHandler(
        filters.UpdateType.BUSINESS_MESSAGES & (_BusinessUnguarded() | ~filters.TEXT), drop_business_message,
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

    # --- 5 и 6. сервис переписки ---
    try:
        _wire_service(application, store)
    except Exception:
        logger.warning("shturman: обработчики сервиса переписки не подключены", exc_info=True)


def _wire_service(application, store) -> None:
    """Запись бизнес-сообщений в архив и кнопки сервиса. Без подключённого сервиса ничего не делает."""
    from telegram.ext import (
        BusinessConnectionHandler, BusinessMessagesDeletedHandler, CallbackQueryHandler, MessageHandler, filters,
    )

    import shturman_bridge

    runtime = shturman_bridge.runtime()
    # Фабрику Hermes вызывает из connect() — цикл событий уже работает, фоновая работа стартует здесь.
    runtime.attach(application, store)

    async def archive(update, context) -> None:
        runtime.forward(update)        # только очередь: шлюз не ждёт сервис

    for handler in (
        BusinessConnectionHandler(archive),
        MessageHandler(filters.UpdateType.BUSINESS_MESSAGES, archive),
        BusinessMessagesDeletedHandler(archive),
    ):
        application.add_handler(handler, group=ARCHIVE_GROUP)

    async def answer(query, text: str | None) -> None:
        try:
            await query.answer(text=text or None)
        except Exception as exc:  # нажатие могло устареть
            logger.warning("shturman: не удалось ответить на нажатие кнопки (%s)", type(exc).__name__)

    async def on_service_button(update, context) -> None:
        query = update.callback_query
        if query is None or not isinstance(query.data, str):
            return
        owner = store.read("owner")
        presser = getattr(query.from_user, "id", None)
        message = query.message
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        if not owner.get("user_id") or presser != owner.get("user_id") \
                or (chat_id is not None and chat_id != owner.get("chat_id")):
            # Нажал не владелец (или не в управляющем чате): сервису это не передаётся.
            runtime.stats.bump("callbacks_refused")
            await answer(query, REPLIES["button_refused"])
            return
        if not runtime.ensure_started():
            await answer(query, REPLIES["service_down"])
            return
        if runtime.own_bot.active:
            # Карточка осталась с тех пор, когда согласования шли через этого бота: сервис нажатие
            # не примет. Не ошибка — пояснение; в сервис ничего не уходит.
            await answer(query, REPLIES["own_bot"])
            return
        try:
            out = await runtime.call("POST", "/api/callbacks/telegram",
                                     {"data": query.data, "from_user_id": presser},
                                     timeout=shturman_bridge.CALLBACK_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # сервис недоступен или отказал — кнопки остаются
            if runtime.own_bot.refused(exc):      # у сервиса включён свой бот: это не сбой
                await answer(query, REPLIES["own_bot"])
                return
            logger.warning("shturman: нажатие кнопки не передано сервису (%s)", type(exc).__name__)
            await answer(query, REPLIES["service_down"])
            return
        runtime.stats.bump("callbacks")
        text = out.get("answer")
        await answer(query, text[:200] if isinstance(text, str) else None)
        new_text = out.get("edit_text")
        remove = bool(out.get("remove_buttons"))
        try:
            if isinstance(new_text, str) and new_text.strip():
                # Обычным текстом: в карточке есть чужой текст. Кнопки при правке текста Telegram
                # снимает сам, поэтому «оставить» значит передать их заново.
                await query.edit_message_text(
                    text=cut_utf16(new_text), parse_mode=None,
                    reply_markup=None if remove else getattr(message, "reply_markup", None))
            elif remove:
                await query.edit_message_reply_markup(reply_markup=None)
        except Exception as exc:  # «не изменено», сообщение удалено и т. п. — решение сервисом уже принято
            logger.warning("shturman: не удалось обновить сообщение с кнопками (%s)", type(exc).__name__)
        # Решение могло поставить задание боту (отправку) — забрать его сразу, а не через паузу.
        # Сервис ставит его чуть позже ответа, поэтому второй раз — через полсекунды.
        runtime.wake()
        asyncio.get_running_loop().call_later(0.6, runtime.wake)

    # block=False: обращение к сервису не задерживает разбор следующих обновлений.
    application.add_handler(CallbackQueryHandler(on_service_button, pattern=SERVICE_BUTTON, block=False))


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

    # --- молчание до привязки ---

    class _Unbound(filters.MessageFilter):
        def filter(self, message) -> bool:
            return instance_unbound(store)

    async def on_unbound_message(update, context) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        if should_reply_unbound(chat.id, time.time()):
            try:
                await message.reply_text(REPLIES["unbound"])
            except Exception:
                logger.warning("shturman: не удалось ответить непривязанному чату", exc_info=True)

    application.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE & _Unbound(), on_unbound_message,
    ))
