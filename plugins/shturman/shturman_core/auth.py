"""Вход в дашборд: ссылка активации, одноразовый код от бота, сессии.

Здесь только правила. Отправка сообщения в Telegram передаётся снаружи функцией `send_code`,
поэтому модуль проверяется тестами без сети и без Hermes.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import tokens
from .state import Store

ACTIVATION_TTL = 30 * 60          # ссылка активации живёт 30 минут
LOGIN_CODE_TTL = 5 * 60           # код входа — 5 минут
LOGIN_RETRY_INTERVAL = 60         # после неудачной отправки — новая попытка не раньше чем через минуту
LOGIN_CODE_DIGITS = 8
LOGIN_MAX_ATTEMPTS = 5            # неверных попыток подряд до блокировки
LOCK_STEPS = (60, 5 * 60, 15 * 60, 60 * 60)   # блокировки растут: 1, 5, 15, 60 минут
ACCESS_TTL = 12 * 60 * 60
SESSION_MAX_AGE = 30 * 24 * 60 * 60           # от входа до обязательного повторного входа
ACTIVATION_PREFIX = "a."
SEND_REQUEST = "send"             # служебное значение `code`: «пришлите мне код»

OWNER_SUBJECT = "owner"


@dataclass(frozen=True)
class SessionTokens:
    subject: str
    display_name: str
    access_token: str
    refresh_token: str
    expires_at: int


def revoke_sessions(store: Store) -> None:
    """Все выданные сессии перестают действовать."""
    with store.locked("sessions") as data:
        data["generation"] = int(data.get("generation", 0)) + 1


class Auth:
    def __init__(
        self,
        store: Store,
        *,
        now: Callable[[], float] = time.time,
        send_code: Optional[Callable[[dict[str, Any], str], None]] = None,
        notify: Optional[Callable[[dict[str, Any], str], None]] = None,
    ) -> None:
        self.store = store
        self.now = now
        self.send_code = send_code      # (owner, code) -> None; исключение = не отправлено
        self.notify = notify            # (owner, text) -> None; ошибки не важны

    # ------------------------------------------------------------------ владелец

    def owner(self) -> Optional[dict[str, Any]]:
        data = self.store.read("owner")
        return data if data.get("chat_id") else None

    # ------------------------------------------------------- ссылка активации

    def issue_activation(self) -> str:
        """Выдаёт значение для одноразовой ссылки. Предыдущая ссылка перестаёт действовать.

        Если владелец уже привязан, это ссылка восстановления: её использование отвязывает
        владельца и завершает все сессии (см. `redeem_activation`).
        """
        value = secrets.token_urlsafe(32)
        secret = self.store.secret()
        self.store.write("activation", {
            "digest": tokens.digest(value, secret),
            "expires_at": int(self.now()) + ACTIVATION_TTL,
            "kind": "recovery" if self.owner() else "activation",
        })
        return value

    def redeem_activation(self, value: str) -> bool:
        secret = self.store.secret()
        with self.store.locked("activation") as data:
            if not data:
                return False
            if int(data.get("expires_at", 0)) <= int(self.now()):
                data.clear()
                return False
            if not tokens.same(value, str(data.get("digest", "")), secret):
                return False
            recovery = data.get("kind") == "recovery"
            data.clear()                 # одноразовая
        # Вход по ссылке снимает блокировку перебора: владелец доказал, что сервер его.
        with self.store.locked("login") as login:
            login.clear()
        if recovery:
            # Восстановление нужно, когда Telegram владельца потерян или захвачен. Прежняя
            # привязка и все прежние сессии перестают действовать; бота привязывают заново.
            self.store.delete("owner")
            self.store.delete("pairing")
            revoke_sessions(self.store)
        return True

    # ----------------------------------------------------------- код от бота

    def request_login_code(self) -> str:
        """Готовит код входа и отправляет его владельцу.

        Возвращает:
          sent        — отправлен новый код;
          reused      — прежний код ещё действует, новый не создаётся и не отправляется;
          no_owner    — владелец не привязан;
          locked      — вход временно закрыт после неверных попыток;
          wait        — недавняя отправка не удалась, повторить можно через минуту;
          send_failed — Telegram не принял сообщение.

        Действующий код никогда не заменяется новым: иначе посторонний, запрашивая коды,
        обесценивал бы тот, что владелец сейчас вводит.
        """
        owner = self.owner()
        if owner is None:
            return "no_owner"
        now = int(self.now())
        secret = self.store.secret()
        with self.store.locked("login") as data:
            if int(data.get("lock_until", 0)) > now:
                return "locked"
            if data.get("digest") and int(data.get("expires_at", 0)) > now:
                return "reused"
            if now - int(data.get("attempt_at", 0)) < LOGIN_RETRY_INTERVAL and not data.get("digest"):
                return "wait"
            data["attempt_at"] = now
            data.pop("digest", None)
            if self.send_code is None:
                return "send_failed"
            code = "".join(secrets.choice("0123456789") for _ in range(LOGIN_CODE_DIGITS))
            try:
                self.send_code(owner, code)
            except Exception:
                return "send_failed"
            data["digest"] = tokens.digest(code, secret)
            data["expires_at"] = now + LOGIN_CODE_TTL
            # Счётчик неверных попыток новым кодом не обнуляется: иначе перебор шёл бы
            # по четыре попытки на каждый свежий код без единой блокировки.
        return "sent"

    def verify_login_code(self, code: str) -> str:
        """ok | wrong | expired | locked | none (код не запрашивали)."""
        code = "".join(ch for ch in str(code) if ch.isdigit())
        now = int(self.now())
        secret = self.store.secret()
        locked_now = False
        with self.store.locked("login") as data:
            if int(data.get("lock_until", 0)) > now:
                return "locked"
            if not data.get("digest"):
                return "none"
            if int(data.get("expires_at", 0)) <= now:
                data.pop("digest", None)
                return "expired"
            if tokens.same(code, str(data["digest"]), secret):
                data.clear()             # код одноразовый, счётчики блокировок сброшены
                return "ok"
            attempts = int(data.get("attempts", 0)) + 1
            data["attempts"] = attempts
            if attempts < LOGIN_MAX_ATTEMPTS:
                return "wrong"
            level = int(data.get("lock_level", 0))
            data["lock_until"] = now + LOCK_STEPS[min(level, len(LOCK_STEPS) - 1)]
            data["lock_level"] = level + 1
            data.pop("digest", None)
            data["attempts"] = 0
            locked_now = True
        if locked_now and self.notify is not None:
            owner = self.owner()
            if owner is not None:
                try:
                    self.notify(owner, "Кто-то несколько раз подряд ввёл неверный код входа. "
                                       "Вход временно закрыт. Если это были не вы, ничего делать "
                                       "не нужно: без кода из этого чата войти нельзя.")
                except Exception:
                    pass
        return "locked" if locked_now else "wrong"

    # ---------------------------------------------------------------- сессии

    def _generation(self) -> int:
        return int(self.store.read("sessions").get("generation", 0))

    def revoke_all_sessions(self) -> None:
        """Все выданные сессии перестают действовать (выход, утерянное устройство)."""
        revoke_sessions(self.store)

    def mint_session(self, *, born: Optional[int] = None) -> SessionTokens:
        """`born` — момент исходного входа; продление его не сдвигает."""
        now = int(self.now())
        born = int(born) if born is not None else now
        secret = self.store.secret()
        gen = self._generation()
        exp = min(now + ACCESS_TTL, born + SESSION_MAX_AGE)
        owner = self.owner() or {}
        common = {"sub": OWNER_SUBJECT, "gen": gen, "born": born}
        return SessionTokens(
            subject=OWNER_SUBJECT,
            display_name=str(owner.get("name") or "Владелец"),
            access_token=tokens.sign({**common, "kind": "access", "exp": exp}, secret),
            refresh_token=tokens.sign(
                {**common, "kind": "refresh", "exp": born + SESSION_MAX_AGE}, secret),
            expires_at=exp,
        )

    def _check(self, token: str, kind: str) -> Optional[dict[str, Any]]:
        payload = tokens.unsign(token, self.store.secret(), kind, now=self.now)
        if payload is None or payload.get("sub") != OWNER_SUBJECT:
            return None
        if int(payload.get("gen", -1)) != self._generation():
            return None
        return payload

    def verify_access(self, token: str) -> Optional[int]:
        """Срок действия токена доступа либо None."""
        payload = self._check(token, "access")
        return int(payload["exp"]) if payload else None

    def refresh_is_valid(self, refresh_token: str) -> bool:
        return self._check(refresh_token, "refresh") is not None

    def refresh(self, refresh_token: str) -> Optional[SessionTokens]:
        payload = self._check(refresh_token, "refresh")
        if payload is None:
            return None
        return self.mint_session(born=int(payload.get("born", 0)))

    # ---------------------------------------------- единая точка для провайдера

    def complete(self, code: str) -> tuple[bool, str]:
        """Разбирает то, что пришло в `code` на /auth/callback. Возвращает (вход выполнен, причина).

        Три вида значения: «send» — просьба прислать код (вход не выполняется, причина — итог
        отправки); «a.<значение>» — ссылка активации; цифры — код от бота.
        """
        code = (code or "").strip()
        if code == SEND_REQUEST:
            return False, self.request_login_code()
        if code.startswith(ACTIVATION_PREFIX):
            ok = self.redeem_activation(code[len(ACTIVATION_PREFIX):])
            return ok, "ok" if ok else "activation_invalid"
        result = self.verify_login_code(code)
        return result == "ok", result
