"""Шаг мастера «Переписка»: что плагин знает о странице настройки переписки. Только стандартная библиотека.

Сбор переписки и бот согласований настраиваются не в мастере, а на отдельной странице, которую
отдаёт сам сервис переписки по пути `/shturman-setup/`, мимо Hermes и за собственным входом.
Там владелец вводит токен бота согласований и ключи приложения Telegram, входит в аккаунт
Telegram и выбирает чаты — ассистенту всё это видеть нельзя.

Поэтому плагин об этой странице знает немного и ничего на неё не передаёт:

  * её адрес — внешний адрес экземпляра плюс `/shturman-setup/`. Это обычная ссылка для
    владельца. Ссылку входа плагин не запрашивает и не показывает: её выдаёт `./ops/setup-link.sh`;
  * состояние — объект `setup` из `GET /api/status` сервиса: признаки и числа. Сервис прежней
    версии такого объекта не отдаёт; тогда состояние — `outdated`, и мастер просит обновить
    экземпляр.

Отсюда в браузер уходят только булевы признаки и целые числа: что бы ни прислал сервис в этих
полях, строки и вложенные объекты дальше не идут.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Mapping

SETUP_PATH = "/shturman-setup/"
STATUS_PATH = "/api/status"

# Признаки из объекта `setup` сервиса. `enabled` разбирается отдельно: от него зависит состояние.
FLAGS = ("origin_set", "tg_keys", "own_bot", "owner_bound", "business_connected", "own_model")

# Состояния страницы настройки глазами плагина.
OK = "ok"                    # сервис отдал объект setup, страница включена
NO_SERVICE = "no_service"    # сервис переписки к плагину не подключён (нет адреса или токена)
UNREACHABLE = "unreachable"  # сервис не ответил
OUTDATED = "outdated"        # сервис прежней версии: объекта setup в его состоянии нет
DISABLED = "disabled"        # объект есть, но страница в сервисе выключена
STATES = (OK, NO_SERVICE, UNREACHABLE, OUTDATED, DISABLED)

MAX_COUNT = 10 ** 12


def page_url(public_url: str) -> str | None:
    """Адрес страницы настройки переписки по внешнему адресу экземпляра.

    None — внешний адрес не задан или не годится для ссылки: тогда страница открывается только
    с самого сервера или через туннель SSH, и мастер кнопку не показывает.
    """
    value = (public_url or "").strip()
    if not value:
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    if any(ch.isspace() or ch in "\"'<>\\" for ch in value):
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    base = f"{parts.scheme}://{host}" + (f":{port}" if port else "") + parts.path.rstrip("/")
    return base + SETUP_PATH


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_COUNT else None


def summary(status: Mapping[str, Any] | None, *, public_url: str = "", state: str | None = None) -> dict[str, Any]:
    """Сводка для шага «Переписка».

    status — ответ `GET /api/status` сервиса либо None, если его нет; тогда причину называет
    state (`no_service` или `unreachable`). Возвращает состояние, адрес страницы и — когда сервис
    их отдал — признаки настройки и размер архива.
    """
    out: dict[str, Any] = {"state": state or UNREACHABLE, "url": page_url(public_url), "setup": None,
                           "archive": {"messages": None, "chats": None}}
    if not isinstance(status, Mapping):
        if out["state"] not in (NO_SERVICE, UNREACHABLE):
            out["state"] = UNREACHABLE
        return out
    out["archive"] = {"messages": _count(status.get("messages")), "chats": _count(status.get("chats"))}
    raw = status.get("setup")
    if not isinstance(raw, Mapping):
        out["state"] = OUTDATED
        return out
    out["setup"] = {name: _flag(raw.get(name)) for name in FLAGS}
    out["setup"]["accounts"] = _count(raw.get("accounts")) or 0
    out["state"] = OK if raw.get("enabled") is True else DISABLED
    return out
