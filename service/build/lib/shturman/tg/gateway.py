"""Граница между сессиями Telegram и остальным сервисом.

Остальные модули не трогают Telethon напрямую: они получают объект с этим интерфейсом из
`state.extras["tg"]`. Так роль «только чтение» нельзя обойти из соседнего модуля, а модули
отправки проверяются тестами без сети.
"""

from __future__ import annotations

from typing import Protocol


class SendForbidden(Exception):
    """У аккаунта нет права отправки: это основной аккаунт владельца, роль «только чтение»."""


class AccountUnavailable(Exception):
    """Сессия аккаунта не запущена или потеряла авторизацию."""


class FloodWait(Exception):
    """Telegram просит подождать. Повторять раньше, чем через `seconds`, нельзя."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"Telegram просит подождать {seconds} с")
        self.seconds = int(seconds)


class TgGateway(Protocol):
    def can_send(self, account_id: int) -> bool:
        """Запущена ли сессия аккаунта и разрешена ли ему отправка (роль assistant)."""

    async def send_text(
        self, account_id: int, peer_class: str, tg_id: int, text: str, *,
        reply_to_tg_id: int | None = None,
    ) -> int:
        """Отправляет одно сообщение (до 4096 знаков) и возвращает его идентификатор в Telegram.

        Бросает SendForbidden, AccountUnavailable, FloodWait. Сам не повторяет и не ждёт.
        Прочитанным ничего не отмечает.
        """

    async def set_typing(self, account_id: int, peer_class: str, tg_id: int, on: bool) -> None:
        """Показывает или гасит «печатает…». Ошибки проглатывает: индикатор не важнее ответа."""
