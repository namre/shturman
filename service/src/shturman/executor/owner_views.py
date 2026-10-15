"""Сводки для владельца в боте согласований — словами, без JSON и служебных номеров (Р-86).

Только чтение: здесь ничего не меняется. Тексты переписки сюда не попадают — только названия
чатов и имена людей там, где без них сводка бессмысленна (владелец видит их в своём же боте),
состояния, числа и время. Точные значения как есть (JSON) — по команде `/config json`, она
перечислена в `/advanced`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from .. import words
from ..sanitize import clean_line

ACCOUNT_STATE = {
    "running": "на связи",
    "starting": "подключается",
    "paused": "на паузе",
    "disconnected": "нет связи",
    "error": "нет связи",
    "failed": "нет связи",
    "locked": "нет связи",
    "unauthorized": "нужно войти заново",
    "no_session": "нужно войти заново",
}
LIST_LIMIT = 25           # строк в одной сводке; остальное — числом


def zone_of(config: Any) -> ZoneInfo:
    try:
        return ZoneInfo(getattr(config, "timezone", None) or "UTC")
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def moment(value: datetime | None, config: Any) -> str:
    """«сегодня в 19:45», «вчера в 08:10», «14.10.2026 в 19:45» — по часам владельца."""
    if value is None:
        return "ещё не было"
    zone = zone_of(config)
    local = value.astimezone(zone)
    today = datetime.now(zone).date()
    time = local.strftime("%H:%M")
    if local.date() == today:
        return f"сегодня в {time}"
    if local.date() == today - timedelta(days=1):
        return f"вчера в {time}"
    return f"{local:%d.%m.%Y} в {time}"


def _name(value: Any, fallback: str, limit: int = 60) -> str:
    return clean_line(value if isinstance(value, str) else None, limit) or fallback


def _more(total: int, shown: int) -> list[str]:
    return [f"…и ещё {total - shown}."] if total > shown else []


async def work_mode(conn: asyncpg.Connection) -> str | None:
    """Способ работы, выбранный на странице настройки: own | staff | None."""
    value = await conn.fetchval("SELECT value->>'scenario' FROM setup_state WHERE key = 'scenario'")
    return value if value in words.MODES else None


# --- «Сейчас» (/status) ---------------------------------------------------------------------------

async def _accounts(conn: asyncpg.Connection, state: Any) -> list[str]:
    from ..tg.manager import TgManager

    lines: list[str] = []
    manager = state.extras.get("tg")
    if isinstance(manager, TgManager) and manager.configured:
        for account in await manager.list_accounts():
            who = "ваш аккаунт" if account["role"] == "owner" else words.HELPER_ACCOUNT
            label = clean_line(account.get("label"), 40)
            lines.append(f"• {who}{f' «{label}»' if label else ''} — "
                         f"{ACCOUNT_STATE.get(account['status'], 'нет связи')}")
    business = await conn.fetchrow(
        """SELECT count(*) FILTER (WHERE enabled) AS on, count(*) AS total FROM business_connections""")
    if business["total"]:
        lines.append("• бизнес-бот в личных чатах — " + ("подключён" if business["on"] else "отключён"))
    return lines


async def _model(conn: asyncpg.Connection) -> str:
    from .. import bridge

    row = await conn.fetchrow(
        """SELECT count(*) FILTER (WHERE status = 'done') AS done,
                  count(*) FILTER (WHERE status = 'failed') AS failed
           FROM jobs WHERE kind = ANY($1::text[]) AND finished_at > now() - interval '1 day'""",
        list(bridge.LLM_KINDS))
    if row["failed"]:
        return (f"Модель: есть сбои — {words.counted(row['failed'], 'неудача', 'неудачи', 'неудач')} "
                f"за сутки из {row['done'] + row['failed']}. Повторить — {words.IN_SETUP_PAGE}.")
    if row["done"]:
        return "Модель: работает, сбоев за сутки нет."
    return "Модель: за сутки обращений не было."


PENDING_WORDS = {
    "draft": ("черновик", "черновика", "черновиков"),
    "action": ("подтверждение", "подтверждения", "подтверждений"),
    "reply_question": ("вопрос ассистента", "вопроса ассистента", "вопросов ассистента"),
    "reply_source": ("просьба прочитать", "просьбы прочитать", "просьб прочитать"),
    "hidden": ("скрытое сообщение", "скрытых сообщения", "скрытых сообщений"),
    "commitment": ("договорённость", "договорённости", "договорённостей"),
    "owner_fact": ("факт о вас", "факта о вас", "фактов о вас"),
    "page": ("страница о человеке", "страницы о людях", "страниц о людях"),
    "project": ("проект", "проекта", "проектов"),
}


async def pending(conn: asyncpg.Connection, state: Any) -> tuple[int, dict[str, int], int]:
    """Сколько на согласовании по видам — тем же списком, что экран «Согласования» в приложении.
    Возвращает (всего, по видам, ещё придут сводкой)."""
    from ..setup_page import approvals

    collected = await approvals.collect(conn, state.config)
    counts: dict[str, int] = {}
    for item in collected["items"]:
        kind = item["kind"]
        if kind == "reply":
            kind = "reply_source" if item.get("kind_title") == words.REPLY_SOURCE else "reply_question"
        counts[kind] = counts.get(kind, 0) + 1
    later = sum(int(n or 0) for n in collected["later"].values())
    return sum(counts.values()), counts, later


def pending_line(total: int, counts: dict[str, int], later: int) -> str:
    if not total:
        line = "На согласовании: ничего."
    else:
        parts = [words.counted(counts[kind], *PENDING_WORDS[kind]) for kind in PENDING_WORDS if counts.get(kind)]
        line = f"На согласовании: {total} — " + ", ".join(parts) + "."
    if later:
        line += f" Ещё {later} придут со следующей сводкой."
    return line


async def status_text(conn: asyncpg.Connection, state: Any) -> str:
    from ..rehearsal import settings as rehearsal_settings
    from ..api_core import overview

    from .. import halt
    config = state.config
    mode = await work_mode(conn)
    counts = await overview(state)
    lines = ["Сейчас в Штурмане", ""]
    stopped = await halt.current(conn)
    if stopped["stopped"]:
        # Остановлено (Р-87) — на самом видном месте: первой строкой.
        lines[2:2] = ["⏸ Остановлено с " + halt.since_words(stopped, config)
                      + ": ассистент ничего не делает сам — ни ответов, ни вопросов, ни отправки. "
                        "Продолжить — /resume.", ""]
        if stopped["paused_accounts"]:
            lines.insert(3, f"Чтение переписки тоже на паузе (аккаунтов: {len(stopped['paused_accounts'])}).")
    lines.append("Способ работы: " + (words.MODES[mode] if mode else
                                      f"ещё не выбран — {words.IN_SETUP_PAGE}"))
    accounts = await _accounts(conn, state)
    lines.append("Аккаунты:" if accounts else "Аккаунты: ни один не подключён.")
    lines.extend(accounts)
    seen = counts.get("last_message_seen_at")
    lines.append("Последнее сообщение в архиве: "
                 + moment(datetime.fromisoformat(seen) if seen else None, config) + ".")
    lines.append(await _model(conn))
    guard = "включена" if counts.get("guard_enabled") else "выключена"
    if counts.get("guard_enabled") and counts.get("guard_problem"):
        guard = "включена, но проверка сейчас не отвечает"
    lines.append(f"Защита от внедрённых инструкций: {guard}.")
    voice = "включена" if counts.get("voice_enabled") else "выключена"
    if counts.get("voice_enabled") and counts.get("voice_problem"):
        voice = "включена, но сейчас не работает"
    lines.append(f"Расшифровка голосовых: {voice}.")
    lines.append("")
    total, by_kind, later = await pending(conn, state)
    lines.append(pending_line(total, by_kind, later))
    lines.append("Отправка: " + ("включена на сервере." if config.sending else
                                 "выключена — ничего не уходит от вашего имени."))
    prepare = "включены" if getattr(config, "prepare_only", False) else "выключены"
    rehearsal = await rehearsal_settings.load(conn)
    rehearsal_line = (f"{words.REHEARSAL}: {'включена' if rehearsal['enabled'] else 'выключена'}, "
                      f"людей в списке: {len(rehearsal['people'])}. Ничего не отправляет.")
    drafts_line = f"{words.DRAFTS_MODE}: {prepare}. Без отправки: черновик только показывается вам."
    if mode == "own":
        lines += [rehearsal_line, words.DRAFTS_ELSEWHERE]
    elif mode == "staff":
        lines += [drafts_line, words.REHEARSAL_ELSEWHERE]
    else:
        lines += [drafts_line + f" (способ «{words.MODE_STAFF}»)",
                  rehearsal_line + f" (способ «{words.MODE_OWN}»)"]
    return "\n".join(lines)


# --- «Согласования» -------------------------------------------------------------------------------

async def approvals_text(conn: asyncpg.Connection, state: Any, *, app: bool) -> str:
    total, counts, later = await pending(conn, state)
    lines = [words.APPROVALS, "", pending_line(total, counts, later)]
    if total:
        lines.append("Карточки с кнопками — выше в этом чате."
                     + (" Все сразу — в приложении «Штурман»: кнопка ниже или «Штурман» внизу чата."
                        if app else ""))
    else:
        lines.append("Когда что-то понадобится, карточка придёт сюда.")
    return "\n".join(lines)


# --- «Настройки» ----------------------------------------------------------------------------------

async def config_text(conn: asyncpg.Connection, state: Any) -> str:
    from ..outbox import autoreply, policy, scopes

    config = state.config
    rules = await policy.load(conn, config)
    lines = ["Настройки", ""]
    lines.append("Отправка: " + ("включена на сервере." if config.sending else
                                 "выключена на сервере. Включает только тот, кто обслуживает сервер."))
    lines.append(f"{words.DRAFTS_MODE}: " + ("включены — готовятся без отправки."
                                             if getattr(config, "prepare_only", False) else "выключены."))
    lines.append(f"Предел: не больше {rules['daily_cap']} сообщений в сутки с аккаунта, "
                 f"в один чат — не больше {rules['chat_window_max']} за {rules['chat_window_seconds']} с.")
    hours = int(rules["draft_ttl_seconds"]) // 3600
    lines.append(f"Черновик ждёт вашего решения: {words.counted(hours, 'час', 'часа', 'часов')}."
                 if hours else f"Черновик ждёт вашего решения: {int(rules['draft_ttl_seconds']) // 60} мин.")
    lines.append("Черновики по умолчанию: "
                 + {"allow": "можно готовить во всех чатах", "deny": "нельзя, кроме разрешённых чатов"}
                 .get(rules.get("drafting_default"), "как в общих правилах") + ".")
    enabled = await conn.fetch(
        """SELECT a.label, a.role FROM outbox_accounts o JOIN accounts a ON a.id = o.account_id
           WHERE o.autoreply_enabled ORDER BY a.id""")
    if enabled:
        lines.append("Автоответ доверенным: включён — "
                     + ", ".join(f"«{_name(r['label'], 'без имени', 40)}»" for r in enabled) + ".")
    else:
        lines.append("Автоответ доверенным: выключен.")
    trusted = await conn.fetchval("SELECT count(*) FROM outbox_trusted")
    lines.append("Доверенные люди: " + (words.counted(trusted, "человек", "человека", "человек")
                                        if trusted else "нет") + ".")
    special = await conn.fetchrow(
        """SELECT count(*) FILTER (WHERE drafting = 'allow') AS allow,
                  count(*) FILTER (WHERE drafting = 'deny') AS deny FROM outbox_chats""")
    if special["allow"] or special["deny"]:
        lines.append(f"Чаты с особым правилом: черновики разрешены — {special['allow']}, запрещены — "
                     f"{special['deny']}.")
    groups = await conn.fetchval("SELECT count(*) FROM outbox_reply_scopes WHERE enabled")
    lines.append("Группы и темы, где ассистент может отвечать: " + (str(groups) if groups else "нет") + ".")
    aliases = (await scopes.addressing(conn))["aliases"]
    if aliases:
        lines.append("Обращения к ассистенту в группах: " + ", ".join(f"«{a}»" for a in aliases[:10]) + ".")
    reply = await autoreply.load(conn)
    lines.append(f"Автоответ: не больше {reply['daily_cap']} в сутки, пауза {reply['pause_seconds']:g} с.")
    lines += ["", "Изменить — командами из /advanced. Все значения как есть — /config json."]
    return "\n".join(lines)


async def config_json(conn: asyncpg.Connection, state: Any) -> str:
    from ..outbox import autoreply
    from ..outbox.service import _policy_view

    view = await _policy_view(conn, state.config)
    view["autoreply"] = await autoreply.load(conn)
    return "Настройки как есть (для точной правки командами из /advanced):\n" + _dump(view)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


# --- списки ---------------------------------------------------------------------------------------

KIND_WORDS = (("personal", "личный чат"), ("bot", "бот"), ("saved", "избранное"),
              ("supergroup", "группа"), ("group", "группа"), ("channel", "канал"))


def chat_kind(chat_type: str | None) -> str:
    return next((word for mark, word in KIND_WORDS if mark in (chat_type or "")), "чат")


async def chats_text(conn: asyncpg.Connection) -> str:
    rows = await conn.fetch(
        """SELECT c.id, c.type, c.title, c.excluded, a.role, p.tg_id FROM chats c
           JOIN accounts a ON a.id = c.account_id JOIN peers p ON p.id = c.peer_id
           ORDER BY c.excluded, c.id LIMIT $1""", LIST_LIMIT)
    total = await conn.fetchval("SELECT count(*) FROM chats")
    if not total:
        return "Чаты в архиве: пока ни одного. Выбрать, что читать, — " + words.IN_SETUP_PAGE + "."
    lines = [f"Чаты в архиве: {total}", ""]
    for r in rows:
        who = "" if r["role"] == "owner" else f" · {words.HELPER_ACCOUNT}"
        lines.append(f"№ {r['id']} — {_name(r['title'], 'без названия')} ({chat_kind(r['type'])}{who})"
                     + (" · не сохраняется" if r["excluded"] else ""))
    lines += _more(total, len(rows))
    lines += ["", "Номер нужен командам из /advanced. Выбрать чаты — в приложении «Штурман» или "
              + words.IN_SETUP_PAGE + "."]
    return "\n".join(lines)


async def drafts_text(conn: asyncpg.Connection, state: Any) -> str:
    rows = await conn.fetch(
        """SELECT d.id, d.prepare_only, d.channel, d.expires_at, c.title, c.type
           FROM outbox_drafts d JOIN chats c ON c.id = d.chat_id
           WHERE d.status = 'pending' ORDER BY d.id DESC LIMIT $1""", LIST_LIMIT)
    total = await conn.fetchval("SELECT count(*) FROM outbox_drafts WHERE status = 'pending'")
    mode = (f"{words.DRAFTS_MODE} без отправки: "
            + ("включены — /prepare off, чтобы выключить." if getattr(state.config, "prepare_only", False)
               else "выключены — /prepare on, чтобы включить.")
            + f" Это для способа «{words.MODE_STAFF}».")
    if not total:
        return "Черновики: сейчас ни один не ждёт решения.\n" + mode
    lines = [f"Черновики, которые ждут решения: {total}", mode, ""]
    for r in rows:
        voice = words.VOICE_OWNER if r["channel"] == "business" else words.VOICE_HELPER
        lines.append(f"№ {r['id']} — {_name(r['title'], 'без названия')} ({chat_kind(r['type'])}) · {voice}"
                     + (" · только просмотр" if r["prepare_only"] else "")
                     + f" · ждёт до {moment(r['expires_at'], state.config)}")
    lines += _more(total, len(rows))
    lines += ["", "Решать — кнопками под карточкой черновика. Снять черновик — /cancel номер."]
    return "\n".join(lines)


async def groups_text(conn: asyncpg.Connection) -> str:
    from ..outbox import scopes

    found = [s for s in await scopes.list_scopes(conn) if s.get("enabled", True)]
    if not found:
        return ("Группы и темы для ответов: сейчас ничего не разрешено — ассистент не отвечает ни в одной "
                "группе. Разрешить группу — /groupallow (см. /advanced).")
    lines = [f"Группы и темы для ответов: {len(found)}", ""]
    for s in found[:LIST_LIMIT]:
        topic = f", тема № {s['topic_tg_id']}" if s.get("topic_tg_id") else ""
        lines.append(f"№ {s['id']} — {_name(s.get('title'), 'чат без названия')}{topic} · "
                     f"аккаунт «{_name(s.get('account_label'), 'без имени', 40)}»")
    lines += _more(len(found), LIST_LIMIT)
    lines += ["", "Убрать разрешение — /groupdeny номер."]
    return "\n".join(lines)


async def sources_text(conn: asyncpg.Connection, state: Any) -> str:
    from ..sources import registry

    connectors = registry.public_sources(state.extras.get("source_registry", {}))
    rows = await conn.fetch(
        """SELECT g.id, g.kind, g.mode, g.request, g.expires_at, c.title FROM source_grants g
           JOIN chats c ON c.id = g.target_chat_id
           WHERE g.revoked_at IS NULL AND g.expires_at > now() ORDER BY g.id DESC LIMIT $1""", LIST_LIMIT)
    lines = ["Источники и разрешения", ""]
    lines.append("Подключённые внешние источники: " + (", ".join(_name(c["name"], c["source_id"]) for c in connectors)
                                                       if connectors else "нет") + ".")
    if not rows:
        lines.append("Разрешения прочитать для ответа: сейчас ничего не разрешено.")
        return "\n".join(lines)
    lines.append(f"Действующие разрешения прочитать для ответа: {len(rows)}")
    what = {"chat": "переписку", "memory": "память о людях", "external": "внешний источник"}
    for r in rows:
        request = json.loads(r["request"]) if isinstance(r["request"], str) else (r["request"] or {})
        query = request.get("query")
        lines.append(f"№ {r['id']} — {what.get(r['kind'], 'источник')}"
                     + (f" по запросу «{clean_line(query, 60)}»" if isinstance(query, str) and query else "")
                     + f" для ответа в «{_name(r['title'], 'чат без названия')}»"
                     + (" (можно пересказать собеседнику)" if r["mode"] == "disclose" else "")
                     + f" · до {moment(r['expires_at'], state.config)}")
    lines += ["", "Отозвать — /sourcerevoke номер."]
    return "\n".join(lines)


async def mcp_text(conn: asyncpg.Connection, state: Any) -> str:
    from ..remote_mcp import core

    grants = [g for g in await core.list_grants(conn) if g.get("revoked_at") is None]
    if not grants:
        return "Внешние программы с доступом к архиву: сейчас ни одной."
    lines = [f"Внешние программы с доступом к архиву: {len(grants)}", ""]
    for g in grants[:LIST_LIMIT]:
        lines.append(f"№ {g['id']} — «{_name(g.get('name'), 'без имени')}» · с {moment(g.get('created_at'), state.config)}"
                     + (f" · до {moment(g['expires_at'], state.config)}" if g.get("expires_at") else ""))
    lines += ["", "Отозвать доступ — /mcprevoke номер."]
    return "\n".join(lines)


TASK_STATE = {
    "queued": "готовится", "generating": "готовится", "drafting": "готовится черновик",
    "waiting_source": "ждёт разрешения прочитать", "waiting_owner": "ассистент ждёт вашего уточнения",
    "draft_ready": "черновик готов", "completed": "отвечено", "declined": "вы отказали",
    "cancelled": "отменено", "expired": "срок вышел", "failed": "не получилось",
}


async def reply_tasks_text(conn: asyncpg.Connection) -> str:
    rows = await conn.fetch(
        """SELECT t.id, t.status, c.title FROM reply_tasks t LEFT JOIN chats c ON c.id = t.chat_id
           ORDER BY t.id DESC LIMIT 20""")
    if not rows:
        return "Вопросы ассистента и ответы на сообщения: пока не было ни одного."
    waiting = [r for r in rows if r["status"] in ("waiting_source", "waiting_owner")]
    lines = [f"Вопросы ассистента и ответы на сообщения — последние {len(rows)}",
             "Ждут вас: " + (str(len(waiting)) if waiting else "ничего") + ".", ""]
    for r in rows:
        lines.append(f"№ {r['id']} — ответ в «{_name(r['title'], 'чат без названия')}» · "
                     f"{TASK_STATE.get(r['status'], r['status'])}")
    if any(r["status"] == "waiting_owner" for r in rows):
        lines += ["", "Ответить ассистенту: /replytask номер answer ваш ответ. Отменить: /replytask номер cancel."]
    return "\n".join(lines)
