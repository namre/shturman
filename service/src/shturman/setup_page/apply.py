"""Проверка значений, введённых на странице настройки, и их применение без перезапуска сервиса.

Как значение начинает действовать. Рассматривались два пути:

  * перезапуск процесса (контейнер поднимет Docker). Просто, но обрывает то, что идёт в эту
    минуту: загруженная и ещё не импортированная выгрузка удаляется при остановке, вход в
    аккаунт Telegram по QR теряется, а без Docker (стенд, тесты) сервис не поднимется вовсе;
  * применение на ходу — выбрано. Меняются ровно два модуля, и у обоих для этого есть
    собственная точка: исполнитель пересобирается целиком (`executor.service.Control.restart`,
    тот же код, что при запуске), модуль аккаунтов Telegram получает новые настройки
    (`TgManager.reconfigure`). Остальные модули от шести значений страницы не зависят.

При запуске сервис читает тот же файл (`config.with_page_values`), поэтому после перезапуска
всё остаётся как было применено.

Главный выключатель отправки (`config.sending`) здесь не меняется никогда: `overlay` трогает
только шесть полей страницы. Токен бота, введённый на странице, отправку не включает.

Перед сохранением токен бота и ключ модели проверяются живым запросом. Ошибка объясняется
простыми словами; само значение ни в текст ошибки, ни в журнал не попадает.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .. import netguard
from ..executor import service as executor_service
from ..executor.botapi import BotApi, BotApiError, NeverLeft, Refused
from ..executor.llm import LlmClient, LlmError
from . import secrets_store as ss

logger = logging.getLogger("shturman.setup")

SERVER, PAGE = "server", "page"
PROBE_SECONDS = 5           # сколько держится пробный запрос обновлений (см. probe_other_poller)

_API_HASH = re.compile(r"^[0-9a-f]{32}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")


class Invalid(Exception):
    """Значение не подходит. Текст — для владельца, простыми словами; самого значения в нём нет."""

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.message, self.code = message, code


class Settings:
    """Значения страницы внутри работающего сервиса: откуда каждое, запись и применение."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.store = ss.SecretStore(state.config.data_dir)
        self.lock = asyncio.Lock()
        config, stored = state.config, self.store.load()
        # Чем распоряжается страница: тем, что не задано окружением и что сервис при запуске
        # не получил откуда-то ещё (значение в настройках есть, а в файле его нет).
        self.managed = frozenset(
            name for name in ss.NAMES
            if name not in config.locked and (name in stored or name == ss.LLM_BASE_URL
                                              or not getattr(config, ss.FIELD[name])))

    def source(self, name: str) -> str | None:
        """server — задано в настройках сервера, страница не меняет; page — введено на странице."""
        if name not in self.managed:
            return SERVER
        return PAGE if self.store.has(name) else None

    def editable(self, *names: str) -> bool:
        return all(name in self.managed for name in names)

    async def save(self, changes: dict[str, str | None]) -> None:
        """Записывает значения в файл и применяет их к работающему сервису."""
        if not self.editable(*changes):
            raise Invalid("Это значение задано в настройках сервера. Изменить его можно только там.", "locked")
        async with self.lock:
            old = self.state.config
            tg = self.state.extras.get("tg")
            preview = ss.overlay(old, {**self.store.load(), **{k: v or "" for k, v in changes.items()}}, self.managed)
            keys_changed = (preview.tg_api_id, preview.tg_api_hash) != (old.tg_api_id, old.tg_api_hash)
            if keys_changed and tg is not None and await tg.has_sessions():
                raise Invalid("Сначала выйдите из подключённых аккаунтов Telegram: их сессии созданы "
                              "с прежними ключами приложения.", "accounts_connected")
            self.store.update(changes)
            new = ss.overlay(old, self.store.load(), self.managed)
            self.state.config = new
            if tg is not None:
                await tg.reconfigure(new)
            control = self.state.extras.get("executor_control")
            executor_changed = any(getattr(new, f) != getattr(old, f)
                                   for f in ("bot_token", "llm_api_key", "llm_base_url", "llm_model"))
            if control is not None and executor_changed:
                await control.restart()


# --- проверки ---

