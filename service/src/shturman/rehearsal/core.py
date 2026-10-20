"""«Черновик ответа» на основном аккаунте владельца (решения Р-85, Р-105).

Что делает. На новое живое входящее сообщение в личном чате основного аккаунта от человека из списка
«кому готовить черновики» (бывшая репетиция, Р-85) сервис готовит ответ **так же, как автоответ доверенным**
(уровень 2а): без инструментов, справка — последние сообщения этого чата и найденное в архиве в этом же чате
(`autoreply.gather_context`), тот же запрос к модели и тот же ограниченный набор исходов
(`replies.workflow.model_request`, `replies.model`). Потом присылает владельцу в бот согласований одну
карточку «Черновик ответа»: кто написал, его сообщение (одной строкой, обезвреженное — как уведомление
Р-84), разговор перед ним (Р-103) и что ассистент предлагает ответить — моноширинным блоком (нажатие
копирует). Тот же черновик готовится по кнопке «Подготовить ответ» под уведомлением и карточкой разбора
входящих — в любом способе работы, — и доверенному с включённым автоответом, когда автоответ сам ответить
сейчас не может (выключена отправка, нет бизнес-подключения, `replies/delivery.auto_send_possible`).

Текст ответа (исход `reply`) хранится в `outbox_drafts` с происхождением `suggest` (`drafts.create_suggestion`)
не дольше суток и стирается через час после решения. Строка `rehearsal_items` — подготовка и статистика,
текста в ней нет: номер черновика (`draft_id`) и оценка владельца.

Кнопки под карточкой — по `replies/delivery.py` (одни и те же в боте и в приложении, Д-46):
  «Отправить»          — только если доставка через бизнес-бота доступна сейчас (свой бот, отправка
                         включена, бизнес-подключение с правом отвечать, входящее за сутки); нажатие —
                         та же проверка, что у черновика ассистента (`drafts.approve`); считается «годится»;
  «Скопировать»        — текст не длиннее 256 знаков;
  «🎙 Наговорить свой» — свой ответ голосом или наброском (dictation/); готовая запись — оценка «почти»;
  «Не годится»         — оценка; черновик снимается, ничего не отправляется;
  «Открыть чат»        — ссылка на чат в Telegram, если она есть.
Основной аккаунт подключён только на чтение: его клиент Telegram не пропускает ни одного запроса отправки
(`tg/client.py`, роль owner). Отправить можно только через бизнес-бота — нажатием владельца.

Что запускает черновик сам — только код, без модели:
  * событие `message.live` из сессии основного аккаунта (не догрузка истории, не импорт, не правка);
  * личный чат с человеком (не бот, не «Избранное», не служебные чаты Telegram и не управляющий бот),
    входящее от самого собеседника, не через бота (`notify.core.personal_candidate` и проверки ниже);
  * собеседник — в списке «кому готовить черновики» (по числовому ID) или доверенный, которому автоответ
    сам сейчас не ответит (`audience`); сообщение видно ассистенту (скрытое защитой от внедрённых
    инструкций — нет), с текстом или вложением, не старше предела автоответа (`max_age_seconds`);
  * в пачке есть собственное сообщение собеседника: пачка из одних пересланных сама черновик не
    запускает (Р-93) — пересланное подаётся модели цитатой третьего лица, когда собеседник его
    прокомментировал;
  * у сервиса свой бот согласований (без него карточку слать некуда: через Hermes чужой текст попал бы
    в разговор ассистента);
  * настоящий автоответ 2а этого сообщения не готовит (иначе было бы два ответа на одно сообщение):
    `autoreply.claim` — есть его задача или черновик на это сообщение, либо он возьмёт его сейчас.
    Доверенный собеседник сам по себе этого не значит (Д-31): на сообщение
    старше предела автоответа, изменённое или с более новым после него автоответ ответа не готовит.
Пачка сообщений (packs.py, Р-93: подряд с паузами до `packs.GAP`, альбом, «переслал и прокомментировал») —
одна карточка на всю пачку, когда она закрылась. Вложения пачки (фото, документы, голосовые) сервис
поднимает в начало очереди разбора и ждёт до `packs.MEDIA_WAIT`; чего не дождался или что выключено
владельцем — карточка говорит прямо («Фото не успел разобрать», «Фото не смотрел: разбор фото и
документов выключен…»). Пауза между черновиками и дневной предел — из настроек `rehearsal.settings`; что в них
не уложилось, пропускается. О пределе владелец узнаёт одним сообщением в сутки с кнопками «Поднять до …»
(`cap_buttons`): нажатие в личном чате бота согласований — его решение, как команда `/drafts cap N`.

«Подготовить ответ» у уведомления (`request_once`) — черновик по просьбе владельца: на пачку,
даже из одних пересланных, человек может быть не в списке. Почему нельзя — словами для каждого кода
(`WORDS`, `outcome_text`; незнакомый код — нейтральной фразой и записью в журнал сервиса, не «удалено»);
если нельзя заранее, кнопки под уведомлением нет (`why_not_once`). Если ответ на это сообщение готовит
автоответ 2а — владелец узнаёт, где его черновик (`autoreply.claim_words`). Владелец, которому ответили
«Готовлю…», без ответа не остаётся: не вышло позже (вложения, модель, мир изменился) — бот скажет почему.

Связка с уведомлениями (Р-84). Уведомление о сообщении, на которое пойдёт черновик, придерживается
(`notify_items.held_for_rehearsal`, не дольше `NOTIFY_HOLD`): карточка черновика сама говорит «вам
написали» и заменяет его — владелец получает одно сообщение, а не два. Черновик не состоялся (предел,
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

from .. import bridge, packs, store, words
from ..outbox import autoreply, policy
from ..outbox import text as textlib
from . import settings as rs

logger = logging.getLogger("shturman.rehearsal")

HANDLER = "rehearsal.reply"
CARD_HANDLER = "rehearsal.card"
CALLBACK_MODULE = "rh"
# Оценки владельца. Под новыми карточками — только «Не годится» (`sh:rh:b`): «годится» — это «Отправить»,
# «почти» — свой вариант голосом (Р-105). Остальные коды разбираются под карточками прежних версий.
RATINGS = (("good", "Годится"), ("almost", "Почти — поправил бы"), ("bad", "Не годится"))
RATING_WORDS = dict(RATINGS)
NOT_GOOD_CODE = "n"           # «Не годится» под карточкой «Черновик ответа» (Р-105): `sh:rh:n:<строка>:<метка>`
CODES = {code[0]: code for code, _ in RATINGS}
NOTIFY_HOLD = timedelta(minutes=3)   # дольше уведомление о сообщении не придерживается
ON_DEMAND_AGE = timedelta(hours=24)  # «Подготовить ответ» у уведомления — на сообщение не старше суток
VARIANT_MAX = 2000                   # свой вариант текстом — только ответ на просьбу прежних версий
KEEP = timedelta(days=30)            # сколько хранятся строки подготовки (в них нет текста)
SWEEP_EVERY = 60.0
QUEUED_STUCK = 60                    # секунд: строка «ждёт пачку» дольше — её разбирает обход
OUTCOMES = ("reply", "decline", "ask_owner", "need_source")
LIMITED = ("daily_cap", "pause")
TIMER_ROUNDS = 20                    # столько раз таймер пачки дождётся её продления, дальше — обход

# Почему ответ не готовится — словами, для владельца (после «Не получилось: »). Код — из `why_not` и
# итогов `Rehearsal.prepare`; у каждого кода своя точная причина, общей «по умолчанию» нет.
WORDS = {
    "disabled": "черновики ответов сами не готовятся — включить: /drafts on.",
    "unavailable": "черновики ответов сейчас недоступны: сервис не запущен.",
    "no_bot": "у сервиса нет своего бота согласований — карточку прислать некуда.",
    "gone": "сообщение удалено или чат исключён из архива.",
    "not_personal": "это не личное сообщение человека вашему аккаунту (группа, канал, бот, служебный чат, "
                    "ваше собственное или изменённое сообщение).",
    "not_session": "сообщение пришло не через вход в ваш аккаунт (из выгрузки или бизнес-режима): по нему не "
                   "видно, переслано ли оно и не от бота ли, поэтому ответ не готовится.",
    "hidden": "сообщение скрыто защитой от внедрённых инструкций — модель его не видит. Решите в карточке защиты.",
    "not_listed": "собеседника нет в списке «кому готовить черновики».",
    "no_text": "в сообщении нет ни текста, ни фото, документа или голосового — отвечать не на что.",
    "forwarded_only": "в пачке только пересланное, без слов собеседника, — само по себе это не повод отвечать.",
    "stale": "сообщение старше суток — ответ на него не готовится.",
    "answered": "вы уже ответили в этом чате.",
    "newer": "в чате уже есть сообщение новее — ответ готовится на него.",
    "daily_cap": "дневной предел черновиков ответов исчерпан.",
    "pause": "не прошла пауза между черновиками.",
    "stopped": "ассистент остановлен.",
    "autoreply": "ответ на это сообщение готовит автоответ доверенным — второй ответ не нужен.",
    "edited": "сообщение изменено, пока ответ готовился.",
    "taken": "ответ на это сообщение уже готовится или уже показан.",
    "missing": "запись о подготовке ответа не найдена — нажмите «Подготовить ответ» ещё раз.",
    "model_unavailable": "модель не ответила — попробуйте позже.",
    "invalid_model_result": "модель вернула непонятный ответ.",
}
UNKNOWN = "Не получилось подготовить ответ: причина не распознана (код «{code}»), она записана в журнал сервиса."


async def outcome_text(conn: asyncpg.Connection, state: Any, chat_id: int, message_id: int, code: str) -> str:
    """Итог подготовки словами для владельца. `autoreply` — где черновик автоответа; код из `WORDS` —
    «Не получилось: …»; незнакомый код — нейтральная фраза и запись в журнал сервиса: «удалено» говорится
    только об удалённом (Д-31)."""
    if code == "autoreply":
        found = await autoreply.claim(conn, state, chat_id, message_id)
        if found is not None:
            return autoreply.claim_words(found)
    if code in WORDS:
        return "Не получилось: " + WORDS[code]
    logger.warning("репетиция: незнакомый код итога «%s» (сообщение № %s)", code, message_id)
    return UNKNOWN.format(code=textlib.one_line(code, 40))

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
                  on_demand: bool = False, state: Any = None) -> str | None:
    """Почему на это сообщение (и его пачку) черновика не будет — код из `WORDS`, или None, если будет.
    Любое сомнение — «нет». `on_demand` — владелец попросил сам («Подготовить ответ»): тогда и пачка из
    одних пересланных подходит. `state` нужен, чтобы узнать, ответит ли доверенному автоответ сам."""
    from ..notify import core as notify_core
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
    code = await audience(conn, state, row, s)
    if code is not None:
        return code
    whole = pack if pack is not None else packs.Pack(rows=[dict(row)], closed=True, closes_at=row["sent_at"])
    trigger = whole.trigger(on_demand=on_demand)
    if trigger is None:
        return "forwarded_only" if whole.forwarded_only else "no_text"
    if ((now or _now()) - trigger["sent_at"]).total_seconds() > max_age:
        return "stale"
    return None


async def audience(conn: asyncpg.Connection, state: Any, row: Mapping[str, Any], s: Mapping[str, Any]) -> str | None:
    """Кому черновик готовится сам (Р-105): человеку из списка, пока черновики включены (`/drafts on`), и
    доверенному, для аккаунта которого владелец включил автоответ, — когда автоответ сам ответить сейчас не
    может. Доверенным без включённого автоответа черновик не готовится: их сообщения модели владелец не
    отдавал. None — готовится; иначе код из `WORDS`."""
    if s["enabled"] and int(row["peer_tg_id"]) in set(s["people"]):
        return None
    if state is not None and await trusted_fallback(conn, state, row):
        return None
    return "disabled" if not s["enabled"] else "not_listed"


async def trusted_fallback(conn: asyncpg.Connection, state: Any, row: Mapping[str, Any]) -> bool:
    """Доверенный, автоответ для этого аккаунта включён, но сам он сейчас не ответит."""
    if not await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM outbox_trusted WHERE tg_user_id = $1)
                  AND EXISTS (SELECT 1 FROM outbox_accounts WHERE account_id = $2 AND autoreply_enabled)""",
            int(row["peer_tg_id"]), row["account_id"]):
        return False
    tgt = await policy.target(conn, row["chat_id"])
    if tgt is None:
        return False
    from ..replies import delivery
    return not await delivery.auto_send_possible(conn, state, tgt)


