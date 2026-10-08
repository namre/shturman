"""Шаг мастера «Переписка»: что плагин знает о странице настройки переписки. Только стандартная библиотека.

Переписка настраивается не в мастере, а на отдельной странице, которую отдаёт сам сервис
переписки по пути `/shturman-setup/`, мимо Hermes и за собственным входом. Там владелец вводит
ключи приложения Telegram, входит в свой аккаунт Telegram и выбирает чаты — ассистенту всё это
видеть нельзя.

У страницы свой адрес (origin: схема, имя и порт) — не тот, по которому открывается дашборд;
по умолчанию это то же имя на другом порту. Причина: ассистент может ставить расширения дашборда,
то есть исполнять свой код на адресе дашборда, а браузер разрешает страницам одного адреса
действовать друг за друга. На общем адресе такой код действовал бы на странице настройки от имени
вошедшего владельца. Поэтому на общем адресе сервис страницу не отдаёт вовсе, а плагин ссылку
на адрес дашборда не строит никогда.

Плагин об этой странице знает немного и ничего на неё не передаёт:

  * её адрес — поле `origin` объекта `setup` из `GET /api/status` сервиса плюс `/shturman-setup/`.
    Это обычная ссылка для владельца. Значение приходит от сервиса, но в ссылку попадает только
    проверенное: синтаксически верный адрес `https://имя[:порт]`, без пути, без имени
    пользователя и не совпадающий с адресом дашборда. Ссылку входа плагин не запрашивает
    и не показывает: её выдаёт `./ops/setup-link.sh`;
  * состояние — тот же объект `setup`: признаки и числа. Сервис версии 0.0.5 такого объекта
    не отдаёт, а сервис, собранный до появления отдельного адреса, не отдаёт в нём полей `origin`
    и `reason`; в обоих случаях состояние — `outdated`, и мастер просит обновить экземпляр.

Отсюда в браузер уходят только булевы признаки, целые числа, одно из известных состояний
и проверенный адрес: что бы ни прислал сервис в этих полях, прочие строки и вложенные объекты
дальше не идут.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

SETUP_PATH = "/shturman-setup/"
STATUS_PATH = "/api/status"

# Признаки из объекта `setup` сервиса, которые нужны шагу мастера: заданы ли ключи приложения
# Telegram и подключён ли бизнес-режим к боту сервиса (так собирает переписку экземпляр, где его
# настроили). Остальное — бот согласований, своя модель — показывает сама страница настройки;
# мастеру это не нужно, и в браузер оно не передаётся.
FLAGS = ("tg_keys", "business_connected")

# Состояния страницы настройки глазами плагина.
OK = "ok"                    # страница включена, у неё свой адрес: мастер показывает кнопку
NO_ORIGIN = "no_origin"      # адрес страницы не задан: она открывается только через туннель SSH
SAME_ORIGIN = "same_origin"  # адрес страницы совпал с адресом дашборда: снаружи сервис её не отдаёт
NO_SERVICE = "no_service"    # сервис переписки к плагину не подключён (нет адреса или токена)
UNREACHABLE = "unreachable"  # сервис не ответил
OUTDATED = "outdated"        # сервис прежней версии или сборки: объекта setup или его новых полей нет
DISABLED = "disabled"        # объект есть, но страница в сервисе выключена по иной причине
STATES = (OK, NO_ORIGIN, SAME_ORIGIN, NO_SERVICE, UNREACHABLE, OUTDATED, DISABLED)

MAX_COUNT = 10 ** 12
MAX_ORIGIN = 300
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_ORIGIN_RE = re.compile(rf"(?P<scheme>https?)://(?P<host>{_LABEL}(?:\.{_LABEL})*)(?::(?P<port>[1-9][0-9]{{0,4}}))?")
_DEFAULT_PORTS = {"https": "443", "http": "80"}


def origin_of(url: Any) -> str | None:
    """Адрес в виде origin — `схема://имя[:порт]`, строчными буквами, обычный порт не пишется.

    Путь, если он есть, отбрасывается. None — значение не адрес такого вида. Так сравниваются
    адрес страницы настройки и адрес дашборда: одно имя на разных портах — разные адреса.
    """
    if not isinstance(url, str) or not 0 < len(url) <= MAX_ORIGIN:
        return None
    text = url.strip().lower()
    head = text.split("/", 3)
    if len(head) < 3 or head[1] != "":
        return None
    match = _ORIGIN_RE.fullmatch("/".join(head[:3]))
    if match is None:
        return None
    scheme, host, port = match.group("scheme"), match.group("host"), match.group("port")
    if len(host) > 253 or (port is not None and int(port) > 65535):
        return None
    if port == _DEFAULT_PORTS[scheme]:
        port = None
    return f"{scheme}://{host}" + (f":{port}" if port else "")


def clean_origin(value: Any) -> str | None:
    """Адрес страницы настройки в том единственном виде, который годится для ссылки.

    Только `https://имя` или `https://имя:порт`: имя — обычное имя узла с точкой из латинских
    букв, цифр и дефисов; ни пути, ни строки запроса, ни имени пользователя, ни пробелов,
    ни IP-адреса. Всё остальное — None: значение приходит от сервиса, а в `href` попадает
    только проверенное.
    """
    if not isinstance(value, str) or not 0 < len(value) <= MAX_ORIGIN:
        return None
    text = (value[:-1] if value.endswith("/") else value).lower()
    match = _ORIGIN_RE.fullmatch(text)
    if match is None or match.group("scheme") != "https":
        return None
    host = match.group("host")
    if "." not in host or host.replace(".", "").isdigit():
        return None
    return origin_of(text)


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_COUNT else None


def summary(status: Mapping[str, Any] | None, *, dashboard_url: str = "", state: str | None = None) -> dict[str, Any]:
    """Сводка для шага «Переписка».

    status — ответ `GET /api/status` сервиса либо None, если его нет; тогда причину называет
    state (`no_service` или `unreachable`). dashboard_url — адрес, по которому открыт дашборд:
    с ним сверяется адрес страницы. Возвращает состояние, адрес страницы (только в состоянии
    `ok`) и — когда сервис их отдал — признаки настройки и размер архива.
    """
    out: dict[str, Any] = {"state": state or UNREACHABLE, "url": None, "setup": None,
                           "archive": {"messages": None, "chats": None}}
    if not isinstance(status, Mapping):
        if out["state"] not in (NO_SERVICE, UNREACHABLE):
            out["state"] = UNREACHABLE
        return out
    out["archive"] = {"messages": _count(status.get("messages")), "chats": _count(status.get("chats"))}
    raw = status.get("setup")
    if not isinstance(raw, Mapping) or ("origin" not in raw and "reason" not in raw):
        # Нет объекта — сервис 0.0.5. Нет полей origin и reason — сборка до отдельного адреса:
        # такая страница могла стоять на адресе дашборда, ссылку на неё мастер не даёт.
        out["state"] = OUTDATED
        return out
    out["setup"] = {name: _flag(raw.get(name)) for name in FLAGS}
    out["setup"]["accounts"] = _count(raw.get("accounts")) or 0

    reason = raw.get("reason")
    origin = clean_origin(raw.get("origin"))
    dashboard = origin_of(dashboard_url)
    if reason == SAME_ORIGIN or (origin is not None and origin == dashboard):
        # Второе условие — на случай, если сервис совпадения не заметил: на адрес дашборда
        # мастер не ведёт никогда.
        out["state"] = SAME_ORIGIN
    elif reason == NO_ORIGIN:
        out["state"] = NO_ORIGIN
    elif raw.get("enabled") is not True:
        out["state"] = DISABLED
    elif origin is None:
        out["state"] = NO_ORIGIN
    else:
        out["state"] = OK
        out["url"] = origin + SETUP_PATH
    return out
