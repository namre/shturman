"""Вход в аккаунт Telegram по QR-коду — единственный способ входа в сервисе.

Владелец видит QR на экране настройки и подтверждает вход в приложении Telegram
(«Настройки → Устройства → Подключить устройство»). Кода из SMS и номера телефона сервис
не запрашивает. Если на аккаунте включён облачный пароль, он вводится один раз, уходит
в Telegram в виде доказательства SRP и нигде не остаётся: ни в объекте входа, ни в журнале,
ни в базе.

Что важно в Telethon 1.x (сверено с 1.45.0):
  * ожидание (`QRLogin.wait`) должно уже идти, когда код сканируют;
  * токен живёт около 30 секунд — до истечения выпускается новый через `QRLogin.recreate()`
    на том же подключённом клиенте;
  * при облачном пароле `wait()` бросает `SessionPasswordNeededError`, дальше —
    `sign_in(password=...)` на том же клиенте.

Состояния: pending → (password_required →) completed | expired | failed | cancelled.

# Основано на leshchenko1979/fast-mcp-telegram (MIT), src/server_components/qr_login.py@6905c6b
# (набор состояний; обновление кода сделано через recreate(), а не новым клиентом)
# Основано на j2h4u/mcp-telegram (MIT; форк sparfenyuk/mcp-telegram, MIT),
#   deploy/telegram_qr_login.py@1acce79 (запас времени до истечения кода, попытки пароля)
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import segno
from telethon import errors
from telethon.tl import functions

logger = logging.getLogger("shturman.tg")

PENDING = "pending"
PASSWORD_REQUIRED = "password_required"
COMPLETED = "completed"
EXPIRED = "expired"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = (COMPLETED, EXPIRED, FAILED, CANCELLED)


class LoginStateError(Exception):
    """Действие не подходит к текущему состоянию входа."""


class LoginRejected(Exception):
    """Вход состоялся, но аккаунт не подходит к роли. Текст — для владельца."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def qr_svg(url: str) -> str:
    """QR-код ссылки входа в виде встраиваемого SVG."""
    return segno.make(url, error="m").svg_inline(scale=6, border=3, dark="#000000", light="#ffffff")


