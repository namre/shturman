"""Клиент Telegram с перечнем разрешённых запросов.

На уровне протокола Telegram не умеет «только чтение»: любая сессия пользователя может всё.
Поэтому ограничение живёт в процессе. Каждый запрос Telethon проходит через
`TelegramClient._call`; подкласс сверяет класс запроса с перечнем и по умолчанию отказывает.

  * роль owner (основной аккаунт владельца) — чтение: состояние обновлений, диалоги, история,
    сообщения по номерам. Отправка, отметка прочитанным, удаление, «печатает…», любые изменения
    аккаунта — отказ. Одна запись (Р-105): облачный черновик в поле ввода личного чата
    (`messages.saveDraft`) — только «взведённым» на этот самый запрос и один раз (`arming(request)`,
    его взводит единственное место — `TgManager.put_draft` по нажатию владельца «В чат»), только
    собеседнику-человеку и только видимым простым текстом без вложений, разметки и цитат;
  * роль assistant (помощник) — то же чтение плюс ровно два запроса шлюза отправки:
    отправить текст и показать «печатает…» — и только когда в окружении сервиса включён
    главный выключатель отправки;
  * запросы входа по QR и облачного пароля разрешены только пока идёт вход.

Перечень не закрывает одного: код в том же процессе может обратиться к `client._sender`
в обход `_call`. От этого защищает правило сервиса — остальные модули не получают клиента,
только шлюз (`tg/gateway.py`).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
import random
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlsplit

from telethon import TelegramClient, errors
from telethon.sessions import SQLiteSession
from telethon.tl import functions, types

from .. import __version__
from .. import authority as _authority
from ..config import Config

logger = logging.getLogger("shturman.tg")

ROLES = ("owner", "assistant")

# Журнал Telethon: на уровне DEBUG он печатает подробности обмена. Глубже WARNING не пускаем,
# какой бы уровень ни стоял у всего сервиса.
_telethon_log = logging.getLogger("shturman.tg.telethon")
_telethon_log.setLevel(logging.WARNING)


class RequestNotAllowed(PermissionError):
    """Запрос к Telegram не входит в перечень разрешённых для роли аккаунта."""

    def __init__(self, request_name: str, role: str) -> None:
        super().__init__(f"запрос {request_name} запрещён для роли {role}")
        self.request_name, self.role = request_name, role


class NotConfigured(RuntimeError):
    """Работа с Telegram не настроена: нет ключей приложения или прокси нечем обслужить."""


# Чтение и служебные запросы самого Telethon. Ничего из этого не меняет состояние аккаунта,
# не отмечает прочитанным и не видно собеседникам.
READ_REQUESTS: frozenset[type] = frozenset({
    functions.help.GetConfigRequest,             # выбор дата-центра при подключении
    functions.updates.GetStateRequest,           # состояние потока обновлений
    functions.updates.GetDifferenceRequest,      # пропущенные обновления
    functions.updates.GetChannelDifferenceRequest,
    functions.users.GetUsersRequest,             # «кто я» и сведения о собеседнике
    functions.messages.GetChatsRequest,
    functions.channels.GetChannelsRequest,
    functions.messages.GetDialogsRequest,        # список чатов для экрана выбора
    # Папки Telegram (их описание: названия, явные перечни чатов, правила) — для раскладки «Папки» и
    # «Сделать сферу из папки» (Р-82). Только чтение: папки не меняются и не создаются, собеседники
    # ничего не видят. Читается той же фоновой загрузкой, что список диалогов, одним запросом.
    functions.messages.GetDialogFiltersRequest,
    functions.messages.GetHistoryRequest,        # история чата
    # Один диалог по собеседнику — его облачный черновик (поле ввода) перед «В чат» (Р-105): записывать
    # поверх текста владельца нельзя. Только чтение, прочитанным ничего не отмечает.
    functions.messages.GetPeerDialogsRequest,
    functions.messages.GetMessagesRequest,       # сообщения по номерам (сверка удалений)
    functions.channels.GetMessagesRequest,
    functions.PingRequest,
    functions.auth.LogOutRequest,                # выход: завершает эту сессию, больше ничего
    # Скачивание голосового или «кружка» для расшифровки (voice/): сам файл частями и, если он
    # лежит в другом дата-центре, перенос авторизации этой же сессии туда — так делает любой
    # клиент Telegram при открытии вложения. Прочитанным это ничего не отмечает.
    functions.upload.GetFileRequest,
    functions.auth.ExportAuthorizationRequest,
    functions.auth.ImportAuthorizationRequest,
})

# Вход по QR и облачный пароль. Запроса кода по номеру телефона здесь нет намеренно.
LOGIN_REQUESTS: frozenset[type] = frozenset({
    functions.auth.ExportLoginTokenRequest,
    functions.auth.ImportLoginTokenRequest,
    functions.account.GetPasswordRequest,
    functions.auth.CheckPasswordRequest,
})

# Единственное, что помощнику разрешено сверх чтения.
SEND_REQUESTS: frozenset[type] = frozenset({
    functions.messages.SendMessageRequest,
    functions.messages.SetTypingRequest,
})

# Единственная запись основного аккаунта (Р-105): облачный черновик — текст в поле ввода чата, который
# владелец сам правит и отправляет. Проходит только «взведённым» (`arming()`) и только простым текстом.
OWNER_WRITE: frozenset[type] = frozenset({
    functions.messages.SaveDraftRequest,
})
DRAFT_TEXT_LIMIT = 4096


@dataclass
class _Arm:
    """Взвод на один запрос: проходит только этот самый объект и только до первой отдачи в сеть."""
    request: Any
    used: bool = False


_armed: contextvars.ContextVar[_Arm | None] = contextvars.ContextVar("shturman_draft_armed", default=None)
# Фоновая задача, запущенная внутри взведённого вызова (подключение Telethon, переход в другой дата-центр),
# взвод не наследует (`authority.background_context`).
_authority.register_transient(_armed)


@contextlib.contextmanager
def arming(request: Any):
    """Взвести запись облачного черновика — ровно этого запроса, один раз. Зовёт только `TgManager.put_draft`."""
    token = _armed.set(_Arm(request))
    try:
        yield
    finally:
        _armed.reset(token)


def _consume(request: Any) -> None:
    """Запрос ушёл в сеть: взвод израсходован, второй раз он не пропустит ничего."""
    inner = request
    while type(inner) in _WRAPPERS:
        inner = getattr(inner, "query", None)
    arm = _armed.get()
    if arm is not None and arm.request is inner:
        arm.used = True


_INVISIBLE = ("Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp", "Zs")


def _visible(text: str) -> bool:
    """Есть хоть один видимый знак: пустой или «невидимый» черновик стёр бы поле ввода владельца."""
    return any(unicodedata.category(ch) not in _INVISIBLE for ch in text)


def _reply_to_ok(reply_to: Any) -> bool:
    """Ответ — только на сообщение того же чата, без цитаты, темы, истории и другого собеседника."""
    if reply_to is None:
        return True
    if type(reply_to) is not types.InputReplyToMessage:
        return False
    number = getattr(reply_to, "reply_to_msg_id", None)
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return False
    return all(getattr(reply_to, name, None) is None for name in (
        "top_msg_id", "reply_to_peer_id", "quote_text", "quote_entities", "quote_offset", "monoforum_peer_id",
        "todo_item_id", "poll_option"))


def _draft_shape_ok(request: Any) -> bool:
    """Облачный черновик — только видимый простой текст человеку, без вложений, разметки, эффектов, цитат
    и предпросмотра ссылок."""
    message = getattr(request, "message", None)
    return (isinstance(message, str) and bool(message.strip()) and _visible(message)
            and len(message.encode("utf-16-le")) // 2 <= DRAFT_TEXT_LIMIT
            and type(getattr(request, "peer", None)) is types.InputPeerUser
            and _reply_to_ok(getattr(request, "reply_to", None))
            and getattr(request, "no_webpage", None) is True
            and not getattr(request, "invert_media", None)
            and getattr(request, "media", None) is None and getattr(request, "entities", None) is None
            and getattr(request, "effect", None) is None and getattr(request, "suggested_post", None) is None
            and getattr(request, "rich_message", None) is None)


# Запросов Mini App (`messages.RequestWebView`, `RequestSimpleWebView`, `RequestMainWebView`,
# `RequestAppWebView`, `ProlongWebView` и любых других `*WebView*`) в перечнях нет и быть не
# должно: ими клиент Telegram получает подписанные данные входа (`initData`) для приложения бота.
# С сессией основного аккаунта сервис сам выпустил бы данные входа владельца в приложение
# «Штурман» (setup_page/miniapp.py) — то есть нажимал бы кнопки бота согласований за него.
# Тест tests/tg/test_tg_policy.py это сверяет.

# Обёртки: проверяется то, что внутри.
_WRAPPERS: tuple[type, ...] = (
    functions.InvokeWithoutUpdatesRequest,
    functions.InvokeWithLayerRequest,
    functions.InitConnectionRequest,
    functions.InvokeAfterMsgRequest,
    functions.InvokeAfterMsgsRequest,
)


class RequestPolicy:
    """Перечень разрешённых запросов для роли. Всё, чего в перечне нет, запрещено."""

    def __init__(self, role: str, *, login: bool = False, sending: bool = True) -> None:
        if role not in ROLES:
            raise ValueError(f"неизвестная роль аккаунта: {role!r}")
        self.role = role
        # Главный выключатель отправки сервиса (config.sending): выключен — запросы отправки
        # не проходят и у помощника.
        self.sending = sending
        # True — идёт вход; после входа выключается и запросы входа перестают проходить.
        self.login = login

    @property
    def can_send(self) -> bool:
        return self.role == "assistant" and self.sending

    def allows(self, request: Any) -> bool:
        cls = type(request)
        if cls in _WRAPPERS:
            return self.allows(getattr(request, "query", None))
        if cls in READ_REQUESTS:
            return True
        if self.login and cls in LOGIN_REQUESTS:
            return True
        if self.role == "owner" and cls in OWNER_WRITE:
            arm = _armed.get()
            return arm is not None and not arm.used and arm.request is request and _draft_shape_ok(request)
        return self.can_send and cls in SEND_REQUESTS

    def check(self, request: Any) -> None:
        items: Iterable[Any] = request if isinstance(request, (list, tuple)) else (request,)
        for item in items:
            if not self.allows(item):
                inner = item
                while type(inner) in _WRAPPERS:
                    inner = getattr(inner, "query", None)
                raise RequestNotAllowed(type(inner).__name__, self.role)


# --- фоновые запросы Telethon по каналам ---
#
# Telethon следит за каждым каналом и супергруппой, о которых узнал (список диалогов сообщает
# ему их состояние). Если по каналу 15 минут не было обновлений, он запрашивает разницу
# (`updates.GetChannelDifference`) — по всем таким каналам сразу, подряд, без пауз. У аккаунта
# с десятками каналов это пачка из десятков запросов за доли секунды, круглые сутки раз в
# 15 минут; Telegram отвечает на неё FLOOD_WAIT (замечено на рабочем экземпляре: пачка отказов за
# полсекунды). Сервису разница нужна только по выбранным чатам — остальное он не сохраняет.
#
# Поэтому запрос разницы по каналу:
#   * по невыбранному каналу в Telegram не уходит: Telethon получает ответ «разницы нет» с
#     прежним номером состояния и вернётся к каналу через 15 минут (опять без запроса). Живые
#     сообщения такого канала по-прежнему приходят — их Telegram присылает сам;
#   * по выбранному — уходит с паузой не меньше CHANNEL_DIFF_GAP (со случайной добавкой);
#   * пока Telegram велит ждать, в Telegram не уходит ничего — тот же ответ «разницы нет».
#     Пропущенное по выбранным чатам дочитывает загрузка истории (sync.HistorySync).

CHANNEL_DIFF_GAP = 2.0
CHANNEL_DIFF_JITTER = (1.0, 1.8)


# Список диалогов Telethon читает страницами по 100 (`messages.GetDialogs`) подряд, без пауз;
# у большого аккаунта это несколько запросов за секунду. Между страницами — пауза.
DIALOGS_PAGE_GAP = 1.5


class Spacer:
    """Не чаще одного запроса в `gap` секунд (со случайной добавкой до 80 %)."""

    def __init__(self, gap: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep, rng: random.Random | None = None) -> None:
        self.gap = gap
        self._clock, self._sleep = clock, sleep
        self._rng = rng or random.SystemRandom()
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def run(self, send: Callable[[], Any]) -> Any:
        async with self._lock:
            now = self._clock()
            if now < self._next_at:
                await self._sleep(self._next_at - now)
            try:
                return await send()
            finally:
                self._next_at = self._clock() + self.gap * self._rng.uniform(*CHANNEL_DIFF_JITTER)


class ChannelUpdatesThrottle:
    """Решает, уходит ли запрос разницы по каналу в Telegram, и держит паузы между ними.

    `wanted(channel_id)` — выбран ли канал владельцем; не задан — не выбран никакой
    (так до запуска приёма и во время входа)."""

    def __init__(self, role: str, *, gap: float = CHANNEL_DIFF_GAP,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep,
                 rng: random.Random | None = None) -> None:
        self.role = role
        self.gap = gap
        self.wanted: Callable[[int], bool] | None = None
        self._clock, self._sleep = clock, sleep
        self._rng = rng or random.SystemRandom()
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self.flood_until = 0.0
        self.skipped = 0     # сколько запросов не ушло: невыбранный канал или ожидание

    def allowed(self, channel_id: int) -> bool:
        wanted = self.wanted
        if wanted is None:
            return False
        try:
            return bool(wanted(int(channel_id)))
        except Exception:
            return False

    @staticmethod
    def empty(request: Any) -> Any:
        """Ответ «разницы нет» с прежним номером состояния: Telethon ничего не меняет и
        откладывает канал на обычные 15 минут."""
        return types.updates.ChannelDifferenceEmpty(pts=int(request.pts), final=True, timeout=None)

    async def run(self, request: Any, send: Callable[[], Any]) -> Any:
        if not self.allowed(request.channel.channel_id):
            self.skipped += 1
            return self.empty(request)
        async with self._lock:
            now = self._clock()
            if now < self.flood_until:
                self.skipped += 1
                return self.empty(request)
            if now < self._next_at:
                await self._sleep(self._next_at - now)
            try:
                return await send()
            except errors.FloodError as exc:
                seconds = int(getattr(exc, "seconds", 0) or 0)
                self.flood_until = self._clock() + max(1, seconds) + 1
                logger.warning("аккаунт (%s): Telegram просит подождать %s с (обновления каналов); "
                               "до тех пор запросы по каналам не отправляются", self.role, seconds)
                raise
            finally:
                self._next_at = self._clock() + self.gap * self._rng.uniform(*CHANNEL_DIFF_JITTER)


class GuardedClient(TelegramClient):
    """`TelegramClient`, который не отправит запрос вне перечня своей роли."""

    def __init__(self, session: Any, api_id: int, api_hash: str, *, policy: RequestPolicy,
                 on_reconnect: Callable[[], None] | None = None, **kwargs: Any) -> None:
        self._shturman_policy = policy
        self._shturman_on_reconnect = on_reconnect
        self._shturman_channels = ChannelUpdatesThrottle(policy.role)
        self._shturman_dialogs = Spacer(DIALOGS_PAGE_GAP)
        super().__init__(session, api_id, api_hash, **kwargs)

    @property
    def policy(self) -> RequestPolicy:
        return self._shturman_policy

    @property
    def channel_updates(self) -> ChannelUpdatesThrottle:
        return self._shturman_channels

    def set_channel_filter(self, wanted: Callable[[int], bool] | None) -> None:
        """Какие каналы выбраны: по остальным Telethon разницу в Telegram не запрашивает."""
        self._shturman_channels.wanted = wanted

    async def __call__(self, request: Any, ordered: bool = False, flood_sleep_threshold: int | None = None) -> Any:
        self._shturman_policy.check(request)
        if isinstance(request, functions.updates.GetChannelDifferenceRequest):
            # Так запрашивает разницу цикл обновлений Telethon (client/updates.py: `self(get_diff)`).
            return await self._shturman_channels.run(request, lambda: super(GuardedClient, self).__call__(
                request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold))
        if isinstance(request, (functions.messages.GetDialogsRequest, functions.messages.GetDialogFiltersRequest)):
            # папки читаются той же загрузкой, что страницы списка, — с теми же паузами
            return await self._shturman_dialogs.run(lambda: super(GuardedClient, self).__call__(
                request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold))
        return await super().__call__(request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold)

    async def _call(self, sender: Any, request: Any, ordered: bool = False,
                    flood_sleep_threshold: int | None = None) -> Any:
        # Узкое место Telethon: сюда приходят и запросы приложения, и его собственные.
        self._shturman_policy.check(request)
        _consume(request)
        return await super()._call(sender, request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold)

    async def _handle_auto_reconnect(self) -> None:
        # Telethon после переподключения пропущенное не запрашивает — сообщаем наверх,
        # там вызовут catch_up() и дочитают историю чатов.
        await super()._handle_auto_reconnect()
        if self._shturman_on_reconnect is not None:
            try:
                self._shturman_on_reconnect()
            except Exception:
                logger.warning("обработчик переподключения завершился с ошибкой")


# --- файл сессии ---

def session_path(config: Config, slot: str) -> Path:
    if slot not in ROLES:
        raise ValueError(f"неизвестная роль аккаунта: {slot!r}")
    return config.sessions_dir / f"{slot}.session"


def prepare_session_file(path: Path) -> None:
    """Каталог сессий — только владельцу процесса (0700), файл сессии — 0600.

    Файл создаётся заранее с нужными правами: SQLite откроет уже существующий и не
    успеет создать его со стандартной маской."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)


