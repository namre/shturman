"""Deterministic owner control in the private service bot; never exposed as HTTP tools.

Authentication happens on genuine Telegram updates in Bot and is checked again here.
Ordinary decisions and policy changes stay in Telegram. Hermes keeps the owner's
conversational interface, while this control path never executes model instructions.

Тексты (Р-86): `/start`, `/menu`, `/help` — несколько строк простыми словами и кнопки; сводки —
словами (`owner_views.py`), пустое называется пустым; полный список команд с номерами и JSON —
в `/advanced`, сырые настройки — `/config json`. Команды и данные кнопок прежние: старые карточки
и запасной путь командами работают как раньше.
"""
from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

import asyncpg

from .. import authority, bridge, confirm, words
from ..outbox import autoreply, drafts, policy
from ..sources.registry import SourceError
from . import binding, menu_button, owner_views

MODULE = "oc"
# Главное меню. «Согласования» открывает приложение «Штурман», когда оно доступно (кнопка web_app —
# только в ответах самого бота, `menu_buttons`); иначе — сводка того, что ждёт решения.
MENU = [
    [("Сейчас", "status"), (words.APPROVALS, "approvals")],
    [("Помощь", "help"), ("Ещё", "more")],
]
MORE = [
    [("Черновики", "drafts"), ("Вопросы ассистента", "replytasks")],
    [("Источники", "sources"), ("Группы и темы", "groups")],
    [("Внешние программы", "mcpaccess"), ("Чаты", "chats")],
    [(words.REHEARSAL, "rehearsal"), ("Настройки", "config")],
    [("Все команды", "advanced")],
]
# Данные кнопок, которые принимает `on_menu`: нынешние меню и «Ещё» и кнопки прежних версий
# (под старыми сообщениями они остаются и должны работать).
MENU_COMMANDS = frozenset({c for rows in (MENU, MORE) for row in rows for _, c in row}
                          | {"config", "replytasks", "sources", "groups"})

START = ("Здесь — ваши решения по ассистенту: черновики ответов, подтверждения, вопросы. "
         "Решайте кнопками под карточками.")
START_APP = "Кнопка «Штурман» внизу чата — всё, что ждёт решения, и выбор чатов."
START_NO_APP = f"Настроить, какие чаты читать, — {words.IN_SETUP_PAGE} в браузере."

HELP = f"""Как пользоваться ботом

• Сюда приходят карточки: черновик ответа, подтверждение действия, вопрос ассистента, скрытое сообщение. Решайте кнопками под карточкой.
• «Сейчас» — что работает и сколько ждёт решения.
• «{words.APPROVALS}» — всё, что ждёт вашего решения, одним списком.
• Подключение аккаунтов, выбор чатов и уведомления — {words.IN_SETUP_PAGE} в браузере.
• От вашего имени ничего не уходит без вашего нажатия «Отправить», и только если отправка включена на сервере. Исключение — автоответ доверенным людям, если вы его включили.

Все команды с точными параметрами — /advanced."""

