"""Привязка владельца к боту.

Мастер выдаёт одноразовую ссылку вида t.me/<бот>?start=<значение> и короткий код на случай,
если ссылка не открылась. Владелец нажимает «Запустить» в Telegram (или отправляет код),
обработчик в процессе шлюза вызывает `try_bind`, и аккаунт, с которого пришло сообщение,
становится кандидатом. Владельцем он становится только после подтверждения в мастере
(`confirm`), то есть из уже выполненного входа: случайно или намеренно угадавший код
посторонний сам себя владельцем сделать не может.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Callable, Optional

from . import tokens
from .state import Store

PAIRING_TTL = 15 * 60
PAIRING_CODE_DIGITS = 6
PAIRING_MAX_WRONG = 5       # неверных значений от кого угодно, после которых привязка отменяется


def extract_candidate(text: str) -> Optional[str]:
    """Достаёт из сообщения то, что может быть значением привязки.

    «/start abc» → «abc»; «482 913» → «482913»; всё остальное → None.
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) == 2 and parts[1].strip() else None
    compact = text.replace(" ", "").replace("-", "")
    if compact.isdigit() and len(compact) == PAIRING_CODE_DIGITS:
        return compact
    return None


class Pairing:
    def __init__(self, store: Store, *, now: Callable[[], float] = time.time) -> None:
        self.store = store
        self.now = now
        self._pending_cache: tuple[float, bool, int] = (-1.0, False, 0)

    def start(self) -> dict[str, Any]:
        """Открывает окно привязки. Значения возвращаются один раз, в файле — только хеши."""
        token = secrets.token_urlsafe(24)
        code = "".join(secrets.choice("0123456789") for _ in range(PAIRING_CODE_DIGITS))
        secret = self.store.secret()
        expires_at = int(self.now()) + PAIRING_TTL
        self.store.write("pairing", {
            "token_digest": tokens.digest(token, secret),
            "code_digest": tokens.digest(code, secret),
            "expires_at": expires_at,
            "wrong": 0,
        })
        return {"token": token, "code": code, "expires_at": expires_at}

    def cancel(self) -> None:
        self.store.delete("pairing")

    def is_pending(self) -> bool:
        """Ждём ли сообщение с кодом прямо сейчас. Вызывается на каждое сообщение, поэтому с кешем."""
        mtime = self.store.mtime("pairing")
        if mtime == 0.0:
            return False
        if self._pending_cache[0] != mtime:
            data = self.store.read("pairing")
            waiting = bool(data.get("token_digest")) and not data.get("candidate")
            self._pending_cache = (mtime, waiting, int(data.get("expires_at", 0)))
        _, pending, expires_at = self._pending_cache
        return pending and expires_at > int(self.now())

    @staticmethod
    def _public(person: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not person.get("chat_id"):
            return None
        return {
            "name": person.get("name", ""),
            "username": person.get("username", ""),
            "user_id": person.get("user_id"),
            "chat_id": person.get("chat_id"),
            "bound_at": person.get("bound_at"),
        }

    def status(self) -> dict[str, Any]:
        data = self.store.read("pairing")
        alive = int(data.get("expires_at", 0)) > int(self.now())
        candidate = self._public(data.get("candidate") or {}) if alive else None
        pending = alive and bool(data.get("token_digest")) and candidate is None
        return {
            "pending": pending,
            "expires_at": int(data.get("expires_at", 0)) if alive else 0,
            "candidate": candidate,
            "owner": self._public(self.store.read("owner")),
        }

    def try_bind(
        self, text: str, *, user_id: int, chat_id: int, name: str = "", username: str = ""
    ) -> str:
        """accepted | wrong | expired | not_pending | cancelled (слишком много неверных)."""
        secret = self.store.secret()
        now = int(self.now())
        with self.store.locked("pairing") as data:
            if not data.get("token_digest") or data.get("candidate"):
                return "not_pending"
            if int(data.get("expires_at", 0)) <= now:
                data.clear()
                return "expired"
            candidate = extract_candidate(text)
            matched = candidate is not None and (
                tokens.same(candidate, str(data.get("token_digest", "")), secret)
                or tokens.same(candidate, str(data.get("code_digest", "")), secret)
            )
            if not matched:
                if candidate is None:
                    return "wrong"       # «/start» без значения и прочее не считаем перебором
                data["wrong"] = int(data.get("wrong", 0)) + 1
                if data["wrong"] >= PAIRING_MAX_WRONG:
                    data.clear()
                    return "cancelled"
                return "wrong"
            # Значение использовано; ждём подтверждения в мастере.
            data.pop("token_digest", None)
            data.pop("code_digest", None)
            data["candidate"] = {
                "user_id": int(user_id),
                "chat_id": int(chat_id),
                "name": (name or "").strip()[:120],
                "username": (username or "").strip()[:64],
                "bound_at": now,
            }
        return "accepted"

    def confirm(self) -> Optional[dict[str, Any]]:
        """Кандидат становится владельцем. Вызывается только из мастера, после входа."""
        now = int(self.now())
        with self.store.locked("pairing") as data:
            candidate = data.get("candidate")
            if not candidate or int(data.get("expires_at", 0)) <= now:
                data.clear()
                return None
            data.clear()
        self.store.write("owner", candidate)
        return self._public(candidate)

    def reject(self) -> None:
        """Кандидат — не владелец: привязка отменяется, прежний владелец остаётся."""
        self.store.delete("pairing")