async def handled_by_autoreply(conn: asyncpg.Connection, state: Any, chat_id: int, message_id: int) -> bool:
    """Это сообщение готовит настоящий автоответ 2а: второй ответ на него не нужен. Не «собеседник
    доверенный», а «автоответ готовит или подготовил ответ на это сообщение» (`autoreply.claim`, Д-31)."""
    return await autoreply.claim(conn, state, chat_id, message_id) is not None


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
    max_age = float((await autoreply.load(conn))["max_age_seconds"])
    if await why_not(conn, row, s, max_age=max_age, pack=await packs.around(conn, row["id"]),
                     state=mod.state) is not None:
        return None
    if await handled_by_autoreply(conn, mod.state, row["chat_id"], row["id"]):
        return None
    return "hold"


async def check_people(conn: asyncpg.Connection, ids: list[int]) -> None:
    """Кого нельзя взять в репетицию (ValueError с объяснением): свой аккаунт, бот, служебный адрес."""
    for tg_id in ids:
        if store.is_blocked_peer("user", tg_id):
            raise ValueError(f"№ {tg_id} — служебный адрес Telegram: ему черновики не готовятся")
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

def rating_buttons(item_id: int, nonce: str, chosen: str | None = None) -> list[list[dict[str, str]]]:
    """Три кнопки оценки карточек прежних версий (до Р-105), по одной в строке; выбранная — с галочкой.
    Нажатие под старой карточкой по-прежнему сохраняет оценку. Последней — «Ответить голосом» (Р-103)."""
    rows = [[bridge.button(("✓ " if code == chosen else "") + label, CALLBACK_MODULE, f"{code[0]}:{item_id}:{nonce}")]
            for code, label in RATINGS]
    from ..dictation import core as dictation
    rows += dictation.voice_row("r", item_id, nonce)
    return rows


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# --- дневной предел: «Поднять до …» -------------------------------------------------------------------------

