"""Как сейчас можно доставить ответ — одна функция для бота, приложения, автоответа и «Сейчас» (Р-105).

Черновик ответа (сервиса — `origin = 'suggest'`, или ассистента — `agent`) получает под карточкой кнопки
по одному правилу, и это правило — здесь. Бот согласований, приложение «Штурман» (раздел «Согласования»),
повторная проверка при нажатии, автоответ доверенным («ответит ли сам») и строка «Ответы» в «Сейчас» и в
`./ops/doctor.sh` спрашивают одно и то же — поэтому кнопки в боте и в приложении не расходятся (Д-46).

Факты (`Facts`) собирает `facts()` из тех же проверок, что стоят перед самой отправкой
(`outbox.policy.check_switch`, `check_channel`): источник правды один. Решение (`decide`) — чистая функция
фактов: её целиком перебирает тест-матрица (`tests/replies/test_delivery_matrix.py`).

  «Отправить» — отправка включена на сервере (`SHTURMAN_SENDING`; без своего бота согласований она не
                включается — `config.py`), ассистент не остановлен и:
                  * основной аккаунт: личный чат с человеком, бизнес-подключение с правом отвечать и
                    входящее этого человека за последние сутки (правило Telegram для бизнес-бота);
                  * аккаунт помощника: его сессия на связи;
                текст помещается в одно сообщение бизнес-бота и показан в карточке целиком.
  «В чат»      — когда «Отправить» нет (Р-105, часть 2): «Черновик ответа» сервиса в личном чате основного
                аккаунта, свой бот согласований, ассистент не остановлен, сессия основного аккаунта на связи,
                текст в одно сообщение. Кладёт текст в поле ввода чата владельца (`replies/put.py`): владелец
                правит и отправляет сам. Это не отправка — от `SHTURMAN_SENDING` не зависит. «Отправить» и
                «В чат» вместе не бывают (решение владельца).
  «Скопировать» — только в боте, текст не длиннее 256 знаков (предел кнопки Telegram).

Чего здесь нет — запросов к Telegram и решений владельца: функция только отвечает «что можно сейчас».
Окончательную проверку делает нажатие (`outbox.drafts.on_button` → `policy.check_send`).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

import asyncpg

from .. import bridge, words
from ..outbox import policy
from ..outbox.text import utf16_len

COPY_LIMIT = 256            # «Скопировать» (CopyTextButton) — до 256 знаков
BUSINESS_TEXT_LIMIT = 4096  # бизнес-бот отправляет текст одним сообщением

# Почему «Отправить» нет — словами для владельца (строка «Ответы», ответ на нажатие).
MISSING = {
    "stopped": "ассистент остановлен",
    "no_own_bot": "нет своего бота согласований",
    "sending_off": "отправка выключена на сервере",
    "not_private": "это не личный чат с человеком",
    "business_absent": "бот согласований не подключён в бизнес-режиме",
    "business_no_reply": "боту согласований в бизнес-режиме не разрешено отвечать",
    "window_closed": "прошло больше суток после сообщения человека",
    "helper_down": f"{words.HELPER_ACCOUNT} не на связи",
    "too_long": "текст длиннее одного сообщения",
    "card_cut": "текст не поместился в карточку целиком",
    "not_pending": "черновик уже не ждёт решения",
    "owner_session_down": "основной аккаунт сейчас не на связи",
    "not_suggest": "«В чат» — только для черновика ответа сервиса",
}


@dataclass(frozen=True)
class Facts:
    """Всё, от чего зависят кнопки. Собирает `facts()`; тест-матрица задаёт их сама."""
    role: str = "owner"            # owner — основной аккаунт, assistant — аккаунт помощника
    private: bool = True           # личный чат с человеком (не группа, не бот, не «Избранное»)
    own_bot: bool = True           # для строки «Ответы»; «Отправить» решает `sending` (он требует своего бота)
    sending: bool = False          # SHTURMAN_SENDING
    business: bool = False         # бизнес-подключение с правом отвечать
    window: bool = True            # входящее этого человека — не старше суток (с запасом)
    helper: bool = False           # сессия помощника на связи
    stopped: bool = False          # «Остановить всё» (Р-87)
    text_len: int = 0              # знаков UTF-16
    card_whole: bool = True        # текст показан в карточке целиком
    pending: bool = True           # черновик ждёт решения
    suggest: bool = True           # черновик ответа сервиса (или «Записано»), а не черновик ассистента
    owner_session: bool = False    # сессия основного аккаунта на связи — можно положить текст в поле ввода


@dataclass(frozen=True)
class Avail:
    send: str | None = None        # business | session | None
    send_missing: str | None = None
    copy: bool = False
    missing: tuple[str, ...] = field(default_factory=tuple)   # все причины, по порядку
    put: bool = False              # «В чат»
    put_missing: str | None = None

    @property
    def why(self) -> str | None:
        return MISSING.get(self.send_missing or "", self.send_missing) if self.send_missing else None


def decide(f: Facts) -> Avail:
    """Что можно сделать с черновиком сейчас. Чистая функция: тот же ответ на те же факты."""
    channel = "business" if f.role == "owner" else "session"
    missing: list[str] = []
    if not f.pending:
        missing.append("not_pending")
    if f.stopped:
        missing.append("stopped")
    if not f.sending:
        missing.append("sending_off")
    if channel == "business":
        if not f.private:
            missing.append("not_private")
        if not f.business:
            missing.append("business_absent")
        if not f.window:
            missing.append("window_closed")
        if f.text_len > BUSINESS_TEXT_LIMIT:
            missing.append("too_long")
    elif not f.helper:
        missing.append("helper_down")
    if not f.card_whole:
        missing.append("card_cut")
    copy = f.pending and 0 < f.text_len <= COPY_LIMIT
    if not missing:
        return Avail(send=channel, copy=copy)
    put_missing = why_no_put(f)
    return Avail(send=None, send_missing=missing[0], copy=copy, missing=tuple(missing),
                 put=put_missing is None, put_missing=put_missing)


def why_no_put(f: Facts) -> str | None:
    """Почему «В чат» нельзя — первый код по порядку, или None. Без учёта «Отправить»: у карточки «Записано»
    отправки нет вовсе, и «В чат» там решает только это (у черновика — `decide`, где «Отправить» главнее)."""
    return next((code for ok, code in (
        (f.pending, "not_pending"), (f.suggest, "not_suggest"), (not f.stopped, "stopped"),
        (f.own_bot, "no_own_bot"), (f.role == "owner" and f.private, "not_private"),
        (0 < f.text_len <= BUSINESS_TEXT_LIMIT, "too_long"), (f.owner_session, "owner_session_down")) if not ok), None)


async def facts(conn: asyncpg.Connection, state: Any, tgt: policy.Target, *, text: str = "",
                card_whole: bool = True, pending: bool = True, suggest: bool = True) -> Facts:
    """Факты о чате из архива и настроек работающего сервиса — те же, что проверяются перед отправкой."""
    rules = await policy.load(conn, state.config)
    tg = state.extras.get("tg") if state is not None else None
    business = helper = owner_session = False
    window = True
    if tgt.account_role == "owner":
        owner_session = tg is not None and hasattr(tg, "can_put") and bool(tg.can_put(tgt.account_id))
        business = await policy.business_connection(conn, tgt.account_id) is not None
        window = await policy.has_fresh_incoming(conn, rules, tgt.chat_id)
    else:
        helper = tg is not None and bool(tg.can_send(tgt.account_id))
    # исключённый чат — не «личный чат», куда можно ответить: ни «Отправить», ни «В чат» (как `put.check`)
    return Facts(role=tgt.account_role, private=tgt.is_private and not tgt.excluded, own_bot=bridge.owns_bot(),
                 sending=policy.check_switch(rules).ok, business=business, window=window, helper=helper,
                 stopped=not (await policy.check_halt(conn)).ok, text_len=utf16_len(text or ""),
                 card_whole=card_whole, pending=pending, suggest=suggest, owner_session=owner_session)


async def for_chat(conn: asyncpg.Connection, state: Any, tgt: policy.Target, **kwargs: Any) -> Avail:
    return decide(await facts(conn, state, tgt, **kwargs))


async def put_possible(conn: asyncpg.Connection, state: Any, tgt: policy.Target | None, text: str) -> bool:
    """«В чат» под карточкой «Записано» (там нет «Отправить»)."""
    if tgt is None or state is None:
        return False
    return why_no_put(await facts(conn, state, tgt, text=text)) is None


async def for_draft(conn: asyncpg.Connection, state: Any, row: Mapping[str, Any],
                    tgt: policy.Target | None, *, card_whole: bool = True) -> Avail:
    """Для строки `outbox_drafts`: ждёт ли она решения и что с ней можно сделать сейчас."""
    if tgt is None:
        return decide(Facts(pending=False))
    found = await facts(conn, state, tgt, text=row["text"], card_whole=card_whole,
                        pending=row["status"] == "pending" and row["text"] != "", suggest=row["origin"] == "suggest")
    if row["channel"] == "business" and tgt.account_role != "owner" or \
            row["channel"] == "session" and tgt.account_role != "assistant":
        found = replace(found, pending=False)
    return decide(found)


async def auto_send_possible(conn: asyncpg.Connection, state: Any, tgt: policy.Target) -> bool:
    """Ответит ли автоответ доверенному сам — прямо сейчас: отправка включена, ассистент не остановлен и
    канал аккаунта доступен (бизнес-подключение с правом отвечать или сессия помощника). Нет — автоответ
    задачу не заводит, а доверенный с включённым автоответом получает «Черновик ответа» (Р-105)."""
    rules = await policy.load(conn, state.config)
    if not policy.check_switch(rules).ok or not (await policy.check_halt(conn)).ok:
        return False
    channel, decision = policy.pick_channel(tgt, None)
    if channel is None:
        return False
    tg = state.extras.get("tg")
    return (await policy.check_channel(conn, tg, rules, tgt, channel)).ok


def buttons(avail: Avail, *, draft: Mapping[str, Any] | None, item: Mapping[str, Any] | None = None,
            link: str | None = None, bot: bool = True, chosen: str | None = None,
            voice: bool = True) -> list[list[dict[str, Any]]]:
    """Кнопки под черновиком — одни и те же в боте и в приложении (Д-46). `bot=False` — приложение: без
    «Скопировать», «Открыть чат» и голоса (их там нет). `item` — строка подготовки черновика сервиса
    (`rehearsal_items`): «🎙 Наговорить свой» и «Не годится»; без неё — черновик ассистента: «Отклонить»."""
    rows: list[list[dict[str, Any]]] = []
    first: list[dict[str, Any]] = []
    if draft is not None and avail.send:
        first.append(bridge.button(words.SEND, "ob", f"s:{draft['id']}:{draft['nonce']}"))
    elif draft is not None and item is not None and avail.put:
        first.append(bridge.button(words.PUT, "ob", f"p:{draft['id']}:{draft['nonce']}"))
    if draft is not None and item is None and draft.get("status") == "pending":
        first.append(bridge.button(words.REJECT, "ob", f"r:{draft['id']}:{draft['nonce']}"))
    if first:
        rows.append(first)
    if bot and draft is not None and avail.copy:
        rows.append([{"text": words.COPY, "copy": draft["text"]}])
    if bot and voice:
        from ..dictation import core as dictation
        if item is not None:
            rows += dictation.voice_row("r", item["id"], item["nonce"], label=words.DICTATE_OWN)
        elif draft is not None:
            rows += dictation.voice_row("d", draft["id"], draft["nonce"])
    if item is not None:
        mark = "✓ " if chosen == "bad" else ""
        rows.append([bridge.button(mark + words.NOT_GOOD, "rh", f"n:{item['id']}:{item['nonce']}")])
    if bot and link:
        rows.append([{"text": words.OPEN_CHAT, "url": link}])
    return rows


def callbacks(rows: list[list[dict[str, Any]]]) -> list[str]:
    """Данные кнопок сервиса — то, чем сравниваются бот и приложение."""
    return [b["data"] for row in rows for b in row if isinstance(b, dict) and isinstance(b.get("data"), str)]


# --- строка «Ответы» ----------------------------------------------------------------------------------

async def summary(conn: asyncpg.Connection, state: Any) -> dict[str, Any]:
    """Что сейчас с доставкой ответов в личных чатах основного аккаунта — только признаки и коды, без имён.
    Для «Сейчас» (/status), `GET /api/status` и строки `replies` в `./ops/doctor.sh`."""
    config = state.config
    rules = await policy.load(conn, config)
    account = await conn.fetchval("SELECT id FROM accounts WHERE role = 'owner' ORDER BY id LIMIT 1")
    missing: list[str] = []
    if not (await policy.check_halt(conn)).ok:
        missing.append("stopped")
    if not bridge.owns_bot():
        missing.append("no_own_bot")
    if not policy.check_switch(rules).ok:
        missing.append("sending_off")
    business = False
    if account is None:
        missing.append("no_main_account")
    else:
        business = await policy.business_connection(conn, account) is not None
        if not business:
            known = await conn.fetchrow(
                """SELECT bool_or(enabled) AS enabled, bool_or(enabled AND NOT can_reply) AS no_reply
                   FROM business_connections WHERE account_id = $1 AND via = 'service'""", account)
            # подключение бота Hermes (схема до 0.0.6) для отправки не годится никогда (Д-45)
            missing.append("business_no_reply" if known and known["no_reply"] else "business_absent")
    helper = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM accounts WHERE role = 'assistant')")
    tg = state.extras.get("tg")
    helper_up = False
    if helper and tg is not None:
        ids = [r["id"] for r in await conn.fetch("SELECT id FROM accounts WHERE role = 'assistant'")]
        helper_up = any(tg.can_send(i) for i in ids)
    put_missing: list[str] = [code for code in missing if code in ("stopped", "no_own_bot", "no_main_account")]
    if account is not None and not (tg is not None and hasattr(tg, "can_put") and tg.can_put(account)):
        put_missing.append("owner_session_down")
    # Автоответ доверенным (Д-51): у скольких аккаунтов включён и ждёт ли отправки сообщение владельцу о том,
    # что обновление до 0.0.12 его выключило (`outbox/service.announce_upgrade`; ключ живёт, пока владелец не
    # привязан). Оператор видит это по строке `replies` в `./ops/doctor.sh` — без входа в базу.
    autoreply_on = await conn.fetchval("SELECT count(*) FROM outbox_accounts WHERE autoreply_enabled")
    announce_pending = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM settings WHERE key = 'upgrade.autoreply_disabled')")
    return {"send": {"available": not missing, "channel": "business", "missing": missing},
            "put": {"available": not put_missing, "missing": put_missing},
            "helper": {"connected": bool(helper), "sending": bool(helper_up)},
            "autoreply": {"accounts_on": int(autoreply_on or 0), "announce_pending": bool(announce_pending)}}


def summary_line(found: Mapping[str, Any]) -> str:
    """«Ответы: …» — для владельца, одной строкой."""
    send, put = found["send"], found.get("put") or {"available": False, "missing": []}
    if send["available"]:
        return (f"Ответы: «{words.SEND}» доступно — ответ уходит от вашего имени через бота согласований, "
                "в течение суток после сообщения человека и только после вашего нажатия.")
    def said(codes):
        return "; ".join(MISSING.get(code, NO_MAIN if code == "no_main_account" else code) for code in codes)
    line = f"Ответы: «{words.SEND}» недоступно — {said(send['missing'])}."
    if put["available"]:
        return line + (f" «{words.PUT}» — доступно: текст кладётся в поле ввода чата, отправляете вы.")
    return line + f" «{words.PUT}» недоступно — {said(put['missing'])}. Текст черновика можно скопировать."


NO_MAIN = "основной аккаунт не подключён"
