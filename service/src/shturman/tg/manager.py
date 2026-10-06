"""Сессии аккаунтов Telegram: запуск, вход, остановка — и шлюз для остальных модулей.

Объект `TgManager` лежит в `state.extras["tg"]` и для остальных модулей выглядит как
`tg/gateway.py::TgGateway`. Клиента Telethon наружу он не отдаёт.

Две роли, на каждую — одна сессия (файл `<роль>.session` в каталоге данных сервиса):
  * owner — основной аккаунт владельца. Только чтение: перечень запросов клиента не содержит
    отправки, а `send_text` отказывает до любого обращения к клиенту;
  * assistant — аккаунт-помощник. Может отправлять, но только через `send_text` шлюза.

Аккаунт владельца нельзя подключить как помощника:
  * вход помощника не начинается, пока сервис не знает владельца (не привязан управляющий чат
    и в архиве нет основного аккаунта) — иначе аккаунты не отличить;
  * после входа аккаунт сверяется с владельцем управляющего чата и с основными аккаунтами
    архива; при совпадении только что созданная сессия завершается;
  * та же сверка — при каждом запуске сессии с диска;
  * если владельцем управляющего чата становится аккаунт, уже подключённый как помощник,
    сессия останавливается и ставится на паузу, а владелец получает уведомление.

Отправка, кроме роли, требует главного выключателя `config.sending` (только из окружения
сервиса): пока он выключен, `can_send` — ложь, `send_text` отказывает, «печатает…» не шлётся.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from telethon import errors
from telethon.tl import functions, types

from .. import bridge, store
from .. import events as ev
from ..config import Config
from ..events import Events
from ..records import ChatRecord
from . import gateway, normalize, sync
from .client import (ROLES, ClientFactory, NotConfigured, RequestPolicy, make_client_factory,
                     remove_session_file, session_path)
from .live import LiveIngest
from .lock import SessionLock, SessionLocked
from .normalize import PeerKey
from .qr import LoginFlow, LoginRejected, LoginStateError

logger = logging.getLogger("shturman.tg")

TEXT_LIMIT = 4096
DIALOGS_TTL = 600.0       # сколько секунд верить списку диалогов
DIALOGS_MIN_GAP = 30.0    # чаще список не перечитывается даже по просьбе
FLOW_KEEP = 600.0         # сколько помнить законченный вход, чтобы экран узнал итог
RETRY_FIRST, RETRY_MAX = 5.0, 600.0

# Состояния аккаунта для экрана.
STARTING, RUNNING, DISCONNECTED, ERROR = "starting", "running", "disconnected", "error"
PAUSED, UNAUTHORIZED, LOCKED, FAILED, NO_SESSION = "paused", "unauthorized", "locked", "failed", "no_session"
ACTIVE = (STARTING, RUNNING, DISCONNECTED, ERROR)

ROLE_NAMES = {"owner": "основной аккаунт владельца", "assistant": "аккаунт-помощник"}


KEEP: Any = object()   # «настройку не трогать»


class TgError(Exception):
    """Отказ с текстом для владельца и кодом ответа HTTP."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message, self.status = message, status


class _Unauthorized(Exception):
    pass


@dataclass
class DialogInfo:
    chat: ChatRecord
    top_message: int

    @property
    def key(self) -> PeerKey:
        return self.chat.peer_class, self.chat.tg_id


@dataclass
class AccountRuntime:
    """Одна подключённая (или подключающаяся) сессия."""

    slot: str
    client: Any = None
    lock: SessionLock | None = None
    policy: RequestPolicy | None = None
    account_id: int | None = None
    self_id: int | None = None
    status: str = STARTING
    error: str | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    reconnected: asyncio.Event = field(default_factory=asyncio.Event)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    pacer: sync.Pacer = field(default_factory=sync.Pacer)
    live: LiveIngest | None = None
    history: sync.HistorySync | None = None
    task: asyncio.Task | None = None
    dialogs: list[DialogInfo] | None = None
    dialogs_at: float = 0.0
    dialogs_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    logout: bool = False
    lock_lost: bool = False
    terminated: bool = False  # сессия завершена на стороне Telegram

    @property
    def role(self) -> str:
        return self.slot

    def on_reconnect(self) -> None:
        self.reconnected.set()
        self.wake.set()


def _label(user: Any) -> str:
    return normalize.display_name(user) or f"Аккаунт {user.id}"


def _sent_message(result: Any, key: PeerKey, text: str, self_id: int,
                  reply_to: int | None) -> tuple[int, Any, dict]:
    """Номер отправленного сообщения и само сообщение из ответа Telegram."""
    if isinstance(result, types.UpdateShortSentMessage):
        message = types.Message(
            id=result.id, peer_id=normalize.to_peer(key), from_id=types.PeerUser(self_id),
            date=result.date, message=text, out=True, entities=result.entities, media=result.media,
            reply_to=types.MessageReplyHeader(reply_to_msg_id=reply_to) if reply_to else None,
        )
        return int(result.id), message, {}
    updates = getattr(result, "updates", None) or ()
    for update in updates:
        if isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)) \
                and isinstance(update.message, types.Message):
            entities = normalize.index_entities(getattr(result, "users", None), getattr(result, "chats", None))
            return int(update.message.id), update.message, entities
    for update in updates:
        if isinstance(update, types.UpdateMessageID):
            return int(update.id), None, {}
    raise RuntimeError("Telegram не вернул номер отправленного сообщения")


