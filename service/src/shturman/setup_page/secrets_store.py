"""Хранилище значений, которые владелец ввёл на странице настройки.

Что лежит: ключи приложения Telegram (`api_id`, `api_hash`), токен бота согласований, ключ, адрес
и имя своей модели сервиса. Где: файл `<каталог данных>/setup/secrets.json`, права 600, каталог —
700. Каталог данных сервиса контейнеру Hermes не подключён.

Почему файл, а не база. База попадает в `pg_dump`, а внутренний API (`/api/*`), токен которого
есть у ассистента в Hermes, читает именно базу. Файл в каталоге данных не читает ни один маршрут.

Правило слияния с окружением: значение из окружения сервиса, если задано, главнее — страница
тогда показывает «задано в настройках сервера» и не даёт его менять. Иначе берётся значение из
файла. Для ключей приложения Telegram правило действует на пару целиком: если в окружении задан
хотя бы один из двух, файл для них не читается.

Значения наружу не отдаются: у хранилища нет способа напечатать их (`repr` показывает только
имена), страница получает лишь «задано / не задано».

Здесь же — ключ подписи кодов входа (`<каталог данных>/setup/key`): код из восьми цифр по
одному SHA-256 из копии базы подбирается перебором, поэтому в базе лежит HMAC с ключом,
которого в базе нет.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger("shturman.setup")

TG_API_ID, TG_API_HASH = "tg_api_id", "tg_api_hash"
BOT_TOKEN = "bot_token"
LLM_API_KEY, LLM_BASE_URL, LLM_MODEL = "llm_api_key", "llm_base_url", "llm_model"
NAMES = (TG_API_ID, TG_API_HASH, BOT_TOKEN, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL)

# Имя значения → переменная окружения сервиса, которая его перекрывает.
ENV = {
    TG_API_ID: "TELEGRAM_API_ID", TG_API_HASH: "TELEGRAM_API_HASH",
    BOT_TOKEN: "SHTURMAN_BOT_TOKEN",
    LLM_API_KEY: "SHTURMAN_LLM_API_KEY", LLM_BASE_URL: "SHTURMAN_LLM_BASE_URL",
    LLM_MODEL: "SHTURMAN_LLM_MODEL",
}
# Имя значения → поле `Config`.
FIELD = {TG_API_ID: "tg_api_id", TG_API_HASH: "tg_api_hash", BOT_TOKEN: "bot_token",
         LLM_API_KEY: "llm_api_key", LLM_BASE_URL: "llm_base_url", LLM_MODEL: "llm_model"}
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
MAX_VALUE = 2048      # знаков: длиннее не бывает ни токен, ни ключ, ни адрес
_FILE, _KEY = "secrets.json", "key"


def directory(data_dir: Path) -> Path:
    return Path(data_dir) / "setup"


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        os.chmod(path, 0o700)


def _write_private(path: Path, data: bytes) -> None:
    """Пишет файл так, что он ни на миг не бывает доступен кому-то, кроме владельца процесса,
    и не остаётся наполовину записанным: временный файл с правами 600, затем замена."""
    _private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    except BaseException:
        with_suppress_unlink(tmp)
        raise
    os.chmod(path, 0o600)


def with_suppress_unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class SecretStore:
    """Значения из файла. Читается с диска при каждом обращении: файл мал, а читают его редко —
    при запуске сервиса и при действиях на странице настройки."""

    def __init__(self, data_dir: Path) -> None:
        self.path = directory(data_dir) / _FILE

    def __repr__(self) -> str:
        return f"<SecretStore задано: {', '.join(sorted(self.load())) or 'ничего'}>"

    __str__ = __repr__

    def load(self) -> dict[str, str]:
        """Все сохранённые значения. Нет файла или он не читается — пусто: сервис обязан
        запуститься и без него."""
        try:
            info = self.path.stat()
        except OSError:
            return {}
        if stat.S_IMODE(info.st_mode) & 0o077:
            # Права ослабили руками или копированием — возвращаем как было задумано.
            try:
                os.chmod(self.path, 0o600)
                logger.warning("файл значений страницы настройки был доступен не только владельцу: права исправлены")
            except OSError:
                logger.error("файл значений страницы настройки доступен не только владельцу, исправить права не удалось")
        try:
            data = json.loads(self.path.read_bytes())
        except (OSError, ValueError):
            logger.error("файл значений страницы настройки не читается: значения из него не применяются")
            return {}
        values = data.get("values") if isinstance(data, dict) else None
        if not isinstance(values, dict):
            return {}
        return {k: v for k, v in values.items()
                if k in NAMES and isinstance(v, str) and v and len(v) <= MAX_VALUE}

    def has(self, name: str) -> bool:
        return name in self.load()

    def get(self, name: str) -> str:
        return self.load().get(name, "")

    def update(self, changes: Mapping[str, str | None]) -> None:
        """Записывает значения; None или пустая строка — убрать значение."""
        values = self.load()
        for name, value in changes.items():
            if name not in NAMES:
                raise KeyError(name)
            if value is None or value == "":
                values.pop(name, None)
            elif not isinstance(value, str) or len(value) > MAX_VALUE or "\x00" in value:
                raise ValueError(f"значение {name} не подходит для хранения")
            else:
                values[name] = value
        body = json.dumps({"version": 1, "values": values}, ensure_ascii=False, sort_keys=True)
        _write_private(self.path, body.encode("utf-8"))


def signing_key(data_dir: Path) -> bytes:
    """Ключ подписи кодов входа. Создаётся при первом обращении, лежит рядом с файлом значений."""
    path = directory(data_dir) / _KEY
    for _ in range(2):
        try:
            key = path.read_bytes()
        except FileNotFoundError:
            key = b""
        if len(key) >= 32:
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                os.chmod(path, 0o600)
            return key
        _private_dir(path.parent)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.stat().st_size >= 32:
                continue          # файл только что создал соседний запрос
            os.unlink(path)       # пустой или обрезанный: создаём заново
            continue
        with os.fdopen(fd, "wb") as fp:
            fp.write(secrets.token_bytes(32))
    return path.read_bytes()


# --- слияние с настройками сервиса ---

def locked_by_env(env: Mapping[str, str]) -> frozenset[str]:
    """Какие значения заданы окружением сервиса: их страница не меняет."""
    def given(name: str) -> bool:
        return bool(env.get(ENV[name], "").strip())

    locked = {name for name in NAMES if given(name)}
    if locked & {TG_API_ID, TG_API_HASH}:
        locked |= {TG_API_ID, TG_API_HASH}     # ключи приложения — только парой
    return frozenset(locked)


def _typed(name: str, value: str) -> Any:
    if name == TG_API_ID:
        try:
            return int(value)
        except ValueError:
            return 0
    if name == LLM_BASE_URL:
        return value.rstrip("/")
    return value


def _empty(name: str) -> Any:
    return 0 if name == TG_API_ID else DEFAULT_LLM_BASE_URL if name == LLM_BASE_URL else ""


def overlay(config: Any, values: Mapping[str, str], managed: Iterable[str]) -> Any:
    """Настройки сервиса с подставленными значениями из файла.

    managed — имена, которыми распоряжается страница (не заданные окружением). Для каждого
    берётся значение из файла, а если его там нет — пустое: так удаление значения на странице
    действительно выключает то, что им включалось. Остальные поля, в том числе главный
    выключатель отправки, не меняются.
    """
    import dataclasses

    managed = list(managed)
    changes = {FIELD[name]: _typed(name, values[name]) if values.get(name) else _empty(name)
               for name in managed}
    if LLM_BASE_URL in managed:
        # Адрес модели пришёл со страницы: запросы по нему ограничены (netguard.py). Адрес из
        # окружения сюда не попадает (он не в managed), адрес по умолчанию — тоже.
        changes["llm_url_from_page"] = bool(values.get(LLM_BASE_URL))
    return dataclasses.replace(config, **changes) if changes else config