RAISE_STEPS = ((50, 100), (200, 300))   # кнопки под «предел исчерпан»: сначала 50 и 100, с предела 100 — 200 и 300


def raise_options(cap: int) -> list[int]:
    """До скольких можно поднять предел кнопкой: только больше нынешнего. Пока нынешний меньше 100 —
    50 и 100 (что из них больше); от 100 — 200 и 300; на 300 (наибольшем) кнопок нет."""
    for step in RAISE_STEPS:
        bigger = [v for v in step if cap < v <= rs.DAILY_CAP[1]]
        if bigger:
            return bigger
    return []


def cap_buttons(cap: int, *, message_id: int | None = None) -> list[list[dict[str, str]]]:
    """Кнопки «Поднять до N» одной строкой: `sh:rh:c:<новый>:<нынешний>[:<сообщение>]`. Нынешний предел
    в данных кнопки — чтобы старая кнопка не сработала, если предел с тех пор уже меняли; номер сообщения —
    у ответа на «Подготовить ответ»: после подъёма просьба повторяется сама."""
    tail = f":{int(message_id)}" if message_id is not None else ""
    row = [bridge.button(f"Поднять до {v}", CALLBACK_MODULE, f"c:{v}:{cap}{tail}") for v in raise_options(cap)]
    return [row] if row else []


def cap_text(cap: int, *, on_demand: bool = False) -> str:
    """«Предел исчерпан» словами — и как его поднять: кнопкой, командой или в приложении."""
    cards = f"{cap} {_plural(cap, 'карточка', 'карточки', 'карточек')}"
    if on_demand:
        head = f"Дневной предел черновиков ответов — {cards} — исчерпан."
    else:
        head = (f"Черновики ответов: дневной предел — {cards} — исчерпан. До конца суток новые сообщения от "
                "людей из списка приходят обычными уведомлениями.")
    if raise_options(cap):
        how = (f"Поднять предел — кнопкой ниже, командой /drafts cap ЧИСЛО (до {rs.DAILY_CAP[1]}) или во "
               "вкладке «Черновики» приложения «Штурман».")
    else:
        how = f"Это наибольший предел — {rs.DAILY_CAP[1]} карточек в сутки."
    return head + "\n" + how


async def tell_cap(conn: asyncpg.Connection, cap: int) -> None:
    """Фоновая репетиция упёрлась в предел: владельцу — одно сообщение (без звука) с «Поднять до …»."""
    await bridge.notify_owner(conn, cap_text(cap), buttons=cap_buttons(cap) or None, silent=True)


