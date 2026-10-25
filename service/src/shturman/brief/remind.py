"""Напоминание исполнителю: кнопка «Напомнить» под договорённостью «вам обещали» (решение владельца 25.10.2026).

Где кнопка. В подробностях справки и сигнала о просрочке («Показать», `brief/core.py`) — у принятой владельцем
открытой договорённости, которую человек обещал владельцу (`owed_to_owner`), если человек входит в круг
«кому предлагать напоминание» (настройка ниже).

Что делает нажатие. Код собирает текст напоминания по шаблону — без модели: что обещано и срок. И решает, куда
его положить, по тому, где с исполнителем можно говорить:
  * `put`    — личный чат основного аккаунта с ним: кнопка «В чат» кладёт текст в поле ввода этого чата
               (`replies/put.py`, тот же путь, что у черновика ответа): владелец правит и отправляет сам;
  * `helper` — личного чата основного аккаунта нет, есть чат аккаунта помощника (личный или группа, где
               договорились): «Скопировать» и отправить оттуда;
  * `group`  — договорились в группе основного аккаунта: «Скопировать» и отправить туда;
  * `none`   — в читаемой переписке чата с ним нет: «Скопировать».
Сервис ничего не отправляет и не согласует; от `SHTURMAN_SENDING` напоминание не зависит. Отправить от
аккаунта помощника кнопкой — позже, отдельным решением: сейчас этому аккаунту запрещены и отправка, и запись
в поле ввода (`docs/service.md`).

Кому предлагать (`remind.settings` в таблице `settings`): всем, кто обещал владельцу (по умолчанию), или
только выбранным людям (по номеру в Telegram) и договорённостям из выбранных групп; можно и выключить.
Меняет владелец на странице настройки — сразу; внутреннего API у настройки нет. Напоминание ничего не
раскрывает: текст видит только владелец в своём боте.

Пределы: не больше `DAILY_CAP` напоминаний в сутки; по одной договорённости повторное нажатие в течение суток
показывает то же напоминание. Строка живёт `KEEP` и удаляется в часы сводки; исключение чата и смена
владельца удаляют её сразу. В журнал действий — номер и исход «В чат», без текста.
"""

from __future__ import annotations

import hmac
import json
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import asyncpg

from .. import bridge
from ..outbox import text as textlib
from ..processing import commitments

SETTINGS_KEY = "remind.settings"
MAX_PEOPLE = 200
MAX_GROUPS = 100
DAILY_CAP = 30                      # напоминаний в сутки
KEEP = timedelta(hours=24)
COPY_LIMIT = 256                    # «Скопировать» (CopyTextButton) — до 256 знаков
WHAT_LIMIT = 150
BUTTON = "напомнить"
PUT = "В чат"
COPY = "Скопировать"
GROUP_TYPES = ("private_group", "private_supergroup", "public_supergroup")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "who": "all",                   # all — всем, кто обещал вам; chosen — только выбранным
    "people": [],                   # числовые ID людей в Telegram
    "groups": [],                   # номера чатов архива (групп), где договорились
}
FIELDS = frozenset(DEFAULTS)
WHO = ("all", "chosen")

_state: Any = None


def set_state(state: Any) -> None:
    global _state
    _state = state


def current_state() -> Any:
    return _state


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- настройка «кому предлагать» ---------------------------------------------------------------------------

def _ids(value: Any, field: str, limit: int) -> list[int]:
    if not isinstance(value, list) or len(value) > limit or any(
            isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in value):
        raise ValueError(f"поле {field}: список числовых номеров, не больше {limit}")
    return sorted(set(value))


def validate(data: Mapping[str, Any]) -> dict[str, Any]:
    """Проверяет изменения. Незнакомое поле или неверное значение — ValueError с объяснением."""
    if not isinstance(data, Mapping):
        raise ValueError("нужен объект с настройками")
    unknown = set(data) - FIELDS
    if unknown:
        raise ValueError("нет таких настроек: " + ", ".join(sorted(unknown)))
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "enabled":
            if not isinstance(value, bool):
                raise ValueError("поле enabled: true или false")
        elif key == "who":
            if value not in WHO:
                raise ValueError("поле who: all (всем, кто обещал вам) или chosen (только выбранным)")
        elif key == "people":
            value = _ids(value, key, MAX_PEOPLE)
        elif key == "groups":
            value = _ids(value, key, MAX_GROUPS)
        out[key] = value
    return out


