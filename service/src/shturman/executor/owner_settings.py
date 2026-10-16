"""Настройки ответов и доверия в боте согласований кнопками, без номеров и JSON (Р-95, находка Б3).

Команды (`owner_chat.py` зовёт эти функции):
  /trusted                — доверенные люди: «Убрать …» у каждого и «Выбрать человека»
  /trust без номера       — выбрать человека из последних собеседников
  /groups                 — группы и темы для ответов: «Запретить …» и «Разрешить группу…»
  /groupallow без номера  — выбрать группу аккаунта помощника
  /drafting без номера    — выбрать чат, затем «можно / нельзя / как обычно»
  /addressing без JSON    — обращения к ассистенту: «Убрать «…»» и «Добавить обращение» (текстом в ответ)
  /autoreply без номера   — автоответ доверенным по аккаунтам: «Включить / Выключить»
  /rehearsal add без номера — выбрать человека для репетиции
  /sources, /mcpaccess    — «Отозвать» у каждого разрешения (вместо /sourcerevoke НОМЕР и /mcprevoke НОМЕР)
JSON и номера остаются запасным путём: `/advanced`.

Кнопки — `sh:hx:…` (свой модуль; данные прежних кнопок не менялись). Нажатие принимает только бот
(`bot.py`): от привязанного владельца, в его личном чате, в контексте владельца из Telegram. Нажатие владельца
в самом боте — его решение: применяется сразу и в сторону ослабления (как команда), тем же путём, что
страница и приложение (`history.change(where="bot")`), со строкой «Истории».

Имена людей и названия чатов — из архива (`clean_line`), коротко; номера — только в данных кнопок.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

import asyncpg

from .. import authority, bridge, confirm, history, store
from ..outbox import scopes
from ..sanitize import clean_line

MODULE = "hx"
PAGE = 8                  # кнопок выбора на одном экране
LABEL = 28                # знаков имени на кнопке

Buttons = list[list[tuple[str, Any]]]


def _b(label: str, rest: str) -> tuple[str, str]:
    return (label, bridge.callback_data(MODULE, rest))


def _short(value: Any, fallback: str) -> str:
    return clean_line(value if isinstance(value, str) else None, LABEL) or fallback


def _alias_mark(alias: str) -> str:
    return hashlib.sha256(alias.casefold().encode("utf-8")).hexdigest()[:6]


# --- экраны -------------------------------------------------------------------------------------------

async def trusted_screen(conn: asyncpg.Connection) -> dict[str, Any]:
    rows = await conn.fetch(
        """SELECT t.tg_user_id, t.note, COALESCE(
                  (SELECT c.title FROM chats c WHERE c.peer_id = p.id AND c.type = 'personal_chat' LIMIT 1), p.name) AS name
           FROM outbox_trusted t LEFT JOIN peers p ON p.class = 'user' AND p.tg_id = t.tg_user_id
           ORDER BY t.created_at, t.tg_user_id LIMIT 30""")
    buttons: Buttons = [[_b("Убрать: " + _short(r["name"], f"№ {r['tg_user_id']}"), f"tu:{r['tg_user_id']}")] for r in rows]
    buttons.append([_b("Выбрать человека", "tp:0")])
    if not rows:
        text = ("Доверенные люди: пока никого.\nДоверенному ассистент отвечает сам, без вашего согласования каждого "
                "ответа — только если вы включили автоответ (/autoreply) и отправка включена на сервере.")
    else:
        lines = [f"• {clean_line(r['name'], 60) or 'человек без имени в архиве'}"
                 + (f" — {clean_line(r['note'], 60)}" if r["note"] else "") for r in rows]
        text = (f"Доверенные люди: {len(rows)}\n" + "\n".join(lines) + "\n\nДоверенному ассистент отвечает сам, если "
                "автоответ включён. Убрать — кнопкой; добавить — «Выбрать человека».")
    return {"text": text, "buttons": buttons}


async def _people(conn: asyncpg.Connection, offset: int, *, exclude: list[int]) -> tuple[list[Any], bool]:
    rows = await conn.fetch(
        """SELECT p.tg_id, COALESCE(c.title, p.name) AS name, p.username,
                  (SELECT max(m.sent_at) FROM messages m WHERE m.chat_id = c.id) AS last_at
           FROM chats c JOIN peers p ON p.id = c.peer_id
           WHERE c.type = 'personal_chat' AND p.class = 'user' AND NOT c.excluded AND p.is_bot IS NOT TRUE
             AND p.tg_id NOT IN (SELECT tg_user_id FROM accounts WHERE tg_user_id IS NOT NULL)
             AND NOT (p.tg_id = ANY($3::bigint[]))
           ORDER BY last_at DESC NULLS LAST, p.tg_id OFFSET $1 LIMIT $2""", offset, PAGE + 1, exclude)
    rows = [r for r in rows if not store.is_blocked_peer("user", r["tg_id"], r["username"])]
    return rows[:PAGE], len(rows) > PAGE


async def people_screen(conn: asyncpg.Connection, offset: int, *, purpose: str) -> dict[str, Any]:
    """Последние собеседники личных чатов — кнопками. purpose: trust | rehearsal."""
    if purpose == "trust":
        exclude = [r["tg_user_id"] for r in await conn.fetch("SELECT tg_user_id FROM outbox_trusted")]
        pick, more, title = "ta", "tp", "Кого сделать доверенным? Последние собеседники:"
    else:
        from ..rehearsal import settings as rs
        exclude = list((await rs.load(conn))["people"])
        pick, more, title = "ra", "rp", "Кого добавить в репетицию? Последние собеседники:"
    rows, has_more = await _people(conn, offset, exclude=exclude)
    if not rows:
        return {"text": "Больше собеседников в личных чатах нет." if offset else
                "Собеседников в личных чатах пока нет: выберите, какие чаты читать, в «Настройках Штурмана».",
                "buttons": []}
    buttons: Buttons = [[_b(_short(r["name"], "без имени") + (f" @{r['username']}" if r["username"] and
                                                                 len(r["username"]) < 20 else ""),
                            f"{pick}:{r['tg_id']}")] for r in rows]
    if has_more:
        buttons.append([_b("Ещё…", f"{more}:{offset + PAGE}")])
    return {"text": title, "buttons": buttons}


async def groups_screen(conn: asyncpg.Connection) -> dict[str, Any]:
    found = [s for s in await scopes.list_scopes(conn) if s.get("enabled", True)]
    buttons: Buttons = [[_b("Запретить: " + _short(s.get("title"), "группа")
                            + (f", тема {s['topic_tg_id']}" if s.get("topic_tg_id") else ""),
                            f"gd:{s['chat_id']}:{s.get('topic_tg_id') or 0}")] for s in found[:20] if s.get("chat_id")]
    buttons.append([_b("Разрешить группу…", "gp:0")])
    if not found:
        text = ("Группы и темы для ответов: сейчас ничего не разрешено — ассистент не отвечает ни в одной группе.\n"
                "Разрешить — кнопкой ниже: группы аккаунта помощника.")
    else:
        text = (f"Группы и темы, где ассистент может отвечать: {len(found)}\n"
                + "\n".join(f"• {clean_line(s.get('title'), 60) or 'группа без названия'}"
                            + (f", тема № {s['topic_tg_id']}" if s.get("topic_tg_id") else "") for s in found[:20]))
    return {"text": text, "buttons": buttons}


async def group_picker(conn: asyncpg.Connection, offset: int) -> dict[str, Any]:
    rows = await conn.fetch(
        """SELECT c.id, c.title FROM chats c JOIN accounts a ON a.id = c.account_id JOIN peers p ON p.id = c.peer_id
           WHERE a.role = 'assistant' AND NOT c.excluded AND c.type = ANY($3::text[])
             AND NOT EXISTS (SELECT 1 FROM outbox_reply_scopes s WHERE s.account_id = c.account_id
                             AND s.peer_id = c.peer_id AND s.enabled AND s.topic_tg_id = 0)
           ORDER BY (SELECT max(m.sent_at) FROM messages m WHERE m.chat_id = c.id) DESC NULLS LAST, c.id
           OFFSET $1 LIMIT $2""", offset, PAGE + 1, sorted(scopes.GROUP_TYPES))
    if not rows:
        return {"text": "Групп аккаунта помощника, где ещё нельзя отвечать, нет. Группы видны, когда подключён аккаунт "
                        "помощника (способ «Через бота и помощника») и выбраны его группы.", "buttons": []}
    buttons: Buttons = [[_b(_short(r["title"], "без названия"), f"ga:{r['id']}")] for r in rows[:PAGE]]
    if len(rows) > PAGE:
        buttons.append([_b("Ещё…", f"gp:{offset + PAGE}")])
    return {"text": "В какой группе ассистенту можно отвечать, когда к нему обращаются?", "buttons": buttons}


async def chat_picker(conn: asyncpg.Connection, offset: int) -> dict[str, Any]:
    rows = await conn.fetch(
        """SELECT c.id, c.title, o.drafting FROM chats c JOIN peers p ON p.id = c.peer_id
           LEFT JOIN outbox_chats o ON o.chat_id = c.id
           WHERE NOT c.excluded AND p.is_bot IS NOT TRUE
           ORDER BY (SELECT max(m.sent_at) FROM messages m WHERE m.chat_id = c.id) DESC NULLS LAST, c.id
           OFFSET $1 LIMIT $2""", offset, PAGE + 1)
    if not rows:
        return {"text": "Чатов в архиве пока нет.", "buttons": []}
    mark = {"allow": " · можно", "deny": " · нельзя"}
    buttons: Buttons = [[_b(_short(r["title"], "без названия") + mark.get(r["drafting"] or "", ""), f"dc:{r['id']}")]
                        for r in rows[:PAGE]]
    if len(rows) > PAGE:
        buttons.append([_b("Ещё…", f"dp:{offset + PAGE}")])
    return {"text": "Для какого чата задать правило черновиков?", "buttons": buttons}


async def drafting_choice(conn: asyncpg.Connection, chat_id: int) -> dict[str, Any]:
    title = await history.chat(conn, chat_id)
    current = await conn.fetchval("SELECT drafting FROM outbox_chats WHERE chat_id = $1", chat_id) or "default"
    said = {"allow": "можно", "deny": "нельзя", "default": "как обычно (по общим правилам)"}
    return {"text": f"Черновики ответов в чате «{title}»: сейчас — {said[current]}.\nОтправка в любом случае — только "
                    "по вашему нажатию.",
            "buttons": [[_b("Можно", f"dv:{chat_id}:a"), _b("Нельзя", f"dv:{chat_id}:d"),
                         _b("Как обычно", f"dv:{chat_id}:0")]]}


async def addressing_screen(conn: asyncpg.Connection) -> dict[str, Any]:
    aliases = (await scopes.addressing(conn))["aliases"]
    buttons: Buttons = [[_b("Убрать «" + clean_line(a, 24) + "»", f"ar:{_alias_mark(a)}")] for a in aliases]
    if len(aliases) < 16:
        buttons.append([_b("Добавить обращение", "aa:0")])
    text = ("Обращения к ассистенту в группах: " + (", ".join(f"«{a}»" for a in aliases) if aliases else "нет")
            + ".\nЕсли реплика в разрешённой группе начинается с такого обращения («Штурман, …»), ассистент считает, "
            "что обращаются к нему. Упоминание @имени и ответ на его сообщение работают и без этого.")
    return {"text": text, "buttons": buttons}


async def autoreply_screen(conn: asyncpg.Connection, state: Any) -> dict[str, Any]:
    rows = await conn.fetch(
        """SELECT a.id, a.label, COALESCE(o.autoreply_enabled, false) AS enabled FROM accounts a
           LEFT JOIN outbox_accounts o ON o.account_id = a.id ORDER BY a.id""")
    config = getattr(state, "config", None)
    if not rows:
        return {"text": "Аккаунтов пока нет — автоответ включать не для чего.", "buttons": []}
    possible = bool(getattr(config, "sending", False) or getattr(config, "prepare_only", False))
    lines, buttons = [], []
    for r in rows:
        name = _short(r["label"], f"аккаунт {r['id']}")
        lines.append(f"• «{name}»: " + ("включён" if r["enabled"] else "выключен"))
        if r["enabled"]:
            buttons.append([_b(f"Выключить: «{name}»", f"au:{r['id']}:0")])
        elif possible:
            buttons.append([_b(f"Включить: «{name}»", f"au:{r['id']}:1")])
    count = await conn.fetchval("SELECT count(*) FROM outbox_trusted")
    text = ("Автоответ доверенным:\n" + "\n".join(lines) + f"\nДоверенных: {count} (/trusted).")
    if not possible:
        from ..outbox import policy
        text += "\n" + policy.REASONS["sending_disabled"]
    return {"text": text, "buttons": buttons}


async def sources_buttons(conn: asyncpg.Connection) -> Buttons:
    rows = await conn.fetch(
        """SELECT g.id, c.title FROM source_grants g JOIN chats c ON c.id = g.target_chat_id
           WHERE g.revoked_at IS NULL AND g.expires_at > now() ORDER BY g.id DESC LIMIT 20""")
    return [[_b(f"Отозвать № {r['id']}: «{_short(r['title'], 'чат')}»", f"sr:{r['id']}")] for r in rows]


async def mcp_buttons(conn: asyncpg.Connection) -> Buttons:
    rows = await conn.fetch(
        """SELECT g.id, c.name FROM remote_mcp_grants g JOIN remote_mcp_clients c ON c.id = g.client_id
           WHERE g.revoked_at IS NULL AND g.expires_at > now() ORDER BY g.id DESC LIMIT 20""")
    return [[_b(f"Отозвать: «{_short(r['name'], 'программа')}»", f"mr:{r['id']}")] for r in rows]


# --- нажатия ------------------------------------------------------------------------------------------

def _digits(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.isdigit() and 0 < len(raw) <= 18 and int(raw) > 0 else None


async def _change(conn: asyncpg.Connection, kind: str, payload: dict[str, Any]) -> None:
    await history.change(conn, kind, payload, where="bot", summary="—")


async def on_press(conn: asyncpg.Connection, state: Any, rest: str, user_id: int) -> dict[str, Any]:
    """Нажатие кнопки `sh:hx:…`. Ответ: answer — подсказка, edit — новый текст и кнопки под тем же сообщением,
    prompt — просьба написать текст следующим сообщением (ForceReply)."""
    principal = authority.get_owner_principal()
    if principal is None or principal.source != "telegram" or principal.user_id != user_id \
            or principal.action == "telegram.miniapp":
        return {"answer": "Кнопка недоступна."}
    from ..outbox import service as outbox
    code, _, arg = rest.partition(":")
    try:
        if code in ("tp", "rp", "gp", "dp") and arg.isdigit():
            offset = min(int(arg), 10_000)
            screen = (await people_screen(conn, offset, purpose="trust" if code == "tp" else "rehearsal")
                      if code in ("tp", "rp") else
                      await group_picker(conn, offset) if code == "gp" else await chat_picker(conn, offset))
            return {"answer": "", "edit": screen}
        if code == "ta" and (tg := _digits(arg)):
            problem, _ = await outbox._trusted_problem(conn, tg)
            if problem:
                return {"answer": problem[:190]}
            await _change(conn, outbox.TRUSTED_ADD, {"tg_user_id": tg, "note": None})
            who = await history.person(conn, tg)
            return {"answer": "Добавлен в доверенные.",
                    "edit": {**await trusted_screen(conn),
                             "text": f"{who} — в доверенных. Вернуть — «Убрать» ниже или в «Истории».\n\n"
                                     + (await trusted_screen(conn))["text"]}}
        if code == "tu" and (tg := _digits(arg)):
            await _change(conn, outbox.TRUSTED_REMOVE, {"tg_user_id": tg})
            return {"answer": "Убран из доверенных.", "edit": await trusted_screen(conn)}
        if code == "ra" and (tg := _digits(arg)):
            from ..rehearsal import service as rehearsal_service
            from ..rehearsal import settings as rs
            people = sorted(set((await rs.load(conn))["people"]) | {tg})
            await _change(conn, rehearsal_service.SETTINGS, {"changes": rs.validate({"people": people})})
            who = await history.person(conn, tg)
            return {"answer": "Добавлен в репетицию.",
                    "edit": {"text": f"{who} — в списке репетиции. Убрать — /rehearsal remove {tg} или в «Настройках "
                                     "Штурмана».", "buttons": []}}
        if code == "ga" and (chat_id := _digits(arg)):
            await _change(conn, outbox.GROUP_SCOPE, {"chat_id": chat_id, "topic_tg_id": None, "enabled": True})
            return {"answer": "Разрешено.", "edit": await groups_screen(conn)}
        if code == "gd":
            chat_raw, _, topic_raw = arg.partition(":")
            chat_id = _digits(chat_raw)
            topic = _digits(topic_raw) if topic_raw not in ("", "0") else None
            if chat_id:
                await _change(conn, outbox.GROUP_SCOPE, {"chat_id": chat_id, "topic_tg_id": topic, "enabled": False})
                return {"answer": "Запрещено.", "edit": await groups_screen(conn)}
        if code == "dc" and (chat_id := _digits(arg)):
            return {"answer": "", "edit": await drafting_choice(conn, chat_id)}
        if code == "dv":
            chat_raw, _, value = arg.partition(":")
            chat_id = _digits(chat_raw)
            if chat_id and value in ("a", "d", "0"):
                choice = {"a": "allow", "d": "deny", "0": "default"}[value]
                await _change(conn, outbox.CHAT_DRAFTING, {"chat_id": chat_id, "value": choice})
                return {"answer": "Сохранено.", "edit": await drafting_choice(conn, chat_id)}
        if code == "ar":
            aliases = (await scopes.addressing(conn))["aliases"]
            kept = [a for a in aliases if _alias_mark(a) != arg]
            if len(kept) != len(aliases):
                await _change(conn, outbox.ADDRESSING, {"aliases": kept})
                return {"answer": "Убрано.", "edit": await addressing_screen(conn)}
            return {"answer": "Этого обращения уже нет.", "edit": await addressing_screen(conn)}
        if code == "aa":
            return {"answer": "", "prompt": {"kind": "alias", "id": 0, "nonce": secrets.token_urlsafe(6),
                                              "text": "Напишите обращение одним сообщением — например: Штурман",
                                              "placeholder": "Например: Штурман"}}
        if code == "au":
            acc_raw, _, value = arg.partition(":")
            account_id = _digits(acc_raw)
            if account_id and value in ("0", "1"):
                await _change(conn, outbox.AUTOREPLY, {"account_id": account_id, "enabled": value == "1"})
                return {"answer": "Включено." if value == "1" else "Выключено.",
                        "edit": await autoreply_screen(conn, state)}
        if code == "sr" and (grant := _digits(arg)):
            return await _revoke_source(conn, state, grant, user_id)
        if code == "mr" and (grant := _digits(arg)):
            from ..remote_mcp import core
            done = await core.revoke_grant(conn, grant)
            from . import owner_views
            text = await owner_views.mcp_text(conn, state)
            return {"answer": "Доступ отозван." if done else "Этого доступа уже нет.",
                    "edit": {"text": text, "buttons": await mcp_buttons(conn)}}
    except confirm.Refused as exc:
        return {"answer": exc.message[:190]}
    except ValueError as exc:
        return {"answer": str(exc)[:190]}
    return {"answer": "Кнопка устарела."}


async def _revoke_source(conn: asyncpg.Connection, state: Any, grant: int, user_id: int) -> dict[str, Any]:
    from ..sources import broker
    from ..sources.registry import SourceError
    from . import owner_views
    active = await conn.fetchval("SELECT revoked_at IS NULL FROM source_grants WHERE id = $1", grant)
    if active:
        try:
            await broker.revoke_grant(conn, grant, owner_id=user_id)
        except SourceError:
            return {"answer": "Отозвать не получилось: нужен привязанный владелец и свой бот согласований."}
    text = await owner_views.sources_text(conn, state)
    return {"answer": "Разрешение отозвано." if active else "Этого разрешения уже нет.",
            "edit": {"text": text, "buttons": await sources_buttons(conn)}}


async def handle_prompt(conn: asyncpg.Connection, prompt: dict[str, Any], text: str) -> dict[str, Any]:
    """Текст владельца в ответ на «Добавить обращение»."""
    if prompt.get("expired"):
        return {"text": "Время вышло — обращение не добавлено. /addressing — ещё раз."}
    from ..outbox import service as outbox
    alias = " ".join(text.split())
    aliases = (await scopes.addressing(conn))["aliases"]
    try:
        clean = scopes.validate_addressing({"aliases": [*aliases, alias]})["aliases"]
    except ValueError:
        return {"text": "Обращение — короткое имя без знаков препинания, например: Штурман. /addressing — ещё раз."}
    try:
        await _change(conn, outbox.ADDRESSING, {"aliases": clean})
    except confirm.Refused as exc:
        return {"text": exc.message}
    screen = await addressing_screen(conn)
    return {"text": f"Добавлено обращение «{alias}».\n\n" + screen["text"], "buttons": screen["buttons"]}

