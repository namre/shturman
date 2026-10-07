"""Настройки сервиса. Всё приходит из переменных окружения; значений по умолчанию для секретов нет."""

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
    tg_api_hash: str = ""
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
    # Защита от внедрённых инструкций: включена, если установлена модель-классификатор.
    guard: bool = True

    @property
    def own_bot(self) -> bool:
        """Есть ли у сервиса свой бот. Только тогда нажатие владельца нельзя подделать из Hermes."""
        return bool(self.bot_token)

    @property
    def own_llm(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)

    @property
    def guard_dir(self) -> Path:
        return self.data_dir / "guard"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

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
        return cls(
            dsn=dsn, api_token=api_token, mcp_token=mcp_token,
            host=_env("SHTURMAN_HOST", "127.0.0.1"), port=port,
            data_dir=Path(_env("SHTURMAN_DATA_DIR", "/data")),
            allowed_hosts=hosts or (f"127.0.0.1:{port}", f"localhost:{port}"),
            tg_api_id=_int("TELEGRAM_API_ID", 0), tg_api_hash=_env("TELEGRAM_API_HASH"),
            proxy_url=_env("EGRESS_PROXY_URL"),
            embeddings_url=_env("SHTURMAN_EMBEDDINGS_URL"),
            embeddings_model=_env("SHTURMAN_EMBEDDINGS_MODEL", "intfloat/multilingual-e5-small"),
            embeddings_dim=_int("SHTURMAN_EMBEDDINGS_DIM", 384),
            timezone=_env("SHTURMAN_TIMEZONE", "Europe/Moscow"),
            nightly_at=_env("SHTURMAN_NIGHTLY_AT", "03:30"),
            # Отправка возможна только со своим ботом согласований: без него нажатие «Отправить»
            # проходит через Hermes и может быть подделано ассистентом.
            sending=(_env("SHTURMAN_SENDING", "off").lower() in ("on", "1", "true", "yes")
                     and bool(_env("SHTURMAN_BOT_TOKEN"))),
            bot_token=_env("SHTURMAN_BOT_TOKEN"),
            llm_api_key=_env("SHTURMAN_LLM_API_KEY"),
            llm_base_url=_env("SHTURMAN_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            llm_model=_env("SHTURMAN_LLM_MODEL"),
            guard=_env("SHTURMAN_GUARD", "on").lower() not in ("off", "0", "false", "no"),
            send_daily_hard_cap=max(0, _int("SHTURMAN_SEND_DAILY_CAP", 50)),
        )
