"""Состояние мастера настройки и мелкие расчёты для него."""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from . import personas
from .pairing import Pairing
from .state import Store

# Отметки шагов. `correspondence_seen` — владелец прошёл шаг «Переписка» (сама настройка идёт
# на отдельной странице сервиса, мастер о ней знает только по состоянию).
# `business_skipped` мастер больше не ставит: шага «Бизнес-режим» с версии 0.0.6 нет. Отметка
# остаётся в перечне, чтобы состояние экземпляров, прошедших мастер раньше, читалось как прежде:
# для них она значит то же, что `correspondence_seen`.
MARKS = ("persona_saved", "model_ok", "bot_applied", "business_skipped", "correspondence_seen", "completed")
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

PROBE_PROMPT = "Ответь одним словом: работает"


def deep_link(bot_username: str, token: str) -> str:
    """Ссылка, по которой Telegram открывает бота и по кнопке «Запустить» шлёт «/start <token>»."""
    if not _USERNAME_RE.match(bot_username or ""):
        raise ValueError("некорректное имя бота")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", token or ""):
        raise ValueError("значение не подходит для ссылки Telegram")
    return f"https://t.me/{bot_username}?start={token}"


def mark(store: Store, key: str, *, now: Callable[[], float] = time.time) -> None:
    if key not in MARKS:
        raise ValueError(f"неизвестная отметка: {key}")
    with store.locked("wizard") as data:
        marks = data.setdefault("marks", {})
        marks[key] = int(now())
        if key == "completed":
            data["completed_at"] = marks[key]


def remember_bot(store: Store, username: str, name: str) -> None:
    with store.locked("wizard") as data:
        data["bot"] = {"username": username, "name": name}


def snapshot(store: Store, *, now: Callable[[], float] = time.time) -> dict[str, Any]:
    """Всё, что нужно странице мастера. Секретов и хешей здесь нет."""
    wizard = store.read("wizard")
    business = store.read("business")
    return {
        "persona": personas.normalize(wizard.get("persona") or {}),
        "resolved": personas.resolved(wizard.get("persona") or {}),
        "catalog": personas.catalog(),
        "marks": {k: int(v) for k, v in (wizard.get("marks") or {}).items() if k in MARKS},
        "completed": bool(wizard.get("completed_at")),
        "bot": wizard.get("bot") or None,
        "pairing": Pairing(store, now=now).status(),
        # Подключён ли в бизнес-режиме Telegram бот-ассистент (бот Hermes). С версии 0.0.6 так
        # не делают: бизнес-режим подключается к боту согласований сервиса. Признак нужен, чтобы
        # мастер сказал об этом владельцу экземпляра, где бот-ассистент уже подключён.
        "business": {
            "connected": bool(business.get("connected")),
            "can_reply": bool(business.get("can_reply")),
            "updated_at": business.get("updated_at"),
        },
    }


def parse_probe_output(returncode: int, output: str) -> dict[str, Any]:
    """Разбирает вывод `hermes chat -Q` на «модель ответила» и «не ответила»."""
    lines = [ln.strip() for ln in (output or "").splitlines()]
    reply_lines = [
        ln for ln in lines
        if ln and not ln.lower().startswith("session_id:") and not ln.startswith("⚠")
    ]
    reply = " ".join(reply_lines).strip()
    if returncode != 0 or not reply:
        tail = " ".join(reply_lines[-3:])[:400]
        return {"ok": False, "error": tail or "Модель не ответила."}
    return {"ok": True, "reply": reply[:300]}
