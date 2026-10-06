"""Минимальный клиент Telegram Bot API: только отправка сообщения и проверка токена.

Получением сообщений занимается шлюз Hermes. Здесь нет getUpdates и быть не должно:
второй получатель на том же токене ломает шлюзу связь с Telegram.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

API_ROOT = "https://api.telegram.org"
TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")


class BotApiError(Exception):
    """Telegram отклонил запрос или недоступен. В тексте нет токена."""


def call(token: str, method: str, params: dict[str, Any] | None = None, *, timeout: float = 10) -> Any:
    if not TOKEN_RE.match(token or ""):
        raise BotApiError("токен не похож на токен бота")
    request = urllib.request.Request(
        f"{API_ROOT}/bot{token}/{method}",
        data=json.dumps(params or {}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    proxy = os.environ.get("TELEGRAM_PROXY", "").strip()
    opener = (
        urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
        if proxy else urllib.request.build_opener()
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            description = json.loads(exc.read().decode()).get("description", "")
        except Exception:
            description = ""
        raise BotApiError(description or f"Telegram ответил кодом {exc.code}") from None
    except Exception as exc:  # сеть, таймаут, разбор ответа
        raise BotApiError(f"нет связи с Telegram ({type(exc).__name__})") from None
    if not isinstance(body, dict) or not body.get("ok"):
        raise BotApiError(str((body or {}).get("description") or "Telegram отклонил запрос"))
    return body.get("result")


def get_me(token: str) -> dict[str, Any]:
    result = call(token, "getMe")
    return result if isinstance(result, dict) else {}


def send_message(token: str, chat_id: int, text: str) -> None:
    call(token, "sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
