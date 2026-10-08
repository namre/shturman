"""Deterministic owner control in the private service bot; never exposed as HTTP tools.

Authentication happens on genuine Telegram updates in Bot and is checked again here.
Ordinary decisions and policy changes stay in Telegram. Hermes keeps the owner's
conversational interface, while this control path never executes model instructions.
"""
from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

import asyncpg

from .. import authority, bridge, confirm
from ..outbox import autoreply, drafts, policy
from ..sources.registry import SourceError
from . import binding

MODULE = "oc"
MENU = [
    [("Статус", "status"), ("Настройки", "config")],
    [("Задания ответов", "replytasks"), ("Источники", "sources")],
    [("Группы и темы", "groups"), ("Помощь", "help")],
]
HELP = """Управление Штурманом в Telegram
/menu — кнопки; /status — готовность; /config — настройки
/replytasks (или /tasks) — задания и решения по источникам
/replytask НОМЕР answer ТЕКСТ — свой ответ; /replytask НОМЕР cancel — отменить
/sources — источники и разрешения; /sourcegrant JSON — разрешение для задания
/sourcepolicy JSON — точное разрешение для будущих заданий в этом чате (до 30 дней)
/sourcerevoke GRANT_ID — отозвать разрешение
/mcpaccess — подключённые внешние MCP-клиенты; /mcprevoke GRANT_ID — отозвать доступ
/groups — разрешённые группы и темы
/groupallow CHAT_ID [TOPIC_ID] — разрешить эту группу/тему
/groupdeny SCOPE_ID — убрать разрешение
/addressing JSON — точные обращения (например {"aliases":["Штурман"]})
/prepare on|off — готовить автоответы без отправки
/policy JSON — правила отправки (например {"daily_cap":20})
/autoconfig JSON — настройки автоответа
/autoreply ACCOUNT_ID on|off — правило автоответа доверенным
/trust USER_ID [ЗАМЕТКА] — доверенный; /untrust USER_ID — убрать
/chats — список чатов; /drafting CHAT_ID allow|deny|default — подготовка в этом чате
/drafts — ожидающие черновики; /cancel DRAFT_ID — отмена

Команды меняют только указанную область. Отправка требует включённого серверного
выключателя и разрешённого канала. В режиме подготовки сообщение остаётся черновиком.
Все решения по дополнительным источникам и раскрытию — в карточках здесь."""
_COMMAND = re.compile(r"^/([a-z]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", re.DOTALL)


def menu_buttons() -> list[list[tuple[str, str]]]:
    return [[(label, bridge.button(label, MODULE, command)["data"]) for label, command in row] for row in MENU]