def normalize(raw: Any) -> dict[str, Any]:
    """Сохранённое поверх умолчаний; испорченное значение заменяется умолчанием."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    out = dict(DEFAULTS)
    if isinstance(raw, Mapping):
        for key in FIELDS & set(raw):
            try:
                out.update(validate({key: raw[key]}))
            except ValueError:
                pass
    return out


async def load(conn: asyncpg.Connection) -> dict[str, Any]:
    return normalize(await conn.fetchval("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY))


async def save(conn: asyncpg.Connection, changes: Mapping[str, Any]) -> dict[str, Any]:
    """Изменение владельцем (страница настройки): проверка, запись, новое значение."""
    value = {**await load(conn), **validate(changes)}
    await conn.execute(
        """INSERT INTO settings (key, value) VALUES ($1, $2::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        SETTINGS_KEY, json.dumps(value, ensure_ascii=False, sort_keys=True))
    return value


def audit_detail(s: Mapping[str, Any]) -> str:
    """Для журнала действий: только признаки и числа, без имён и номеров."""
    return (f"предлагать: {'да' if s['enabled'] else 'нет'}; кому: "
            f"{'всем' if s['who'] == 'all' else 'выбранным'}; людей: {len(s['people'])}; групп: {len(s['groups'])}")


async def offered(conn: asyncpg.Connection, items: Sequence[Mapping[str, Any]]) -> set[int]:
    """Номера договорённостей, под которыми предлагать «Напомнить»: принята владельцем, открыта, обещана ему
    человеком — и человек (или группа, где договорились) в круге настройки."""
    candidates = [int(i["id"]) for i in items
                  if i["direction"] == "owed_to_owner" and i["status"] == "open"
                  and (i.get("debtor") or {}).get("peer_id") is not None]
    if not candidates:
        return set()
    s = await load(conn)
    if not s["enabled"]:
        return set()
    if s["who"] == "all":
        return set(candidates)
    rows = await conn.fetch(
        """SELECT DISTINCT c.id FROM commitments c
           JOIN peers p ON p.id = c.debtor_peer_id
           LEFT JOIN person_peers pp ON pp.peer_id = c.debtor_peer_id
           LEFT JOIN person_peers pp2 ON pp2.person_id = pp.person_id
           LEFT JOIN peers p2 ON p2.id = pp2.peer_id
           WHERE c.id = ANY($1::bigint[])
             AND ((p.class = 'user' AND p.tg_id = ANY($2::bigint[]))
                  OR (p2.class = 'user' AND p2.tg_id = ANY($2::bigint[]))
                  OR c.chat_id = ANY($3::bigint[]))""",
        candidates, s["people"], s["groups"])
    return {int(r["id"]) for r in rows}


def button(pos: int, commitment_id: int) -> dict[str, str]:
    return bridge.button(f"{pos} — {BUTTON}", "br", f"n:{commitment_id}")


# --- куда и что ---------------------------------------------------------------------------------------------

async def _debtor_peers(conn: asyncpg.Connection, peer_id: int) -> list[int]:
    rows = await conn.fetch(
        """SELECT pp2.peer_id FROM person_peers pp JOIN person_peers pp2 ON pp2.person_id = pp.person_id
           WHERE pp.peer_id = $1""", peer_id)
    return sorted({peer_id, *(int(r["peer_id"]) for r in rows)})


async def route_for(conn: asyncpg.Connection, item: Mapping[str, Any]) -> tuple[str, int | None]:
    """Куда положить напоминание: (вид, номер чата архива). Порядок — в описании модуля."""
    peers = await _debtor_peers(conn, int(item["debtor"]["peer_id"]))
    source = int(item["chat"]["id"])
    personal = await conn.fetch(
        """SELECT c.id, a.role FROM chats c JOIN accounts a ON a.id = c.account_id
           WHERE c.peer_id = ANY($1::bigint[]) AND c.type = 'personal_chat' AND NOT c.excluded
           ORDER BY (c.id = $2) DESC, c.id""", peers, source)
    own = next((int(r["id"]) for r in personal if r["role"] == "owner"), None)
    if own is not None:
        return "put", own
    src = await conn.fetchrow(
        """SELECT c.type, c.excluded, a.role FROM chats c JOIN accounts a ON a.id = c.account_id
           WHERE c.id = $1""", source)
    if src is not None and not src["excluded"] and src["role"] == "assistant":
        return "helper", source
    helper = next((int(r["id"]) for r in personal if r["role"] == "assistant"), None)
    if helper is not None:
        return "helper", helper
    if src is not None and not src["excluded"] and src["type"] in GROUP_TYPES:
        return "group", source
    return "none", None


