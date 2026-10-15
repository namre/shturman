"""Экран «Согласования» в Mini App бота согласований: что ждёт решения владельца и решение по нему.

Правило одно: **приложение — ещё один способ нажать ту же кнопку**. Своей логики решений здесь нет.

  * Список собирают сами модули, которые отправляют карточки в бота, — функциями `awaiting_owner`
    рядом со своими обработчиками нажатий. Каждый пункт несёт ровно те данные кнопок (`sh:…`),
    что стоят под его карточкой в боте: с той же случайной меткой или отпечатком показанного.
  * Решение — это данные одной из этих кнопок. Они принимаются, только если такая кнопка есть в
    списке прямо сейчас, и передаются в `bridge.dispatch_callback` — тот же вызов, которым бот
    передаёт нажатие (`executor/bot.py`). Дальше всё как в боте: проверка владельца, метки,
    отпечатка, срока, выключателя отправки (`SHTURMAN_SENDING`) и правил — в обработчике модуля.
  * Кто нажимает, удостоверяет Telegram: сессия приложения выдана по данным входа, подписанным для
    бота согласований (`miniapp.py`), и только привязанному владельцу. Поэтому решение принимается
    в контексте владельца из Telegram (`authority.miniapp_context`) — как нажатие в боте.

Что показывается (и чем решается):

  | Вид | Откуда | Кнопки | Обработчик |
  |---|---|---|---|
  | Черновик ответа | `outbox/drafts.py` | «Отправить», «Отклонить» | `drafts.on_button` |
  | Подтверждение действия | `confirm.py` | «Да, сделать», «Нет» | `confirm._pressed` |
  | Ответ на сообщение (нужно разрешение прочитать) или вопрос ассистента | `replies/workflow.py` | «Прочитать один раз», «на 30 дней», «Отказать» / «Не отвечать» | `replies/owner.on_button` |
  | Скрытое сообщение | `guard/alerts.py` | «Показать ассистенту», «Оставить скрытым» | `alerts._pressed` |
  | Договорённость и изменение договорённости | `processing/commitments.py` | «Верно», «Не так» / «Применить», «Оставить как есть» | `commitments.handle_callback` |
  | Факт о владельце | `processing/facts.py` | «Запомнить», «Не запоминать» | `facts.on_button` |
  | Страница о человеке | `processing/pages_build.py` | «Завести страницу», «Не нужно» | `pages_build.on_button` |
  | Предложенный проект | `processing/projects.py` | «Завести проект», «Не нужно» | `projects.on_button` |

Чего нет: запросов облачного клиента MCP (`remote_mcp`): метка их кнопки хранится только в виде
хеша, и собрать ту же кнопку нельзя — они решаются в боте. Меню бота (`/menu`) и команды —
тоже только в боте. Пункты сводок, ещё не ушедшие владельцу, не показываются (у них нет отпечатка
показанного) — только их число.

Карточка в боте. После решения в приложении текст карточки в боте меняется так же, как после
нажатия под ней, — там, где сервис знает, каким сообщением ушла карточка (черновики, подтверждения
действий, скрытые сообщения). Под сводками (обязательства, факты, страницы, проекты) и задачами
ответа кнопки остаются; нажатие на них в боте ответит «Уже решено».
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from .. import authority, bridge, words

logger = logging.getLogger("shturman.setup")

# Данные кнопок, которые приложение вообще может прислать: только модули из таблицы выше и только
# их виды нажатий. Остальное отвергается до поиска в списке.
REF = re.compile(
    r"^sh:(?:"
    r"ob:[sr]:\d{1,18}:[A-Za-z0-9_-]{8,43}"
    r"|cf:[yn]:\d{1,18}:[A-Za-z0-9_-]{8,43}"
    r"|rp:[yrn]:\d{1,18}:[A-Za-z0-9_-]{8,43}"
    r"|gd:[rc]:\d{1,18}:[A-Za-z0-9_-]{8,43}"
    r"|cm:(?:a:\d{1,18}:[0-9a-f]{24}|r:\d{1,18}|ca:\d{1,18}:[0-9a-f]{24}|cr:\d{1,18})"
    r"|of:(?:a:\d{1,18}:[0-9a-f]{24}|r:\d{1,18})"
    r"|pg:[ar]:\d{1,18}"
    r"|pj:[ar]:\d{1,18}"
    r")$")

DRAFT_NOTE = ("Изменить текст здесь нельзя — как и в боте. Нужен другой текст — попросите ассистента "
              "подготовить новый черновик: этот заменится.")
PRIMARY = frozenset({"Отправить", "Да, сделать", "Прочитать один раз", "Показать ассистенту", words.COMMITMENT_YES,
                     words.CHANGE_YES, words.FACT_YES, words.PAGE_YES, words.PROJECT_YES, words.DRAFT_SEEN})


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


# В карточке бота — как ответить там («Нажмите «Ответить»… или командой /replytask …»); в приложении
# для этого поле «Ваш ответ», и подсказка бота там только путает.
_BOT_HOW = re.compile(r"Нажмите «Ответить» и напишите ответ одним сообщением[^\n]*?/replytask \d+ answer ваш ответ\.")


def _for_app(text: str, has_answer: bool) -> str:
    return _BOT_HOW.sub("Ответьте в поле ниже.", text) if has_answer else text


def _item(kind: str, kind_title: str, entry: dict[str, Any], *, note: str | None = None) -> dict[str, Any]:
    from ..replies import workflow
    # «Ответить» на вопрос ассистента (П1): в приложении это не кнопка, а поле «Ваш ответ» с кнопкой
    # «Ответить» — ответ идёт своим маршрутом (`miniapp.approvals_answer`), но тем же путём на сервере.
    answer = [b["data"] for row in entry["buttons"] for b in row if b["data"].startswith(workflow.ANSWER_PREFIX)]
    actions = [{"label": b["text"], "ref": b["data"], "primary": b["text"] in PRIMARY}
               for row in entry["buttons"] for b in row if not b["data"].startswith(workflow.ANSWER_PREFIX)]
    return {
        "key": f"{kind}:{entry['id']}", "kind": kind, "kind_title": kind_title, "id": entry["id"],
        "title": entry.get("title") or kind_title, "head": list(entry.get("head") or []),
        "text": _for_app(entry.get("text") or "", bool(answer)), "note": note or entry.get("note"), "actions": actions,
        "expires_at": _iso(entry.get("expires_at")),
        "answer": {"ref": answer[0], "question": entry.get("question"), "max": 2000} if answer else None,
        # Только для сервера: каким сообщением в боте ушла карточка. В ответ приложению не идёт.
        "_card_message_id": entry.get("card_message_id"),
    }


def today_of(config: Any) -> date:
    try:
        zone = ZoneInfo(config.timezone)
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("UTC")
    return datetime.now(zone).date()


async def collect(conn: asyncpg.Connection, config: Any) -> dict[str, Any]:
    """Всё, что сейчас ждёт решения владельца, — с теми же кнопками, что в боте."""
    from ..guard import alerts
    from ..outbox import drafts
    from ..processing import commitments, facts, pages_build, projects
    from ..replies import workflow
    from .. import confirm

    items: list[dict[str, Any]] = []
    sendable = bool(getattr(config, "sending", False)) and not getattr(config, "prepare_only", False)
    for entry in await drafts.awaiting_owner(conn):
        item = _item("draft", "Черновик ответа", entry, note=DRAFT_NOTE)
        if entry.get("prepare_only") or not sendable:
            item = _seen_only(item)
        items.append(item)
    for entry in await confirm.awaiting_owner(conn):
        items.append(_item("action", "Подтверждение действия", entry))
    for entry in await workflow.awaiting_owner(conn):
        title = words.REPLY_SOURCE if entry.get("subkind") == "source" else words.REPLY_QUESTION
        items.append(_item("reply", title, entry))
    for entry in await alerts.awaiting_owner(conn):
        items.append(_item("hidden", "Скрытое сообщение", entry))
    later: dict[str, int] = {"hidden": await alerts.waiting(conn)}
    for kind, kind_title, found in (
        ("commitment", "Договорённость", await commitments.awaiting_owner(conn, today=today_of(config))),
        ("owner_fact", "Факт о вас", await facts.awaiting_owner(conn)),
        ("page", "Страница памяти", await pages_build.awaiting_owner(conn, days=pages_build.Options().propose_days)),
        ("project", "Проект", await projects.awaiting_owner(conn)),
    ):
        items.extend(_item(kind, kind_title, entry) for entry in found["items"])
        later[kind] = found["later"]
    return {"items": items, "later": later}


def _seen_only(item: dict[str, Any]) -> dict[str, Any]:
    """Черновик, который сейчас отправить нельзя (отправка выключена, режим без отправки или черновик
    подготовлен только для просмотра), — П2: вместо «Отправить» и «Отклонить» одна кнопка «Понятно».
    Она нажимает ту же кнопку, что «Отклонить» под карточкой в боте (данные `sh:ob:r:…`): ничего не
    отправляется, черновик снимается. Кнопка «Отправить» из списка уходит — её нажатие из приложения
    больше не принимается (`find`), а в боте всё равно ответило бы «Пока нельзя»."""
    reject = [a for a in item["actions"] if a["ref"].startswith("sh:ob:r:")]
    if reject:
        item["actions"] = [{"label": words.DRAFT_SEEN, "ref": reject[0]["ref"], "primary": True}]
        item["note"] = ("Отправка сейчас выключена: этот черновик только для просмотра. «Понятно» убирает "
                        "его из списка, ничего не отправляя.")
    return item


def public(collected: dict[str, Any]) -> dict[str, Any]:
    """То, что уходит в приложение: без служебных полей."""
    return {"items": [{k: v for k, v in item.items() if not k.startswith("_")} for item in collected["items"]],
            "later": collected["later"]}


def find(collected: dict[str, Any], ref: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    for item in collected["items"]:
        for action in item["actions"]:
            if action["ref"] == ref:
                return item, action
    return None


ANSWER_REF = re.compile(r"^sh:rp:a:(\d{1,18}):([A-Za-z0-9_-]{8,43})$")


def find_answer(collected: dict[str, Any], ref: str) -> dict[str, Any] | None:
    """Вопрос ассистента, на который сейчас можно ответить из приложения: `ref` — ровно то, что стоит
    под его карточкой в боте как «Ответить» (с той же меткой)."""
    for item in collected["items"]:
        if item.get("answer") and item["answer"]["ref"] == ref:
            return item
    return None


async def decide(conn: asyncpg.Connection, item: dict[str, Any], ref: str, *, user_id: int,
                 chat_id: int) -> dict[str, Any]:
    """Нажатие кнопки `ref` от владельца, удостоверенного Telegram. Тот же вызов, что у бота."""
    with authority.miniapp_context(user_id, chat_id=chat_id):
        out = await bridge.dispatch_callback(conn, ref, user_id)
    message_id = item.get("_card_message_id")
    text = out.get("edit_text")
    if out.get("remove_buttons") and isinstance(text, str) and text.strip() and isinstance(message_id, int):
        # То же, что бот делает с карточкой, под которой нажали: новый текст, без кнопок.
        try:
            await bridge.edit_owner_message(conn, message_id, text, remove_buttons=True)
        except Exception as exc:  # noqa: BLE001 — решение уже принято; карточка обновится по сроку
            logger.warning("приложение: карточка в боте не обновлена (%s)", type(exc).__name__)
    return out


def wake(state: Any) -> None:
    """Будит исполнителя бота: решение могло поставить сообщение или правку карточки. Как бот —
    дважды: часть заданий модуль ставит чуть позже ответа."""
    worker = getattr(state.extras.get("executor"), "worker", None)
    if worker is None:
        return
    worker.wake()
    try:
        asyncio.get_running_loop().call_later(0.6, worker.wake)
    except RuntimeError:
        pass