async def _raise_cap(conn: asyncpg.Connection, principal: Any, tail: str) -> dict[str, Any]:
    """Нажатие «Поднять до N» владельцем в личном чате бота согласований — его решение: сразу, как
    команда `/drafts cap N`. Из приложения (`telegram.miniapp`) эта кнопка не нажимается."""
    from .. import authority, confirm
    from . import service as rehearsal_service
    refused = {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
    if principal.action != "telegram.callback":
        return {**refused, "answer": "Поднять предел можно только нажатием в боте согласований."}
    parts = tail.split(":")
    if len(parts) not in (2, 3) or not all(p.isascii() and p.isdigit() and 0 < len(p) <= 18 for p in parts):
        return refused
    new, old = int(parts[0]), int(parts[1])
    message_id = int(parts[2]) if len(parts) == 3 else None
    if new not in raise_options(old):
        return refused
    s = await rs.load(conn)
    if s["daily_cap"] != old:
        return {"answer": f"Предел уже изменён: сейчас {s['daily_cap']} в сутки.", "edit_text": None,
                "remove_buttons": True}
    with authority.owner_context(principal.user_id, chat_id=principal.chat_id, action="telegram.button"):
        await confirm.apply_owner(conn, rehearsal_service.SETTINGS, {"changes": {"daily_cap": new}})
    out: dict[str, Any] = {"answer": f"Предел — {new} в сутки.", "edit_text": None, "remove_buttons": True,
                           "reply": {"text": f"Предел — {new} в сутки. Вернуть: /drafts cap {old}"}}
    mod = current()
    if message_id is not None and mod is not None:
        async def again() -> None:
            # После фиксации: просьба «Подготовить ответ» повторяется уже с новым пределом. Ответ
            # бота собирается после этой работы (`bridge.dispatch_callback`) — итог дописывается в него.
            got = await request_once(mod.state, message_id)
            out["reply"]["text"] += "\n" + got["text"]
        out["after_commit"] = again
    return out


CARD_HEAD = "✍️ Черновик ответа"
SUGGEST_STATUS = {
    "pending": "ничего не отправлено",
    "approved": "принят, отправляется",
    "sending": "отправляется",
    "sent": "отправлен",
    "failed": "не отправлен",
    "outcome_unknown": "неизвестно, дошёл ли — проверьте чат",
    "rejected": "не годится, ничего не отправлено",
    "expired": "срок истёк, ничего не отправлено",
    "superseded": "заменён новым, ничего не отправлено",
    "placed": "положен в поле ввода чата, отправляете вы",
}
CARD_ROOM = 220          # запас под строку о доставке и служебное


class Shown:
    """Предложенный ответ, каким он показан в карточке: исход модели и текст черновика."""

    def __init__(self, kind: str, text: str = "", question: str = "") -> None:
        self.kind, self.text, self.question = kind, text, question


async def compose_card(conn: asyncpg.Connection, item: Mapping[str, Any], row: Mapping[str, Any], outcome: Any,
                       *, tz: Any, now: datetime, used: list[str] | None = None, status: str = "pending",
                       hint: str | None = None) -> tuple[str, list[dict[str, Any]], bool]:
    """Карточка «Черновик ответа»: «вам написали» (как уведомление Р-84), свёрнутый разговор и на что ответ
    (Р-103), что ассистент предлагает ответить — моноширинным блоком (нажатие копирует). Текст без разметки
    и его оформление (richtext.py). `hint` — строка о доставке (что делает «Отправить» или почему его нет).
    Возвращает текст, оформление и `whole` — показан ли текст ответа целиком: обрезанный не отправляется
    («Отправить» его не берёт, `replies/delivery.py`)."""
    from .. import convo, richtext, spheres
    from ..notify import core as notify_core
    titles = (await spheres.titles_by_chat(conn, [item["chat_id"]])).get(item["chat_id"], [])
    sphere = (" · " + ", ".join(textlib.one_line(t, 40) for t in titles)) if titles else ""
    who = textlib.one_line(row["chat_title"], 60) or textlib.one_line(row["sender_name"], 60) or "собеседник"
    head = f"{CARD_HEAD} — {SUGGEST_STATUS.get(status, 'ничего не отправлено')}"
    if item.get("on_demand"):
        head += "\nПо вашей просьбе — кнопкой «Подготовить ответ»."
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
    # На что ответ (Р-103): последнее собственное сообщение собеседника в пачке — первыми словами;
    # опора ответа — только то, что модель получила и назвала сама (найденное в архиве этого чата).
    about: list[str] = []
    trigger = packs.Pack(rows=[dict(r) for r in rows], closed=True, closes_at=rows[-1]["sent_at"]).trigger(
        on_demand=True)
    answer = convo.answer_line(trigger) if trigger is not None else None
    if answer:
        about.append(answer)
    if used:
        about.append("Учтено найденное в архиве этого чата: " + "; ".join(f"«{u}»" for u in used))
    body: list[str] = [""]
    proposal = ""
    if outcome.kind == "reply":
        body.append("Ассистент предлагает ответить (нажмите на текст, чтобы скопировать):")
        proposal = outcome.text or ""
    elif outcome.kind == "decline":
        body.append("Ассистент не стал бы отвечать на это сообщение.")
    elif outcome.kind == "ask_owner":
        body += ["Ассистент не ответил бы сам, а спросил бы вас:", f"«{textlib.one_line(outcome.question, 500)}»"]
    else:
        body.append("Ассистенту не хватило бы сведений из этого чата: он попросил бы прочитать ещё один источник. "
                    "Черновик готовится только по этому чату.")
    tail: list[str] = []
    answered = await conn.fetchval(
        """SELECT EXISTS (SELECT 1 FROM messages n JOIN messages t ON t.id = $1
           WHERE n.chat_id = t.chat_id AND n.kind = 'message' AND n.deleted_at IS NULL AND n.is_outgoing IS TRUE
             AND (n.sent_at, n.id) > (t.sent_at, t.id))""", item["trigger_message_id"])
    if answered:
        tail += ["", "Пока ответ готовился, вы ответили сами — можно сравнить."]
    if hint:
        tail += ["", hint]
    # Текст ответа важнее контекста: разговор получает то, что осталось от предела сообщения. Не поместился
    # текст целиком — показывается начало, и отправить его отсюда нельзя.
    fixed = sum(richtext.utf16_len(x) + 1 for x in lines + about + body + tail) + CARD_ROOM
    room = bridge.MESSAGE_LIMIT - fixed
    whole = richtext.utf16_len(proposal) <= room
    if not whole:
        cut = proposal
        while cut and richtext.utf16_len(cut) > max(0, room - 60):
            cut = cut[: max(0, len(cut) - 200)]
        proposal = cut.rstrip() + "… (текст длиннее карточки — показано начало)"
    found = await convo.rows(conn, item["chat_id"], pack_ids=[r["id"] for r in rows])
    quote = convo.fit(convo.lines(found, tz, now, peer=who),
                      room - richtext.utf16_len(proposal) - 40) if whole else []
    rich = richtext.Rich().lines(lines)
    if quote or about:
        rich.line()
    convo.add_quote(rich, quote)
    rich.lines(about)
    rich.lines(body)
    if proposal:
        rich.span(proposal, "pre")
    rich.lines(tail)
    text, entities = rich.build()
    return bridge.fit_message(text), entities, whole


def delivery_hint(avail: Any, *, kind: str) -> str:
    """Строка о доставке под ответом: что сделает «Отправить» или почему его нет."""
    from .. import words
    if kind != "reply":
        return f"Ничего не отправлено. Свой ответ — «{words.DICTATE_OWN}»."
    if avail.send:
        return (f"«{words.SEND}» — ответ уйдёт от вашего имени через бота согласований, только после нажатия. "
                f"Свой вариант — «{words.DICTATE_OWN}».")
    why = f" ({avail.why})" if avail.why and avail.send_missing != "not_pending" else ""
    if getattr(avail, "put", False):
        # «В чат» (Р-105, часть 2): текст ляжет в поле ввода чата, отправляет владелец сам
        return (f"Отправить отсюда сейчас нельзя{why}. «{words.PUT}» положит текст в поле ввода этого чата — "
                f"отправите сами. Свой вариант — «{words.DICTATE_OWN}».")
    return (f"Отправить отсюда сейчас нельзя{why}: скопируйте текст и отправьте в чате сами. "
            f"Свой вариант — «{words.DICTATE_OWN}».")


async def _card_parts(conn: asyncpg.Connection, item: Mapping[str, Any], draft: Mapping[str, Any] | None,
                      outcome: Any, *, used: list[str] | None = None, status: str = "pending",
                      chosen: str | None = None,
                      note: str | None = None) -> tuple[str, list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """Текст и кнопки карточки по нынешнему состоянию черновика — для первой отправки и для перерисовки."""
    from ..notify import core as notify_core
    from ..replies import delivery
    mod = current()
    tz = mod.zone() if mod is not None else notify_core.zone("UTC")
    row = await message_row(conn, item["trigger_message_id"])
    if row is None:
        raise LookupError("сообщения больше нет")
    now = _now()
    tgt = await policy.target(conn, item["chat_id"]) if draft is not None else None
    state = mod.state if mod is not None else None
    # Сначала — помещается ли текст целиком (от этого зависит «Отправить»), потом строка о доставке.
    _, _, whole = await compose_card(conn, item, row, outcome, tz=tz, now=now, used=used, status=status,
                                     hint="x" * 160)
    avail = await delivery.for_draft(conn, state, draft, tgt, card_whole=whole) if draft is not None \
        else delivery.decide(delivery.Facts(pending=False))
    text, entities, _ = await compose_card(conn, item, row, outcome, tz=tz, now=now, used=used, status=status,
                                           hint=note or delivery_hint(avail, kind=outcome.kind))
    rows = delivery.buttons(avail, draft=draft, item=item, link=notify_core._link(row), chosen=chosen) \
        if status in ("pending",) else []
    return text, entities, rows


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


async def _tell_once(conn: asyncpg.Connection, item: Mapping[str, Any], code: str) -> None:
    """Разовая репетиция, на которую владелец уже получил «Готовлю…», не состоялась позже (в фоне, после
    ответа модели): владелец узнаёт причину, а не ждёт карточку молча. Без своего бота — некуда."""
    mod = current()
    if not item["on_demand"] or mod is None or not bridge.owns_bot():
        return
    text = await outcome_text(conn, mod.state, item["chat_id"], item["trigger_message_id"], code)
    await bridge.notify_owner(conn, "Черновика ответа не будет. " + text, silent=True)


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
            if await conn.fetchval("SELECT shturman_halted()"):
                return None       # владелец остановил ассистента (Р-87)
            settings = await autoreply.load(conn)
            row = await message_row(conn, message_id)
            if row is None or row["chat_id"] != chat_id or row["account_id"] != payload.get("account_id"):
                return None
            pack = await packs.around(conn, message_id)
            if await why_not(conn, row, s, max_age=float(settings["max_age_seconds"]), pack=pack,
                             state=self.state) is not None:
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
            if item is None:
                return "missing"
            if item["state"] != "queued":
                return "taken"      # уже готовится или показана (второе нажатие, обход)
            pack = await packs.around(conn, item["trigger_message_id"])
        if not packs.ready(pack) and not item["on_demand"]:
            return "waiting"        # человек дописывает: карточка — когда пачка закроется
        if pack is not None:
            await packs.settle(self.pool, pack.ids, state=self.state)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.rehearsal'))")
            item = await conn.fetchrow("SELECT * FROM rehearsal_items WHERE id = $1 FOR UPDATE", item_id)
            if item is None:
                return "missing"
            if item["state"] != "queued":
                return "taken"
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
            code = await why_not(conn, row, s, max_age=max_age, pack=pack, on_demand=on_demand, state=self.state)
            if code is not None:
                return await _skip(conn, item, code)
            # Ответ на это сообщение готовит (или уже подготовил) автоответ 2а — не «удалено» (Д-31).
            if await handled_by_autoreply(conn, self.state, chat_id, trigger):
                return await _skip(conn, item, "autoreply")
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
                    await tell_cap(conn, s["daily_cap"])
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
                context={"item_id": item_id, "hits": [h["id"] for h in hits]}, dedup_key=f"rehearsal:{item_id}")
            await conn.execute(
                """UPDATE rehearsal_items SET state = 'preparing', job_id = $2, asked_at = now(), covered = $3,
                          offered = $4::bigint[], pack_ids = $5::bigint[], media_notes = $6::text[],
                          updated_at = now() WHERE id = $1""",
                item_id, job_id, max(1, min(int(covered), 1000)), offered, [r["id"] for r in visible],
                packs.card_lines(visible, codes))
        return "preparing"

    async def prepare_once(self, item_id: int) -> str:
        """Разовая репетиция в фоне (вложения ещё разбираются, обход после перезапуска): владельцу уже
        ответили «Готовлю…», поэтому любой итог, кроме «готовится», он получает словами."""
        outcome = await self.prepare(item_id)
        if outcome not in ("preparing", "waiting", "taken"):
            async with self.pool.acquire() as conn:
                item = await conn.fetchrow("SELECT * FROM rehearsal_items WHERE id = $1", item_id)
                if item is not None:
                    await _tell_once(conn, item, outcome)
        return outcome

    async def sweep(self) -> int:
        """Обход: строки, оставшиеся «ждать пачку» (сервис перезапускался), разбираются; старые — удаляются."""
        async with self.pool.acquire() as conn:
            stuck = [(r["id"], r["on_demand"]) for r in await conn.fetch(
                """SELECT id, on_demand FROM rehearsal_items WHERE state = 'queued'
                   AND created_at < now() - make_interval(secs => $1) ORDER BY id""", float(QUEUED_STUCK))]
            await conn.execute("DELETE FROM rehearsal_items WHERE created_at < now() - make_interval(secs => $1)",
                               KEEP.total_seconds())
        for item_id, on_demand in stuck:
            await (self.prepare_once(item_id) if on_demand else self.prepare(item_id))
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

def _reply_anchor(item: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> int:
    """На какое сообщение отвечает черновик: последнее собственное сообщение собеседника в пачке."""
    if rows:
        found = packs.Pack(rows=[dict(r) for r in sorted(rows, key=lambda r: (r["sent_at"], r["id"]))],
                           closed=True, closes_at=max(r["sent_at"] for r in rows)).trigger(on_demand=True)
        if found is not None:
            return int(found["id"])
    return int(item["trigger_message_id"])


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
        await _tell_once(conn, item, "stopped")
        return
    # Пока модель думала, мир мог измениться: удалено, изменено, скрыто защитой, чат исключён,
    # человека убрали из списка, черновики выключили, бота не стало — карточки не будет. У каждой
    # причины свой код: «удалено» — только об удалённом (Д-31).
    code = ("unavailable" if mod is None else
            "gone" if row is None or row["deleted_at"] is not None or row["excluded"] else
            "hidden" if not row["agent_visible"] else
            "edited" if row["edited_at"] is not None else None)
    if code is None:
        code = await audience(conn, mod.state, row, s)
    if code is None and not bridge.owns_bot():
        code = "no_bot"
    if code is not None:
        await _skip(conn, item, code)
        await _tell_once(conn, item, code)
        return
    from ..replies import model
    settings = await autoreply.load(conn)
    try:
        outcome = model.parse(result.get("parsed"), [{"message_id": i} for i in item["offered"]],
                              max_chars=settings["max_reply_chars"] + 500)
    except (ValueError, TypeError, KeyError):
        await _skip(conn, item, "invalid_model_result", state="failed")
        await _tell_once(conn, item, "invalid_model_result")
        return
    from .. import convo
    from ..notify import core as notify_core
    from ..notify import settings as ns
    from ..outbox import drafts
    now = _now()
    tz = mod.zone()
    used = await convo.used(conn, item["chat_id"], outcome.source_keys, (job.get("context") or {}).get("hits"))
    draft = None
    refused_note: str | None = None
    if outcome.kind == "reply":
        # Текст ответа — черновиком сервиса (Р-105): только его можно потом отправить нажатием владельца.
        tgt = await policy.target(conn, item["chat_id"])
        pack_rows = await conn.fetch(notify_core._ROWS, list(item["pack_ids"] or []))
        try:
            draft = await drafts.create_suggestion(conn, tgt, text=outcome.text,
                                                   trigger_message_id=item["trigger_message_id"],
                                                   reply_to_message_id=_reply_anchor(item, list(pack_rows)))
        except drafts.Refused as exc:
            logger.info("черновик ответа № %s: без отправки (%s)", item["id"], exc.decision.code)
            if exc.decision.code == "agent_draft_pending":
                refused_note = words.AGENT_DRAFT_WAITING
    shown = Shown(outcome.kind, draft["text"] if draft is not None else (outcome.text if outcome.kind == "reply" else ""),
                  getattr(outcome, "question", "") or "")
    if draft is not None:
        await conn.execute("UPDATE rehearsal_items SET draft_id = $2 WHERE id = $1", item["id"], draft["id"])
    full = {**dict(item), "draft_id": draft["id"] if draft is not None else None}
    text, entities, keyboard = await _card_parts(conn, full, draft, shown, used=used, note=refused_note)
    quiet = notify_core.in_quiet(await ns.load(conn), now.astimezone(tz))
    await bridge.notify_owner(conn, text, buttons=keyboard or None, silent=quiet,
                              handler=CARD_HANDLER, context={"item_id": item["id"]},
                              dedup_key=f"rehearsal-card:{item['id']}", entities=entities)
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
        await _tell_once(conn, item, "model_unavailable")


@bridge.on_result(CARD_HANDLER)
async def _card_delivered(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    """Карточка дошла: каким сообщением — запоминается у черновика, чтобы потом перерисовать её итогом."""
    item_id, message_id = (job.get("context") or {}).get("item_id"), result.get("message_id")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (item_id, message_id)):
        return
    draft = await conn.fetchrow(
        """UPDATE outbox_drafts d SET card_message_ids = array_append(d.card_message_ids, $2),
                  card_messages = d.card_messages || jsonb_build_object('1', $2::bigint)
           FROM rehearsal_items r WHERE r.id = $1 AND d.id = r.draft_id RETURNING d.*""", item_id, message_id)
    if draft is not None and draft["status"] != "pending":
        from ..outbox import drafts
        await drafts.refresh_cards(conn, draft, await policy.target(conn, draft["chat_id"]))


@bridge.on_failure(CARD_HANDLER)
async def _card_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    """Карточка не дошла: оценить нечего, черновик снимается, а уведомление, которое она заменяла,
    возвращается в очередь."""
    item_id = (job.get("context") or {}).get("item_id")
    if not isinstance(item_id, int):
        return
    item = await conn.fetchrow(
        """UPDATE rehearsal_items SET state = 'failed', reason = 'card_not_delivered', updated_at = now()
           WHERE id = $1 AND state = 'shown' AND rating IS NULL RETURNING *""", item_id)
    if item is None:
        return
    if item["draft_id"] is not None:
        from ..outbox import drafts
        draft = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1 FOR UPDATE", item["draft_id"])
        if draft is not None and draft["status"] == "pending":
            await drafts.finish(conn, draft, await policy.target(conn, draft["chat_id"]), "failed",
                                code="card_not_delivered", message=policy.REASONS["card_not_delivered"],
                                tell_owner=False)
    done = await conn.execute(
        """UPDATE notify_items n SET state = 'waiting', dropped_reason = NULL, due_at = now()
           FROM messages m, messages t
           WHERE t.id = $2 AND n.message_id = m.id AND n.chat_id = $1 AND n.state = 'dropped'
             AND n.dropped_reason = 'rehearsal' AND (m.sent_at, m.id) <= (t.sent_at, t.id)""",
        item["chat_id"], item["trigger_message_id"])
    if int(done.split()[-1]):
        _wake_notify()


# --- черновик сервиса: перерисовка, приложение, «Отправить» ------------------------------------------------

def _shown_of(item: Mapping[str, Any], draft: Mapping[str, Any] | None) -> Shown:
    if draft is not None:
        return Shown("reply", draft["text"])
    return Shown(item["outcome"] or "decline")


async def refresh_card(conn: asyncpg.Connection, draft: Mapping[str, Any], delivered: Mapping[int, int]) -> int:
    """Черновик сервиса решился (отправлен, истёк, заменён, отклонён): карточка — с итогом и без кнопок
    доставки. Текст уже стёрт — одна строка итога."""
    item = await conn.fetchrow("SELECT * FROM rehearsal_items WHERE draft_id = $1", draft["id"])
    message_id = delivered.get(1)
    if message_id is None:
        return 0
    status = draft["status"]
    entities = None
    if item is None or draft["text"] == "":
        text = f"{CARD_HEAD} — {SUGGEST_STATUS.get(status, status)}. Текст удалён."
    else:
        try:
            text, entities, _ = await _card_parts(conn, item, draft, _shown_of(item, draft), status=status)
        except LookupError:
            text = f"{CARD_HEAD} — {SUGGEST_STATUS.get(status, status)}."
    await bridge.edit_owner_message(conn, message_id, text, remove_buttons=True, entities=entities)
    return 1


async def app_entry(conn: asyncpg.Connection, draft: Mapping[str, Any], tgt: policy.Target) -> dict[str, Any] | None:
    """«Черновик ответа» для приложения «Штурман» (раздел «Согласования»): та же шапка, текст дословно и
    те же кнопки сервиса, что под карточкой в боте (`replies/delivery.buttons`, Д-46)."""
    from .. import convo
    from ..outbox import drafts
    from ..replies import delivery
    item = await conn.fetchrow("SELECT * FROM rehearsal_items WHERE draft_id = $1", draft["id"])
    if item is None:
        return None
    mod = current()
    avail = await delivery.for_draft(conn, mod.state if mod is not None else None, draft, tgt)
    who = textlib.one_line(tgt.display_name, 60) or "собеседник"
    head = [f"💬 Вам написали: {who} · личный чат"]
    anchor = await conn.fetchrow(
        "SELECT * FROM messages WHERE id = $1 AND deleted_at IS NULL AND agent_visible", item["trigger_message_id"])
    line = convo.answer_line(anchor) if anchor is not None else None
    if line:
        head.append(line)
    head.append(delivery_hint(avail, kind="reply"))
    found = await convo.rows(conn, item["chat_id"], pack_ids=list(item["pack_ids"] or []))
    from ..notify import core as notify_core
    conversation = convo.lines(found, mod.zone() if mod is not None else notify_core.zone("UTC"), peer=who)
    return {
        "id": draft["id"], "title": words_reply_draft(), "head": head, "context": conversation[-5:],
        "text": draft["text"], "suggest": True,
        "buttons": delivery.buttons(avail, draft=draft, item=item, bot=False),
        "card_message_id": drafts._card_messages(draft).get(1),
        "expires_at": draft["expires_at"],
    }


def words_reply_draft() -> str:
    from .. import words
    return words.REPLY_DRAFT


async def on_draft_button(conn: asyncpg.Connection, action: str, draft: Mapping[str, Any],
                          tgt: policy.Target) -> dict[str, Any]:
    """«Отправить» под «Черновиком ответа» (`sh:ob:s`): та же проверка и тот же путь отправки, что у
    черновика ассистента (`drafts.approve`); нажатие — оценка «годится». `sh:ob:r` под такой карточкой не
    ставится; если пришло — то же, что «Не годится»."""
    from ..outbox import drafts
    item = await conn.fetchrow("SELECT * FROM rehearsal_items WHERE draft_id = $1 FOR UPDATE", draft["id"])
    if item is None:
        return {"answer": "Эта карточка уже недоступна.", "edit_text": None, "remove_buttons": True}
    if draft["status"] != "pending":
        return {"answer": drafts.ALREADY.get(draft["status"], "Черновик уже решён."), "edit_text": None,
                "remove_buttons": True}
    if await conn.fetchval("SELECT $1::timestamptz <= now()", draft["expires_at"]):
        await drafts.finish(conn, draft, tgt, "expired", tell_owner=False)
        return {"answer": "Срок черновика истёк — ничего не отправлено.", "edit_text": None, "remove_buttons": True}
    if action == "r":
        return await _not_good(conn, item, draft, tgt)
    if action == "p":
        return await _put(conn, item, draft, tgt)
    decided = await drafts.approve(conn, draft, tgt)
    if decided.get("wait"):
        text, entities, keyboard = await _card_parts(conn, item, draft, _shown_of(item, draft))
        return {"answer": decided["answer"][:190], "edit_text": None, "remove_buttons": False, "buttons": keyboard}
    if not decided["ok"]:
        await drafts.finish(conn, draft, tgt, "failed", code=decided["code"], message=decided["message"],
                            tell_owner=False)
        return {"answer": "Не отправлено: " + decided["message"], "edit_text": None, "remove_buttons": True}
    journal = await _rate(conn, item, "good", "send")
    fresh = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1", draft["id"])
    text, entities, _ = await _card_parts(conn, item, fresh, _shown_of(item, fresh), status=fresh["status"])
    return {"answer": "Принято, отправляю.", "edit_text": text, "edit_entities": entities, "remove_buttons": True,
            "after_commit": journal}



async def _put(conn: asyncpg.Connection, item: Mapping[str, Any], draft: Mapping[str, Any],
               tgt: policy.Target) -> dict[str, Any]:
    """«В чат» (Р-105, часть 2): текст черновика — в поле ввода этого чата владельца (`replies/put.py`).
    В транзакции нажатия — только проверки и отметка «кладу»; запрос к Telegram — после неё (`after_commit`),
    результат — отдельной короткой транзакцией. Повторов нет."""
    from ..outbox import drafts
    from ..replies import put as put_rules
    try:
        put_rules.require_press()
    except put_rules.Refused:
        return {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
    code = await put_rules.check(conn, tgt, draft["text"])
    if code is not None:
        return {"answer": put_rules.WORDS[code][:190], "edit_text": None, "remove_buttons": False}
    if await conn.fetchval("SELECT put_started_at > now() - make_interval(secs => $2) FROM outbox_drafts WHERE id = $1",
                           draft["id"], float(put_rules.BUSY_SECONDS)):
        # идёт запрос — или его исход неизвестен и текст ещё может появиться в поле: второй записи нет
        return {"answer": "Уже кладу текст в поле ввода — проверьте поле через минуту.", "edit_text": None,
                "remove_buttons": False}
    await conn.execute("UPDATE outbox_drafts SET put_started_at = now() WHERE id = $1", draft["id"])
    mod = current()
    out: dict[str, Any] = {"answer": "Кладу текст в поле ввода…", "edit_text": None, "remove_buttons": False}

    async def run() -> None:
        result = await put_rules.put(mod.state if mod is not None else None, tgt, draft["text"],
                                     reply_to_tg_id=draft["reply_to_tg_id"])
        pool = mod.pool if mod is not None else None
        if pool is None:
            return
        async with pool.acquire() as conn2, conn2.transaction():
            fresh = await conn2.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1 FOR UPDATE", draft["id"])
            if result.placed and fresh is not None and fresh["status"] == "pending":
                await drafts.finish(conn2, fresh, tgt, "placed", tell_owner=False)
                await conn2.execute(
                    """UPDATE rehearsal_items SET rating = 'good', rated_via = 'put', rated_at = now(), updated_at = now()
                       WHERE id = $1 AND state = 'shown'""", item["id"])
            elif fresh is not None:
                # «неизвестно» — запись могла уйти: отметка «кладу» остаётся на минуту, повторное нажатие ждёт
                await conn2.execute("UPDATE outbox_drafts SET put_started_at = CASE WHEN $2 THEN now() END "
                                    "WHERE id = $1", draft["id"], result.code == "unknown")
            from ..setup_page import audit
            # В журнал — номер и код итога. Ни текста ответа, ни того, что было в поле ввода владельца.
            await audit.write_conn(conn2, "draft.put", detail=f"черновик № {draft['id']}; итог: {result.code}")
        out["answer"] = result.text[:190]
        if result.code not in ("placed", "already"):
            out["reply"] = {"text": result.text}

    out["after_commit"] = run
    return out


async def _rate(conn: asyncpg.Connection, item: Mapping[str, Any], rating: str, via: str) -> Any:
    """Оценка ответа. Возвращает запись в журнал действий — её вызывают после фиксации нажатия
    (`after_commit`): номер карточки и коды, без текста."""
    before = item["rating"]
    await conn.execute(
        """UPDATE rehearsal_items SET rating = $2, rated_via = $3, rated_at = now(), updated_at = now()
           WHERE id = $1 AND state = 'shown'""", item["id"], rating, via)
    mod = current()

    async def journal() -> None:
        if mod is not None:
            from ..setup_page import audit
            await audit.write(mod.pool, "rehearsal.rate",
                              detail=f"карточка № {item['id']}; оценка: {rating}; как: {via}"
                                     + (f"; была: {before}" if before else ""))
    return journal


async def _not_good(conn: asyncpg.Connection, item: Mapping[str, Any], draft: Mapping[str, Any] | None,
                    tgt: policy.Target | None) -> dict[str, Any]:
    """«Не годится»: оценка; ждущий черновик снимается — ничего не отправлено. Под карточкой остаются
    «Наговорить свой» и «Открыть чат»."""
    from .. import authority
    from ..outbox import drafts
    journal = await _rate(conn, item, "bad", "button")
    in_bot = (authority.get_owner_principal() is not None
              and authority.get_owner_principal().action == "telegram.callback")
    if draft is not None and draft["status"] == "pending":
        # Нажатие в боте само перерисует кнопки этой карточки; из приложения — правкой карточки в боте.
        await drafts.finish(conn, draft, tgt, "rejected", tell_owner=False, skip_part=1 if in_bot else None)
        draft = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1", draft["id"])
    from ..notify import core as notify_core
    from ..replies import delivery
    row = await message_row(conn, item["trigger_message_id"])
    keyboard = delivery.buttons(delivery.decide(delivery.Facts(pending=False)), draft=draft, item=item,
                                link=notify_core._link(row) if row is not None else None, chosen="bad")
    return {"answer": "Оценка сохранена: не годится. Ничего не отправлено.", "edit_text": None,
            "remove_buttons": False, "buttons": keyboard, "after_commit": journal}


# --- оценка владельца -----------------------------------------------------------------------------------

@bridge.on_callback(CALLBACK_MODULE)
async def _rated(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    from .. import authority, confirm
    principal = authority.requires_owner()
    if principal.user_id != user_id or principal.source != "telegram":
        raise confirm.Refused("Кнопка требует проверенного владельца Telegram.", 403, "owner_required")
    code, _, tail = rest.partition(":")
    if code == "c":
        return await _raise_cap(conn, principal, tail)
    raw_id, _, nonce = tail.partition(":")
    if code == NOT_GOOD_CODE:
        # «Не годится» под карточкой «Черновик ответа» (Р-105).
        if not raw_id.isascii() or not raw_id.isdigit() or len(raw_id) > 18:
            return {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
        item = await conn.fetchrow("SELECT * FROM rehearsal_items WHERE id = $1 FOR UPDATE", int(raw_id))
        if item is None or item["state"] != "shown" or not hmac.compare_digest(item["nonce"], nonce):
            return {"answer": "Эта карточка уже недоступна.", "edit_text": None, "remove_buttons": True}
        draft = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1 FOR UPDATE", item["draft_id"]) \
            if item["draft_id"] is not None else None
        return await _not_good(conn, item, draft, await policy.target(conn, item["chat_id"]))
    rating = CODES.get(code)
    if (rating is None and code != "k") or not raw_id.isdigit():
        return {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
    item = await conn.fetchrow("SELECT * FROM rehearsal_items WHERE id = $1 FOR UPDATE", int(raw_id))
    if item is None or item["state"] != "shown" or not hmac.compare_digest(item["nonce"], nonce):
        return {"answer": "Эта карточка уже недоступна.", "edit_text": None, "remove_buttons": True}
    if code == "k":
        # «Не писать свой вариант» под карточкой прежней версии: просьба в боте снимается, оценка остаётся.
        await conn.execute(
            """DELETE FROM executor_state WHERE key = 'prompt' AND value->>'kind' = 'variant'
               AND (value->>'id')::bigint = $1""", item["id"])
        return {"answer": "Хорошо, без своего варианта.", "edit_text": None, "remove_buttons": False,
                "buttons": rating_buttons(item["id"], item["nonce"], chosen=item["rating"])}
    # Карточка прежней версии (репетиция до Р-105): оценка сохраняется, как раньше.
    out = {"answer": f"Оценка сохранена: {RATING_WORDS[rating]}. Её можно изменить.", "edit_text": None,
           "remove_buttons": False, "buttons": rating_buttons(item["id"], item["nonce"], chosen=rating),
           "after_commit": await _rate(conn, item, rating, "button")}
    if rating == "almost":
        out["answer"] = "Оценка сохранена: почти. Свой вариант теперь — «🎙 Ответить голосом»."
    return out


async def save_variant(conn: asyncpg.Connection, item_id: int, nonce: str, text: str) -> dict[str, Any]:
    """Свой вариант текстом — ответ на просьбу, которую бот выдал до Р-105 (после «Почти — поправил бы»).
    Новых просьб нет: свой вариант теперь — голосом или наброском («🎙 Наговорить свой»). Хранится у строки
    подготовки и удаляется вместе с ней (30 дней)."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "text": "Вариант пустой — ничего не записано."}
    if len(text) > VARIANT_MAX:
        return {"ok": False, "text": f"Вариант длиннее {VARIANT_MAX} знаков — ничего не записано."}
    item = await conn.fetchrow("SELECT * FROM rehearsal_items WHERE id = $1 FOR UPDATE", item_id)
    if item is None or item["state"] != "shown" or not hmac.compare_digest(item["nonce"], nonce):
        return {"ok": False, "text": "Эта карточка уже недоступна — вариант не записан."}
    await conn.execute("UPDATE rehearsal_items SET owner_variant = $2, owner_variant_at = now(), updated_at = now() "
                       "WHERE id = $1", item_id, text)
    from ..setup_page import audit
    await audit.write_conn(conn, "rehearsal.variant", detail=f"карточка № {item_id}; знаков: {len(text)}")
    return {"ok": True, "text": "Записал ваш вариант; хранится 30 дней вместе с карточкой. Ничего не отправлено."}


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
    """«Подготовить ответ» у уведомления и карточки разбора входящих — в любом способе работы (Р-105):
    черновик ответа на это сообщение, даже если человека нет в списке и черновики сами не готовятся.
    Ничего не отправляется; входит в дневной предел. Возвращает {ok, text} — ответ владельцу словами."""
    mod = current()
    if mod is None or not bridge.owns_bot():
        return {"ok": False, "text": "Черновик ответа сейчас не подготовить: сервис не запущен или нет бота согласований."}
    async with state.pool.acquire() as conn:
        if await conn.fetchval("SELECT shturman_halted()"):
            from .. import halt
            return {"ok": False, "text": "Ассистент остановлен — ответ не готовится. Продолжить — /resume."}
        row = await message_row(conn, message_id)
        s = await rs.load(conn)
        code = await _once_code(conn, row, s, message_id)
        if code is not None:
            return {"ok": False, "text": "Не получилось: " + WORDS[code]}
        # Ответ на это сообщение уже готовит автоответ 2а (Д-31): второй не готовим, но и не молчим —
        # владелец узнаёт, где черновик. Раньше предела: эта просьба в него не входит.
        found = await autoreply.claim(conn, state, row["chat_id"], message_id)
        if found is not None:
            logger.info("черновик по просьбе: сообщение № %s готовит автоответ (задача № %s)", message_id,
                        found.task["id"] if found.task is not None else "—")
            return {"ok": True, "answer": "Ответ готовит автоответ.", "text": autoreply.claim_words(found)}
        pack = await packs.around(conn, message_id)
        media, voice = await packs.media_on(conn), packs.switches_of(state)
        slow = pack is not None and packs.MEDIA_WAIT > 0 and any(packs.waiting(r, media, voice) for r in pack.visible)
        used = await conn.fetchval(
            "SELECT count(*) FROM rehearsal_items WHERE asked_at > now() - interval '24 hours'")
        if used >= s["daily_cap"]:
            # Под ответом — «Поднять до …»: нажатие поднимает предел и сразу повторяет эту просьбу.
            return {"ok": False, "text": cap_text(s["daily_cap"], on_demand=True),
                    "buttons": cap_buttons(s["daily_cap"], message_id=message_id)}
        existing = await conn.fetchrow("SELECT id, state FROM rehearsal_items WHERE trigger_message_id = $1", message_id)
        if existing is not None and existing["state"] in ("queued", "preparing"):
            return {"ok": True, "text": "Ответ на это сообщение уже готовится — карточка придёт сюда."}
        if existing is not None and existing["state"] == "shown":
            return {"ok": False, "text": "Черновик ответа на это сообщение уже был — карточка выше в этом чате."}
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
        mod.state.spawn(mod.prepare_once(item_id), name=f"rehearsal-once-{item_id}")
        return {"ok": True, "text": "Готовлю черновик ответа — сначала дождусь разбора вложений "
                                    f"(до {int(packs.MEDIA_WAIT)} с), потом пришлю карточкой. "
                                    "Ничего не отправляется."}
    outcome = await mod.prepare(item_id)
    if outcome == "autoreply":
        async with state.pool.acquire() as conn:
            text = await outcome_text(conn, state, row["chat_id"], message_id, outcome)
        return {"ok": True, "answer": "Ответ готовит автоответ.", "text": text}
    if outcome != "preparing":
        async with state.pool.acquire() as conn:
            text = await outcome_text(conn, state, row["chat_id"], message_id, outcome)
        return {"ok": False, "text": text}
    return {"ok": True, "text": "Готовлю черновик ответа — пришлю карточкой. Ничего не отправляется."}


async def _once_code(conn: asyncpg.Connection, row: Mapping[str, Any] | None, s: Mapping[str, Any],
                     message_id: int) -> str | None:
    probe = {"on_demand": True, "peer_tg_id": int(row["peer_tg_id"]) if row is not None else None}
    relaxed, max_age = _for_item(s, {"max_age_seconds": 0}, probe)
    pack = await packs.around(conn, message_id) if row is not None else None
    return await why_not(conn, row, relaxed, max_age=max_age, pack=pack, on_demand=True)   # список не нужен


async def why_not_once(conn: asyncpg.Connection, message_id: int) -> str | None:
    """Для кнопки «Подготовить ответ» под уведомлением: почему черновик на это сообщение
    невозможна (код из `WORDS`) — тогда кнопки нет; None — кнопка будет. Временное (остановка,
    дневной предел) кнопку не убирает: на нажатие владелец получит точную причину."""
    if current() is None:
        return "unavailable"
    row = await message_row(conn, message_id)
    return await _once_code(conn, row, await rs.load(conn), message_id)


@bridge.on_owner_change
async def _owner_changed(conn: asyncpg.Connection, new_user_id: int) -> None:
    """Новый владелец не наследует чужой выбор: черновики выключаются, список людей очищается, ждущие
    черновики сервиса истекают — отправить их некому."""
    await conn.execute("DELETE FROM settings WHERE key = $1", rs.SETTINGS_KEY)
    await conn.execute(
        """UPDATE rehearsal_items SET state = 'skipped', reason = 'owner_changed', updated_at = now()
           WHERE state IN ('queued', 'preparing')""")
    await conn.execute(
        """UPDATE outbox_drafts SET status = 'expired', error_code = 'owner_changed', finished_at = now()
           WHERE origin = 'suggest' AND status = 'pending'""")


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
                  count(*) FILTER (WHERE rated_via = 'send') AS sent,
                  count(*) FILTER (WHERE rated_via = 'voice') AS voice,
                  count(*) FILTER (WHERE rated_via = 'put') AS put,
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
