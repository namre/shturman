"""Настройки сервиса. Приходят из переменных окружения; значений по умолчанию для секретов нет.

Шесть значений владелец может ввести не в окружении, а на странице настройки (`setup_page/`):
ключи приложения Telegram, токен бота согласований, ключ, адрес и имя своей модели. Они лежат
в файле каталога данных и подставляются здесь, если окружение их не задало: окружение главнее.
Главный выключатель отправки к ним не относится — он читается только из окружения.

Седьмое — подписка ChatGPT вместо ключа модели (вход через ChatGPT на той же странице,
`executor/siwc.py`): её запись лежит отдельным файлом и действует, только если ключ модели не
задан окружением (`SHTURMAN_LLM_API_KEY`). Одновременно действует один способ: ключ или подписка.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name}: нужно целое число") from None


@dataclass(frozen=True)
class Config:
    dsn: str = field(repr=False)
    # Токен внутреннего API: им пользуется плагин «Штурмана» в Hermes.
    api_token: str = field(repr=False)
    # Токен MCP-сервера архива: им пользуется агент Hermes. Даёт только чтение.
    mcp_token: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8765
    # Каталог данных сервиса: сессии Telegram, загруженные выгрузки. Hermes его не видит.
    data_dir: Path = Path("/data")
    # Имена, под которыми к сервису обращаются (защита от подмены адреса в MCP).
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:8765", "localhost:8765")
    tg_api_id: int = 0
    tg_api_hash: str = field(default="", repr=False)
    proxy_url: str = ""
    embeddings_url: str = ""
    embeddings_model: str = "intfloat/multilingual-e5-small"
    embeddings_dim: int = 384
    timezone: str = "Europe/Moscow"
    # Время ночной обработки по часам владельца, «ЧЧ:ММ».
    nightly_at: str = "03:30"
    # Главный выключатель отправки. Задаётся ТОЛЬКО окружением сервиса (на сервере — в .env),
    # через API его изменить нельзя: тот, кто завладел токеном API, не может включить отправку сам.
    # Пока выключено, сервис не отправляет ничего и никому: ни согласованных черновиков, ни автоответов.
    sending: bool = False
    # Жёсткий потолок отправок на аккаунт в сутки. Тоже только из окружения; настройки шлюза
    # отправки могут его уменьшить, но не превысить.
    send_daily_hard_cap: int = 50
    # Свой бот сервиса («бот согласований»): уведомления владельцу, кнопки, бизнес-режим.
    # Его токен есть только у сервиса — в Hermes, где у ассистента терминал, он не попадает.
    bot_token: str = field(default="", repr=False)
    # Свой доступ сервиса к модели (API, совместимый с OpenAI). Пусто — модель вызывает плагин в Hermes.
    llm_api_key: str = field(default="", repr=False)
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = ""
    # Своя модель сервиса — по подписке ChatGPT (вход на странице настройки), а не по ключу API.
    # Модель выбирается на странице из списка, который отдаёт OpenAI для этой учётной записи.
    chatgpt: bool = False
    chatgpt_model: str = ""
    # Защита от внедрённых инструкций во входящих сообщениях (guard/, docs/guard.md). По умолчанию
    # выключена; включает её ./ops/guard.sh on — он же скачивает модель и поднимает контейнер с ней.
    guard: bool = False
    # Адрес контейнера с моделью-классификатором (TEI). Пусто при включённой защите — работают
    # одни правила: это заметно слабее модели.
    guard_url: str = ""
    guard_model: str = "Horizon-Labs/prompt-injection-guard-small"
    # Расшифровка голосовых и «кружков» (voice/, docs/voice.md). По умолчанию выключена; включает
    # её ./ops/asr.sh on — он же скачивает модель и поднимает контейнер распознавания речи.
    asr: bool = False
    asr_url: str = ""
    # Сколько дней назад от сегодняшнего голосовые ещё ставятся в очередь; более старые — нет.
    asr_days: int = 30
    # Предел длительности (секунд) и размера файла: длиннее — не скачивается и не распознаётся.
    asr_max_seconds: int = 600
    asr_max_bytes: int = 20 * 1024 * 1024
    # Разбор фото и документов (media/, docs/media.md). Включает и выключает владелец на странице
    # настройки переписки (setup_state 'media'): файлы уходят модели. Здесь — только пределы.
    media_days: int = 30
    media_max_bytes: int = 20 * 1024 * 1024
    # Внешний адрес страницы настройки (схема, имя и порт, без пути), например
    # https://assistant.example.com:8443. Пусто — страница отвечает только под локальными именами
    # из allowed_hosts (туннель SSH). Адрес обязан отличаться от адреса дашборда Hermes хотя бы
    # портом: см. `setup_reason`.
    setup_origin: str = ""
    # Адрес дашборда Hermes (схема, имя, порт). Нужен только для сравнения с адресом страницы.
    dashboard_origin: str = ""
    # Адрес API своей модели введён на странице настройки (а не задан окружением и не взят по
    # умолчанию): запросы по нему идут только наружу и только по https (netguard.py).
    llm_url_from_page: bool = False
    # Какие из значений страницы настройки заданы окружением сервиса: страница их не меняет
    # (имена — как в setup_page/secrets_store.py).
    locked: frozenset[str] = frozenset()
    # Operator-provisioned read-only source definitions. Credentials stay in the
    # service's private storage, outside the Hermes mount and model context.
    sources_file: str = ""
    prepare_only: bool = False
    # Optional OAuth read-only archive endpoint. Daily permissions are decided
    # in Telegram; external-client sign-in is a one-time browser flow.
    remote_mcp_origin: str = ""
    remote_mcp_allow_loopback: bool = False
    remote_mcp_clients: tuple[dict, ...] = ()

    @property
    def own_bot(self) -> bool:
        """Есть ли у сервиса свой бот. Только тогда нажатие владельца нельзя подделать из Hermes."""
        return bool(self.bot_token)

    @property
    def own_llm(self) -> bool:
        return self.chatgpt or bool(self.llm_api_key and self.llm_model)

    @property
    def llm_way(self) -> str | None:
        """Как сервис обращается к своей модели: subscription | api_key | None (никак)."""
        if self.chatgpt:
            return "subscription"
        return "api_key" if self.llm_api_key and self.llm_model else None

    @property
    def setup_reason(self) -> str | None:
        """Почему страница настройки не отдаётся по внешнему адресу; None — отдаётся.

        same_origin — адрес страницы совпал с адресом дашборда Hermes. Ассистент в Hermes может
        исполнять свой JavaScript на адресе дашборда; на том же адресе этот скрипт читал бы
        хранилище страницы и слал бы запросы от её имени. Поэтому страница по такому адресу не
        обслуживается вовсе — только под локальными именами."""
        if not self.setup_origin:
            return "no_origin"
        if self.dashboard_origin and self.setup_origin == self.dashboard_origin:
            return "same_origin"
        return None

    @property
    def setup_external(self) -> str:
        """Внешний адрес, под которым страница настройки действительно отдаётся, либо пусто."""
        return self.setup_origin if self.setup_reason is None else ""

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def media_files_dir(self) -> Path:
        """Файлы вложений из загруженной выгрузки: лежат до разбора (media/files.py)."""
        return self.data_dir / "media-files"

    @property
    def pages_dir(self) -> Path:
        return self.data_dir / "pages"

    @classmethod
    def from_env(cls) -> "Config":
        dsn = _env("SHTURMAN_DSN")
        api_token = _env("SHTURMAN_API_TOKEN")
        mcp_token = _env("SHTURMAN_MCP_TOKEN")
        missing = [n for n, v in (("SHTURMAN_DSN", dsn), ("SHTURMAN_API_TOKEN", api_token),
                                  ("SHTURMAN_MCP_TOKEN", mcp_token)) if not v]
        if missing:
            raise ConfigError("не заданы переменные окружения: " + ", ".join(missing))
        if api_token == mcp_token:
            raise ConfigError("SHTURMAN_API_TOKEN и SHTURMAN_MCP_TOKEN должны различаться")
        for name, value in (("SHTURMAN_API_TOKEN", api_token), ("SHTURMAN_MCP_TOKEN", mcp_token)):
            if len(value) < 32:
                raise ConfigError(f"{name}: слишком короткое значение, нужно не меньше 32 знаков")
        port = _int("SHTURMAN_PORT", 8765)
        hosts = tuple(h.strip() for h in _env("SHTURMAN_ALLOWED_HOSTS").split(",") if h.strip())
        data_dir = Path(_env("SHTURMAN_DATA_DIR", "/data"))
        config = cls(
            dsn=dsn, api_token=api_token, mcp_token=mcp_token,
            host=_env("SHTURMAN_HOST", "127.0.0.1"), port=port,
            data_dir=data_dir,
            sources_file=_env("SHTURMAN_SOURCES_FILE"),
            prepare_only=_env("SHTURMAN_PREPARE_ONLY", "off").lower() in ("on", "1", "true", "yes"),
            remote_mcp_origin=normalize_origin(_env("SHTURMAN_REMOTE_MCP_ORIGIN"),
                                               "SHTURMAN_REMOTE_MCP_ORIGIN"),
            allowed_hosts=hosts or (f"127.0.0.1:{port}", f"localhost:{port}"),
            setup_origin=_setup_origin(_env("SHTURMAN_SETUP_ORIGIN")),
            dashboard_origin=normalize_origin(_env("SHTURMAN_DASHBOARD_ORIGIN"), "SHTURMAN_DASHBOARD_ORIGIN",
                                              strict=False),
            tg_api_id=_int("TELEGRAM_API_ID", 0), tg_api_hash=_env("TELEGRAM_API_HASH"),
            proxy_url=_env("EGRESS_PROXY_URL"),
            embeddings_url=_env("SHTURMAN_EMBEDDINGS_URL"),
            embeddings_model=_env("SHTURMAN_EMBEDDINGS_MODEL", "intfloat/multilingual-e5-small"),
            embeddings_dim=_int("SHTURMAN_EMBEDDINGS_DIM", 384),
            timezone=_env("SHTURMAN_TIMEZONE", "Europe/Moscow"),
            nightly_at=_env("SHTURMAN_NIGHTLY_AT", "03:30"),
            # Отправка возможна только со своим ботом согласований: без него нажатие «Отправить»
            # проходит через Hermes и может быть подделано ассистентом. Токен бота здесь —
            # именно из окружения: токен, введённый на странице настройки, отправку не включает,
            # иначе её можно было бы включить со страницы (docs/decisions.md).
            sending=(_env("SHTURMAN_SENDING", "off").lower() in ("on", "1", "true", "yes")
                     and bool(_env("SHTURMAN_BOT_TOKEN"))),
            bot_token=_env("SHTURMAN_BOT_TOKEN"),
            llm_api_key=_env("SHTURMAN_LLM_API_KEY"),
            llm_base_url=_env("SHTURMAN_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            llm_model=_env("SHTURMAN_LLM_MODEL"),
            guard=_env("SHTURMAN_GUARD", "off").lower() in ("on", "1", "true", "yes"),
            guard_url=_env("SHTURMAN_GUARD_URL").rstrip("/"),
            guard_model=_env("SHTURMAN_GUARD_MODEL", "Horizon-Labs/prompt-injection-guard-small"),
            asr=_env("SHTURMAN_ASR", "off").lower() in ("on", "1", "true", "yes"),
            asr_url=_env("SHTURMAN_ASR_URL").rstrip("/"),
            asr_days=min(3650, max(0, _int("SHTURMAN_ASR_DAYS", 30))),
            asr_max_seconds=min(3600, max(10, _int("SHTURMAN_ASR_MAX_SECONDS", 600))),
            media_days=min(3650, max(0, _int("SHTURMAN_MEDIA_DAYS", 30))),
            send_daily_hard_cap=max(0, _int("SHTURMAN_SEND_DAILY_CAP", 50)),
        )
        return with_page_values(config, os.environ)


def with_page_values(config: Config, env) -> Config:
    """Подставляет значения, введённые на странице настройки, туда, где окружение молчит."""
    import dataclasses

    from .setup_page import secrets_store

    locked = secrets_store.locked_by_env(env)
    stored = secrets_store.SecretStore(config.data_dir).load()
    managed = [name for name in secrets_store.NAMES if name not in locked and stored.get(name)]
    config = dataclasses.replace(secrets_store.overlay(config, stored, managed), locked=locked)
    return with_subscription(config)


def with_subscription(config: Config) -> Config:
    """Подписка ChatGPT как своя модель сервиса — если она выбрана на странице и ключ модели
    не задан окружением (окружение главнее). Ключ со страницы при включении подписки удаляется,
    так что одновременно действует только один способ; если оба всё же оказались заданы (файл
    правили руками), действует ключ."""
    import dataclasses

    from .executor.siwc import CredentialStore
    from .setup_page import secrets_store

    record = CredentialStore(config.data_dir)
    if secrets_store.LLM_API_KEY in config.locked or config.llm_api_key or not record.active():
        return dataclasses.replace(config, chatgpt=False, chatgpt_model="")
    return dataclasses.replace(config, chatgpt=True, chatgpt_model=record.load().get("model", ""))


def normalize_origin(raw: str, name: str, *, strict: bool = True) -> str:
    """Адрес как его сравнивает браузер (origin): схема, имя узла и порт — и ничего больше.

    Приводится к одному виду: регистр, порт по умолчанию (80 для http, 443 для https) не
    пишется, точка в конце имени убирается, имя не латиницей записывается в punycode. Два
    адреса, которые после этого совпали, для браузера — один origin.

    strict — путь, запрос и фрагмент запрещены (так задаётся адрес страницы настройки). Без
    него они отбрасываются: адрес дашборда нужен только для сравнения."""
    if not raw:
        return ""
    from urllib.parse import urlsplit

    example = "https://assistant.example.com:8443"
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise ConfigError(f"{name}: неверный адрес или порт; нужен адрес вида {example}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None \
            or parts.password is not None or "@" in parts.netloc:
        raise ConfigError(f"{name}: нужен адрес вида {example}")
    if strict and (parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ConfigError(f"{name}: только схема, имя и порт, без пути — например {example}")
    host = parts.hostname.lower().rstrip(".")
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ConfigError(f"{name}: имя узла записано неверно") from None
    if not host or port == 0:
        raise ConfigError(f"{name}: нужен адрес вида {example}")
    if ":" in host:
        host = f"[{host}]"
    default = {"http": 80, "https": 443}[parts.scheme]
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


def _setup_origin(raw: str) -> str:
    """Внешний адрес страницы настройки: схема, имя узла и порт (если он не обычный)."""
    return normalize_origin(raw, "SHTURMAN_SETUP_ORIGIN")
