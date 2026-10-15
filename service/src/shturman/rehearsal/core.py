"""Репетиция ответов на основном аккаунте владельца (решение Р-85).

Что делает. На новое живое входящее сообщение в личном чате основного аккаунта от человека из списка
репетиции сервис готовит ответ **так же, как автоответ доверенным** (уровень 2а): без инструментов,
справка — последние сообщения этого чата и найденное в архиве в этом же чате (`autoreply.gather_context`),
тот же запрос к модели и тот же ограниченный набор исходов (`replies.workflow.model_request`,
`replies.model`). Потом присылает владельцу в бот согласований одну карточку: кто написал, его
сообщение (одной строкой, обезвреженное — как уведомление Р-84), что ответил бы ассистент, и три
кнопки оценки — «Годится», «Почти — поправил бы», «Не годится». Оценка хранится для статистики.

Чего здесь нет и быть не может — отправки. Основной аккаунт подключён только на чтение: его клиент
Telegram не пропускает ни одного запроса отправки (`tg/client.py`, роль owner). Репетиция не создаёт
ни черновика (`outbox_drafts`), ни задачи ответа (`reply_tasks`), ни задания `business.send`;
предложенный ответ не хранится в базе вовсе — он есть только в карточке владельцу. Поэтому ни нажатие,
ни смена режима подготовки (`/prepare`), ни включение отправки (`SHTURMAN_SENDING`) позже не превращают
карточку репетиции в доставляемое сообщение: доставлять нечего и неоткуда.

Что запускает репетицию — только код, без модели:
  * событие `message.live` из сессии основного аккаунта (не догрузка истории, не импорт, не правка);
  * личный чат с человеком (не бот, не «Избранное», не служебные чаты Telegram и не управляющий бот),
    входящее от самого собеседника, не через бота (`notify.core.personal_candidate` и проверки ниже);
  * собеседник — в списке репетиции (по числовому ID); сообщение видно ассистенту (скрытое защитой от
    внедрённых инструкций — нет), с текстом или вложением, не старше предела автоответа
    (`max_age_seconds`);
  * в пачке есть собственное сообщение собеседника: пачка из одних пересланных сама репетицию не
    запускает (Р-93) — пересланное подаётся модели цитатой третьего лица, когда собеседник его
    прокомментировал;
  * у сервиса свой бот согласований (без него карточку слать некуда: через Hermes чужой текст попал бы
    в разговор ассистента);
  * настоящий автоответ 2а этого сообщения не готовит (иначе было бы два ответа на одно сообщение).
Пачка сообщений (packs.py, Р-93: подряд с паузами до `packs.GAP`, альбом, «переслал и прокомментировал») —
одна карточка на всю пачку, когда она закрылась. Вложения пачки (фото, документы, голосовые) сервис
поднимает в начало очереди разбора и ждёт до `packs.MEDIA_WAIT`; чего не дождался или что выключено
владельцем — карточка говорит прямо («Фото не успел разобрать», «Фото не смотрел: разбор фото и
документов выключен…»). Пауза между репетициями и дневной предел — из настроек репетиции; что в них
не уложилось, пропускается.

«Подготовить ответ» у уведомления (`request_once`) — разовая репетиция по просьбе владельца: на пачку,
даже из одних пересланных, человек может быть не в списке. Почему нельзя — словами для каждого кода
(`WORDS`); если нельзя заранее, кнопки под уведомлением нет (`why_not_once`).

Связка с уведомлениями (Р-84). Уведомление о сообщении, на которое пойдёт репетиция, придерживается
(`notify_items.held_for_rehearsal`, не дольше `NOTIFY_HOLD`): карточка репетиции сама говорит «вам
написали» и заменяет его — владелец получает одно сообщение, а не два. Репетиция не состоялась (предел,
пауза, сбой модели, сообщение удалено) — придержанное уведомление уходит сразу, как обычно. Сервис
остановился посреди — уведомление уйдёт само, когда выйдет срок придержки.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import asyncpg

from .. import bridge, packs, store
from ..outbox import autoreply, policy
from ..outbox import text as textlib
from . import settings as rs

logger = logging.getLogger("shturman.rehearsal")

HANDLER = "rehearsal.reply"
CARD_HANDLER = "rehearsal.card"
CALLBACK_MODULE = "rh"
# Оценки владельца. Кнопки отправки здесь нет и быть не может: репетиция ничего не отправляет.
RATINGS = (("good", "Годится"), ("almost", "Почти — поправил бы"), ("bad", "Не годится"))
RATING_WORDS = dict(RATINGS)
CODES = {code[0]: code for code, _ in RATINGS}
NOTIFY_HOLD = timedelta(minutes=3)   # дольше уведомление о сообщении не придерживается
ON_DEMAND_AGE = timedelta(hours=24)  # «Подготовить ответ» у уведомления — на сообщение не старше суток
SKIP_VARIANT = "Не писать свой вариант"
VARIANT_MAX = 2000
VARIANT_PROMPT = ("Напишите, как ответили бы вы, — одним сообщением. Ваш вариант сохранится рядом с оценкой "
                  "(30 дней, как сама карточка) и будет виден в приложении «Штурман», вкладка «Репетиция». "
                  f"Не хотите — «{SKIP_VARIANT}» под карточкой.")
KEEP = timedelta(days=30)            # сколько хранятся строки репетиции (в них нет текста)
SWEEP_EVERY = 60.0
QUEUED_STUCK = 60                    # секунд: строка «ждёт пачку» дольше — её разбирает обход
OUTCOMES = ("reply", "decline", "ask_owner", "need_source")
LIMITED = ("daily_cap", "pause")
TIMER_ROUNDS = 20                    # столько раз таймер пачки дождётся её продления, дальше — обход

# Почему ответ не готовится — словами, для владельца (после «Не получилось: »). Код — из `why_not` и
# итогов `Rehearsal.prepare`; у каждого кода своя точная причина, общей «по умолчанию» нет.
WORDS = {
    "disabled": "репетиция ответов выключена.",
    "unavailable": "репетиция сейчас недоступна: сервис репетиции не запущен.",
    "no_bot": "у сервиса нет своего бота согласований — карточку прислать некуда.",
    "gone": "сообщение удалено или чат исключён из архива.",
    "not_personal": "это не личное сообщение человека вашему аккаунту (группа, канал, бот, служебный чат, "
                    "ваше собственное или изменённое сообщение).",
    "not_session": "сообщение пришло не через вход в ваш аккаунт (из выгрузки или бизнес-режима): по нему не "
                   "видно, переслано ли оно и не от бота ли, поэтому ответ не готовится.",
    "hidden": "сообщение скрыто защитой от внедрённых инструкций — модель его не видит. Решите в карточке защиты.",
    "not_listed": "собеседника нет в списке репетиции.",
    "no_text": "в сообщении нет ни текста, ни фото, документа или голосового — отвечать не на что.",
    "forwarded_only": "в пачке только пересланное, без слов собеседника, — само по себе это не повод отвечать.",
    "stale": "сообщение старше суток — ответ на него не готовится.",
    "answered": "вы уже ответили в этом чате.",
    "newer": "в чате уже есть сообщение новее — ответ готовится на него.",
    "daily_cap": "дневной предел репетиции исчерпан.",
    "pause": "не прошла пауза между репетициями.",
    "stopped": "ассистент остановлен.",
}

_current: "Rehearsal | None" = None


def current() -> "Rehearsal | None":
    return _current


def set_current(mod: "Rehearsal | None") -> None:
    global _current
    _current = mod


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- кто подходит ---------------------------------------------------------------------------------

async def message_row(conn: asyncpg.Connection, message_id: int) -> asyncpg.Record | None:
    from ..notify import core as notify_core
    return await conn.fetchrow(notify_core._ROWS, [message_id])


async def why_not(conn: asyncpg.Connection, row: Mapping[str, Any] | None, s: Mapping[str, Any], *,
                  max_age: float, now: datetime | None = None, pack: packs.Pack | None = None,
                  on_demand: bool = False) -> str | None:
    """Почему на это сообщение (и его пачку) репетиции нет — код из `WORDS`, или None, если она будет.
    Любое сомнение — «нет». `on_demand` — владелец попросил сам («Подготовить ответ»): тогда и пачка из
    одних пересланных подходит."""
    from ..notify import core as notify_core
    if not s["enabled"]:
        return "disabled"
    if not bridge.owns_bot():
        return "no_bot"
    if row is None or row["deleted_at"] is not None or row["excluded"]:
        return "gone"
    if not await notify_core.personal_candidate(conn, row):
        return "not_personal"          # не личный чат основного аккаунта, бот, своё, правка, служебный чат
    if row["is_forwarded"] is None or "session" not in (row["sources"] or ()):
        return "not_session"           # признаков живой сессии нет (пересылка, бот) — не угадываем
    rows = pack.rows if pack is not None else [row]
    if not any(r["agent_visible"] for r in rows):
        return "hidden"                # скрыто защитой от внедрённых инструкций
    if int(row["peer_tg_id"]) not in set(s["people"]):
        return "not_listed"
    whole = pack if pack is not None else packs.Pack(rows=[dict(row)], closed=True, closes_at=row["sent_at"])
    trigger = whole.trigger(on_demand=on_demand)
    if trigger is None:
        return "forwarded_only" if whole.forwarded_only else "no_text"
    if ((now or _now()) - trigger["sent_at"]).total_seconds() > max_age:
        return "stale"
    return None


async def handled_by_autoreply(conn: asyncpg.Connection, state: Any, chat_id: int, message_id: int) -> bool:
    """Это сообщение готовит настоящий автоответ 2а: второй ответ на него не нужен."""
    from ..replies import workflow
    if not workflow.preparing(state):
        return False
    tgt = await policy.target(conn, chat_id)
    return tgt is not None and (await autoreply.eligible(conn, tgt, message_id)).ok


async def notify_hold(conn: asyncpg.Connection, row: Mapping[str, Any]) -> str | None:
    """Для уведомлений (Р-84): придержать уведомление об этом сообщении ради карточки репетиции?

    "hold" — репетиция для него будет (или уже готовится): уведомление ждёт её;
    "covered" — карточка репетиции о нём уже ушла: уведомление не нужно;
    None — репетиции нет: уведомление идёт как обычно."""
    mod = current()
    if mod is None:
        return None
    state = await conn.fetchval("SELECT state FROM rehearsal_items WHERE trigger_message_id = $1", row["id"])
    if state == "shown":
        return "covered"
    if state in ("queued", "preparing"):
        return "hold"
    if state is not None:
        return None
    s = await rs.load(conn)
    if not s["enabled"]:
        return None
    max_age = float((await autoreply.load(conn))["max_age_seconds"])
    if await why_not(conn, row, s, max_age=max_age, pack=await packs.around(conn, row["id"])) is not None:
        return None
    if await handled_by_autoreply(conn, mod.state, row["chat_id"], row["id"]):
        return None
    return "hold"


async def check_people(conn: asyncpg.Connection, ids: list[int]) -> None:
    """Кого нельзя взять в репетицию (ValueError с объяснением): свой аккаунт, бот, служебный адрес."""
    for tg_id in ids:
        if store.is_blocked_peer("user", tg_id):
            raise ValueError(f"№ {tg_id} — служебный адрес Telegram: на него репетиция не готовится")
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM accounts WHERE tg_user_id = $1)", tg_id):
            raise ValueError(f"№ {tg_id} — ваш собственный аккаунт")
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM peers WHERE class = 'user' AND tg_id = $1 AND is_bot)",
                               tg_id):
            raise ValueError(f"№ {tg_id} — бот: ботам ответы не готовятся")


async def people_names(conn: asyncpg.Connection, ids: list[int]) -> list[dict[str, Any]]:
    from ..sanitize import clean_line
    rows = await conn.fetch(
        "SELECT tg_id, name, username FROM peers WHERE class = 'user' AND tg_id = ANY($1::bigint[])", ids)
    found = {r["tg_id"]: r for r in rows}
    return [{"tg_id": i, "name": clean_line(found[i]["name"], 80) if i in found and found[i]["name"] else None,
             "username": found[i]["username"] if i in found else None} for i in ids]


# --- кнопки и карточка --------------------------------------------------------------------------------

def rating_buttons(item_id: int, nonce: str, chosen: str | None = None, *,
                   asking: bool = False) -> list[list[dict[str, str]]]:
    """Три кнопки оценки, по одной в строке; выбранная — с галочкой. Отправки среди них нет.
    asking — бот ждёт свой вариант владельца: ещё кнопка «Не писать свой вариант»."""
    rows = [[bridge.button(("✓ " if code == chosen else "") + label, CALLBACK_MODULE, f"{code[0]}:{item_id}:{nonce}")]
            for code, label in RATINGS]
    if asking:
        rows.append([bridge.button(SKIP_VARIANT, CALLBACK_MODULE, f"k:{item_id}:{nonce}")])
    return rows


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


async def compose_card(conn: asyncpg.Connection, item: Mapping[str, Any], row: Mapping[str, Any], outcome: Any,
                       *, tz: Any, now: datetime) -> str:
    """Карточка: «вам написали» (как уведомление Р-84) + что ответил бы ассистент. Простым текстом."""
    from .. import spheres
    from ..notify import core as notify_core
    titles = (await spheres.titles_by_chat(conn, [item["chat_id"]])).get(item["chat_id"], [])
    sphere = (" · " + ", ".join(textlib.one_line(t, 40) for t in titles)) if titles else ""
    who = textlib.one_line(row["chat_title"], 60) or textlib.one_line(row["sender_name"], 60) or "собеседник"
    head = "🎭 Репетиция ответа — ничего не отправлено"
    if item.get("on_demand"):
        head += "\nПо вашей просьбе — кнопкой «Подготовить ответ» под уведомлением."
    rows = [r for r in await conn.fetch(notify_core._ROWS, list(item.get("pack_ids") or []))] or [row]
    rows.sort(key=lambda r: (r["sent_at"], r["id"]))
    when = notify_core._clock(rows[0]["sent_at"], tz, now)
    if len(rows) > 1 and notify_core._clock(rows[-1]["sent_at"], tz, now) != when:
        when += "–" + notify_core._clock(rows[-1]["sent_at"], tz, now)
    lines = [head,
             f"💬 Вам написали: {who}",
             f"Личный чат{sphere} · {when}",
             notify_core.body(rows[0], {"preview": True}) if len(rows) == 1
             else notify_core.pack_line(rows, {"preview": True})]
    if len(rows) > 1:
        lines.append(f"Ассистент отвечал на всю пачку — {len(rows)} "
                     f"{_plural(len(rows), 'сообщение', 'сообщения', 'сообщений')} подряд.")
    lines += list(item.get("media_notes") or [])
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM notify_items WHERE message_id = $1 AND state = 'sent')",
                           item["trigger_message_id"]):
        lines.append("Уведомление об этом сообщении уже приходило.")
    link = notify_core._link(row)
    if link:
        lines.append(f"Открыть: {link}")
    lines.append("")
    if outcome.kind == "reply":
        lines += ["Ассистент ответил бы так:", outcome.text]
    elif outcome.kind == "decline":
        lines.append("Ассистент не стал бы отвечать на это сообщение.")
    elif outcome.kind == "ask_owner":
        lines += ["Ассистент не ответил бы сам, а спросил бы вас:", f"«{textlib.one_line(outcome.question, 500)}»"]
    else:
        lines.append("Ассистенту не хватило бы сведений из этого чата: он попросил бы прочитать ещё один источник. "
                     "В репетиции другие источники не читаются.")
    answered = await conn.fetchval(
        """SELECT EXISTS (SELECT 1 FROM messages n JOIN messages t ON t.id = $1
           WHERE n.chat_id = t.chat_id AND n.kind = 'message' AND n.deleted_at IS NULL AND n.is_outgoing IS TRUE
             AND (n.sent_at, n.id) > (t.sent_at, t.id))""", item["trigger_message_id"])
    if answered:
        lines.append("")
        lines.append("Пока ответ готовился, вы ответили сами — можно сравнить.")
    lines += ["", "Это только репетиция: отправить отсюда нельзя. Оцените ответ — оценка нужна для статистики качества."]
    return bridge.fit_message("\n".join(lines))


# --- придержанные уведомления ---------------------------------------------------------------------------

_UPTO = """FROM messages m, messages t
           WHERE t.id = $2 AND n.message_id = m.id AND n.chat_id = $1 AND n.state = 'waiting' AND n.held_for_rehearsal
             AND (m.sent_at, m.id) <= (t.sent_at, t.id)"""


async def release_notify(conn: asyncpg.Connection, chat_id: int, trigger_message_id: int) -> int:
    """Репетиция не состоялась: придержанные уведомления этого чата (до сообщения включительно) — сразу."""
    done = await conn.execute(
        f"UPDATE notify_items n SET held_for_rehearsal = false, due_at = LEAST(n.due_at, now()) {_UPTO}",
        chat_id, trigger_message_id)
    count = int(done.split()[-1])
    if count:
        _wake_notify()
    return count


async def cover_notify(conn: asyncpg.Connection, chat_id: int, trigger_message_id: int) -> int:
    """Карточка репетиции ушла: придержанные уведомления этого чата она заменяет."""
    done = await conn.execute(
        f"""UPDATE notify_items n SET state = 'dropped', dropped_reason = 'rehearsal', held_for_rehearsal = false
            {_UPTO}""", chat_id, trigger_message_id)
    return int(done.split()[-1])


def _wake_notify() -> None:
    mod = current()
    notifier = mod.state.extras.get("notify") if mod is not None else None
    if notifier is None:
        return
    try:
        # С небольшой задержкой: вызывающая транзакция должна успеть закрыться.
        asyncio.get_running_loop().call_later(0.1, notifier.wake.set)
    except RuntimeError:
        pass


# --- работающая репетиция -----------------------------------------------------------------------------

async def _skip(conn: asyncpg.Connection, item: Mapping[str, Any], reason: str, *, state: str = "skipped",
                release: bool = True) -> str:
    await conn.execute("UPDATE rehearsal_items SET state = $2, reason = $3, updated_at = now() WHERE id = $1",
                       item["id"], state, reason)
    if release:
        await release_notify(conn, item["chat_id"], item["trigger_message_id"])
    return reason


class Rehearsal:
    """Одна на процесс сервиса (`state.extras["rehearsal"]`)."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.debounce: dict[int, asyncio.Task] = {}

    @property
    def pool(self) -> asyncpg.Pool:
        return self.state.pool

    def zone(self) -> Any:
        from ..notify import core as notify_core
        return notify_core.zone(self.state.config.timezone)

    async def on_message(self, payload: Mapping[str, Any]) -> int | None:
        """Новое живое сообщение. Возвращает номер строки репетиции, если она заведена."""
        if payload.get("outgoing") is not False or payload.get("via_bot") is not False \
                or payload.get("edited") is not False or payload.get("source") != "session":
            return None
        message_id, chat_id = payload.get("message_id"), payload.get("chat_id")
        if not isinstance(message_id, int) or not isinstance(chat_id, int) or not bridge.owns_bot():
            return None
        async with self.pool.acquire() as conn:
            s = await rs.load(conn)
            if not s["enabled"]:
                return None
            if await conn.fetchval("SELECT shturman_halted()"):
                return None       # владелец остановил ассистента (Р-87)
            settings = await autoreply.load(conn)
            row = await message_row(conn, message_id)
            if row is None or row["chat_id"] != chat_id or row["account_id"] != payload.get("account_id"):
                return None
            pack = await packs.around(conn, message_id)
            if await why_not(conn, row, s, max_age=float(settings["max_age_seconds"]), pack=pack) is not None:
                return None
            if await handled_by_autoreply(conn, self.state, chat_id, message_id):
                return None
            item_id = await conn.fetchval(
                """INSERT INTO rehearsal_items (account_id, chat_id, trigger_message_id, peer_tg_id, nonce)
                   VALUES ($1, $2, $3, $4, $5) ON CONFLICT (trigger_message_id) DO NOTHING RETURNING id""",
                row["account_id"], chat_id, message_id, int(row["peer_tg_id"]), secrets.token_urlsafe(12))
            delay = await packs.closes_in(conn, message_id)
        if item_id is None:
            return None
        # Пачка — одна карточка (Р-93): прежний таймер чата снимается, новый ждёт закрытия пачки.
        prior = self.debounce.pop(chat_id, None)
        if prior is not None and not prior.done():
            prior.cancel()
        if delay <= 0:
            await self.prepare(item_id)
        else:
            self.debounce[chat_id] = self.state.spawn(self._later(chat_id, item_id, delay),
                                                      name=f"rehearsal-{chat_id}")
        return item_id

    async def _later(self, chat_id: int, item_id: int, delay: float) -> None:
        for _ in range(TIMER_ROUNDS):
            await asyncio.sleep(delay + 0.5)
            if await self.prepare(item_id) != "waiting":
                break
            async with self.pool.acquire() as conn:
                trigger = await conn.fetchval("SELECT trigger_message_id FROM rehearsal_items WHERE id = $1", item_id)
                delay = await packs.closes_in(conn, trigger) if trigger is not None else 0.0
        if self.debounce.get(chat_id) is asyncio.current_task():
            self.debounce.pop(chat_id, None)

    async def prepare(self, item_id: int) -> str:
        """Проверяет всё заново и просит модель подготовить ответ на всю пачку. Возвращает итог:
        preparing, waiting (пачка ещё не закрылась) либо код пропуска. Ничего не отправляет."""
        # Пачка и её вложения — до транзакции: ожидание разбора вложений длится до `packs.MEDIA_WAIT`.
        async with self.pool.acquire() as conn:
            item = await conn.fetchrow("SELECT * FROM rehearsal_items WHERE id = $1", item_id)
            if item is None or item["state"] != "queued":
                return "gone"
            pack = await packs.around(conn, item["trigger_message_id"])
        if not packs.ready(pack) and not item["on_demand"]:
            return "waiting"        # человек дописывает: карточка — когда пачка закроется
        if pack is not None:
            await packs.settle(self.pool, pack.ids, state=self.state)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.rehearsal'))")
            item = await conn.fetchrow("SELECT * FROM rehearsal_items WHERE id = $1 FOR UPDATE", item_id)
            if item is None or item["state"] != "queued":
                return "gone"
            chat_id, trigger = item["chat_id"], item["trigger_message_id"]
            on_demand = bool(item["on_demand"])
            if await conn.fetchval("SELECT shturman_halted()"):
                return await _skip(conn, item, "stopped")
            # В том же чате ждёт сообщение новее — эту пачку закроет его карточка.
            if not on_demand and await conn.fetchval(
                    """SELECT EXISTS (SELECT 1 FROM rehearsal_items r JOIN messages m ON m.id = r.trigger_message_id
                       JOIN messages t ON t.id = $2
                       WHERE r.chat_id = $1 AND r.state = 'queued' AND r.id <> $3 AND (m.sent_at, m.id) > (t.sent_at, t.id))""",
                    chat_id, trigger, item_id):
                return await _skip(conn, item, "newer", release=False)
            s = await rs.load(conn)
            settings = await autoreply.load(conn)
            row = await message_row(conn, trigger)
            s, max_age = _for_item(s, settings, item)
            pack = await packs.around(conn, trigger)      # заново: пересказы вложений уже в тексте
            code = await why_not(conn, row, s, max_age=max_age, pack=pack, on_demand=on_demand)
            if code is not None:
                return await _skip(conn, item, code)
            if await handled_by_autoreply(conn, self.state, chat_id, trigger):
                return await _skip(conn, item, "gone")
            newer = await conn.fetchrow(
                """SELECT n.is_outgoing FROM messages n JOIN messages t ON t.id = $1
                   WHERE n.chat_id = t.chat_id AND n.kind = 'message' AND n.deleted_at IS NULL
                     AND (n.sent_at, n.id) > (t.sent_at, t.id)
                   ORDER BY n.sent_at DESC, n.id DESC LIMIT 1""", pack.last["id"])
            if newer is not None and (newer["is_outgoing"] or not on_demand):
                return await _skip(conn, item, "answered" if newer["is_outgoing"] else "newer")
            used = await conn.fetchrow(
                """SELECT count(*) FILTER (WHERE asked_at > now() - interval '24 hours') AS day,
                          bool_or(asked_at > now() - make_interval(secs => $1)) AS recent,
                          bool_or(reason = 'daily_cap' AND updated_at > now() - interval '24 hours') AS capped
                   FROM rehearsal_items""", float(s["pause_seconds"]))
            if used["day"] >= s["daily_cap"]:
                if not used["capped"]:
                    await bridge.notify_owner(
                        conn, f"Репетиция ответов: дневной предел — {s['daily_cap']} карточек — исчерпан. "
                              "До конца суток новые сообщения из списка репетиции приходят обычными уведомлениями. "
                              "Предел меняется в «Настройках Штурмана», раздел «Репетиция ответов».", silent=True)
                return await _skip(conn, item, "daily_cap")
            if used["recent"] and not on_demand:     # о разовой репетиции владелец попросил сам — без паузы
                return await _skip(conn, item, "pause")
            # Карточка — на всю пачку (Р-93): строки репетиции её прежних сообщений закрываются этой.
            # Сообщения чата до пачки — только справка.
            if not on_demand:
                await conn.execute(
                    """UPDATE rehearsal_items SET state = 'skipped', reason = 'newer', updated_at = now()
                       WHERE chat_id = $1 AND state = 'queued' AND id <> $2""", chat_id, item_id)
            visible = pack.visible
            covered = len(visible)
            tgt = await policy.target(conn, chat_id)
            who = textlib.one_line(row["chat_title"], 60) or textlib.one_line(row["sender_name"], 60) or "Собеседник"
            codes = await packs.current_notes(conn, visible, self.state)     # что вышло после ожидания
            asked = packs.prompt_text(visible, codes, who=who)
            topic, recent, hits = await autoreply.gather_context(conn, self.state, settings, chat_id, trigger,
                                                                 asked, exclude=[r["id"] for r in visible])
            from ..replies import model, workflow
            messages = autoreply.build_messages(settings, tgt, "business", asked, recent, hits, self.zone(),
                                                trigger_limit=6000)
            offered: list[int] = []
            for mid in [r["id"] for r in visible] + [r["id"] for r in recent] + [h["id"] for h in hits]:
                if mid not in offered:
                    offered.append(mid)
            refs = [{"kind": "chat", "source_id": str(chat_id), "message_id": mid, "topic_tg_id": topic}
                    for mid in offered]
            instructions, input_text = workflow.model_request(self.state, messages, refs)
            job_id = await bridge.request_structured(
                conn, handler=HANDLER, instructions=instructions, input=input_text, json_schema=model.SCHEMA,
                schema_name="reply_outcome", task="shturman_reply", max_tokens=4000,
                context={"item_id": item_id}, dedup_key=f"rehearsal:{item_id}")
            await conn.execute(
                """UPDATE rehearsal_items SET state = 'preparing', job_id = $2, asked_at = now(), covered = $3,
                          offered = $4::bigint[], pack_ids = $5::bigint[], media_notes = $6::text[],
                          updated_at = now() WHERE id = $1""",
                item_id, job_id, max(1, min(int(covered), 1000)), offered, [r["id"] for r in visible],
                packs.card_lines(visible, codes))
        return "preparing"

    async def sweep(self) -> int:
        """Обход: строки, оставшиеся «ждать пачку» (сервис перезапускался), разбираются; старые — удаляются."""
        async with self.pool.acquire() as conn:
            stuck = [r["id"] for r in await conn.fetch(
                """SELECT id FROM rehearsal_items WHERE state = 'queued'
                   AND created_at < now() - make_interval(secs => $1) ORDER BY id""", float(QUEUED_STUCK))]
            await conn.execute("DELETE FROM rehearsal_items WHERE created_at < now() - make_interval(secs => $1)",
                               KEEP.total_seconds())
        for item_id in stuck:
            await self.prepare(item_id)
        return len(stuck)

    async def run(self) -> None:
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — сбой одного обхода не останавливает репетицию
                logger.error("репетиция: обход не выполнен (%s)", type(exc).__name__)
            await asyncio.sleep(SWEEP_EVERY)

    def close(self) -> None:
        for task in self.debounce.values():
            task.cancel()
        self.debounce.clear()


