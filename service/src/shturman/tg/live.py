"""Приём сообщений аккаунта в реальном времени: новые, изменённые, удалённые.

Обновления берутся «сырыми» (`events.Raw`), а не через `events.NewMessage`: тот пропускает
служебные сообщения (вступил, закрепил, звонок), а они в архиве нужны.

Правила:
  * из чата, который владелец не выбрал, или который исключён в архиве, не сохраняется ничего;
  * новый чат включается сам только настройкой аккаунта «брать новые личные чаты / группы»,
    и только если сервис этот чат раньше не видел и владелец его не выключал;
  * направление сообщения берётся из его флага `out`, а не из сравнения отправителя;
  * изменение без смены текста (реакция, кнопки) историю правок не пополняет — это решает
    общий слой записи; событие о правке тогда тоже не рассылается;
  * удаление в личном чате и обычной группе приходит без чата — сообщение ищется по номеру
    среди таких чатов аккаунта и помечается только при единственном совпадении;
  * событие `message.live` рассылается только отсюда и из шлюза отправки — загрузка истории
    событий не порождает;
  * прочитанным ничего не отмечается: таких запросов нет в коде и нет в перечне разрешённых.

# Основано на j2h4u/mcp-telegram (MIT; форк sparfenyuk/mcp-telegram, MIT),
#   src/mcp_telegram/event_handlers.py@1acce79 (состав обработчиков и порядок проверок)
# Основано на kawaiiDango/telegram-delete-logger (Apache-2.0), tg_delete_logger.py@fda6d47
# (поиск удалённого сообщения без указания чата; сам поиск — в store.mark_deleted_without_chat)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncpg
from telethon import events as tl_events
from telethon.tl import types

from .. import events as ev
from .. import store
from ..records import MessageRecord
from . import normalize, sync
from .normalize import PeerKey

logger = logging.getLogger("shturman.tg")

NEW_UPDATES = (types.UpdateNewMessage, types.UpdateNewChannelMessage,
               types.UpdateShortMessage, types.UpdateShortChatMessage)
EDIT_UPDATES = (types.UpdateEditMessage, types.UpdateEditChannelMessage)
DELETE_UPDATES = (types.UpdateDeleteMessages, types.UpdateDeleteChannelMessages)


class LiveIngest:
    def __init__(
        self, *, pool: asyncpg.Pool, events: ev.Events, account_id: int, self_id: int,
        wake: asyncio.Event,
    ) -> None:
        self.pool, self.events = pool, events
        self.account_id, self.self_id = account_id, self_id
        self.wake = wake
        self.index: dict[PeerKey, sync.ChatState] = {}
        self.auto_personal = False
        self.auto_groups = False
        self._seen_names: dict[PeerKey, tuple] = {}

    async def reload(self) -> None:
        """Перечитывает из базы выбранные чаты и настройки аккаунта."""
        async with self.pool.acquire() as conn:
            self.index = await sync.load_index(conn, self.account_id)
            row = await conn.fetchrow(
                "SELECT auto_personal, auto_groups FROM tg_sessions WHERE account_id = $1", self.account_id)
        self.auto_personal = bool(row and row["auto_personal"])
        self.auto_groups = bool(row and row["auto_groups"])

    def register(self, client: Any) -> None:
        client.add_event_handler(
            self.on_update, tl_events.Raw(types=[*NEW_UPDATES, *EDIT_UPDATES, *DELETE_UPDATES]))

    async def on_update(self, update: Any) -> None:
        """Обработчик Telethon. Ошибку не выпускает: одно сообщение не должно ронять приём."""
        try:
            if isinstance(update, NEW_UPDATES):
                await self._on_message(update, edited=False)
            elif isinstance(update, EDIT_UPDATES):
                await self._on_message(update, edited=True)
            elif isinstance(update, DELETE_UPDATES):
                await self._on_deleted(update)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Только вид ошибки и вид обновления: в тексте исключения может оказаться содержимое.
            logger.error("аккаунт %s: обновление %s не обработано (%s)",
                         self.account_id, type(update).__name__, type(exc).__name__)

    # --- новые и изменённые ---

    async def _on_message(self, update: Any, *, edited: bool) -> None:
        message = normalize.message_from_update(update, self_id=self.self_id)
        if message is None:
            return
        key = normalize.peer_key(message.peer_id)
        if key is None:
            return
        entities = normalize.index_entities((getattr(update, "_entities", None) or {}).values())
        state = self.index.get(key)
        if state is None:
            state = await self._auto_enable(key, entities)
        if state is None or not state.enabled or state.chat_id is None:
            return
        record = normalize.message_record(message, entities, self_id=self.self_id)
        if record is None:
            return
        await self.save(key, state.chat_id, record, entities,
                        outgoing=normalize.is_outgoing(message, self_id=self.self_id),
                        edited=edited, via_bot=bool(message.via_bot_id))

    async def save(
        self, key: PeerKey, chat_id: int, record: MessageRecord, entities: normalize.Entities, *,
        outgoing: bool, edited: bool, via_bot: bool,
    ) -> None:
        async with self.pool.acquire() as conn:
            await self._refresh_chat(conn, key, entities)
            result = await store.upsert_messages(
                conn, [(chat_id, record, outgoing)], source="session", owner_tg_id=self.self_id)
        if edited:
            # Правка без смены текста (реакция) никому не интересна; правка сообщения,
            # которого в архиве не было, приходит как новая запись с пометкой «изменено».
            ids = result.new_ids + result.known_ids if (result.versions or result.new) else ()
        else:
            ids = result.new_ids
        for message_id in ids:
            self.events.publish(ev.MESSAGE_LIVE, {
                "account_id": self.account_id, "chat_id": chat_id, "message_id": message_id,
                "source": "session", "outgoing": outgoing, "edited": edited, "via_bot": via_bot,
            })

    async def _refresh_chat(self, conn: asyncpg.Connection, key: PeerKey, entities: normalize.Entities) -> None:
        """Живой источник знает актуальное название чата — обновляет его, когда оно изменилось."""
        entity = entities.get(key)
        if entity is None or getattr(entity, "min", False):
            return
        chat = normalize.chat_record(entity, self_id=self.self_id)
        if chat is None:
            return
        seen = (chat.name, chat.username)
        if self._seen_names.get(key) == seen:
            return
        await store.ensure_chat(conn, self.account_id, chat, refresh=True)
        self._seen_names[key] = seen

    async def _auto_enable(self, key: PeerKey, entities: normalize.Entities) -> sync.ChatState | None:
        if not (self.auto_personal or self.auto_groups):
            return None
        entity = entities.get(key)
        if entity is None or getattr(entity, "min", False):
            return None
        chat = normalize.chat_record(entity, self_id=self.self_id)
        if chat is None:
            return None
        wanted = (self.auto_personal and chat.type in normalize.PERSONAL_TYPES) or \
                 (self.auto_groups and chat.type in normalize.GROUP_TYPES)
        if not wanted:
            return None
        async with self.pool.acquire() as conn:
            chat_id, enabled = await sync.enable_chat(conn, self.account_id, chat, auto=True)
        state = sync.ChatState(chat_id, enabled)
        self.index[key] = state
        if enabled:
            logger.info("аккаунт %s: новый чат %s%s включён настройкой аккаунта", self.account_id, *key)
            self.wake.set()  # историю нового чата догрузит фоновая работа
        return state

    # --- удалённые ---

    async def _on_deleted(self, update: Any) -> None:
        ids = [int(i) for i in update.messages or ()]
        if not ids:
            return
        async with self.pool.acquire() as conn:
            if isinstance(update, types.UpdateDeleteChannelMessages):
                state = self.index.get(("channel", int(update.channel_id)))
                if state is None or not state.enabled or state.chat_id is None:
                    return
                deleted = await store.mark_deleted(conn, state.chat_id, ids)
            else:
                deleted = await store.mark_deleted_without_chat(conn, self.account_id, ids)
        if deleted:
            self.events.publish(ev.MESSAGES_DELETED, {"message_ids": deleted})
