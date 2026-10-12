"""Проверка данных входа Mini App (`Telegram.WebApp.initData`) — подписи Telegram для бота согласований.

Правило взято из документации Telegram «Validating data received via the Mini App»
(https://core.telegram.org/bots/webapps, сверено 2026-10-12; на странице — Bot API 10.1 от 11 июня 2026):

  * initData — строка запроса `ключ=значение&…`;
  * data-check-string — все полученные поля, кроме `hash`, отсортированные по ключу, в виде
    `ключ=значение`, через перевод строки (0x0A). Поле `signature` в этой строке ОСТАЁТСЯ: документ
    исключает только `hash` («a chain of all received fields»); `signature` исключается только в
    проверке третьей стороной по Ed25519 (ниже);
  * секретный ключ — HMAC-SHA-256 токена бота с ключом-строкой `WebAppData`;
  * подпись верна, если шестнадцатеричная запись HMAC-SHA-256 строки data-check-string этим
    ключом совпала с `hash`. Сравнение — за постоянное время;
  * «чтобы не принимать устаревшие данные», документ советует проверять `auth_date` — время,
    когда Mini App получило данные. Мы принимаем данные не старше `MAX_AGE` (10 минут) и не из
    будущего дальше `CLOCK_SKEW` (1 минута).

Проверка третьей стороной (поле `signature`, Ed25519 открытым ключом Telegram, строка
`<bot_id>:WebAppData` в начале) нам не нужна: токен бота согласований есть у самого сервиса.

Что ещё здесь проверяется сверх документа (строже, ничего не ослабляет):
  * строка не длиннее `MAX_LEN`, каждый ключ встречается один раз, запись разбирается без
    поблажек (`strict_parsing`), в значениях нет перевода строки и возврата каретки (они —
    разделитель data-check-string);
  * `user` — объект JSON с целым положительным `id`; кто это, решает вызывающий (сверка с
    привязанным владельцем — `miniapp.py`).

Модуль ничего не пишет и в сеть не ходит; токен бота сюда передаёт вызывающий и обратно он не
возвращается ни в ответе, ни в тексте исключения.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl

MAX_AGE = 10 * 60          # секунд: данные входа старше этого не принимаются
CLOCK_SKEW = 60            # секунд: насколько auth_date может быть «из будущего» (часы сервера и Telegram)
MAX_LEN = 8192             # знаков: настоящая строка initData — около килобайта
_HASH = re.compile(r"^[0-9a-f]{64}$")
_KEY = re.compile(r"^[a-z_]{1,64}$")


class Invalid(Exception):
    """Данные входа не приняты. `code` — короткая причина для журнала; наружу не отдаётся."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class InitData:
    user_id: int
    auth_date: int
    # Подпись Telegram (`hash`) — одна на одни и те же данные, как бы ни были переставлены поля
    # в строке. По ней сессия выдаётся один раз (`miniapp.py`).
    signature_hash: str
    query_id: str | None


def secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def data_check_string(fields: dict[str, str]) -> str:
    return "\n".join(f"{key}={fields[key]}" for key in sorted(fields) if key != "hash")


def sign(fields: dict[str, str], bot_token: str) -> str:
    """Подпись, которую поставил бы Telegram. Нужна тестам и стенду; сервис ею ничего не подписывает."""
    return hmac.new(secret_key(bot_token), data_check_string(fields).encode("utf-8"), hashlib.sha256).hexdigest()


def parse(raw: object) -> dict[str, str]:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_LEN:
        raise Invalid("malformed")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, errors="strict")
    except (ValueError, UnicodeDecodeError):
        raise Invalid("malformed") from None
    fields: dict[str, str] = {}
    for key, value in pairs:
        # Перевод строки — разделитель строки data-check-string: значение с ним могло бы выдать
        # себя за несколько полей. У настоящих данных Telegram его в значениях нет.
        if not _KEY.match(key) or key in fields or "\n" in value or "\r" in value:
            raise Invalid("malformed")
        fields[key] = value
    return fields


def check(raw: object, bot_token: str, *, now: float, max_age: int = MAX_AGE) -> InitData:
    """Проверяет подпись и срок. Возвращает, кто открыл приложение, — или бросает `Invalid`."""
    if not bot_token:
        raise Invalid("no_bot")
    fields = parse(raw)
    got = fields.get("hash", "")
    if not _HASH.match(got):
        raise Invalid("malformed")
    if not hmac.compare_digest(sign(fields, bot_token), got):
        raise Invalid("bad_signature")
    auth_date = fields.get("auth_date", "")
    if not auth_date.isascii() or not auth_date.isdigit() or len(auth_date) > 12:
        raise Invalid("malformed")
    moment = int(auth_date)
    if moment > now + CLOCK_SKEW:
        raise Invalid("from_future")
    if now - moment > max_age:
        raise Invalid("expired")
    try:
        user = json.loads(fields.get("user", ""))
    except ValueError:
        raise Invalid("malformed") from None
    user_id = user.get("id") if isinstance(user, dict) else None
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise Invalid("malformed")
    query_id = fields.get("query_id")
    return InitData(user_id=user_id, auth_date=moment, signature_hash=got,
                    query_id=query_id if isinstance(query_id, str) and query_id else None)