def _json(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("После команды нужен JSON-объект настроек.") from None
    if not isinstance(value, dict):
        raise ValueError("Нужен JSON-объект настроек.")
    return value


def _int(raw: str) -> int:
    if not raw.isascii() or not raw.isdigit() or len(raw) > 18 or int(raw) <= 0:
        raise ValueError("Нужен положительный числовой идентификатор.")
    return int(raw)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


async def handle_message(conn: asyncpg.Connection, state: Any, text: str, *,
                         bot_id: int, user_id: int, chat_id: int) -> dict[str, Any] | None:
    """Only Bot calls this, using IDs obtained from Telegram, never from request JSON."""
    owner = await binding.bound_owner(conn, bot_id)
    if owner is None or owner["user_id"] != user_id or owner["chat_id"] != chat_id or chat_id != user_id:
        return None
    if not text.startswith("/"):
        return {"text": "Для управления используйте /menu. /help — доступные команды.",
                "buttons": menu_buttons()}
    with authority.owner_context(user_id, chat_id=chat_id, action="telegram.command"):
        try:
            result = await handle_command(conn, state, text, user_id)
        except (ValueError, confirm.Refused, drafts.Refused, SourceError) as exc:
            result = {"text": str(exc)}
    return result


async def handle_command(conn: asyncpg.Connection, state: Any, text: str, user_id: int) -> dict[str, Any]:
    if not authority.is_owner():
        raise PermissionError("owner Telegram context required")
    match = _COMMAND.fullmatch(text.strip())
    if match is None:
        return {"text": "Не удалось разобрать команду. /help — точный формат.", "buttons": menu_buttons()}
    command, argument = match.group(1), (match.group(2) or "").strip()
    if command in ("menu", "start", "help"):
        return {"text": HELP, "buttons": menu_buttons()}
    if command in ("replytasks", "replytask", "tasks"):
        from ..replies import owner
        return await owner.handle_command(conn, state, text.replace("/tasks", "/replytasks", 1), user_id)
    if command == "status":
        counts = await conn.fetchrow("""SELECT
            (SELECT count(*) FROM outbox_drafts WHERE status = 'pending') AS pending,
            (SELECT count(*) FROM reply_tasks WHERE status IN ('waiting_source','waiting_owner')) AS decisions""")
        return {"text": (f"Отправка: {'включена' if state.config.sending else 'выключена'}.\n"
                         f"Подготовка автоответов: {'включена' if getattr(state.config, 'prepare_only', False) else 'по правилам отправки'}.\n"
                         f"Черновиков: {counts['pending']}. Заданий с вашим решением: {counts['decisions']}.\n"
                         "Основной аккаунт остаётся только для чтения; группы — от аккаунта помощника."),
                "buttons": menu_buttons()}
    if command == "config":
        from ..outbox.service import _policy_view
        view = await _policy_view(conn, state.config)
        view["autoreply"] = await autoreply.load(conn)
        return {"text": "Настройки (точная область указана в каждой записи):\n" + _dump(view),
                "buttons": menu_buttons()}
    if command == "prepare":
        if argument not in ("on", "off"):
            raise ValueError("Формат: /prepare on|off")
        enabled = argument == "on"
        await policy.save_setting(conn, "outbox.prepare_only", {"enabled": enabled})
        state.config = dataclasses.replace(state.config, prepare_only=enabled)
        return {"text": "Подготовка без отправки " + ("включена. Ответы будут ждать в черновиках." if enabled else "выключена."),
                "buttons": menu_buttons()}
    if command == "policy":
        from ..outbox.service import POLICY
        await confirm.apply_owner(conn, POLICY, {"changes": policy.validate_update(_json(argument))})
    elif command == "autoconfig":
        from ..outbox.service import AUTOREPLY
        await confirm.apply_owner(conn, AUTOREPLY, {"changes": autoreply.validate_update(_json(argument))})
    elif command == "autoreply":
        from ..outbox.service import AUTOREPLY
        parts = argument.split()
        if len(parts) != 2 or parts[1] not in ("on", "off"):
            raise ValueError("Формат: /autoreply ACCOUNT_ID on|off")
        await confirm.apply_owner(conn, AUTOREPLY, {"account_id": _int(parts[0]), "enabled": parts[1] == "on"})
    elif command == "drafting":
        from ..outbox.service import CHAT_DRAFTING
        parts = argument.split()
        if len(parts) != 2 or parts[1] not in ("allow", "deny", "default"):
            raise ValueError("Формат: /drafting CHAT_ID allow|deny|default")
        await confirm.apply_owner(conn, CHAT_DRAFTING, {"chat_id": _int(parts[0]), "value": parts[1]})
    elif command == "trust":
        from ..outbox.service import TRUSTED_ADD
        raw, _, note = argument.partition(" ")
        await confirm.apply_owner(conn, TRUSTED_ADD, {"tg_user_id": _int(raw), "note": note})
    elif command == "untrust":
        await conn.execute("DELETE FROM outbox_trusted WHERE tg_user_id = $1", _int(argument))
    elif command == "chats":
        rows = await conn.fetch("""SELECT c.id AS chat_id, a.id AS account_id, a.role,
            c.type, c.title, p.tg_id, c.excluded FROM chats c JOIN accounts a ON a.id=c.account_id
            JOIN peers p ON p.id=c.peer_id ORDER BY c.id LIMIT 80""")
        return {"text": "Чаты: точные номера архива\n" + _dump([dict(r) for r in rows])}
    elif command in ("drafts", "cancel"):
        if command == "cancel":
            result = await drafts.cancel(conn, _int(argument))
            return {"text": "Черновик не найден." if result is None else _dump(result)}
        rows = await conn.fetch("SELECT id, chat_id, status, prepare_only, task_id FROM outbox_drafts WHERE status = 'pending' ORDER BY id DESC LIMIT 40")
        return {"text": "Ожидающие черновики:\n" + _dump([dict(r) for r in rows])}
    elif command in ("groups", "groupallow", "groupdeny", "addressing"):
        from ..outbox import scopes
        if command == "groups":
            return {"text": "Разрешённые группы и темы:\n" + _dump(await scopes.list_scopes(conn))}
        async with conn.transaction():
            if command == "groupdeny":
                await scopes.remove(conn, _int(argument))
            elif command == "addressing":
                await scopes.update_addressing(conn, scopes.validate_addressing(_json(argument)))
            else:
                parts = argument.split()
                if not 1 <= len(parts) <= 2:
                    raise ValueError("Формат: /groupallow CHAT_ID [TOPIC_ID]")
                await scopes.put(conn, _int(parts[0]), topic_tg_id=_int(parts[1]) if len(parts) == 2 else None)
    elif command in ("mcpaccess", "mcpclients", "mcprevoke"):
        from ..remote_mcp import core
        if command == "mcprevoke":
            revoked = await core.revoke_grant(conn, _int(argument))
            return {"text": "Доступ отозван." if revoked else "Разрешение не найдено."}
        return {"text": "Внешние MCP-клиенты и проверяемые адреса возврата:\n" + _dump(await core.list_grants(conn))}
    elif command in ("sources", "sourcegrant", "sourcerevoke", "sourcepolicy"):
        from ..sources import broker, registry
        if command == "sources":
            connectors = registry.public_sources(state.extras.get("source_registry", {}))
            grants = await conn.fetch("SELECT id, task_id, target_chat_id, target_topic_tg_id, mode, request, expires_at, revoked_at FROM source_grants ORDER BY id DESC LIMIT 40")
            return {"text": "Источники и точные разрешения:\n" + _dump({"sources": connectors, "grants": [dict(r) for r in grants]})}
        if command == "sourcerevoke":
            await broker.revoke_grant(conn, _int(argument), owner_id=user_id)
        else:
            data = _json(argument)
            allowed = {"task_id", "request", "mode"} | ({"expires_at"} if command == "sourcepolicy" else set())
            if set(data) - allowed:
                raise ValueError("Допустимы task_id, request, mode и для /sourcepolicy expires_at.")
            task = await conn.fetchrow("SELECT * FROM reply_tasks WHERE id = $1 FOR UPDATE", _int(str(data.get("task_id", ""))))
            if task is None:
                raise ValueError("Задание не найдено.")
            mode = data.get("mode", "read")
            if mode not in ("read", "disclose"):
                raise ValueError("mode: read или disclose")
            request = await broker.resolve_request(conn, state, data.get("request", {}), dict(task))
            await broker.grant_for_task(conn, dict(task), request,
                                        owner_id=user_id, mode=mode, persistent=command == "sourcepolicy",
                                        expires_at=data.get("expires_at"))
            from ..replies import workflow
            await workflow.owner_granted(conn, state, task["id"])
            return {"text": (f"Разрешение сохранено для точного источника, запроса, чата {task['chat_id']}"
                             + (f" и темы {task['topic_tg_id']}" if task['topic_tg_id'] is not None else " без ограничения темы")
                             + ". "
                             + ("Действует для будущих заданий в этой области до истечения срока. " if command == "sourcepolicy" else "")
                             + ("Чтение не разрешает раскрывать содержимое собеседнику." if mode == "read" else
                                "Разрешено раскрытие в ответе этому чату в пределах запроса."))}
    else:
        return {"text": "Неизвестная команда. /help — доступные действия.", "buttons": menu_buttons()}
    return {"text": "Сделано. Изменена только указанная настройка/область.", "buttons": menu_buttons()}


async def on_menu(conn: asyncpg.Connection, state: Any, rest: str, user_id: int) -> dict[str, Any]:
    if not authority.is_owner() or rest not in {command for row in MENU for _, command in row}:
        return {"answer": "Кнопка недоступна.", "remove_buttons": False}
    try:
        result = await handle_command(conn, state, "/" + rest, user_id)
    except (ValueError, confirm.Refused, drafts.Refused, SourceError) as exc:
        result = {"text": str(exc)}
    await bridge.notify_owner(conn, result.get("text", "Готово."),
                              buttons=[[bridge.button(label, MODULE, command) for label, command in row] for row in MENU])
    return {"answer": "Готово.", "remove_buttons": False}