class TgManager:
    def __init__(
        self, config: Config, pool: asyncpg.Pool, events: Events, *,
        client_factory: ClientFactory | None = None, pacing: float = 3.0,
    ) -> None:
        self.config, self.pool, self.events = config, pool, events
        self.client_factory: ClientFactory = client_factory or make_client_factory(config)
        self.pacing = pacing
        self.runtimes: dict[str, AccountRuntime] = {}
        self.flows: dict[str, LoginFlow] = {}
        self._flow_done_at: dict[str, float] = {}
        self._retry_first = RETRY_FIRST
        self._background: set[asyncio.Task] = set()
        events.subscribe(ev.CHAT_EXCLUDED, self.on_chat_excluded)

    # ------------------------------------------------------------------ общее

    @property
    def configured(self) -> bool:
        return bool(self.config.tg_api_id and self.config.tg_api_hash)

    def require_configured(self) -> None:
        if not self.configured:
            raise TgError(
                "Работа с аккаунтами Telegram не настроена: не заданы ключи приложения "
                "(TELEGRAM_API_ID и TELEGRAM_API_HASH).", 503)

    def _by_account(self, account_id: int) -> AccountRuntime | None:
        return next((rt for rt in self.runtimes.values() if rt.account_id == account_id), None)

    def _running(self, account_id: int) -> AccountRuntime:
        rt = self._by_account(account_id)
        if rt is None or rt.status != RUNNING or rt.client is None:
            raise gateway.AccountUnavailable("сессия аккаунта не запущена")
        return rt

    async def _slot_of(self, account_id: int) -> asyncpg.Record:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT s.slot, s.paused, a.role FROM tg_sessions s JOIN accounts a ON a.id = s.account_id
                   WHERE s.account_id = $1""", account_id)
        if row is None:
            raise TgError("У этого аккаунта нет сессии Telegram.", 404)
        return row

    # ------------------------------------------------------------------ запуск и остановка

    async def start(self) -> None:
        """Запускает сессии, файлы которых лежат на диске. Не ждёт подключения."""
        if not self.configured:
            return
        async with self.pool.acquire() as conn:
            paused = {r["slot"] for r in await conn.fetch("SELECT slot FROM tg_sessions WHERE paused")}
        for slot in ROLES:
            if slot in paused or not session_path(self.config, slot).exists():
                continue
            await self._launch(slot)

    async def stop(self) -> None:
        for flow in list(self.flows.values()):
            await flow.cancel()
        for rt in list(self.runtimes.values()):
            await self._shutdown(rt)
        self.runtimes.clear()
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------------ события сервиса

    async def on_chat_excluded(self, payload: dict[str, Any]) -> None:
        """Владелец исключил чат из архива: синхронизация чата прекращается немедленно, не
        дожидаясь, пока очередная запись в него будет отвергнута. Страница истории, которая в
        этот момент уже запрошена, при записи отбрасывается."""
        async with self.pool.acquire() as conn:
            hit = await sync.chat_excluded(conn, int(payload["chat_id"]), purged=bool(payload.get("purged")))
        if hit is None:
            return
        account_id, key = hit
        rt = self._by_account(account_id)
        if rt is not None and rt.live is not None:
            rt.live.drop_chat(key)
        logger.info("аккаунт %s: чат %s%s исключён из архива, синхронизация выключена", account_id, *key)

    async def owner_changed(self, conn: asyncpg.Connection, new_user_id: int) -> None:
        """Сменился владелец управляющего чата (вызывается внутри транзакции смены).

        Если новым владельцем оказался аккаунт, подключённый как помощник, это основной аккаунт
        владельца с правом отправки — так быть не должно. Сессия останавливается и ставится на
        паузу; снять паузу не выйдет (запуск такую сессию отвергнет), остаётся выйти из неё.
        """
        if not new_user_id:
            return
        account_id = await conn.fetchval(
            """SELECT s.account_id FROM tg_sessions s JOIN accounts a ON a.id = s.account_id
               WHERE s.slot = 'assistant' AND a.tg_user_id = $1""", int(new_user_id))
        if account_id is None:
            return
        reason = "Аккаунт владельца подключён как помощник: сессия остановлена."
        await conn.execute("UPDATE tg_sessions SET paused = true, last_error = $2 WHERE account_id = $1",
                           account_id, reason)
        rt = self.runtimes.pop("assistant", None)   # с этого мгновения can_send — ложь
        if rt is not None:
            rt.stop.set()
            self._spawn(self._shutdown(rt), "tg-assistant-owner-stop")
        await bridge.notify_owner(
            conn,
            "Аккаунт, подключённый к сервису как помощник, оказался вашим основным аккаунтом. "
            "Помощнику разрешена отправка сообщений, поэтому его сессия остановлена и поставлена "
            "на паузу. Выйдите из неё в разделе аккаунтов и подключите помощником другой аккаунт.")
        logger.warning("аккаунт %s: владелец управляющего чата совпал с помощником, сессия остановлена", account_id)

    async def _open(self, slot: str, rt: AccountRuntime, *, login: bool) -> None:
        """Берёт блокировки и создаёт клиента. Блокировка — до любого подключения."""
        path = session_path(self.config, slot)
        lock = SessionLock(path, self.config.dsn, on_lost=lambda: self._lock_lost(rt))
        await lock.acquire(f"slot:{slot}")
        try:
            policy = RequestPolicy(slot, login=login, sending=self.config.sending)
            client = self.client_factory(slot, path, policy, rt.on_reconnect)
        except BaseException:
            await lock.release()
            raise
        rt.lock, rt.policy, rt.client = lock, policy, client
        rt.pacer = sync.Pacer(self.pacing)

    async def _launch(self, slot: str) -> AccountRuntime:
        rt = AccountRuntime(slot=slot)
        self.runtimes[slot] = rt
        try:
            await self._open(slot, rt, login=False)
        except SessionLocked as exc:
            rt.status, rt.error = LOCKED, str(exc)
            rt.ready.set()
            logger.error("аккаунт (%s): сессия занята другим процессом, не подключаюсь", slot)
            return rt
        except NotConfigured as exc:
            rt.status, rt.error = FAILED, str(exc)
            rt.ready.set()
            return rt
        rt.task = asyncio.get_running_loop().create_task(self._supervise(rt), name=f"tg-{slot}")
        return rt

    def _lock_lost(self, rt: AccountRuntime) -> None:
        if rt.stop.is_set() or rt.task is None or rt.task.done():
            return
        rt.lock_lost = True
        rt.task.cancel()

    async def _shutdown(self, rt: AccountRuntime, *, logout: bool = False) -> None:
        rt.logout = logout
        rt.stop.set()
        rt.wake.set()
        if rt.task is not None and not rt.task.done():
            rt.task.cancel()
            await asyncio.gather(rt.task, return_exceptions=True)
        else:
            await self._close(rt)

    async def _close(self, rt: AccountRuntime) -> None:
        client, rt.client = rt.client, None
        if client is not None:
            try:
                if rt.logout:
                    # log_out() сам отключается и удаляет файл сессии
                    rt.terminated = bool(await client.log_out())
                if not rt.terminated:
                    await client.disconnect()
            except Exception as exc:
                logger.warning("аккаунт (%s): отключение завершилось с ошибкой (%s)", rt.slot, type(exc).__name__)
        if rt.logout:
            remove_session_file(session_path(self.config, rt.slot))
        lock, rt.lock = rt.lock, None
        if lock is not None:
            await lock.release()

    async def _supervise(self, rt: AccountRuntime) -> None:
        """Держит сессию подключённой. Неустранимая ошибка останавливает её без повторов."""
        delay = self._retry_first
        try:
            while not rt.stop.is_set():
                try:
                    await self._session(rt)
                    delay = self._retry_first
                    rt.status = DISCONNECTED
                except asyncio.CancelledError:
                    raise
                except (_Unauthorized, errors.UnauthorizedError):
                    rt.status, rt.error = UNAUTHORIZED, "Сессия больше не действует: войдите в аккаунт заново."
                    break
                except errors.AuthKeyError as exc:
                    # В том числе AUTH_KEY_DUPLICATED: ключ использован вторым подключением
                    # и отозван. Повтор только ухудшит положение.
                    rt.status = FAILED
                    rt.error = f"Ключ сессии недействителен ({type(exc).__name__}): войдите в аккаунт заново."
                    break
                except (LoginRejected, SessionLocked) as exc:
                    rt.status, rt.error = FAILED, str(exc)
                    break
                except Exception as exc:
                    rt.status, rt.error = ERROR, type(exc).__name__
                    logger.warning("аккаунт (%s): подключение не удалось (%s), повтор через %s с",
                                   rt.slot, type(exc).__name__, int(delay))
                finally:
                    rt.ready.set()
                if await sync.interruptible_sleep(delay, rt.stop):
                    break
                delay = min(delay * 2, RETRY_MAX)
        except asyncio.CancelledError:
            if rt.lock_lost and not rt.stop.is_set():
                rt.status, rt.error = FAILED, "Потеряна блокировка сессии: соединение с базой оборвалось."
                logger.error("аккаунт (%s): блокировка сессии потеряна, отключаюсь", rt.slot)
            else:
                raise
        finally:
            if rt.status in ACTIVE:
                rt.status = DISCONNECTED
            await self._close(rt)
            await self._save_error(rt)

    async def _save_error(self, rt: AccountRuntime) -> None:
        if rt.account_id is None or rt.logout or rt.stop.is_set():
            return  # остановка по команде причину прежней остановки не стирает
        with contextlib.suppress(Exception):
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE tg_sessions SET last_error = $2 WHERE account_id = $1",
                                   rt.account_id, rt.error if rt.status in (FAILED, UNAUTHORIZED) else None)

    async def _session(self, rt: AccountRuntime) -> None:
        """Одно подключение: от connect() до разрыва."""
        client = rt.client
        if not client.is_connected():
            await client.connect()
        # Не is_user_authorized(): тот считает «не авторизован» любую ошибку запроса, в том числе
        # временную. get_me() возвращает None только когда Telegram прямо ответил, что сессия не действует.
        me = await client.get_me()
        if me is None:
            raise _Unauthorized()
        if rt.account_id is None:
            await self._register(rt, me, fresh=False)
        elif int(me.id) != rt.self_id:
            raise LoginRejected("Сессия принадлежит другому аккаунту, чем была при запуске.")
        rt.policy.login = False
        if rt.live is None:
            rt.live = LiveIngest(pool=self.pool, events=self.events, account_id=rt.account_id,
                                 self_id=rt.self_id, wake=rt.wake)
            await rt.live.reload()
            rt.live.register(client)  # до первого чтения истории, чтобы ничего не проскочило
        rt.history = sync.HistorySync(
            client=client, pool=self.pool, events=self.events, account_id=rt.account_id,
            self_id=rt.self_id, pacer=rt.pacer, stop=rt.stop)
        rt.status, rt.error = RUNNING, None
        rt.ready.set()
        loop = asyncio.get_running_loop()
        worker = loop.create_task(
            rt.history.run(rt.wake, rt.reconnected, tops=lambda: self._tops(rt)), name=f"tg-{rt.slot}-history")
        gone = asyncio.ensure_future(client.disconnected)
        try:
            await asyncio.wait({worker, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if gone.done() and not gone.cancelled():
            gone.exception()  # причина разрыва не важна: дальше — переподключение
        if not worker.cancelled() and worker.exception() is not None:
            raise worker.exception()  # например, сессию отозвали посреди работы

    async def _register(self, rt: AccountRuntime, user: Any, *, fresh: bool) -> None:
        """Сверяет аккаунт с ролью и записывает его. Бросает LoginRejected с текстом для владельца."""
        role, tg_user_id = rt.role, int(user.id)
        async with self.pool.acquire() as conn, conn.transaction():
            known = await conn.fetchrow("SELECT id, role FROM accounts WHERE tg_user_id = $1", tg_user_id)
            if known is not None and known["role"] != role:
                raise LoginRejected(
                    f"Этот аккаунт уже записан в архиве как {ROLE_NAMES[known['role']]} — "
                    f"подключить его как {ROLE_NAMES[role]} нельзя.")
            if role == "assistant":
                owner = await bridge.get_owner(conn)
                if owner is not None and int(owner["user_id"]) == tg_user_id:
                    raise LoginRejected(
                        "Это основной аккаунт владельца. Как помощника его подключать нельзя: "
                        "помощнику разрешена отправка сообщений. Отсканируйте код другим аккаунтом.")
            else:
                other = await conn.fetchval(
                    "SELECT 1 FROM accounts WHERE role = 'owner' AND tg_user_id <> $1 LIMIT 1", tg_user_id)
                if other:
                    raise LoginRejected(
                        "В архиве уже есть основной аккаунт с другим идентификатором. "
                        "Отсканируйте код тем аккаунтом, чья переписка уже загружена.")
            bound = await conn.fetchval(
                """SELECT a.tg_user_id FROM tg_sessions s JOIN accounts a ON a.id = s.account_id
                   WHERE s.slot = $1""", rt.slot)
            if bound is not None and bound != tg_user_id:
                if not fresh:
                    raise LoginRejected("Файл сессии принадлежит другому аккаунту, чем записан в базе.")
                await conn.execute("DELETE FROM tg_sessions WHERE slot = $1", rt.slot)
            account_id = await store.ensure_account(conn, tg_user_id, _label(user), role)
            await conn.execute(
                """INSERT INTO tg_sessions (account_id, slot, last_started_at) VALUES ($1, $2, now())
                   ON CONFLICT (account_id) DO UPDATE SET last_started_at = now(), last_error = NULL""",
                account_id, rt.slot)
        # Вторая блокировка — по самому аккаунту: тот же аккаунт не подключится из другой роли.
        await rt.lock.add(f"account:{tg_user_id}")
        rt.account_id, rt.self_id = account_id, tg_user_id

    # ------------------------------------------------------------------ вход

    def _sweep_flows(self) -> None:
        now = time.monotonic()
        for flow_id, flow in list(self.flows.items()):
            if not flow.done:
                continue
            done_at = self._flow_done_at.setdefault(flow_id, now)
            if now - done_at > FLOW_KEEP:
                self.flows.pop(flow_id, None)
                self._flow_done_at.pop(flow_id, None)

    def login(self, login_id: str) -> LoginFlow:
        self._sweep_flows()
        flow = self.flows.get(login_id)
        if flow is None:
            raise TgError("Вход не найден или уже завершён. Начните заново.", 404)
        return flow

    async def start_login(self, role: str, *, confirm_owner: bool = False) -> LoginFlow:
        self.require_configured()
        if role not in ROLES:
            raise TgError("Поле role: нужно assistant или owner.")
        if role == "owner" and not confirm_owner:
            raise TgError(
                "Подключение основного аккаунта нужно подтвердить (confirm_owner: true): на сервере "
                "появится сессия вашего основного аккаунта. Сервис будет только читать, но риск "
                "ограничений со стороны Telegram ложится на основной номер.")
        if role == "assistant":
            async with self.pool.acquire() as conn:
                owner_known = await bridge.get_owner(conn) is not None or await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM accounts WHERE role = 'owner')")
            if not owner_known:
                raise TgError(
                    "Сначала привяжите к сервису своего бота (управляющий чат). Пока сервис не знает, "
                    "какой аккаунт — ваш основной, он не сможет отличить его от помощника, а помощнику "
                    "разрешена отправка сообщений. После привязки подключите помощника снова.", 409)
        self._sweep_flows()
        for flow in list(self.flows.values()):
            if flow.role == role and not flow.done:
                await flow.cancel()  # экран входа открыли заново — прежний код больше не нужен
        current = self.runtimes.get(role)
        if current is not None and current.status in ACTIVE:
            raise TgError(f"{ROLE_NAMES[role].capitalize()} уже подключён. Чтобы сменить его, сначала выйдите.", 409)
        async with self.pool.acquire() as conn:
            if await conn.fetchval("SELECT paused FROM tg_sessions WHERE slot = $1", role):
                raise TgError("Аккаунт на паузе. Снимите паузу или выйдите из него.", 409)
        if current is not None:
            await self._shutdown(current)
            self.runtimes.pop(role, None)
        path = session_path(self.config, role)
        if path.exists():
            # Остаток прежней сессии (недействительной или чужой): завершаем её в Telegram,
            # а не просто стираем файл, чтобы она не осталась в списке устройств.
            await self._logout_offline(role)

        rt = AccountRuntime(slot=role)
        try:
            # Блокировка берётся до того, как тронут файл: его может держать другой процесс.
            lock = SessionLock(path, self.config.dsn, on_lost=lambda: self._lock_lost(rt))
            await lock.acquire(f"slot:{role}")
        except SessionLocked as exc:
            raise TgError(f"Сессия занята другим процессом: {exc}.", 409) from None
        try:
            rt.lock, rt.policy = lock, RequestPolicy(role, login=True, sending=self.config.sending)
            rt.pacer = sync.Pacer(self.pacing)
            rt.client = self.client_factory(role, path, rt.policy, rt.on_reconnect)
        except NotConfigured as exc:
            await lock.release()
            raise TgError(str(exc), 503) from None
        except BaseException:
            await lock.release()
            raise

        async def done(user: Any) -> int | None:
            return await self._login_done(rt, user)

        async def close() -> None:
            await self._login_abort(rt)

        flow = LoginFlow(rt.client, role, on_success=done, on_close=close)
        try:
            await flow.start()
        except errors.FloodWaitError as exc:
            await close()
            raise TgError(f"Telegram просит подождать {int(exc.seconds)} с. Попробуйте позже.", 429) from None
        except Exception as exc:
            await close()
            logger.warning("вход (%s): не удалось начать (%s)", role, type(exc).__name__)
            raise TgError("Не удалось связаться с Telegram. Попробуйте позже.", 502) from None
        self.flows[flow.id] = flow
        return flow

    async def _login_done(self, rt: AccountRuntime, user: Any) -> int | None:
        try:
            await self._register(rt, user, fresh=True)
        except (LoginRejected, SessionLocked) as exc:
            # Сессия на стороне Telegram уже создана — завершаем её, чтобы не осталась
            # в списке устройств аккаунта, который сюда подключать не собирались.
            with contextlib.suppress(Exception):
                await rt.client.log_out()
            raise LoginRejected(str(exc)) from None
        rt.policy.login = False
        self.runtimes[rt.slot] = rt
        rt.task = asyncio.get_running_loop().create_task(self._supervise(rt), name=f"tg-{rt.slot}")
        logger.info("аккаунт %s (%s): вход выполнен", rt.account_id, rt.slot)
        return rt.account_id

    async def _login_abort(self, rt: AccountRuntime) -> None:
        client, rt.client = rt.client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
        remove_session_file(session_path(self.config, rt.slot))
        lock, rt.lock = rt.lock, None
        if lock is not None:
            await lock.release()

    async def submit_password(self, login_id: str, password: str) -> LoginFlow:
        flow = self.login(login_id)
        try:
            await flow.submit_password(password)
        except LoginStateError as exc:
            raise TgError(f"{str(exc).capitalize()}.", 409) from None
        return flow

    async def cancel_login(self, login_id: str) -> LoginFlow:
        flow = self.login(login_id)
        await flow.cancel()
        return flow

    # ------------------------------------------------------------------ аккаунты

    async def list_accounts(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT a.id, a.tg_user_id, a.label, s.slot, s.paused, s.auto_personal, s.auto_groups,
                          s.backfill_months, s.logged_in_at, s.last_error
                   FROM tg_sessions s JOIN accounts a ON a.id = s.account_id ORDER BY s.slot""")
        out, seen = [], set()
        for row in rows:
            slot = row["slot"]
            seen.add(slot)
            rt = self.runtimes.get(slot)
            if rt is not None:
                status, error = rt.status, rt.error
            elif row["paused"]:
                status, error = PAUSED, row["last_error"]
            elif not session_path(self.config, slot).exists():
                status, error = NO_SESSION, "Файла сессии нет: войдите в аккаунт заново."
            else:
                status, error = DISCONNECTED, row["last_error"]
            out.append({
                "role": slot, "account_id": row["id"], "tg_user_id": row["tg_user_id"],
                "label": row["label"], "status": status, "error": error, "paused": row["paused"],
                "can_send": self.can_send(row["id"]),
                "auto_personal": row["auto_personal"], "auto_groups": row["auto_groups"],
                "backfill_months": row["backfill_months"],
                "logged_in_at": row["logged_in_at"].isoformat(),
            })
        for slot, rt in self.runtimes.items():
            if slot not in seen:  # сессия не дошла до записи в базу: занята или недействительна
                out.append({"role": slot, "account_id": None, "tg_user_id": None, "label": None,
                            "status": rt.status, "error": rt.error, "paused": False, "can_send": False,
                            "auto_personal": False, "auto_groups": False, "backfill_months": None,
                            "logged_in_at": None})
        return out

    async def logout(self, account_id: int) -> dict[str, Any]:
        """Завершает сессию в Telegram и удаляет её файл. Архив остаётся."""
        self.require_configured()
        slot = (await self._slot_of(account_id))["slot"]
        rt = self.runtimes.pop(slot, None)
        if rt is not None and rt.client is not None:
            await self._shutdown(rt, logout=True)
            terminated = rt.terminated
        else:
            if rt is not None:
                await self._shutdown(rt)
            terminated = await self._logout_offline(slot)
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM tg_sessions WHERE account_id = $1", account_id)
        logger.info("аккаунт %s (%s): выход, сессия на стороне Telegram %s",
                    account_id, slot, "завершена" if terminated else "не подтверждена")
        return {"ok": True, "terminated": terminated}

    async def _logout_offline(self, slot: str) -> bool:
        """Выход из аккаунта, который сейчас не подключён (пауза, сбой)."""
        path = session_path(self.config, slot)
        if not path.exists():
            return False
        rt = AccountRuntime(slot=slot)
        try:
            await self._open(slot, rt, login=False)
        except SessionLocked:
            raise TgError("Сессия занята другим процессом — выйти из неё отсюда нельзя.", 409) from None
        except NotConfigured as exc:
            raise TgError(str(exc), 503) from None
        terminated = False
        try:
            await rt.client.connect()
            if await rt.client.get_me() is not None:
                terminated = bool(await rt.client.log_out())
        except Exception as exc:
            logger.warning("аккаунт (%s): завершить сессию в Telegram не удалось (%s)", slot, type(exc).__name__)
        finally:
            with contextlib.suppress(Exception):
                await rt.client.disconnect()
            remove_session_file(path)
            await rt.lock.release()
        return terminated

    async def pause(self, account_id: int) -> None:
        slot = (await self._slot_of(account_id))["slot"]
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE tg_sessions SET paused = true WHERE account_id = $1", account_id)
        rt = self.runtimes.pop(slot, None)
        if rt is not None:
            await self._shutdown(rt)

    async def resume(self, account_id: int) -> None:
        self.require_configured()
        slot = (await self._slot_of(account_id))["slot"]
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE tg_sessions SET paused = false WHERE account_id = $1", account_id)
        current = self.runtimes.get(slot)
        if current is not None and current.status in ACTIVE:
            return
        if current is not None:
            await self._shutdown(current)
        if not session_path(self.config, slot).exists():
            self.runtimes.pop(slot, None)
            raise TgError("Файла сессии нет: войдите в аккаунт заново.", 409)
        await self._launch(slot)

    async def set_options(
        self, account_id: int, *, auto_personal: bool | None, auto_groups: bool | None,
        backfill_months: Any = KEEP,
    ) -> None:
        """Настройки аккаунта.

        «Брать новые личные чаты / группы»: при включении все нынешние диалоги запоминаются как
        уже существующие — настройка касается только чатов, появившихся позже.
        `backfill_months` — глубина истории для чатов, которые включат после этого: число месяцев
        или None (вся история). Уже включённых чатов не касается."""
        await self._slot_of(account_id)
        rt = self._by_account(account_id)
        if auto_personal or auto_groups:
            if rt is None or rt.status != RUNNING:
                raise TgError("Аккаунт не подключён: включить настройку можно только на работающем аккаунте.", 409)
            dialogs = await self.dialogs(account_id, refresh=True)
            async with self.pool.acquire() as conn:
                await sync.mark_seen(conn, account_id, [d.key for d in dialogs])
        async with self.pool.acquire() as conn:
            await conn.execute(
                """UPDATE tg_sessions SET auto_personal = COALESCE($2, auto_personal),
                                          auto_groups = COALESCE($3, auto_groups),
                                          backfill_months = CASE WHEN $4 THEN $5 ELSE backfill_months END
                   WHERE account_id = $1""",
                account_id, auto_personal, auto_groups, backfill_months is not KEEP,
                None if backfill_months is KEEP else backfill_months)
        if rt is not None and rt.live is not None:
            await rt.live.reload()

    # ------------------------------------------------------------------ диалоги и выбор чатов

    async def dialogs(self, account_id: int, *, refresh: bool = False) -> list[DialogInfo]:
        try:
            rt = self._running(account_id)
        except gateway.AccountUnavailable:
            raise TgError("Аккаунт не подключён: список чатов недоступен.", 409) from None
        age = time.monotonic() - rt.dialogs_at
        if rt.dialogs is None or age > DIALOGS_TTL or (refresh and age > DIALOGS_MIN_GAP):
            waiting = rt.pacer.flood_remaining()
            if waiting > 5:   # запрос экрана не должен висеть всё время ожидания
                raise TgError(f"Telegram просит подождать {int(waiting)} с. Попробуйте позже.", 429)
            try:
                await self._load_dialogs(rt)
            except gateway.FloodWait as exc:
                raise TgError(f"Telegram просит подождать {exc.seconds} с. Попробуйте позже.", 429) from None
            except sync.Stopped:
                raise TgError("Аккаунт останавливается.", 409) from None
        return rt.dialogs or []

    async def _load_dialogs(self, rt: AccountRuntime) -> list[DialogInfo]:
        async with rt.dialogs_lock:
            await rt.pacer.wait(rt.stop)
            items: list[DialogInfo] = []
            try:
                async for dialog in rt.client.iter_dialogs():
                    chat = normalize.chat_record(dialog.entity, self_id=rt.self_id)
                    if chat is not None:
                        items.append(DialogInfo(chat, int(getattr(dialog.dialog, "top_message", 0) or 0)))
            except sync.FLOOD as exc:
                rt.pacer.flood(exc.seconds)
                raise gateway.FloodWait(exc.seconds) from None
            rt.dialogs, rt.dialogs_at = items, time.monotonic()
            # Список диалогов наполнил Telethon ключами доступа — чаты, которые не удавалось
            # опросить из-за их отсутствия, можно пробовать снова.
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE tg_sync_chats SET last_error = NULL WHERE account_id = $1 AND last_error = 'entity_unknown'",
                    rt.account_id)
            return items

    async def _tops(self, rt: AccountRuntime) -> dict[PeerKey, int] | None:
        return {d.key: d.top_message for d in await self._load_dialogs(rt)}

    async def list_dialogs(
        self, account_id: int, *, offset: int = 0, limit: int = 100,
        chat_type: str | None = None, refresh: bool = False,
    ) -> dict[str, Any]:
        dialogs = await self.dialogs(account_id, refresh=refresh)
        if chat_type:
            dialogs = [d for d in dialogs if d.chat.type == chat_type]
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT p.class, p.tg_id, c.excluded, COALESCE(s.enabled, false) AS enabled
                   FROM chats c JOIN peers p ON p.id = c.peer_id
                   LEFT JOIN tg_sync_chats s ON s.chat_id = c.id
                   WHERE c.account_id = $1""", account_id)
        known = {(r["class"], r["tg_id"]): r for r in rows}
        items = []
        for d in dialogs[offset:offset + limit]:
            row = known.get(d.key)
            blocked = store.is_blocked_peer(d.chat.peer_class, d.chat.tg_id, d.chat.username)
            items.append({
                "peer_class": d.chat.peer_class, "tg_id": d.chat.tg_id, "type": d.chat.type,
                "title": d.chat.name, "username": d.chat.username,
                "enabled": bool(row and row["enabled"]),
                "excluded": blocked or bool(row and row["excluded"]),
                # В каналах и супергруппах номера сообщений свои — номер последнего близок к их
                # числу. В личных чатах и обычных группах нумерация общая на аккаунт.
                "message_estimate": d.top_message if d.chat.peer_class == "channel" else None,
            })
        return {"total": len(dialogs), "offset": offset, "items": items}

    async def set_sync(
        self, account_id: int, *, enabled: bool,
        chats: list[PeerKey] | None = None, chat_types: list[str] | None = None,
        since: Any = sync.ACCOUNT_DEFAULT,
    ) -> list[dict[str, Any]]:
        """Включает или выключает синхронизацию чатов — поштучно или всех чатов заданных видов.

        `since` — граница загрузки истории вглубь для включаемых чатов: дата, None (вся история)
        или не названа (глубина по умолчанию из настроек аккаунта)."""
        await self._slot_of(account_id)
        rt = self._by_account(account_id)
        by_key: dict[PeerKey, DialogInfo] = {}
        if enabled or chat_types:
            by_key = {d.key: d for d in await self.dialogs(account_id)}
        keys: list[PeerKey] = list(chats or [])
        if chat_types:
            keys += [k for k, d in by_key.items() if d.chat.type in chat_types and k not in keys]
        out = []
        async with self.pool.acquire() as conn:
            for key in keys:
                if not enabled:
                    await sync.disable_chat(conn, account_id, key)
                    out.append({"peer_class": key[0], "tg_id": key[1], "enabled": False})
                    continue
                dialog = by_key.get(key)
                if dialog is None:
                    out.append({"peer_class": key[0], "tg_id": key[1], "enabled": False,
                                "error": "Чат не найден среди диалогов аккаунта."})
                    continue
                _, on = await sync.enable_chat(conn, account_id, dialog.chat, since=since)
                item = {"peer_class": key[0], "tg_id": key[1], "enabled": on}
                if not on:
                    item["excluded"] = True
                    item["error"] = "Чат исключён из архива и не синхронизируется."
                out.append(item)
        if rt is not None and rt.live is not None:
            await rt.live.reload()
            rt.wake.set()
        return out

    async def sync_status(self, account_id: int) -> dict[str, Any]:
        """Состояние синхронизации: счётчики и курсоры, без названий и содержимого."""
        await self._slot_of(account_id)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT s.peer_class, s.tg_id, s.chat_id, s.enabled, s.auto_enabled, s.backfill_before,
                          s.backfill_done, s.backfill_since, s.forward_id, s.gap_checked_at, s.reconciled_at,
                          s.access_lost_at, s.access_lost_reason, s.last_error,
                          (SELECT count(*) FROM messages m WHERE m.chat_id = s.chat_id) AS messages
                   FROM tg_sync_chats s WHERE s.account_id = $1 AND s.chat_id IS NOT NULL
                   ORDER BY s.peer_class, s.tg_id""", account_id)
        rt = self._by_account(account_id)

        def iso(value: Any) -> str | None:
            return value.isoformat() if value is not None else None

        chats = [{
            "peer_class": r["peer_class"], "tg_id": r["tg_id"], "chat_id": r["chat_id"],
            "enabled": r["enabled"], "auto_enabled": r["auto_enabled"], "messages": r["messages"],
            "backfill_before": r["backfill_before"], "backfill_done": r["backfill_done"],
            "backfill_since": iso(r["backfill_since"]),
            "forward_id": r["forward_id"], "gap_checked_at": iso(r["gap_checked_at"]),
            "reconciled_at": iso(r["reconciled_at"]), "access_lost_at": iso(r["access_lost_at"]),
            "access_lost_reason": r["access_lost_reason"], "last_error": r["last_error"],
        } for r in rows]
        on = [c for c in chats if c["enabled"]]
        return {
            "account_id": account_id,
            "status": rt.status if rt is not None else DISCONNECTED,
            "busy": rt.history.busy if rt is not None and rt.history is not None else None,
            "flood_wait_until": iso(rt.pacer.flood_until) if rt is not None else None,
            "counts": {
                "enabled": len(on),
                "backfill_done": sum(1 for c in on if c["backfill_done"]),
                "backfill_pending": sum(1 for c in on if not c["backfill_done"] and not c["access_lost_at"]),
                "access_lost": sum(1 for c in on if c["access_lost_at"]),
                "errors": sum(1 for c in on if c["last_error"]),
                "messages": sum(c["messages"] for c in chats),
            },
            "chats": chats,
        }

    # ------------------------------------------------------------------ шлюз (tg/gateway.py)

    def can_send(self, account_id: int) -> bool:
        rt = self._by_account(account_id)
        return bool(self.config.sending and rt is not None and rt.role == "assistant"
                    and rt.status == RUNNING and rt.client is not None
                    and rt.policy is not None and rt.policy.can_send)

    async def _role_of(self, account_id: int) -> str | None:
        rt = self._by_account(account_id)
        if rt is not None:
            return rt.role
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT role FROM accounts WHERE id = $1", account_id)

    async def send_text(
        self, account_id: int, peer_class: str, tg_id: int, text: str, *,
        reply_to_tg_id: int | None = None,
    ) -> int:
        # Роль проверяется первой, до любого обращения к клиенту: у основного аккаунта
        # владельца пути отправки нет.
        role = await self._role_of(account_id)
        if role != "assistant":
            raise gateway.SendForbidden(
                "отправка от этого аккаунта запрещена: это не аккаунт-помощник")
        # Главный выключатель — тоже до любого обращения к клиенту.
        if not self.config.sending:
            raise gateway.SendForbidden(
                "отправка выключена в настройках сервиса (SHTURMAN_SENDING): сервис ничего не отправляет")
        rt = self._running(account_id)
        if rt.policy is None or not rt.policy.can_send:
            raise gateway.SendForbidden("отправка от этого аккаунта запрещена")
        if not isinstance(text, str) or not text.strip() or bridge.utf16_len(text) > TEXT_LIMIT:
            raise ValueError(f"текст сообщения должен быть непустым и не длиннее {TEXT_LIMIT} знаков")
        key: PeerKey = (peer_class, int(tg_id))
        peer = await rt.client.get_input_entity(normalize.to_peer(key))
        request = functions.messages.SendMessageRequest(
            peer=peer, message=text, no_webpage=True,
            reply_to=types.InputReplyToMessage(reply_to_msg_id=int(reply_to_tg_id)) if reply_to_tg_id else None,
        )
        try:
            result = await rt.client(request)
        except (errors.FloodWaitError, errors.SlowModeWaitError) as exc:
            raise gateway.FloodWait(exc.seconds) from None
        except (errors.UnauthorizedError, errors.AuthKeyError, ConnectionError) as exc:
            raise gateway.AccountUnavailable(f"сессия аккаунта недоступна ({type(exc).__name__})") from None
        message_id, message, entities = _sent_message(result, key, text, rt.self_id, reply_to_tg_id)
        # Свои же отправки Telethon обработчикам не раздаёт — записываем сами, если чат выбран.
        state = rt.live.index.get(key) if rt.live is not None else None
        if message is not None and state is not None and state.enabled and state.chat_id is not None:
            record = normalize.message_record(message, entities, self_id=rt.self_id)
            if record is not None:
                try:
                    await rt.live.save(key, state.chat_id, record, entities,
                                       outgoing=True, edited=False, via_bot=False)
                except Exception as exc:  # сообщение уже ушло: сбой записи не должен выглядеть как сбой отправки
                    logger.error("аккаунт %s: отправленное сообщение не записано в архив (%s)",
                                 account_id, type(exc).__name__)
        return message_id

    async def set_typing(self, account_id: int, peer_class: str, tg_id: int, on: bool) -> None:
        if not self.can_send(account_id):
            return  # основной аккаунт владельца ничем не выдаёт своё присутствие
        try:
            rt = self._running(account_id)
            peer = await rt.client.get_input_entity(normalize.to_peer((peer_class, int(tg_id))))
            action = types.SendMessageTypingAction() if on else types.SendMessageCancelAction()
            await rt.client(functions.messages.SetTypingRequest(peer=peer, action=action))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # индикатор не важнее ответа
