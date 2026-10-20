"""Граница между сессиями Telegram и остальным сервисом.

Остальные модули не трогают Telethon напрямую: они получают объект с этим интерфейсом из
`state.extras["tg"]`. Так роль «только чтение» нельзя обойти из соседнего модуля, а модули
отправки проверяются тестами без сети.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol


class SendForbidden(Exception):
    """У аккаунта нет права отправки: это основной аккаунт владельца, роль «только чтение»."""


class AccountUnavailable(Exception):
    """Сессия аккаунта не запущена или потеряла авторизацию."""


class MediaUnavailable(Exception):
    """Вложения нет: сообщение удалено, файл другой или больше предела. Повторять бессмысленно."""


class DraftBusy(Exception):
    """В поле ввода чата уже есть другой текст владельца: «В чат» его не перезаписывает (Р-105)."""


class Halted(Exception):
    """Ассистент остановлен («Остановить всё») — последняя проверка перед записью в Telegram."""


class DraftRefused(Exception):
    """Telegram ответил отказом на запись облачного черновика — она не применена."""


class PutUnknown(Exception):
    """Запись облачного черновика ушла, а ответа нет (оборвалась связь): текст ещё может появиться в поле."""


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
        reply_to_tg_id: int | None = None, topic_tg_id: int | None = None,
    ) -> int:
        """Отправляет одно сообщение (до 4096 знаков) и возвращает его идентификатор в Telegram.

        Бросает SendForbidden, AccountUnavailable, FloodWait. Сам не повторяет и не ждёт.
        Прочитанным ничего не отмечает.
        """

    async def set_typing(self, account_id: int, peer_class: str, tg_id: int, on: bool) -> None:
        """Показывает или гасит «печатает…». Ошибки проглатывает: индикатор не важнее ответа."""

    def can_put(self, account_id: int) -> bool:
        """Запущена ли сессия основного аккаунта (роль owner) — можно ли положить текст в поле ввода."""

    async def put_draft(self, account_id: int, peer_class: str, tg_id: int, text: str, *,
                        reply_to_tg_id: int | None = None,
                        before_write: Callable[[], Awaitable[bool]] | None = None) -> str:
        """Кладёт текст облачным черновиком в поле ввода чата основного аккаунта (Р-105). Возвращает
        «placed» или «already» (там уже ровно этот текст); бросает DraftBusy (в поле другой текст),
        Halted (`before_write` сказал «нельзя»), AccountUnavailable, FloodWait (до записи), DraftRefused
        (Telegram отказал), PutUnknown (запись ушла, ответа нет). Сам не повторяет; запись по времени не
        прерывает. Текст из поля только сравнивается."""