# --- ответ модели и карточка -----------------------------------------------------------------------------

@bridge.on_result(HANDLER)
async def _ready(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    # Ответ модели прочитан — второй его копии в задании не остаётся.
    await conn.execute("UPDATE jobs SET payload = '{}'::jsonb, result = NULL WHERE id = $1", job["id"])
    item_id = (job.get("context") or {}).get("item_id")
    item = await conn.fetchrow("SELECT * FROM rehearsal_items WHERE id = $1 FOR UPDATE", item_id) \
        if isinstance(item_id, int) else None
    if item is None or item["state"] != "preparing" or item["job_id"] != job["id"]:
        return
    mod = current()
    s = await rs.load(conn)
    row = await message_row(conn, item["trigger_message_id"])
    if row is not None:
        s, _ = _for_item(s, {"max_age_seconds": 0}, item, peer=int(row["peer_tg_id"]))
    if await conn.fetchval("SELECT shturman_halted()"):
        await _skip(conn, item, "stopped")      # владелец остановил ассистента, пока модель думала (Р-87)
        return
    # Пока модель думала, мир мог измениться: удалено, изменено, скрыто защитой, чат исключён,
    # человека убрали из списка, репетицию выключили, бота не стало — карточки не будет.
    if mod is None or row is None or row["deleted_at"] is not None or row["excluded"] or not row["agent_visible"] \
            or row["edited_at"] is not None or not s["enabled"] or int(row["peer_tg_id"]) not in set(s["people"]) \
            or not bridge.owns_bot():
        await _skip(conn, item, "gone")
        return
    from ..replies import model
    settings = await autoreply.load(conn)
    try:
        outcome = model.parse(result.get("parsed"), [{"message_id": i} for i in item["offered"]],
                              max_chars=settings["max_reply_chars"] + 500)
    except (ValueError, TypeError, KeyError):
        await _skip(conn, item, "invalid_model_result", state="failed")
        return
    from ..notify import core as notify_core
    from ..notify import settings as ns
    now = _now()
    tz = mod.zone()
    text = await compose_card(conn, item, row, outcome, tz=tz, now=now)
    quiet = notify_core.in_quiet(await ns.load(conn), now.astimezone(tz))
    await bridge.notify_owner(conn, text, buttons=rating_buttons(item["id"], item["nonce"]), silent=quiet,
                              handler=CARD_HANDLER, context={"item_id": item["id"]},
                              dedup_key=f"rehearsal-card:{item['id']}")
    await conn.execute("UPDATE rehearsal_items SET state = 'shown', outcome = $2, updated_at = now() WHERE id = $1",
                       item["id"], outcome.kind)
    await cover_notify(conn, item["chat_id"], item["trigger_message_id"])


@bridge.on_failure(HANDLER)
async def _model_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    item_id = (job.get("context") or {}).get("item_id")
    if not isinstance(item_id, int):
        return
    item = await conn.fetchrow("SELECT * FROM rehearsal_items WHERE id = $1 FOR UPDATE", item_id)
    if item is not None and item["state"] == "preparing" and item["job_id"] == job["id"]:
        await _skip(conn, item, "model_unavailable", state="failed")


@bridge.on_failure(CARD_HANDLER)
async def _card_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    """Карточка не дошла: оценить нечего, а уведомление, которое она заменяла, возвращается в очередь."""
    item_id = (job.get("context") or {}).get("item_id")
    if not isinstance(item_id, int):
        return
    item = await conn.fetchrow(
        """UPDATE rehearsal_items SET state = 'failed', reason = 'card_not_delivered', updated_at = now()
           WHERE id = $1 AND state = 'shown' AND rating IS NULL RETURNING *""", item_id)
    if item is None:
        return
    done = await conn.execute(
        """UPDATE notify_items n SET state = 'waiting', dropped_reason = NULL, due_at = now()
           FROM messages m, messages t
           WHERE t.id = $2 AND n.message_id = m.id AND n.chat_id = $1 AND n.state = 'dropped'
             AND n.dropped_reason = 'rehearsal' AND (m.sent_at, m.id) <= (t.sent_at, t.id)""",
        item["chat_id"], item["trigger_message_id"])
    if int(done.split()[-1]):
        _wake_notify()


# --- оценка владельца -----------------------------------------------------------------------------------

@bridge.on_callback(CALLBACK_MODULE)
async def _rated(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    from .. import authority, confirm
    principal = authority.requires_owner()
    if principal.user_id != user_id or principal.source != "telegram":
        raise confirm.Refused("Кнопка требует проверенного владельца Telegram.", 403, "owner_required")
    code, _, tail = rest.partition(":")
    raw_id, _, nonce = tail.partition(":")
    rating = CODES.get(code)
    if (rating is None and code != "k") or not raw_id.isdigit():
        return {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
    item = await conn.fetchrow("SELECT * FROM rehearsal_items WHERE id = $1 FOR UPDATE", int(raw_id))
    if item is None or item["state"] != "shown" or not hmac.compare_digest(item["nonce"], nonce):
        return {"answer": "Эта карточка уже недоступна.", "edit_text": None, "remove_buttons": True}
    if code == "k":
        # «Не писать свой вариант»: просьба в боте снимается, оценка остаётся.
        await conn.execute(
            """DELETE FROM executor_state WHERE key = 'prompt' AND value->>'kind' = 'variant'
               AND (value->>'id')::bigint = $1""", item["id"])
        return {"answer": "Хорошо, без своего варианта.", "edit_text": None, "remove_buttons": False,
                "buttons": rating_buttons(item["id"], item["nonce"], chosen=item["rating"])}
    before = item["rating"]
    await conn.execute("UPDATE rehearsal_items SET rating = $2, rated_at = now(), updated_at = now() WHERE id = $1",
                       item["id"], rating)
    mod = current()

    async def journal() -> None:
        if mod is not None:
            from ..setup_page import audit
            await audit.write(mod.pool, "rehearsal.rate",
                              detail=f"карточка № {item['id']}; оценка: {rating}" + (f"; была: {before}" if before else ""))

    out = {"answer": f"Оценка сохранена: {RATING_WORDS[rating]}. Её можно изменить.", "edit_text": None,
           "remove_buttons": False, "buttons": rating_buttons(item["id"], item["nonce"], chosen=rating),
           "after_commit": journal}
    if rating == "almost":
        # Свой вариант (Б14): бот попросит написать, как ответили бы вы, — следующим сообщением.
        out["buttons"] = rating_buttons(item["id"], item["nonce"], chosen=rating, asking=True)
        out["prompt"] = {"kind": "variant", "id": item["id"], "nonce": item["nonce"], "text": VARIANT_PROMPT,
                         "placeholder": "Как ответили бы вы"}
    return out


async def save_variant(conn: asyncpg.Connection, item_id: int, nonce: str, text: str) -> dict[str, Any]:
    """Свой вариант владельца после «Почти — поправил бы». Только текст владельца: хранится у строки
    репетиции, удаляется вместе с ней (30 дней). В обучение стиля не идёт — только хранение и показ."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "text": "Вариант пустой — ничего не записано."}
    if len(text) > VARIANT_MAX:
        return {"ok": False, "text": f"Вариант длиннее {VARIANT_MAX} знаков — сократите и нажмите «Почти» ещё раз."}
    item = await conn.fetchrow("SELECT * FROM rehearsal_items WHERE id = $1 FOR UPDATE", item_id)
    if item is None or item["state"] != "shown" or not hmac.compare_digest(item["nonce"], nonce):
        return {"ok": False, "text": "Эта карточка уже недоступна — вариант не записан."}
    await conn.execute("UPDATE rehearsal_items SET owner_variant = $2, owner_variant_at = now(), updated_at = now() "
                       "WHERE id = $1", item_id, text)
    from ..setup_page import audit
    await audit.write_conn(conn, "rehearsal.variant", detail=f"карточка № {item_id}; знаков: {len(text)}")
    return {"ok": True, "text": "Записал ваш вариант. Он виден в приложении «Штурман», вкладка «Репетиция»; "
                                "хранится 30 дней вместе с карточкой. Ничего не отправлено."}


def _for_item(s: Mapping[str, Any], settings: Mapping[str, Any], item: Mapping[str, Any], *,
              peer: int | None = None) -> tuple[dict[str, Any], float]:
    """Настройки для этой строки. Разовая репетиция (`on_demand`, «Подготовить ответ» у уведомления):
    владелец попросил сам — список людей и выключатель не нужны, сообщение — не старше суток."""
    s = dict(s)
    if not item["on_demand"]:
        return s, float(settings["max_age_seconds"])
    s["enabled"] = True
    peer_id = peer if peer is not None else item["peer_tg_id"]
    if peer_id is not None and int(peer_id) not in set(s["people"]):
        s["people"] = list(s["people"]) + [int(peer_id)]
    return s, ON_DEMAND_AGE.total_seconds()


async def request_once(state: Any, message_id: int) -> dict[str, Any]:
    """«Подготовить ответ» у уведомления, способ «Через ваш аккаунт»: разовая репетиция для этого
    сообщения — даже если человека нет в списке и репетиция выключена. Ничего не отправляется; входит
    в дневной предел репетиции. Возвращает {ok, text} — ответ владельцу словами."""
    mod = current()
    if mod is None or not bridge.owns_bot():
        return {"ok": False, "text": "Репетиция сейчас недоступна: сервис репетиции не запущен или нет бота согласований."}
    async with state.pool.acquire() as conn:
        if await conn.fetchval("SELECT shturman_halted()"):
            from .. import halt
            return {"ok": False, "text": "Ассистент остановлен — ответ не готовится. Продолжить — /resume."}
        row = await message_row(conn, message_id)
        s = await rs.load(conn)
        code = await _once_code(conn, row, s, message_id)
        if code is not None:
            return {"ok": False, "text": "Не получилось: " + WORDS[code]}
        pack = await packs.around(conn, message_id)
        media, voice = await packs.media_on(conn), packs.switches_of(state)
        slow = pack is not None and packs.MEDIA_WAIT > 0 and any(packs.waiting(r, media, voice) for r in pack.visible)
        used = await conn.fetchval(
            "SELECT count(*) FROM rehearsal_items WHERE asked_at > now() - interval '24 hours'")
        if used >= s["daily_cap"]:
            return {"ok": False, "text": f"Дневной предел репетиции — {s['daily_cap']} карточек — исчерпан. Предел "
                                         "меняется в «Настройках Штурмана», раздел «Репетиция ответов»."}
        existing = await conn.fetchrow("SELECT id, state FROM rehearsal_items WHERE trigger_message_id = $1", message_id)
        if existing is not None and existing["state"] in ("queued", "preparing"):
            return {"ok": True, "text": "Ответ на это сообщение уже готовится — карточка придёт сюда."}
        if existing is not None and existing["state"] == "shown":
            return {"ok": False, "text": "Репетиция на это сообщение уже была — карточка выше в этом чате."}
        if existing is not None:
            item_id = existing["id"]
            await conn.execute(
                """UPDATE rehearsal_items SET state = 'queued', reason = NULL, on_demand = true, job_id = NULL,
                          updated_at = now() WHERE id = $1""", item_id)
        else:
            item_id = await conn.fetchval(
                """INSERT INTO rehearsal_items (account_id, chat_id, trigger_message_id, peer_tg_id, nonce, on_demand)
                   VALUES ($1, $2, $3, $4, $5, true) RETURNING id""",
                row["account_id"], row["chat_id"], message_id, int(row["peer_tg_id"]), secrets.token_urlsafe(12))
    if slow:
        # Вложения пачки ещё разбираются: ждать их в нажатии кнопки нельзя — готовится в фоне (Р-93).
        mod.state.spawn(mod.prepare(item_id), name=f"rehearsal-once-{item_id}")
        return {"ok": True, "text": "Готовлю, что ответил бы ассистент, — сначала дождусь разбора вложений "
                                    f"(до {int(packs.MEDIA_WAIT)} с), потом пришлю карточкой репетиции. "
                                    "Ничего не отправляется."}
    outcome = await mod.prepare(item_id)
    if outcome != "preparing":
        return {"ok": False, "text": "Не получилось: " + WORDS.get(outcome, WORDS["gone"])}
    return {"ok": True, "text": "Готовлю, что ответил бы ассистент, — пришлю карточкой репетиции. Ничего не отправляется."}


async def _once_code(conn: asyncpg.Connection, row: Mapping[str, Any] | None, s: Mapping[str, Any],
                     message_id: int) -> str | None:
    probe = {"on_demand": True, "peer_tg_id": int(row["peer_tg_id"]) if row is not None else None}
    relaxed, max_age = _for_item(s, {"max_age_seconds": 0}, probe)
    pack = await packs.around(conn, message_id) if row is not None else None
    return await why_not(conn, row, relaxed, max_age=max_age, pack=pack, on_demand=True)


async def why_not_once(conn: asyncpg.Connection, message_id: int) -> str | None:
    """Для кнопки «Подготовить ответ» под уведомлением: почему разовая репетиция на это сообщение
    невозможна (код из `WORDS`) — тогда кнопки нет; None — кнопка будет. Временное (остановка,
    дневной предел) кнопку не убирает: на нажатие владелец получит точную причину."""
    if current() is None:
        return "unavailable"
    row = await message_row(conn, message_id)
    return await _once_code(conn, row, await rs.load(conn), message_id)


@bridge.on_owner_change
async def _owner_changed(conn: asyncpg.Connection, new_user_id: int) -> None:
    """Новый владелец не наследует чужой выбор: репетиция выключается, список людей очищается."""
    await conn.execute("DELETE FROM settings WHERE key = $1", rs.SETTINGS_KEY)
    await conn.execute(
        """UPDATE rehearsal_items SET state = 'skipped', reason = 'owner_changed', updated_at = now()
           WHERE state IN ('queued', 'preparing')""")


# --- статистика ---------------------------------------------------------------------------------------

async def stats(conn: asyncpg.Connection, days: int = 7) -> dict[str, int]:
    """Только числа: сколько карточек, какие исходы, какие оценки, сколько пропущено — за `days` суток."""
    row = await conn.fetchrow(
        """SELECT count(*) FILTER (WHERE state = 'shown') AS shown,
                  count(*) FILTER (WHERE state = 'shown' AND outcome = 'reply') AS reply,
                  count(*) FILTER (WHERE state = 'shown' AND outcome = 'decline') AS decline,
                  count(*) FILTER (WHERE state = 'shown' AND outcome = 'ask_owner') AS ask_owner,
                  count(*) FILTER (WHERE state = 'shown' AND outcome = 'need_source') AS need_source,
                  count(*) FILTER (WHERE rating = 'good') AS good,
                  count(*) FILTER (WHERE rating = 'almost') AS almost,
                  count(*) FILTER (WHERE rating = 'bad') AS bad,
                  count(*) FILTER (WHERE state = 'shown' AND rating IS NULL) AS unrated,
                  count(*) FILTER (WHERE state = 'skipped' AND reason = ANY($2::text[])) AS limited,
                  count(*) FILTER (WHERE state = 'failed') AS failed,
                  count(*) FILTER (WHERE state IN ('queued', 'preparing')) AS in_progress,
                  count(*) FILTER (WHERE owner_variant IS NOT NULL) AS variants,
                  count(*) FILTER (WHERE on_demand AND state = 'shown') AS on_demand
           FROM rehearsal_items WHERE created_at > now() - make_interval(days => $1)""", days, list(LIMITED))
    out = {key: int(value) for key, value in dict(row).items()}
    out["asked_24h"] = int(await conn.fetchval(
        "SELECT count(*) FROM rehearsal_items WHERE asked_at > now() - interval '24 hours'"))
    out["days"] = days
    return out