def _first_name(name: str | None) -> str:
    word = (name or "").strip().split(" ")[0]
    return word if word and len(word) <= 30 else ""


def reminder_text(item: Mapping[str, Any], today: date, *, in_group: bool) -> str:
    """Текст по шаблону: что обещано и срок. Без модели; владелец правит его сам перед отправкой."""
    what = textlib.one_line(str(item["what"] or ""), WHAT_LIMIT).rstrip(" .")
    due = date.fromisoformat(item["due_date"]) if item.get("due_date") else None
    when = ""
    if due is not None and item.get("due_reason") == "ok":
        when = f" — срок был {due:%d.%m}" if due < today else f" — срок {due:%d.%m}"
    name = _first_name((item.get("debtor") or {}).get("name")) if in_group else ""
    head = f"{name}, напоминаю" if name else "Добрый день! Напоминаю"
    return f"{head}: {what}{when}. Как с этим?"


ROUTE_WORDS = {
    "put": "«В чат» положит текст в поле ввода вашего личного чата с ним — поправите и отправите сами.",
    "helper": ("Личного чата вашего основного аккаунта с ним в архиве нет; есть чат аккаунта помощника{where}. "
               "Скопируйте текст и отправьте оттуда."),
    "group": "Договорились в группе{where}. Скопируйте текст и отправьте туда.",
    "none": "Чата с ним в читаемой переписке нет. Скопируйте текст и отправьте, где удобно.",
}


async def _where(conn: asyncpg.Connection, route: str, chat_id: int | None) -> str:
    if route not in ("helper", "group") or chat_id is None:
        return ""
    row = await conn.fetchrow("SELECT type, title FROM chats WHERE id = $1", chat_id)
    if row is None or row["type"] not in GROUP_TYPES or not row["title"]:
        return ""
    return f" — «{textlib.one_line(row['title'], 60)}»"


def card_buttons(row: Mapping[str, Any]) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    if row["route"] == "put" and row["placed_at"] is None:
        rows.append([bridge.button(PUT, "br", f"p:{row['id']}:{row['nonce']}")])
    if textlib.utf16_len(row["text"]) <= COPY_LIMIT:
        rows.append([{"text": COPY, "copy": row["text"]}])
    return rows


async def card(conn: asyncpg.Connection, row: Mapping[str, Any], name: str) -> dict[str, Any]:
    where = await _where(conn, row["route"], row["chat_id"])
    lines = [f"Напоминание — {name}:", row["text"], ROUTE_WORDS[row["route"]].format(where=where),
             "Сервис ничего не отправляет: отправляете вы."]
    return {"text": bridge.fit_message("\n\n".join(lines)), "buttons": card_buttons(row)}


# --- нажатия ------------------------------------------------------------------------------------------------

def _answer(text: str, **extra: Any) -> dict[str, Any]:
    return {"answer": text, "edit_text": None, "remove_buttons": False, **extra}


async def on_remind(conn: asyncpg.Connection, commitment_id: int, *, today: date) -> dict[str, Any]:
    """«Напомнить»: напоминание по договорённости — новое или уже собранное за сутки."""
    if await conn.fetchval("SELECT shturman_halted()"):
        return _answer("Ассистент остановлен.")
    item = await commitments.get_commitment(conn, commitment_id, today=today)
    if item is None or commitment_id not in await offered(conn, [item]):
        return _answer("Напомнить по этой договорённости нельзя: она закрыта, не принята или человек вне "
                       "круга напоминаний.")
    name = commitments._short((item["debtor"] or {}).get("name") or "Собеседник", 60)
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.remind'), $1::int)", commitment_id % 2**31)
    row = await conn.fetchrow(
        "SELECT * FROM reminders WHERE commitment_id = $1 AND created_at > $2 ORDER BY id DESC LIMIT 1",
        commitment_id, _now() - KEEP)
    if row is None:
        used = await conn.fetchval("SELECT count(*) FROM reminders WHERE created_at > $1", _now() - KEEP)
        if used >= DAILY_CAP:
            return _answer(f"Напоминаний — не больше {DAILY_CAP} в сутки. Попробуйте позже.")
        route, chat_id = await route_for(conn, item)
        kind = await conn.fetchval("SELECT type FROM chats WHERE id = $1", chat_id) if chat_id is not None else None
        text = reminder_text(item, today, in_group=kind in GROUP_TYPES)
        row = await conn.fetchrow(
            """INSERT INTO reminders (commitment_id, chat_id, route, nonce, text) VALUES ($1, $2, $3, $4, $5)
               RETURNING *""", commitment_id, chat_id, route, secrets.token_urlsafe(12), text)
    return _answer("", reply=await card(conn, row, name))


