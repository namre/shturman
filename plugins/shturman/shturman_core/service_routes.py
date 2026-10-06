"""Какие маршруты сервиса переписки кому разрешены.

У внутреннего API сервиса один токен на всё, поэтому границы между ролями держит плагин:
каждый клиент создаётся со своим перечнем пар «метод + путь» и ничего другого отправить не может.

  BRIDGE — исполнитель заданий и обработчики Telegram в процессе шлюза;
  TOOLS  — инструменты агента: создать черновик, посмотреть и поправить обязательства и людей;
  UI     — страницы владельца в дашборде (проходят через plugin_api, уже за входом).

Агенту недоступно всё, что меняет правила отправки, список доверенных, автоответ, правила
наблюдателя, исключения чатов, аккаунты Telegram и импорт: это действия владельца в интерфейсе.
Маршруты сверены с кодом сервиса (`service/src/shturman/**`) на коммите fb46275.
"""

from __future__ import annotations

import re
from typing import Iterable

Routes = tuple[tuple[str, "re.Pattern[str]"], ...]

_INT = r"[0-9]{1,19}"
_HEX32 = r"[0-9a-f]{32}"
_TOKEN = r"[A-Za-z0-9_-]{1,64}"


def _compile(items: Iterable[tuple[str, str]]) -> Routes:
    return tuple((method, re.compile(pattern)) for method, pattern in items)


def allowed(routes: Routes, method: str, path: str) -> bool:
    """Разрешена ли пара. Путь сравнивается целиком: «..», двойные косые и хвосты не проходят."""
    if not isinstance(path, str) or not isinstance(method, str):
        return False
    method = method.upper()
    return any(m == method and pattern.fullmatch(path) for m, pattern in routes)


BRIDGE: Routes = _compile([
    ("GET", r"/api/status"),
    ("PUT", r"/api/owner"),
    ("POST", r"/api/jobs/claim"),
    ("POST", rf"/api/jobs/{_INT}/(complete|fail)"),
    ("POST", r"/api/callbacks/telegram"),
    ("POST", r"/api/ingest/business/(connection|message|deleted)"),
])

TOOLS: Routes = _compile([
    ("POST", r"/api/outbox/drafts"),
    ("GET", r"/api/commitments"),
    ("GET", rf"/api/commitments/{_INT}"),
    # accept и reject сюда не входят: предложение принимает или отклоняет владелец кнопкой.
    ("POST", rf"/api/commitments/{_INT}/(close|cancel|reopen|reschedule)"),
    ("GET", r"/api/people"),
    ("GET", rf"/api/people/{_INT}"),
    ("POST", rf"/api/people/{_INT}/aliases"),
])

UI: Routes = _compile([
    ("GET", r"/api/status"),
    ("GET", r"/api/embeddings/status"),
    ("GET", r"/api/processing/status"),
    # чаты и исключения
    ("GET", r"/api/chats"),
    ("PUT", rf"/api/chats/{_INT}/excluded"),
    # импорт экспорта Telegram Desktop
    ("POST", r"/api/imports"),
    ("GET", r"/api/imports"),
    ("GET", rf"/api/imports/{_HEX32}"),
    ("DELETE", rf"/api/imports/{_HEX32}"),
    ("GET", rf"/api/imports/{_HEX32}/scan"),
    ("POST", rf"/api/imports/{_HEX32}/run"),
    # аккаунты Telegram
    ("GET", r"/api/tg/accounts"),
    ("POST", r"/api/tg/login"),
    ("GET", rf"/api/tg/login/{_TOKEN}"),
    ("POST", rf"/api/tg/login/{_TOKEN}/(password|cancel)"),
    ("POST", rf"/api/tg/accounts/{_INT}/(logout|pause|resume|sync)"),
    ("PUT", rf"/api/tg/accounts/{_INT}/options"),
    ("GET", rf"/api/tg/accounts/{_INT}/(dialogs|sync)"),
    # шлюз отправки
    ("GET", r"/api/outbox/drafts"),
    ("POST", rf"/api/outbox/drafts/{_INT}/cancel"),
    ("GET", r"/api/outbox/policy"),
    ("PUT", r"/api/outbox/policy"),
    ("PUT", rf"/api/outbox/chats/{_INT}"),
    ("GET", r"/api/outbox/autoreply"),
    ("PUT", r"/api/outbox/autoreply"),
    ("GET", r"/api/outbox/trusted"),
    ("POST", r"/api/outbox/trusted"),
    ("DELETE", r"/api/outbox/trusted"),
    # наблюдатель
    ("GET", r"/api/watch/rules"),
    ("POST", r"/api/watch/rules"),
    ("PUT", rf"/api/watch/rules/{_INT}"),
    ("DELETE", rf"/api/watch/rules/{_INT}"),
    ("GET", r"/api/watch/hits"),
    # обязательства
    ("GET", r"/api/commitments"),
    ("GET", rf"/api/commitments/{_INT}"),
    ("POST", rf"/api/commitments/{_INT}/(close|cancel|reopen|reschedule|accept|reject)"),
    # люди
    ("GET", r"/api/people"),
    ("GET", r"/api/people/proposals"),
    ("POST", rf"/api/people/proposals/{_INT}/reject"),
    ("POST", r"/api/people/merge"),
    ("GET", rf"/api/people/{_INT}"),
    ("POST", rf"/api/people/{_INT}/(aliases|split)"),
    ("DELETE", rf"/api/people/{_INT}/aliases"),
])