class LoginFlow:
    """Один вход по QR: от показа кода до готовой сессии или отказа."""

    def __init__(
        self, client: Any, role: str, *,
        on_success: Callable[[Any], Awaitable[int | None]],
        on_close: Callable[[], Awaitable[None]],
        lifetime: float = 300.0, refresh_margin: float = 5.0,
        password_timeout: float = 300.0, password_attempts: int = 3,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.id = secrets.token_urlsafe(18)
        self.role = role
        self.status = PENDING
        self.error: str | None = None
        self.hint: str | None = None
        self.account_id: int | None = None
        self.attempts_left = password_attempts
        self._client = client
        self._on_success, self._on_close = on_success, on_close
        self._lifetime, self._margin = lifetime, refresh_margin
        self._password_timeout = password_timeout
        self._clock = clock
        self._qr: Any = None
        self._deadline = 0.0  # отметка времени, после которой вход считается просроченным
        self._task: asyncio.Task | None = None
        self._inbox: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._reply: asyncio.Future | None = None
        self._finished = asyncio.Event()

    # --- состояние для экрана входа ---

    @property
    def done(self) -> bool:
        return self.status in TERMINAL

    def snapshot(self) -> dict[str, Any]:
        """Состояние входа для экрана. QR и ссылка отдаются только пока код ждёт сканирования;
        сам токен нигде больше не появляется."""
        out: dict[str, Any] = {"login_id": self.id, "role": self.role, "status": self.status}
        if self.status == PENDING and self._qr is not None:
            out["qr_svg"] = qr_svg(self._qr.url)
            out["link"] = self._qr.url
            out["expires_at"] = self._qr.expires.isoformat()
        if self.status == PASSWORD_REQUIRED:
            out["hint"] = self.hint
            out["attempts_left"] = self.attempts_left
        if self.error:
            out["error"] = self.error
        if self.account_id is not None:
            out["account_id"] = self.account_id
        return out

    # --- запуск и остановка ---

    async def start(self) -> None:
        """Подключается и выпускает первый код. Ошибки пробрасывает: вход не начался."""
        from .. import authority
        await asyncio.get_running_loop().create_task(self._client.connect(),
                    name="tg-qr-connect", context=authority.background_context())
        self._qr = await self._client.qr_login()
        self._deadline = self._clock().timestamp() + self._lifetime
        from .. import authority
        self._task = asyncio.get_running_loop().create_task(self._run(), name=f"tg-login-{self.role}",
                                                          context=authority.background_context())

    async def wait_finished(self) -> None:
        await self._finished.wait()

    async def cancel(self) -> None:
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        if not self._finished.is_set():
            # Задачу отменили раньше, чем она успела начаться: её уборка не выполнилась.
            self.status = CANCELLED
            await self._finalize()

    async def submit_password(self, password: str) -> None:
        """Передаёт облачный пароль и дожидается результата проверки. Пароль не сохраняется."""
        if self.status != PASSWORD_REQUIRED:
            raise LoginStateError("пароль сейчас не требуется")
        if self._reply is not None and not self._reply.done():
            raise LoginStateError("предыдущий пароль ещё проверяется")
        reply = asyncio.get_running_loop().create_future()
        self._reply = reply
        self._inbox.put_nowait(password)
        await reply

    # --- ход входа ---

    def _remaining(self) -> float:
        return self._deadline - self._clock().timestamp()

    def _answer(self) -> None:
        if self._reply is not None and not self._reply.done():
            self._reply.set_result(None)

    async def _run(self) -> None:
        try:
            user = await self._wait_scan()
            if user is None:
                return
            self.account_id = await self._on_success(user)
            self.error = None
            self.status = COMPLETED
        except asyncio.CancelledError:
            self.status = CANCELLED
            raise
        except LoginRejected as exc:
            self.status, self.error = FAILED, str(exc)
        except errors.FloodWaitError as exc:
            self.status = FAILED
            self.error = f"Telegram просит подождать {int(exc.seconds)} с. Попробуйте позже."
        except Exception as exc:
            # В журнал — только вид ошибки: в тексте исключения могут оказаться данные запроса.
            logger.warning("вход по QR (%s) не удался: %s", self.role, type(exc).__name__)
            self.status, self.error = FAILED, "Не удалось войти. Попробуйте ещё раз."
        finally:
            await self._finalize()

    async def _finalize(self) -> None:
        self._qr = None
        if self.status != COMPLETED:
            if self.status not in TERMINAL:
                self.status = FAILED
            try:
                await self._on_close()
            except Exception as exc:
                logger.warning("уборка после входа (%s): %s", self.role, type(exc).__name__)
        self._answer()
        self._finished.set()

    async def _wait_scan(self) -> Any:
        while True:
            remaining = self._remaining()
            if remaining <= 0:
                self.status, self.error = EXPIRED, "Время на вход истекло."
                return None
            until_expiry = (self._qr.expires - self._clock()).total_seconds() - self._margin
            try:
                return await self._qr.wait(timeout=max(1.0, min(until_expiry, remaining)))
            except asyncio.TimeoutError:
                if self._remaining() <= 0:
                    self.status, self.error = EXPIRED, "Время на вход истекло."
                    return None
                await self._qr.recreate()  # новый код на том же клиенте
            except errors.SessionPasswordNeededError:
                return await self._password_phase()

    async def _password_phase(self) -> Any:
        try:
            self.hint = (await self._client(functions.account.GetPasswordRequest())).hint or None
        except errors.RPCError:
            self.hint = None
        self._qr = None
        self.status = PASSWORD_REQUIRED
        while True:
            try:
                password = await asyncio.wait_for(self._inbox.get(), timeout=self._password_timeout)
            except asyncio.TimeoutError:
                self.status, self.error = EXPIRED, "Время на ввод пароля истекло."
                return None
            try:
                user = await self._client.sign_in(password=password)
            except errors.PasswordHashInvalidError:
                self.attempts_left -= 1
                if self.attempts_left <= 0:
                    self.status, self.error = FAILED, "Пароль введён неверно слишком много раз."
                    return None
                self.error = "Неверный облачный пароль."
                self._answer()
                continue
            finally:
                password = None  # noqa: F841 — не держим пароль дольше одной проверки
            return user