def remove_session_file(path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.unlink(str(path) + suffix)
        except FileNotFoundError:
            pass


# --- прокси ---

def parse_proxy(url: str) -> dict[str, Any] | None:
    """Адрес прокси вида socks5://[имя:пароль@]узел:порт (также socks4, http) → настройка Telethon.

    Прокси обслуживает пакет python-socks, тот же, что нужен самому Telethon. Если прокси задан,
    а пакета нет, сервис не подключается напрямую, а отказывается работать: обойти заданный
    владельцем выход в сеть хуже, чем не подключиться.
    """
    if not url:
        return None
    parts = urlsplit(url)
    scheme = parts.scheme.lower().replace("socks5h", "socks5")
    if scheme not in ("socks5", "socks4", "http") or not parts.hostname or not parts.port:
        raise NotConfigured("EGRESS_PROXY_URL: нужен адрес вида socks5://узел:порт или http://узел:порт")
    try:
        import python_socks  # noqa: F401
    except ImportError:
        raise NotConfigured(
            "задан EGRESS_PROXY_URL, но не установлен пакет python-socks — "
            "без него Telegram через прокси недоступен"
        ) from None
    return {
        "proxy_type": scheme, "addr": parts.hostname, "port": parts.port,
        "username": unquote(parts.username) if parts.username else None,
        "password": unquote(parts.password) if parts.password else None,
        "rdns": True,
    }


ClientFactory = Callable[[str, Path, RequestPolicy, Callable[[], None] | None], Any]


def make_client_factory(config: Config) -> ClientFactory:
    """Фабрика настоящих клиентов. Тесты подставляют свою с тем же видом вызова:
    (роль, путь к файлу сессии, перечень запросов, обработчик переподключения) → клиент."""

    def factory(role: str, path: Path, policy: RequestPolicy, on_reconnect: Callable[[], None] | None) -> Any:
        if not config.tg_api_id or not config.tg_api_hash:
            raise NotConfigured("не заданы TELEGRAM_API_ID и TELEGRAM_API_HASH")
        proxy = parse_proxy(config.proxy_url)
        prepare_session_file(path)
        # Файловая сессия, а не строковая: в ней Telethon хранит состояние обновлений,
        # без него после перезапуска нечем запросить пропущенное.
        session = SQLiteSession(str(path))
        return GuardedClient(
            session, config.tg_api_id, config.tg_api_hash,
            policy=policy, on_reconnect=on_reconnect, proxy=proxy,
            # Ждать при ограничениях будет сервис, а не библиотека: так ожидание видно в
            # состоянии и прерывается при остановке.
            flood_sleep_threshold=0,
            request_retries=3, raise_last_call_error=True,
            auto_reconnect=True,
            receive_updates=True,   # нужно и для входа по QR
            catch_up=True,          # загрузить сохранённое состояние обновлений и догнать
            device_model="Shturman", app_version=__version__,
            lang_code="ru", system_lang_code="ru",
            base_logger=_telethon_log,
        )

    return factory
