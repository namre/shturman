"""Клиент Telegram с перечнем разрешённых запросов.

На уровне протокола Telegram не умеет «только чтение»: любая сессия пользователя может всё.
Поэтому ограничение живёт в процессе. Каждый запрос Telethon проходит через
`TelegramClient._call`; подкласс сверяет класс запроса с перечнем и по умолчанию отказывает.

  * роль owner (основной аккаунт владельца) — только чтение: состояние обновлений, диалоги,
    история, сообщения по номерам. Отправка, отметка прочитанным, удаление, «печатает…»,
    любые изменения аккаунта — отказ;
  * роль assistant (помощник) — то же чтение плюс ровно два запроса шлюза отправки:
    отправить текст и показать «печатает…» — и только когда в окружении сервиса включён
    главный выключатель отправки;
  * запросы входа по QR и облачного пароля разрешены только пока идёт вход.

Перечень не закрывает одного: код в том же процессе может обратиться к `client._sender`
в обход `_call`. От этого защищает правило сервиса — остальные модули не получают клиента,
только шлюз (`tg/gateway.py`).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlsplit

from telethon import TelegramClient
from telethon.sessions import SQLiteSession
from telethon.tl import functions

from .. import __version__
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
    functions.messages.GetHistoryRequest,        # история чата
    functions.messages.GetMessagesRequest,       # сообщения по номерам (сверка удалений)
    functions.channels.GetMessagesRequest,
    functions.PingRequest,
    functions.auth.LogOutRequest,                # выход: завершает эту сессию, больше ничего
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
        return self.can_send and cls in SEND_REQUESTS

    def check(self, request: Any) -> None:
        items: Iterable[Any] = request if isinstance(request, (list, tuple)) else (request,)
        for item in items:
            if not self.allows(item):
                inner = item
                while type(inner) in _WRAPPERS:
                    inner = getattr(inner, "query", None)
                raise RequestNotAllowed(type(inner).__name__, self.role)


class GuardedClient(TelegramClient):
    """`TelegramClient`, который не отправит запрос вне перечня своей роли."""

    def __init__(self, session: Any, api_id: int, api_hash: str, *, policy: RequestPolicy,
                 on_reconnect: Callable[[], None] | None = None, **kwargs: Any) -> None:
        self._shturman_policy = policy
        self._shturman_on_reconnect = on_reconnect
        super().__init__(session, api_id, api_hash, **kwargs)

    @property
    def policy(self) -> RequestPolicy:
        return self._shturman_policy

    async def __call__(self, request: Any, ordered: bool = False, flood_sleep_threshold: int | None = None) -> Any:
        self._shturman_policy.check(request)
        return await super().__call__(request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold)

    async def _call(self, sender: Any, request: Any, ordered: bool = False,
                    flood_sleep_threshold: int | None = None) -> Any:
        # Узкое место Telethon: сюда приходят и запросы приложения, и его собственные.
        self._shturman_policy.check(request)
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
