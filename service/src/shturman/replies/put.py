"""«В чат»: текст ответа — в поле ввода чата владельца, облачным черновиком Telegram (Р-105, часть 2).

Нажатие «В чат» под «Черновиком ответа» (`sh:ob:p`) или под карточкой «Записано» (`sh:vr:p`) — и сервис
кладёт текст в поле ввода этого личного чата основного аккаунта (`messages.saveDraft`): владелец видит его на
всех своих устройствах, правит и отправляет сам. Сервис при этом ничего не отправляет: это не отправка, и от
выключателя `SHTURMAN_SENDING` «В чат» не зависит.

Единственная запись основного аккаунта в Telegram — этот запрос, и только здесь:
  * только по нажатию проверенного владельца — в своём боте согласований (`telegram.callback`) или в
    приложении «Штурман» (`telegram.miniapp`); не из фоновой работы, не из команды, не со страницы, не через
    Hermes (`require_press`, мутационный якорь `put_requires_owner_press`);
  * только личный чат основного аккаунта с человеком, свой бот, ассистент не остановлен, сессия основного
    аккаунта на связи и не на паузе;
  * поле ввода владельца не перезаписывается: перед записью сервис читает облачный черновик этого чата
    (`messages.getPeerDialogs`) — пусто: кладёт; там ровно этот текст: «уже лежит»; другой текст: не трогает
    (мутационный якорь `put_never_overwrites_owner_text`). Прочитанный текст владельца только сравнивается —
    нигде не хранится и не пишется в журнал;
  * «Остановить всё» проверяется дважды: до нажатия (`check`) и прямо перед записью (`put`);
  * запись по времени не прерывается: прерванная, она могла бы уйти после переподключения и лечь поверх
    того, что владелец начал набирать. Исход «неизвестно» (связь оборвалась после отправки) — повтора нет,
    отметка «кладу» остаётся `BUSY_SECONDS`, владелец проверяет поле сам;
  * известный предел: черновик с другого устройства, пришедший в облако между чтением и записью (доли
    секунды), будет заменён — проверяется вживую;
  * не чаще `HOURLY_CAP` раз в час.
В журнал действий — номер черновика (или записи) и код исхода, без текста.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

from .. import authority, bridge
from ..outbox import policy
from ..outbox.text import utf16_len

logger = logging.getLogger("shturman.replies.put")

PRESS_ACTIONS = frozenset({"telegram.callback", "telegram.miniapp"})
TEXT_LIMIT = 4096
HOURLY_CAP = 60
BUSY_SECONDS = 60             # после нажатия (и после «не удалось узнать») повторное нажатие ждёт столько

# Исходы словами — для ответа на нажатие.
WORDS = {
    "placed": "Положил в поле ввода — отправьте сами, когда будете готовы.",
    "already": "Этот текст уже лежит в поле ввода.",
    "busy": ("В поле ввода этого чата уже есть текст — не трогаю его. Скопируйте ответ из карточки "
             "(нажмите на текст)."),
    "unavailable": "Основной аккаунт сейчас не на связи — текст не положен. Скопируйте его из карточки или нажмите позже.",
    "flood": "Telegram просит подождать — текст не положен. Нажмите «В чат» чуть позже.",
    "unknown": ("Не удалось узнать, лёг ли текст: он ещё может появиться в поле ввода. Минуту ничего не "
                "набирайте в этом чате, потом проверьте поле; если пусто — нажмите «В чат» ещё раз. Само "
                "повторно не кладётся."),
    "refused": "Telegram не принял текст — он не положен. Скопируйте его из карточки.",
    "stopped": "Пока нельзя: ассистент остановлен. Продолжить — /resume.",
    "no_own_bot": "«В чат» работает только со своим ботом согласований.",
    "not_private": "«В чат» — только для личных чатов вашего основного аккаунта.",
    "too_long": "Текст длиннее одного сообщения — скопируйте его из карточки.",
    "cap": f"«В чат» — не чаще {HOURLY_CAP} раз в час. Скопируйте текст из карточки.",
    "not_pressed": "«В чат» срабатывает только от вашего нажатия.",
}


class Refused(Exception):
    """Нажатие не от проверенного владельца — «В чат» не выполняется вовсе."""


@dataclass(frozen=True)
class PutResult:
    code: str

    @property
    def placed(self) -> bool:
        return self.code in ("placed", "already")

    @property
    def text(self) -> str:
        return WORDS.get(self.code, WORDS["unknown"])


def require_press() -> authority.AuthorityPrincipal:
    """Только нажатие проверенного владельца — в его боте согласований или в приложении «Штурман»."""
    principal = authority.get_owner_principal()
    if principal is None or principal.source != "telegram" or principal.action not in PRESS_ACTIONS:
        raise Refused(WORDS["not_pressed"])
    return principal


async def check(conn: asyncpg.Connection, tgt: policy.Target | None, text: str) -> str | None:
    """Почему положить нельзя — код из `WORDS`, или None. Без сети: перед `put` и для кнопки."""
    if not bridge.owns_bot():
        return "no_own_bot"
    if not (await policy.check_halt(conn)).ok:
        return "stopped"
    if tgt is None or tgt.account_role != "owner" or not tgt.is_private or tgt.excluded:
        return "not_private"
    if not text or utf16_len(text) > TEXT_LIMIT:
        return "too_long"
    used = await conn.fetchval(
        """SELECT (SELECT count(*) FROM outbox_drafts WHERE put_started_at > now() - interval '1 hour')
                + (SELECT count(*) FROM dictations WHERE put_started_at > now() - interval '1 hour')
                + (SELECT count(*) FROM reminders WHERE put_started_at > now() - interval '1 hour')""")
    if used >= HOURLY_CAP:
        return "cap"
    return None


async def put(state: Any, tgt: policy.Target, text: str, *, reply_to_tg_id: int | None) -> PutResult:
    """Кладёт текст в поле ввода чата `tgt`. Сеть — здесь, вне транзакций; повторов нет.

    Ограничение по времени — только до записи (чтение поля, `TgManager.put_draft`): поставленную запись
    не прерывают, иначе она могла бы лечь позже. Прямо перед записью — ещё раз «Остановить всё»."""
    require_press()
    tg = state.extras.get("tg") if state is not None else None
    if tg is None or not hasattr(tg, "put_draft"):
        return PutResult("unavailable")
    pool = getattr(state, "pool", None)

    async def before_write() -> bool:
        if pool is None:
            return False
        async with pool.acquire() as conn:
            return not await conn.fetchval("SELECT shturman_halted()")

    from ..tg.gateway import AccountUnavailable, DraftBusy, DraftRefused, FloodWait, Halted, PutUnknown
    try:
        outcome = await tg.put_draft(tgt.account_id, tgt.peer_class, tgt.tg_id, text,
                                     reply_to_tg_id=reply_to_tg_id, before_write=before_write)
    except DraftBusy:
        return PutResult("busy")
    except FloodWait:
        return PutResult("flood")
    except AccountUnavailable:
        return PutResult("unavailable")
    except Halted:
        return PutResult("stopped")
    except DraftRefused:
        return PutResult("refused")
    except PutUnknown as exc:
        logger.warning("«В чат»: запись ушла, ответа нет (%s)", exc)
        return PutResult("unknown")
    except asyncio.CancelledError:
        if bridge.own_task_cancelled():
            raise
        logger.warning("«В чат»: ожидание ответа Telegram отменено изнутри — исход неизвестен")
        return PutResult("unknown")
    except Exception as exc:  # noqa: BLE001 — запрос мог уйти: исход неизвестен, повтора нет
        logger.warning("«В чат»: ответ Telegram не получен (%s)", type(exc).__name__)
        return PutResult("unknown")
    return PutResult(outcome if outcome in ("placed", "already") else "unknown")