ADVANCED = """Все команды (для точной настройки)
/menu — кнопки; /status — что работает; /config — настройки словами, /config json — как есть
/replytasks (или /tasks) — вопросы ассистента и ответы на сообщения
/replytask НОМЕР answer ТЕКСТ — ответить ассистенту; /replytask НОМЕР cancel — отменить
/sources — источники и разрешения; /sourcegrant JSON — разрешение для ответа
/sourcepolicy JSON — точное разрешение для будущих ответов в этом чате (до 30 дней)
/sourcerevoke НОМЕР — отозвать разрешение
/mcpaccess — внешние программы с доступом к архиву; /mcprevoke НОМЕР — отозвать доступ
/groups — группы и темы для ответов
/groupallow НОМЕР_ЧАТА [НОМЕР_ТЕМЫ] — разрешить отвечать в группе или теме
/groupdeny НОМЕР — убрать разрешение
/addressing JSON — обращения к ассистенту в группах (например {"aliases":["Штурман"]})
/prepare on|off — черновики ответов без отправки (способ «Через бота и помощника»)
/rehearsal — репетиция в личных чатах вашего аккаунта: on|off, add НОМЕР_В_TELEGRAM, remove НОМЕР_В_TELEGRAM
/policy JSON — правила отправки (например {"daily_cap":20})
/autoconfig JSON — настройки автоответа
/autoreply НОМЕР_АККАУНТА on|off — автоответ доверенным
/trust НОМЕР_В_TELEGRAM [ЗАМЕТКА] — доверенный; /untrust НОМЕР_В_TELEGRAM — убрать
/chats — чаты и их номера; /drafting НОМЕР_ЧАТА allow|deny|default — черновики в этом чате
/drafts — черновики, которые ждут решения; /cancel НОМЕР — снять черновик

Команды меняют только названное. Отправка требует включённого выключателя на сервере и разрешённого канала. В режиме без отправки сообщение остаётся черновиком. Решения по источникам и раскрытию — в карточках здесь."""

NOT_COMMAND = "Я понимаю кнопки и команды. /menu — главное, /help — как пользоваться."
UNKNOWN = "Такой команды нет. /help — как пользоваться, /advanced — все команды."
BAD_NUMBER = ("Нужен номер — целое число больше нуля. Номера видны в списках: /chats, /drafts, /groups, "
              "/sources. Точный вид команд — /advanced.")
BAD_JSON = ("После команды нужны настройки в фигурных скобках, например {\"daily_cap\": 20}. "
            "Точный вид — /advanced, нынешние значения — /config json.")
_COMMAND = re.compile(r"^/([a-z]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", re.DOTALL)

Buttons = list[list[tuple[str, Any]]]


def _rows(rows: list[list[tuple[str, str]]]) -> Buttons:
    return [[(label, bridge.button(label, MODULE, command)["data"]) for label, command in row] for row in rows]


def menu_buttons(state: Any = None) -> Buttons:
    """Главное меню. С `state` и внешним адресом страницы настройки «Согласования» — кнопка, которая
    открывает приложение «Штурман» (web_app); без них — обычная кнопка сводки."""
    rows = _rows(MENU)
    url = menu_button.app_url(state.config) if state is not None else None
    if url:
        rows[0][1] = (words.APPROVALS, {"web_app": url})
    return rows


def more_buttons() -> Buttons:
    return _rows(MORE)


def start_text(state: Any = None) -> str:
    app = state is not None and menu_button.app_url(state.config)
    return START + "\n" + (START_APP if app else START_NO_APP)


