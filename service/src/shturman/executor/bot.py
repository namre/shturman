"""Бот согласований: опрос обновлений Telegram и их разбор.

Что бот принимает:
  * `/start <код>` в личном чате — привязка владельца по одноразовой ссылке (`binding.py`);
  * нажатие кнопки под своим сообщением — передаётся в `bridge.dispatch_callback` с настоящим
    идентификатором нажавшего, который сообщил Telegram;
  * обновления бизнес-режима — подключение, сообщения, правки, удаления — пишутся в архив теми же
    функциями, что и при приёме от плагина (`ingest_api.accept_business_*`), с пометкой `via="service"`.

Чего бот не делает: не отвечает посторонним (совсем — чтобы не выдавать, что он существует),
принимает только детерминированные команды текущего владельца в его личном чате.

Порядок работы с обновлениями: обновление разобрано — его номер записан в базу
(`executor_state.offset`), и следующий запрос к Telegram его подтверждает. После перезапуска
опрос продолжается с записанного места. Если сервис упал между разбором и записью, обновление
придёт ещё раз; повтор безвреден: кнопка срабатывает один раз (метка в базе), сообщение
в архиве не дублируется, код привязки одноразовый.

В журнал пишутся только виды событий и коды ошибок: ни текстов, ни имён, ни идентификаторов.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from typing import Any, Callable

import asyncpg

from .. import authority, bridge, control_peers, ingest_api
from ..api_core import BadRequest
from ..app import AppState
from ..sanitize import clean_line
from ..outbox.text import split_text
from . import binding, owner_chat
from .botapi import BotApi, BotApiError, NeverLeft, OutcomeUnknown, Refused

logger = logging.getLogger("shturman.executor.bot")

MAX_BACKOFF = 60.0
HINT_EVERY = 3600.0               # секунд между подсказками владельцу на свободный текст
REFUSED_TTL = 600.0               # секунд не переспрашивать у Telegram подключение, которое не подошло
UPDATE_RETRIES = 5                # столько раз обновление разбирается заново при временном сбое Telegram

TEXT_BOUND = ("Готово: вы привязаны как владелец.\n\n"
              "Управляйте настройками и заданиями здесь: /menu. Решения по источникам приходят карточками с кнопками.")
TEXT_HINT = "Управление Штурманом: /menu — настройки, задания ответов и источники. /help — команды."
TEXT_REFUSED = "Кнопка недоступна."
TEXT_FAILED = "Не получилось. Попробуйте ещё раз."

_START = re.compile(r"^/start(?:@\w+)?(?:\s+(.+?))?\s*$", re.DOTALL)
# Сбой базы: обновление не потеряно, разберём его заново, когда база вернётся.
_DB_DOWN = (asyncpg.PostgresConnectionError, asyncpg.TooManyConnectionsError,
            asyncpg.CannotConnectNowError, ConnectionError)


class _Later(Exception):
    """Обновление сейчас разобрать нельзя по временной причине — оно остаётся неподтверждённым."""

    def __init__(self, *, forever: bool = False) -> None:
        super().__init__()
        self.forever = forever      # повторять без предела (сбой базы), а не несколько раз


def _id(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _display_name(user: dict[str, Any]) -> str | None:
    """Имя отправителя одной строкой, без управляющих знаков — для отметки о привязке владельца."""
    parts = [user.get(key) for key in ("first_name", "last_name")]
    return clean_line(" ".join(p for p in parts if isinstance(p, str) and p.strip()), 64) or None


class Bot:
    """Опрос и разбор обновлений. `wake` — будит исполнителя заданий после нажатия кнопки."""

    def __init__(self, state: AppState, api: BotApi, *, wake: Callable[[], None] = lambda: None,
                 clock: Callable[[], float] = time.monotonic, poll: int | None = None,
                 sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self.state, self.api = state, api
        self.wake = wake
        self._clock, self._sleep = clock, sleep
        self._poll = poll
        self.identity: dict[str, Any] | None = None      # {id, username} по ответу getMe
        self.business_capable: bool | None = None
        self.offset: int | None = None
        self.flood = binding.Flood(clock=clock)
        self.counters: Counter[str] = Counter()
        self.polling: bool | None = None                  # None — опрос ещё не начинался
        self.problem: str | None = None                   # код последней неполадки
        self.last_poll_at: float | None = None
        self.last_update_at: float | None = None
        self._hint_at: float | None = None
        self._refused: dict[str, float] = {}
        self._retries: dict[int, int] = {}

    @property
    def bot_id(self) -> int | None:
        return self.identity["id"] if self.identity else None

    # --- запуск ---

    async def ensure_identity(self) -> None:
        """Узнаёт у Telegram, каким ботом работает сервис, и поднимает записанное место опроса."""
        me = await self.api.get_me()
        bot_id, username = _id(me.get("id")), me.get("username")
        if bot_id is None or not isinstance(username, str) or not username:
            raise OutcomeUnknown("bad_get_me")
        async with self.state.pool.acquire() as conn, conn.transaction():
            await control_peers.register(conn, bot_id, reason="service_bot")
            await binding.put_state(conn, "bot", {"id": bot_id, "username": username})
            saved = await binding.get_state(conn, "offset")
        # Номера обновлений у каждого бота свои: место опроса другого бота не годится.
        same_bot = saved is not None and saved.get("bot_id") == bot_id
        self.offset = _id(saved.get("next")) if same_bot else None
        self.identity = {"id": bot_id, "username": username}
        self.business_capable = me.get("can_connect_to_business") is True
        logger.info("бот согласований подключён к Telegram")

    async def run(self) -> None:
        """Бесконечный опрос. Останавливается отменой задачи."""
        failures = 0
        while True:
            base = 1.0
            try:
                if self.identity is None:
                    await self.ensure_identity()
                await self.poll_once()
                failures = 0
                continue
            except asyncio.CancelledError:
                raise
            except Refused as exc:
                self.polling = False
                if exc.code == 409:
                    # Бота опрашивает кто-то ещё либо у него включён webhook: вдвоём опрашивать нельзя.
                    self.problem, base = (exc.reason if exc.reason in ("webhook", "other_poller") else "conflict"), 5.0
                elif exc.code in (401, 404):
                    self.problem, base = "token_rejected", 30.0
                elif exc.code == 429:
                    self.problem, base = "too_many_requests", float((exc.retry_after or 5) + 1)
                else:
                    self.problem = f"refused_{exc.code}"
            except (NeverLeft, OutcomeUnknown):
                self.polling = False
                self.problem = self.api.broken or "no_connection"
            except _Later:
                self.problem = "busy"
            except Exception as exc:  # noqa: BLE001 — опрос не должен остановиться насовсем
                self.problem = "internal"
                logger.warning("бот согласований: сбой опроса (%s)", type(exc).__name__)
            failures += 1
            if failures == 1 or failures % 20 == 0:
                logger.warning("бот согласований: опрос приостановлен (%s)", self.problem)
            await self._sleep(min(MAX_BACKOFF * (5 if self.problem == "token_rejected" else 1),
                                  base * 2 ** min(failures - 1, 6)))

    async def poll_once(self) -> int:
        """Один запрос обновлений и их разбор по порядку. Возвращает число полученных."""
        if self._poll is None:
            updates = await self.api.get_updates(self.offset)
        else:
            updates = await self.api.get_updates(self.offset, poll=self._poll)
        self.polling, self.problem = True, None
        self.last_poll_at = self._clock()
        for update in updates:
            update_id = _id(update.get("update_id"))
            if update_id is None or (self.offset is not None and update_id < self.offset):
                continue
            self.last_update_at = self._clock()
            try:
                await self.handle_update(update)
            except asyncio.CancelledError:
                raise
            except _DB_DOWN:
                raise _Later(forever=True) from None
            except _Later as exc:
                tries = self._retries.get(update_id, 0) + 1
                if exc.forever or tries < UPDATE_RETRIES:
                    self._retries = {update_id: tries}
                    raise
                self.counters["updates_dropped"] += 1
                logger.warning("бот согласований: обновление пропущено после повторов")
            except Exception as exc:  # noqa: BLE001 — одно обновление не должно остановить остальные
                self.counters["update_errors"] += 1
                logger.warning("бот согласований: сбой при разборе обновления (%s)", type(exc).__name__)
            self._retries.pop(update_id, None)
            await self._advance(update_id + 1)
        return len(updates)

    async def _advance(self, next_id: int) -> None:
        self.offset = next_id
        try:
            async with self.state.pool.acquire() as conn:
                await binding.put_state(conn, "offset", {"bot_id": self.bot_id, "next": next_id})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — место опроса осталось в памяти; после перезапуска обновление повторится
            logger.warning("бот согласований: место опроса не записано (%s)", type(exc).__name__)

    # --- разбор ---

    async def handle_update(self, update: dict[str, Any]) -> None:
        self.counters["updates"] += 1
        if isinstance(update.get("callback_query"), dict):
            await self._on_callback(update["callback_query"])
        elif isinstance(update.get("message"), dict):
            await self._on_message(update["message"])
        elif isinstance(update.get("business_connection"), dict):
            await self._on_business_connection(update["business_connection"])
        elif isinstance(update.get("business_message"), dict):
            await self._on_business_message(update["business_message"], edited=False)
        elif isinstance(update.get("edited_business_message"), dict):
            await self._on_business_message(update["edited_business_message"], edited=True)
        elif isinstance(update.get("deleted_business_messages"), dict):
            await self._on_business_deleted(update["deleted_business_messages"])

    async def _owner(self) -> dict[str, int] | None:
        async with self.state.pool.acquire() as conn:
            return await binding.bound_owner(conn, self.bot_id)

    # --- сообщения боту: привязка и подсказка ---

    async def _on_message(self, message: dict[str, Any]) -> None:
        chat, sender = message.get("chat"), message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict) or sender.get("is_bot") is True:
            return
        user_id, chat_id = _id(sender.get("id")), _id(chat.get("id"))
        if user_id is None or chat_id is None:
            return
        private = chat.get("type") == "private" and chat_id == user_id
        text = message.get("text") if isinstance(message.get("text"), str) else ""
        start = _START.match(text.strip())
        code = start.group(1) if start else None
        if code is not None:
            await self._bind(code, user_id=user_id, chat_id=chat_id, private=private, name=_display_name(sender))
            return
        if not private:
            return
        owner = await self._owner()
        if owner is None or owner["user_id"] != user_id or owner["chat_id"] != chat_id:
            return                    # посторонним бот не отвечает вовсе
        if text.startswith("/"):
            async with self.state.pool.acquire() as conn:
                result = await owner_chat.handle_message(conn, self.state, text, bot_id=self.bot_id,
                                                         user_id=user_id, chat_id=chat_id)
            if result is not None:
                await self._say(chat_id, result.get("text", "Готово."), buttons=result.get("buttons"))
                self.wake()
            return
        now = self._clock()
        if self._hint_at is not None and now - self._hint_at < HINT_EVERY:
            return
        self._hint_at = now
        await self._say(chat_id, TEXT_HINT, buttons=owner_chat.menu_buttons())

    async def _bind(self, code: str, *, user_id: int, chat_id: int, private: bool,
                    name: str | None = None) -> None:
        """`/start <код>`. При любом отказе бот молчит: посторонний не узнает даже, что код неверен."""
        if self.bot_id is None:
            return
        if not private:
            # Ссылку открыли в группе: код увидели посторонние — он больше не годится.
            if binding.well_formed(code):
                async with self.state.pool.acquire() as conn:
                    if await binding.revoke_code(conn, code):
                        logger.warning("бот согласований: код привязки прислан не в личный чат и отменён")
            return
        if self.flood.locked():
            self.counters["bind_ignored"] += 1
            return
        bound = False
        if binding.well_formed(code):
            async with self.state.pool.acquire() as conn:
                bound = await binding.redeem(conn, code, user_id=user_id, chat_id=chat_id, bot_id=self.bot_id,
                                             name=name)
        if not bound:
            self.counters["bind_wrong"] += 1
            self.flood.wrong()
            if self.flood.locked():
                logger.warning("бот согласований: много неверных кодов привязки — приём кодов приостановлен")
            return
        self.counters["binds"] += 1
        self._hint_at = None
        logger.info("бот согласований: владелец привязан")
        await self._say(chat_id, TEXT_BOUND, buttons=owner_chat.menu_buttons())
        self.wake()                   # карточки, ждавшие владельца, можно отправлять

    async def _say(self, chat_id: int, text: str, *, buttons: Any = None) -> None:
        try:
            for index, part in enumerate(split_text(text)):
                await self.api.send_message(chat_id, part, no_preview=True,
                                            buttons=buttons if index == 0 else None)
        except BotApiError as exc:
            logger.warning("бот согласований: сообщение владельцу не отправлено (%s)", exc)

    # --- кнопки ---

    async def _answer(self, query_id: str, text: str | None) -> None:
        try:
            await self.api.answer_callback_query(query_id, text[:200] if text else None)
        except BotApiError as exc:      # нажатие могло устареть
            logger.warning("бот согласований: не удалось ответить на нажатие (%s)", exc)

    async def _on_callback(self, query: dict[str, Any]) -> None:
        query_id, data = query.get("id"), query.get("data")
        if not isinstance(query_id, str):
            return
        sender = query.get("from") if isinstance(query.get("from"), dict) else {}
        message = query.get("message") if isinstance(query.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        presser, chat_id, message_id = _id(sender.get("id")), _id(chat.get("id")), _id(message.get("message_id"))
        owner = await self._owner()
        allowed = (
            isinstance(data, str) and data.startswith(bridge.CALLBACK_PREFIX)
            and owner is not None and presser == owner["user_id"]
            # Только под сообщением в личном чате владельца с ботом: кнопка из любого другого
            # места (группа, чужой чат, сообщение без чата) не принимается.
            and chat.get("type") == "private" and chat_id == owner["chat_id"] and message_id is not None
        )
        if not allowed:
            self.counters["callbacks_refused"] += 1
            await self._answer(query_id, TEXT_REFUSED)
            return
        try:
            async with self.state.pool.acquire() as conn:
                with authority.owner_context(presser, chat_id=chat_id, action="telegram.callback"):
                    if data.startswith(bridge.CALLBACK_PREFIX + owner_chat.MODULE + ":"):
                        out = await owner_chat.on_menu(conn, self.state, data.split(":", 2)[2], presser)
                    else:
                        out = await bridge.dispatch_callback(conn, data, presser)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — кнопки остаются, владелец может нажать ещё раз
            self.counters["callback_errors"] += 1
            logger.warning("бот согласований: нажатие не разобрано (%s)", type(exc).__name__)
            await self._answer(query_id, TEXT_FAILED)
            return
        self.counters["callbacks"] += 1
        answer = out.get("answer")
        await self._answer(query_id, answer if isinstance(answer, str) else None)
        new_text, remove = out.get("edit_text"), bool(out.get("remove_buttons"))
        try:
            if isinstance(new_text, str) and new_text.strip():
                # Обычным текстом: в карточке есть чужой текст. При правке текста Telegram снимает
                # кнопки сам, поэтому «оставить» значит передать прежнюю клавиатуру заново.
                markup = None if remove else _own_keyboard(message.get("reply_markup"))
                await self.api.edit_message_text(chat_id, message_id, bridge.fit_message(new_text),
                                                 reply_markup=markup)
            elif remove:
                await self.api.remove_keyboard(chat_id, message_id)
        except BotApiError as exc:    # «не изменено», сообщение удалено — решение уже принято
            if not (isinstance(exc, Refused) and exc.reason in ("not_modified", "message_not_found")):
                logger.warning("бот согласований: карточка не обновлена (%s)", exc)
        # Нажатие могло поставить задание (отправку). Сервис ставит его чуть позже ответа,
        # поэтому исполнителя будим дважды.
        self.wake()
        try:
            asyncio.get_running_loop().call_later(0.6, self.wake)
        except RuntimeError:
            pass

    # --- бизнес-режим ---

    async def _owner_ids(self, conn: asyncpg.Connection) -> set[int]:
        owner = await binding.bound_owner(conn, self.bot_id)
        return {owner["user_id"]} if owner else set()

    async def _on_business_connection(self, link: dict[str, Any]) -> bool:
        """Записывает подключение. True — принято."""
        try:
            await ingest_api.accept_business_connection(
                self.state.pool, link, via="service", owner_ids=self._owner_ids)
        except BadRequest as exc:
            # Чужой аккаунт, владелец ещё не привязан, подключение другого бота, неразборчивый объект.
            self.counters["business_refused"] += 1
            logger.warning("бот согласований: бизнес-подключение не принято (%s)", getattr(exc, "code", "bad_request"))
            return False
        self.counters["business_connections"] += 1
        return True

    def _is_refused(self, connection_id: str) -> bool:
        until = self._refused.get(connection_id)
        if until is None:
            return False
        if until <= self._clock():
            del self._refused[connection_id]
            return False
        return True

    def _refuse(self, connection_id: str) -> None:
        if len(self._refused) > 500:
            self._refused.clear()
        self._refused[connection_id] = self._clock() + REFUSED_TTL

    async def _resync_connection(self, connection_id: str) -> bool:
        """Сервис не знает подключения (или держит выключенным): спрашиваем его у Telegram."""
        if self._is_refused(connection_id):
            return False
        try:
            link = await self.api.get_business_connection(connection_id)
        except Refused as exc:
            if exc.code == 429:
                raise _Later() from None
            link = None               # Telegram такого подключения не знает
        except BotApiError:
            raise _Later() from None  # Telegram сейчас недоступен — спросим позже
        if not isinstance(link, dict) or not await self._on_business_connection(link):
            self._refuse(connection_id)
            return False
        return True

    async def _on_business_message(self, message: dict[str, Any], *, edited: bool) -> None:
        connection_id = message.get("business_connection_id")
        connection_id = connection_id if isinstance(connection_id, str) and connection_id else None
        for attempt in (1, 2):
            try:
                out = await ingest_api.accept_business_message(self.state, message, edited=edited, via="service")
            except BadRequest as exc:
                code = getattr(exc, "code", None)
                if exc.status == 503:
                    raise _Later(forever=True) from None       # архив занят — обновление не теряем
                if (attempt == 1 and code == "unknown_connection" and connection_id
                        and await self._resync_connection(connection_id)):
                    continue
                self.counters["business_rejected"] += 1
                return
            if out.get("stored") is not False:
                self.counters["business_messages"] += 1
                return
            if (attempt == 1 and out.get("reason") == "connection_disabled" and connection_id
                    and await self._resync_connection(connection_id)):
                continue
            if out.get("reason") == "connection_disabled" and connection_id:
                self._refuse(connection_id)
            self.counters["business_not_stored"] += 1
            return

    async def _on_business_deleted(self, data: dict[str, Any]) -> None:
        try:
            out = await ingest_api.accept_business_deleted(self.state, data, via="service")
        except BadRequest:
            self.counters["business_rejected"] += 1
            return
        self.counters["business_deleted"] += int(out.get("deleted") or 0)


def _own_keyboard(markup: Any) -> dict[str, Any] | None:
    """Клавиатура сообщения, под которым нажали кнопку, — только кнопки сервиса."""
    rows = markup.get("inline_keyboard") if isinstance(markup, dict) else None
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        kept = [{"text": b["text"], "callback_data": b["callback_data"]} for b in row or []
                if isinstance(b, dict) and isinstance(b.get("text"), str)
                and isinstance(b.get("callback_data"), str)
                and b["callback_data"].startswith(bridge.CALLBACK_PREFIX)]
        if kept:
            out.append(kept)
    return {"inline_keyboard": out} if out else None