async def on_put(conn: asyncpg.Connection, reminder_id: int, nonce: str) -> dict[str, Any]:
    """«В чат» под напоминанием: проверки и отметка «кладу» — в транзакции нажатия, запрос к Telegram — после."""
    from ..outbox import policy
    from ..replies import put as put_rules
    from ..setup_page import audit
    row = await conn.fetchrow("SELECT * FROM reminders WHERE id = $1 FOR UPDATE", reminder_id)
    if row is None or not hmac.compare_digest(row["nonce"], nonce) or row["route"] != "put":
        return _answer("Это напоминание уже недоступно — оно хранится сутки.")
    if not await commitments.is_visible(conn, row["commitment_id"]):
        return _answer("Договорённость больше не видна (чат исключён или сообщение удалено).")
    if row["placed_at"] is not None:
        return _answer(put_rules.WORDS["already"])
    try:
        put_rules.require_press()
    except put_rules.Refused:
        return _answer("Кнопка недоступна.")
    tgt = await policy.target(conn, row["chat_id"])
    code = await put_rules.check(conn, tgt, row["text"])
    if code is not None:
        return _answer(put_rules.WORDS[code][:190])
    if row["put_started_at"] is not None and \
            row["put_started_at"] > _now() - timedelta(seconds=put_rules.BUSY_SECONDS):
        return _answer("Уже кладу текст в поле ввода — проверьте поле через минуту.")
    await conn.execute("UPDATE reminders SET put_started_at = now() WHERE id = $1", reminder_id)
    text = row["text"]
    state = current_state()
    out = _answer("Кладу текст в поле ввода…")

    async def run() -> None:
        assert tgt is not None
        result = await put_rules.put(state, tgt, text, reply_to_tg_id=None)
        pool = getattr(state, "pool", None)
        if pool is not None:
            async with pool.acquire() as conn2, conn2.transaction():
                if result.placed:
                    await conn2.execute("UPDATE reminders SET placed_at = now() WHERE id = $1", reminder_id)
                else:
                    # «неизвестно» — запись могла уйти: отметка «кладу» остаётся на минуту
                    await conn2.execute("UPDATE reminders SET put_started_at = CASE WHEN $2 THEN now() END "
                                        "WHERE id = $1", reminder_id, result.code == "unknown")
                await audit.write_conn(conn2, "remind.put", detail=f"напоминание № {reminder_id}; итог: {result.code}")
        out["answer"] = result.text[:190]
        if not result.placed:
            out["reply"] = {"text": result.text}

    out["after_commit"] = run
    return out


# --- уборка -------------------------------------------------------------------------------------------------

async def prune(conn: asyncpg.Connection, *, now: datetime | None = None) -> int:
    done = await conn.execute("DELETE FROM reminders WHERE created_at < $1", (now or _now()) - KEEP)
    return int(done.split()[-1])


async def drop_chat(conn: asyncpg.Connection, chat_id: int) -> int:
    """Чат исключён: напоминания в него и по договорённостям из него удаляются."""
    done = await conn.execute(
        """DELETE FROM reminders r USING commitments c
           WHERE c.id = r.commitment_id AND (r.chat_id = $1 OR c.chat_id = $1)""", chat_id)
    return int(done.split()[-1])


@bridge.on_owner_change
async def _owner_changed(conn: asyncpg.Connection, new_user_id: int) -> None:
    """Новый владелец не видит напоминаний прежнего."""
    await conn.execute("DELETE FROM reminders")