def _json(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError(BAD_JSON) from None
    if not isinstance(value, dict):
        raise ValueError(BAD_JSON)
    return value


def _int(raw: str) -> int:
    if not raw.isascii() or not raw.isdigit() or len(raw) > 18 or int(raw) <= 0:
        raise ValueError(BAD_NUMBER)
    return int(raw)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


async def handle_message(conn: asyncpg.Connection, state: Any, text: str, *,
                         bot_id: int, user_id: int, chat_id: int) -> dict[str, Any] | None:
    """Only Bot calls this, using IDs obtained from Telegram, never from request JSON."""
    owner = await binding.bound_owner(conn, bot_id)
    if owner is None or owner["user_id"] != user_id or owner["chat_id"] != chat_id or chat_id != user_id:
        return None
    if not text.startswith("/"):
        return {"text": NOT_COMMAND, "buttons": menu_buttons(state)}
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
        return {"text": "Не удалось разобрать команду. /help — как пользоваться, /advanced — точный вид команд.",
                "buttons": menu_buttons(state)}
    command, argument = match.group(1), (match.group(2) or "").strip()
    menu = menu_buttons(state)
    if command in ("menu", "start"):
        return {"text": start_text(state), "buttons": menu}
    if command == "help":
        return {"text": HELP, "buttons": menu}
    if command == "advanced":
        return {"text": ADVANCED, "buttons": menu}
    if command == "more":
        return {"text": ("Ещё: черновики ответов, вопросы ассистента, источники, группы, внешние программы, "
                         "чаты, репетиция и настройки. Полный список команд — /advanced."),
                "buttons": more_buttons()}
    if command == "approvals":
        return {"text": await owner_views.approvals_text(conn, state, app=bool(menu_button.app_url(state.config))),
                "buttons": menu}
    if command in ("replytasks", "replytask", "tasks"):
        from ..replies import owner
        return await owner.handle_command(conn, state, text.replace("/tasks", "/replytasks", 1), user_id)
    if command == "status":
        return {"text": await owner_views.status_text(conn, state), "buttons": menu}
    if command == "rehearsal":
        return await _rehearsal(conn, state, argument)
    if command == "config":
        if argument == "json":
            return {"text": await owner_views.config_json(conn, state), "buttons": menu}
        return {"text": await owner_views.config_text(conn, state), "buttons": menu}
    if command == "prepare":
        return await _prepare(conn, state, argument)
    if command == "policy":
        from ..outbox.service import POLICY
        changes = policy.validate_update(_json(argument))
        before = await policy.stored(conn)
        await confirm.apply_owner(conn, POLICY, {"changes": changes})
        return _done("Правила отправки изменены:\n" + policy.describe_changes(before, changes, policy.LABELS),
                     "/policy " + _compact({k: before.get(k) for k in changes}), menu)
    if command == "autoconfig":
        from ..outbox.service import AUTOREPLY
        changes = autoreply.validate_update(_json(argument))
        before = await autoreply.load(conn)
        await confirm.apply_owner(conn, AUTOREPLY, {"changes": changes})
        return _done("Настройки автоответа изменены:\n" + policy.describe_changes(before, changes, autoreply.LABELS),
                     "/autoconfig " + _compact({k: before.get(k) for k in changes}), menu)
    if command == "autoreply":
        from ..outbox.service import AUTOREPLY
        parts = argument.split()
        if len(parts) != 2 or parts[1] not in ("on", "off"):
            raise ValueError("Так: /autoreply номер-аккаунта on — включить или off — выключить. Номера "
                             "аккаунтов — в /config json.")
        account_id, on = _int(parts[0]), parts[1] == "on"
        await confirm.apply_owner(conn, AUTOREPLY, {"account_id": account_id, "enabled": on})
        label = await conn.fetchval("SELECT label FROM accounts WHERE id = $1", account_id)
        return _done(f"Автоответ доверенным для аккаунта «{owner_views._name(label, str(account_id), 40)}» "
                     + ("включён: этим людям ассистент будет отвечать сам, если отправка включена на сервере."
                        if on else "выключен."),
                     f"/autoreply {account_id} {'off' if on else 'on'}", menu)
    if command == "drafting":
        from ..outbox.service import CHAT_DRAFTING
        parts = argument.split()
        if len(parts) != 2 or parts[1] not in ("allow", "deny", "default"):
            raise ValueError("Так: /drafting номер-чата allow (можно готовить черновики), deny (нельзя) или "
                             "default (как в общих правилах). Номера чатов — в /chats.")
        chat_id = _int(parts[0])
        before = await conn.fetchval("SELECT drafting FROM outbox_chats WHERE chat_id = $1", chat_id) or "default"
        await confirm.apply_owner(conn, CHAT_DRAFTING, {"chat_id": chat_id, "value": parts[1]})
        title = await conn.fetchval("SELECT title FROM chats WHERE id = $1", chat_id)
        word = {"allow": "можно готовить", "deny": "нельзя готовить", "default": "как в общих правилах"}[parts[1]]
        return _done(f"Черновики в чате «{owner_views._name(title, f'№ {chat_id}')}»: {word}.",
                     f"/drafting {chat_id} {before}", menu)
    if command == "trust":
        from ..outbox.service import TRUSTED_ADD
        raw, _, note = argument.partition(" ")
        tg_id = _int(raw)
        await confirm.apply_owner(conn, TRUSTED_ADD, {"tg_user_id": tg_id, "note": note})
        return _done(f"Человек с номером {tg_id} в Telegram — в доверенных. Если автоответ включён, ассистент "
                     "будет отвечать ему сам.", f"/untrust {tg_id}", menu)
    if command == "untrust":
        tg_id = _int(argument)
        removed = await conn.fetchval("DELETE FROM outbox_trusted WHERE tg_user_id = $1 RETURNING true", tg_id)
        if not removed:
            return {"text": f"Номера {tg_id} в доверенных не было — ничего не изменилось.", "buttons": menu}
        return _done(f"Человек с номером {tg_id} в Telegram больше не в доверенных: ассистент не отвечает ему сам.",
                     f"/trust {tg_id}", menu)
    if command == "chats":
        return {"text": await owner_views.chats_text(conn)}
    if command in ("drafts", "cancel"):
        if command == "cancel":
            draft_id = _int(argument)
            result = await drafts.cancel(conn, draft_id)
            if result is None:
                return {"text": f"Черновика № {draft_id} нет. Ждущие черновики — /drafts."}
            if not result.get("cancelled"):
                return {"text": f"Черновик № {draft_id} уже решён — снимать нечего."}
            return {"text": f"Черновик № {draft_id} снят, ничего не отправлено. Вернуть его нельзя — "
                            "попросите ассистента подготовить новый."}
        return {"text": await owner_views.drafts_text(conn, state)}
    if command in ("groups", "groupallow", "groupdeny", "addressing"):
        from ..outbox import scopes
        if command == "groups":
            return {"text": await owner_views.groups_text(conn)}
        async with conn.transaction():
            if command == "groupdeny":
                scope_id = _int(argument)
                if not await scopes.remove(conn, scope_id):
                    return {"text": f"Разрешения № {scope_id} нет. Список — /groups.", "buttons": menu}
                return _done(f"Разрешение № {scope_id} убрано: в этой группе ассистент больше не отвечает.",
                             "/groupallow с номером чата из /chats", menu)
            if command == "addressing":
                before = (await scopes.addressing(conn))["aliases"]
                after = (await scopes.update_addressing(conn, scopes.validate_addressing(_json(argument))))["aliases"]
                return _done("Обращения к ассистенту в группах: " + (", ".join(f"«{a}»" for a in after) or "нет") + ".",
                             "/addressing " + _compact({"aliases": before}), menu)
            parts = argument.split()
            if not 1 <= len(parts) <= 2:
                raise ValueError("Так: /groupallow номер-чата или /groupallow номер-чата номер-темы. "
                                 "Номера чатов — в /chats.")
            row = await scopes.put(conn, _int(parts[0]), topic_tg_id=_int(parts[1]) if len(parts) == 2 else None)
            title = await conn.fetchval("SELECT title FROM chats WHERE id = $1", _int(parts[0]))
            topic = f", тема № {parts[1]}" if len(parts) == 2 else ""
            return _done(f"Ассистенту можно отвечать в группе «{owner_views._name(title, 'без названия')}»{topic}.",
                         f"/groupdeny {row['id']}", menu)
    if command in ("mcpaccess", "mcpclients", "mcprevoke"):
        from ..remote_mcp import core
        if command == "mcprevoke":
            grant_id = _int(argument)
            revoked = await core.revoke_grant(conn, grant_id)
            return {"text": f"Доступ № {grant_id} отозван: эта программа больше не читает архив. Вернуть — только "
                            "подключить её заново." if revoked else f"Доступа № {grant_id} нет. Список — /mcpaccess."}
        return {"text": await owner_views.mcp_text(conn, state)}
    if command in ("sources", "sourcegrant", "sourcerevoke", "sourcepolicy"):
        from ..sources import broker
        if command == "sources":
            return {"text": await owner_views.sources_text(conn, state)}
        if command == "sourcerevoke":
            grant_id = _int(argument)
            await broker.revoke_grant(conn, grant_id, owner_id=user_id)
            return _done(f"Разрешение № {grant_id} отозвано: ассистент больше не читает по нему.",
                         "новое разрешение приходит карточкой, когда ассистент снова попросит", menu)
        data = _json(argument)
        allowed = {"task_id", "request", "mode"} | ({"expires_at"} if command == "sourcepolicy" else set())
        if set(data) - allowed:
            raise ValueError("Допустимы поля task_id, request, mode, а для /sourcepolicy ещё expires_at.")
        task = await conn.fetchrow("SELECT * FROM reply_tasks WHERE id = $1 FOR UPDATE", _int(str(data.get("task_id", ""))))
        if task is None:
            raise ValueError("Такого ответа на сообщение нет. Номера — в /replytasks.")
        mode = data.get("mode", "read")
        if mode not in ("read", "disclose"):
            raise ValueError("mode: read (только прочитать) или disclose (можно пересказать собеседнику)")
        request = await broker.resolve_request(conn, state, data.get("request", {}), dict(task))
        await broker.grant_for_task(conn, dict(task), request,
                                    owner_id=user_id, mode=mode, persistent=command == "sourcepolicy",
                                    expires_at=data.get("expires_at"))
        from ..replies import workflow
        await workflow.owner_granted(conn, state, task["id"])
        return {"text": (f"Разрешение сохранено: этот источник и запрос, ответ в чат № {task['chat_id']}"
                         + (f", тема {task['topic_tg_id']}" if task['topic_tg_id'] is not None else ", сообщения без темы")
                         + ". "
                         + ("Действует и для будущих ответов в этом чате до конца срока. " if command == "sourcepolicy" else "")
                         + ("Только прочитать: пересказывать собеседнику нельзя." if mode == "read" else
                            "Можно пересказать собеседнику в ответе в пределах запроса.")
                         + " Отозвать — /sourcerevoke с номером из /sources.")}
    return {"text": UNKNOWN, "buttons": menu}


def _done(what: str, undo: str, menu: Buttons) -> dict[str, Any]:
    """Ответ на действие: что изменилось и как вернуть (Б8)."""
    return {"text": f"{what}\nВернуть: {undo}", "buttons": menu}


async def _prepare(conn: asyncpg.Connection, state: Any, argument: str) -> dict[str, Any]:
    """Черновики ответов без отправки (`outbox.prepare_only`). Включает и выключает сам владелец."""
    mode = await owner_views.work_mode(conn)
    elsewhere = ("\n" + f"Сейчас выбран способ «{words.MODE_OWN}» — там черновиков нет, есть "
                 f"«{words.REHEARSAL}»: /rehearsal.") if mode == "own" else ""
    current = bool(getattr(state.config, "prepare_only", False))
    if argument not in ("on", "off"):
        return {"text": (f"{words.DRAFTS_MODE}: {'включены' if current else 'выключены'}. Когда включены, на новые "
                         "сообщения ассистент готовит ответ и показывает его вам карточкой — без отправки. "
                         "/prepare on — включить, /prepare off — выключить." + elsewhere),
                "buttons": menu_buttons(state)}
    enabled = argument == "on"
    await policy.save_setting(conn, "outbox.prepare_only", {"enabled": enabled})
    state.config = dataclasses.replace(state.config, prepare_only=enabled)
    if enabled:
        text = (f"{words.DRAFTS_MODE} включены: на новые сообщения ассистент будет готовить ответ и присылать "
                "вам карточкой, ничего не отправляя. Выключить — /prepare off." + elsewhere)
    else:
        text = f"{words.DRAFTS_MODE} выключены: ответы без отправки больше не готовятся. Включить — /prepare on."
    return {"text": text, "buttons": menu_buttons(state)}


REHEARSAL_FORMAT = ("Так: /rehearsal — посмотреть; /rehearsal on или off — включить или выключить; "
                    "/rehearsal add номер-в-Telegram или remove номер-в-Telegram — добавить или убрать человека.")


async def _rehearsal(conn: asyncpg.Connection, state: Any, argument: str) -> dict[str, Any]:
    """Репетиция ответов (Р-85): посмотреть, включить, выключить, добавить или убрать человека.
    Команда владельца из личного чата с ботом — его решение: применяется сразу. Отправки здесь нет."""
    from ..rehearsal import core as rehearsal
    from ..rehearsal import service as rehearsal_service
    from ..rehearsal import settings as rs
    mode = await owner_views.work_mode(conn)
    elsewhere = ("\n" + f"Сейчас выбран способ «{words.MODE_STAFF}» — репетиция работает только с вашим "
                 f"основным аккаунтом (способ «{words.MODE_OWN}»), там она недоступна. Для этого способа — "
                 f"«{words.DRAFTS_MODE}»: /prepare.") if mode == "staff" else ""
    parts = argument.split()
    s = await rs.load(conn)
    if not parts:
        st = await rehearsal.stats(conn)
        text = (f"{words.REHEARSAL}: {'включена' if s['enabled'] else 'выключена'}. Людей в списке: {len(s['people'])}. "
                f"Не больше {s['daily_cap']} карточек в сутки.\n"
                f"За {st['days']} дней карточек: {st['shown']}; оценки — годится: {st['good']}, "
                f"почти: {st['almost']}, не годится: {st['bad']}, без оценки: {st['unrated']}.\n"
                "Ассистент пишет, что ответил бы, вы оцениваете. Ничего не отправляется: предложенный ответ "
                "приходит только вам, в карточке.\n" + REHEARSAL_FORMAT + elsewhere)
        return {"text": text, "buttons": menu_buttons(state)}
    if len(parts) == 1 and parts[0] in ("on", "off"):
        changes: dict[str, Any] = {"enabled": parts[0] == "on"}
    elif len(parts) == 2 and parts[0] in ("add", "remove"):
        tg_id = _int(parts[1])
        people = set(s["people"])
        if parts[0] == "add":
            people.add(tg_id)
        else:
            people.discard(tg_id)
        changes = {"people": sorted(people)}
    else:
        raise ValueError(REHEARSAL_FORMAT)
    out = await confirm.apply_owner(conn, rehearsal_service.SETTINGS, {"changes": rs.validate(changes)})
    after = out["result"]["settings"]
    text = f"{words.REHEARSAL}:\n" + "\n".join(rs.describe(after))
    if "enabled" in changes:
        text += "\nВернуть: /rehearsal " + ("off" if after["enabled"] else "on")
    elif parts[0] == "add":
        text += f"\nВернуть: /rehearsal remove {parts[1]}"
    else:
        text += f"\nВернуть: /rehearsal add {parts[1]}"
    if after["enabled"] and not after["people"]:
        text += (f"\nВ списке пока никого: добавьте человека — /rehearsal add номер-в-Telegram или "
                 f"{words.IN_SETUP_PAGE}.")
    return {"text": text + elsewhere, "buttons": menu_buttons(state)}


async def on_menu(conn: asyncpg.Connection, state: Any, rest: str, user_id: int) -> dict[str, Any]:
    """Нажатие кнопки меню. Ответ уходит прямо в чат владельца (`reply`): бот отправляет его сам,
    поэтому в нём может быть кнопка, открывающая приложение «Штурман»."""
    if not authority.is_owner() or rest not in MENU_COMMANDS:
        return {"answer": "Кнопка недоступна.", "remove_buttons": False}
    try:
        result = await handle_command(conn, state, "/" + rest, user_id)
    except (ValueError, confirm.Refused, drafts.Refused, SourceError) as exc:
        result = {"text": str(exc)}
    buttons = result.get("buttons") or menu_buttons(state)
    return {"answer": "Готово.", "remove_buttons": False,
            "reply": {"text": result.get("text", "Готово."), "buttons": buttons}}