def clean(value: Any, *, limit: int = ss.MAX_VALUE) -> str:
    """Строка из запроса: без пробелов по краям и без управляющих знаков. Не строка — пусто."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if len(value) > limit or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise Invalid("В значении есть лишние знаки или оно слишком длинное. Скопируйте его ещё раз целиком.")
    return value


def check_tg_keys(api_id: Any, api_hash: Any) -> tuple[str, str]:
    """Ключи приложения Telegram: число и строка из 32 шестнадцатеричных знаков."""
    raw_id = str(api_id).strip() if isinstance(api_id, (str, int)) and not isinstance(api_id, bool) else ""
    if not raw_id.isdigit() or not 0 < int(raw_id) < 2**31:
        raise Invalid("api_id — это число из нескольких цифр со страницы my.telegram.org "
                      "(строка «App api_id»). Проверьте, что скопировали его целиком.", "bad_api_id")
    key = clean(api_hash).lower()
    if not _API_HASH.match(key):
        raise Invalid("api_hash — это строка из 32 знаков: цифр и латинских букв от a до f "
                      "(строка «App api_hash»). Проверьте, что скопировали её целиком и без пробелов.",
                      "bad_api_hash")
    return str(int(raw_id)), key


_BOT_PROBLEMS = {
    "bad_token_format": "Это не похоже на токен бота. Токен — длинная строка с двоеточием из сообщения "
                        "@BotFather, например 1234567890:AAH…; скопируйте её целиком.",
    "proxy_needs_socksio": "Сервис выходит в интернет через прокси SOCKS, а нужного для этого пакета в нём нет. "
                           "Это правится на сервере, а не здесь.",
    "bad_proxy_url": "Адрес прокси в настройках сервера записан неверно. Это правится на сервере, а не здесь.",
}
_NO_TELEGRAM = ("Не удалось связаться с Telegram. Проверьте, что у сервера есть доступ в интернет "
                "(или настроен прокси), и попробуйте ещё раз.")
OTHER_POLLER = ("Этим ботом уже пользуется другая программа. Скорее всего, это бот, с которым вы разговариваете "
                "с ассистентом, — его токен сюда не подходит: двум программам один бот отвечать не может. "
                "Создайте у @BotFather нового бота только для согласований и введите его токен.")
WEBHOOK = ("У этого бота включён приём сообщений по адресу (webhook) — значит, им уже пользуется другая "
           "программа. Создайте у @BotFather нового бота только для согласований и введите его токен.")


def _bot_api(config: Any, token: str) -> BotApi:
    return BotApi(token, proxy_url=config.proxy_url,
                  transport=executor_service.TEST_OVERRIDES.get("bot_transport"), timeout=15.0)


async def check_bot_token(config: Any, token: str) -> dict[str, Any]:
    """Спрашивает у Telegram, что это за бот (`getMe`). Возвращает {id, username, name, business}."""
    api = _bot_api(config, token)
    try:
        if api.broken:
            raise Invalid(_BOT_PROBLEMS.get(api.broken, _NO_TELEGRAM), api.broken)
        try:
            me = await api.get_me()
        except Refused as exc:
            if exc.code in (401, 404):
                raise Invalid("Telegram не принял этот токен. Скопируйте токен из сообщения @BotFather ещё раз "
                              "целиком; если вы недавно выпускали новый токен, прежний больше не действует.",
                              "token_rejected") from None
            if exc.code == 429:
                raise Invalid("Telegram просит подождать. Попробуйте через минуту.", "too_many_requests") from None
            raise Invalid(_NO_TELEGRAM, "refused") from None
        except BotApiError:
            raise Invalid(_NO_TELEGRAM, "no_connection") from None
    finally:
        await api.aclose()
    bot_id, username = me.get("id"), me.get("username")
    if isinstance(bot_id, bool) or not isinstance(bot_id, int) or not isinstance(username, str) or not username:
        raise Invalid(_NO_TELEGRAM, "bad_get_me")
    name = me.get("first_name") if isinstance(me.get("first_name"), str) else ""
    return {"id": bot_id, "username": username, "name": name[:64],
            "business": me.get("can_connect_to_business") is True}


async def probe_other_poller(config: Any, token: str) -> None:
    """Пробует понять, не опрашивает ли этого бота кто-то ещё, до того как сервис начнёт сам.

    Узнать это можно только по отказу 409 на запрос обновлений. Делается один запрос без
    подтверждения обновлений (без `offset`) и без смены их перечня (без `allowed_updates`),
    который держится несколько секунд: если бота опрашивает другая программа, её следующий
    запрос оборвёт наш, и Telegram ответит 409. Включённый webhook даёт 409 сразу.

    Проверка неполная: другая программа может не успеть переспросить за эти секунды. Тогда
    конфликт проявится уже в работе, и страница покажет его в состоянии бота.
    На настоящем Telegram эта проверка не запускалась (поведение — по документации Bot API).
    """
    seconds = int(executor_service.TEST_OVERRIDES.get("probe", PROBE_SECONDS))
    api = _bot_api(config, token)
    try:
        await api.call("getUpdates", {"timeout": seconds, "limit": 1}, timeout=seconds + 10.0)
    except Refused as exc:
        if exc.code == 409:
            raise Invalid(WEBHOOK if exc.reason == "webhook" else OTHER_POLLER,
                          "webhook" if exc.reason == "webhook" else "other_poller") from None
    except (BotApiError, NeverLeft):
        pass        # связь моргнула: getMe только что прошёл, сохранять можно
    finally:
        await api.aclose()


_BAD_URL = ("Адрес API должен выглядеть как https://api.openai.com/v1: начинаться с https:// и не содержать "
            "ничего после пути.")
_INNER_URL = ("Этот адрес ведёт внутрь сервера или в домашнюю сеть, а со страницы настройки можно указать только "
              "адрес в интернете. Локальную модель (Ollama и подобные) подключает оператор в настройках сервера: "
              "там задаётся SHTURMAN_LLM_BASE_URL.")
_NOT_HTTPS = ("Адрес API должен начинаться с https://. Адрес без шифрования (http://) со страницы настройки "
              "указать нельзя: ключ ушёл бы открытым текстом. Локальную модель подключает оператор "
              "в настройках сервера.")
_URL_PROBLEMS = {"not_https": _NOT_HTTPS, "bad_address": _BAD_URL, "blocked_address": _INNER_URL}


def check_llm_url(base_url: Any) -> str:
    """Адрес API, введённый на странице: только https и только наружу (`netguard.check_url`).

    Здесь — проверка по записи адреса; по разрешённому имени адрес проверяется при каждом
    запросе (`netguard.PinnedTransport`). Возвращает адрес в одном виде — со схемой и именем
    узла строчными буквами, без косой черты в конце: по нему же решается, сменился ли адрес."""
    url = clean(base_url).rstrip("/") or ss.DEFAULT_LLM_BASE_URL
    try:
        parts = netguard.check_url(url)
    except netguard.Blocked as exc:
        raise Invalid(_URL_PROBLEMS.get(exc.code, _BAD_URL),
                      "blocked_base_url" if exc.code == "blocked_address" else "bad_base_url") from None
    return f"https://{parts.netloc.lower()}{parts.path}".rstrip("/")


def check_llm_model(model: Any) -> str:
    name = clean(model)
    if not _MODEL.match(name):
        raise Invalid("Имя модели — как его пишет провайдер, латиницей, без пробелов: например gpt-4o-mini "
                      "или openai/gpt-4o-mini.", "bad_model")
    return name


_LLM_PROBLEMS = {
    "http_401": "Провайдер не принял ключ. Проверьте, что ключ скопирован целиком и не отозван.",
    "http_403": "Провайдер отказал этому ключу в доступе: ключ не тот, отозван или недоступен в вашей стране.",
    "http_404": "Провайдер не знает такой модели — или адрес API указан неверно. Проверьте имя модели и адрес.",
    "http_400": "Провайдер отклонил пробный запрос. Чаще всего так бывает при неверном имени модели.",
    "http_422": "Провайдер отклонил пробный запрос. Чаще всего так бывает при неверном имени модели.",
    "http_402": "Провайдер сообщает, что на счёте нет средств.",
    "http_429": "Провайдер ограничил запросы: исчерпан лимит или на счёте нет средств. Проверьте кабинет провайдера.",
    "timeout": "Провайдер не ответил вовремя. Попробуйте ещё раз; если повторится — проверьте адрес API.",
    "bad_response": "По этому адресу ответили не так, как отвечает API, совместимый с OpenAI. Проверьте адрес API.",
    "proxy_needs_socksio": _BOT_PROBLEMS["proxy_needs_socksio"],
    "bad_proxy_url": _BOT_PROBLEMS["bad_proxy_url"],
    "blocked_address": _INNER_URL,
    "not_https": _NOT_HTTPS,
    "bad_address": _BAD_URL,
    "dns_failed": "Сервер не нашёл такого адреса: имя узла не разрешается. Проверьте адрес API. Если сервер "
                  "выходит в интернет только через прокси и сам имён не разрешает, адрес модели задают "
                  "в настройках сервера.",
}


def llm_problem_text(code: str | None) -> str | None:
    if not code:
        return None
    if code.startswith(("connect:", "network:")):
        return ("Не удалось соединиться с адресом API. Проверьте адрес и то, что у сервера есть доступ "
                "в интернет (или настроен прокси).")
    if code.startswith("http_5"):
        return "Сервер провайдера ответил ошибкой. Попробуйте позже."
    if code.startswith("http_3"):
        return ("По этому адресу отвечают перенаправлением на другой адрес. Сервис по перенаправлениям не ходит: "
                "укажите конечный адрес API.")
    return _LLM_PROBLEMS.get(code, "Провайдер ответил отказом. Проверьте ключ, адрес и имя модели.")


async def check_llm(config: Any, *, api_key: str, base_url: str, model: str, restricted: bool) -> str:
    """Задаёт модели самый короткий вопрос. Возвращает имя модели, которым ответил провайдер.

    restricted — адрес введён на странице: пробный запрос уходит только на адрес в интернете,
    проверенный в момент соединения, и по перенаправлениям не идёт."""
    client = LlmClient(base_url=base_url, api_key=api_key, model=model, proxy_url=config.proxy_url,
                       transport=executor_service.TEST_OVERRIDES.get("llm_transport"), timeout=30.0, slots=1,
                       sleep=_no_wait, restricted=restricted)
    try:
        _, used = await client.chat([{"role": "user", "content": "Ответь одним словом: да"}], max_tokens=16)
    except LlmError as exc:
        raise Invalid(llm_problem_text(exc.code) or "", exc.code.split(":")[0]) from None
    finally:
        await client.aclose()
    return used


async def _no_wait(seconds: float) -> None:
    """Повторы пробного запроса идут без пауз: владелец ждёт ответа на странице."""
    await asyncio.sleep(0)
