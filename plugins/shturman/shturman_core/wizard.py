"""Состояние мастера настройки и мелкие расчёты для него."""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from . import personas
from .pairing import Pairing
from .state import Store

# Плагин бизнес-режима ставится из официального репозитория по полному SHA коммита.
# Проверено 2026-10-06: это вершина main, в неё входит исправление проверки владельца черновика
# (коммит 77dec03 от 2026-08-15), которого нет в ревизии из каталога Hermes 0.21.5.
BUSINESS_PLUGIN_NAME = "telegram-business"
BUSINESS_PLUGIN_SOURCE = "NousResearch/hermes-telegram-business"
BUSINESS_PLUGIN_REF = "98c60afc00d36c885bb040ebe973b1aa908886c0"

MARKS = ("persona_saved", "model_ok", "bot_applied", "business_skipped", "completed")
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
        "business": {
            "connected": bool(business.get("connected")),
            "can_reply": bool(business.get("can_reply")),
            "updated_at": business.get("updated_at"),
            "plugin": {
                "name": BUSINESS_PLUGIN_NAME,
                "identifier": BUSINESS_PLUGIN_SOURCE,
                "ref": BUSINESS_PLUGIN_REF,
            },
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
