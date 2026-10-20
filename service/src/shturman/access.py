"""Кто обращается к сервису и что ему можно — на сервере, по токену (Р-104).

Решение владельца: Hermes — полноценный личный ассистент (терминал, интернет, память), его не урезаем.
Ответственность «Штурмана» — собственная граница: не отдать лишнего и не дать изменить данные владельца
без его нажатия. Модель Hermes считается полностью захваченной, а токены из её окружения — известными
программе в её терминале. Поэтому ограничения здесь, в сервисе, по тому, каким токеном пришёл запрос, —
а не в перечнях плагина и не в описаниях инструментов.

Токены (`app.Gate`):

  SHTURMAN_OPERATOR_TOKEN — оператор: `shturman call` и скрипты ops/ в контейнере сервиса. Весь внутренний
                            API. В контейнер Hermes не передаётся;
  SHTURMAN_API_TOKEN      — плагин «Штурмана» в Hermes (он же — ассистент в терминале Hermes). Только
                            перечень `HERMES_ROUTES` ниже; остальное — 403 `hermes_forbidden`;
  SHTURMAN_MCP_TOKEN      — архив по MCP, только чтение; выдача мерится и ограничивается
                            (`archive_guard.py`). Внешний шлюз архива (`remote_mcp/`) приходит сюда же
                            со своей меткой вызывающего.

Каждый маршрут перечня — одного из трёх видов:

  READ   — только чтение. Ответы с текстом переписки мерятся и ограничиваются (`METERED`);
  ASK    — изменение, которое для этого токена НИКОГДА не применяется сразу: сервис ставит карточку
           владельцу в бот согласований (`api_core.settle`) или создаёт черновик, который без нажатия
           владельца никуда не уходит. Даже ужесточение: «закрыть», «перенести», «сузить» — тоже решение
           владельца. Ответ — только технический итог (`ask_answer`, Д-53): ни текста карточки, ни объектов;
           пока владелец закрыл выдачу архива — 423 до обработчика (`archive_guard.ask_api`);
  BRIDGE — служебный обмен исполнителя заданий плагина: очередь заданий, первичная подсказка владельца и
           (только без своего бота сервиса) нажатия и бизнес-сообщения. Каждый из этих маршрутов сам
           проверяет, что можно (`api_core.only_without_own_bot`, `authority`).

Новый маршрут сервиса этому токену недоступен, пока его не внесли сюда. Тест
`tests/test_hermes_access.py` перечисляет все маршруты приложения и падает, если среди доступных Hermes
появился изменяющий маршрут вида не ASK/BRIDGE или маршрут ASK применился без нажатия.
"""

from __future__ import annotations

import contextvars
import re
from typing import Iterable

OPERATOR = "operator"
HERMES = "hermes"          # внутренний API по токену плагина Hermes
ARCHIVE = "mcp"            # архив по MCP по местному токену
REMOTE = "remote"          # архив по MCP через внешний шлюз с OAuth

READ, ASK, BRIDGE = "read", "ask", "bridge"

_caller: contextvars.ContextVar[str | None] = contextvars.ContextVar("shturman_caller", default=None)

_INT = r"[0-9]{1,19}"


def current() -> str | None:
    """Кто прислал текущий запрос: OPERATOR, HERMES, ARCHIVE, REMOTE; None — не HTTP (фоновая работа, команды)."""
    return _caller.get()


def is_hermes() -> bool:
    return _caller.get() == HERMES


def enter(caller: str) -> contextvars.Token:
    return _caller.set(caller)


def leave(token: contextvars.Token) -> None:
    _caller.reset(token)


Table = tuple[tuple[str, "re.Pattern[str]", str, bool], ...]


def _compile(items: Iterable[tuple[str, str, str, bool]]) -> Table:
    return tuple((m, re.compile(p), kind, metered) for m, p, kind, metered in items)


# (метод, путь, вид, мерить ли выдачу). Пути сравниваются целиком.
HERMES_ROUTES: Table = _compile([
    # --- чтение: состояние без текста переписки — не мерится ---
    ("GET", r"/api/status", READ, False),
    ("GET", r"/api/(embeddings|guard|voice|media|processing|executor)/status", READ, False),
    ("GET", r"/api/outbox/policy", READ, False),
    # --- чтение для инструментов ассистента: в ответах цитаты и имена — мерится, ключи вычищаются ---
    ("GET", r"/api/commitments", READ, True),
    ("GET", rf"/api/commitments/{_INT}", READ, True),
    ("GET", r"/api/people", READ, True),
    ("GET", rf"/api/people/{_INT}", READ, True),
    ("GET", r"/api/projects", READ, True),
    ("GET", rf"/api/projects/{_INT}", READ, True),
    ("GET", r"/api/spheres", READ, True),
    # --- изменения: только карточкой владельцу (или черновиком, который ждёт его нажатия) ---
    ("POST", r"/api/outbox/drafts", ASK, False),
    ("POST", rf"/api/commitments/{_INT}/(close|cancel|reopen|reschedule)", ASK, False),
    ("POST", rf"/api/people/{_INT}/aliases", ASK, False),
    ("POST", r"/api/projects", ASK, False),
    ("POST", rf"/api/projects/{_INT}/(chats|archive|sphere)", ASK, False),
    ("POST", r"/api/spheres", ASK, False),
    ("POST", rf"/api/spheres/{_INT}/chats", ASK, False),
    # --- исполнитель заданий плагина ---
    ("PUT", r"/api/owner", BRIDGE, False),
    ("DELETE", r"/api/owner", BRIDGE, False),
    ("POST", r"/api/jobs/claim", BRIDGE, False),
    ("POST", rf"/api/jobs/{_INT}/(complete|fail)", BRIDGE, False),
    ("POST", r"/api/callbacks/telegram", BRIDGE, False),
    ("POST", r"/api/ingest/business/(connection|message|deleted)", BRIDGE, False),
])


def hermes_route(method: str, path: str) -> tuple[str, bool] | None:
    """(вид, мерить ли) для пары, доступной токену Hermes; None — недоступна."""
    method = (method or "").upper()
    if method == "HEAD":
        method = "GET"
    for m, pattern, kind, metered in HERMES_ROUTES:
        if m == method and pattern.fullmatch(path or ""):
            return kind, metered
    return None


FORBIDDEN_TEXT = ("Этот адрес внутреннего API токену ассистента недоступен (Р-104): изменения данных и настроек "
                  "владельца — только им самим, в приложении «Штурман», на странице «Настройки Штурмана» или "
                  "нажатием в боте согласований.")


# Что увидит владелец на карточке, если маршрут не написал описания сам (ужесточение, которое токену
# ассистента сразу не применяется). Вид действия — из перечня, без чужого текста.
KIND_TITLES = {
    "commitments.decide": "изменить договорённость",
    "people.alias": "добавить человеку ещё одно имя",
    "projects.create": "завести проект",
    "projects.chats": "изменить чаты проекта",
    "projects.archive": "убрать проект в архив",
    "spheres.create": "завести сферу",
    "spheres.chats": "изменить чаты сферы",
    "spheres.project": "изменить сферу проекта",
}


def ask_summary(kind: str) -> str:
    title = KIND_TITLES.get(kind, f"изменить настройку ({kind})")
    return (f"Ассистент просит {title}. Сам он это изменить не может (Р-104): только после вашего «Да». "
            "Если вы об этом не просили — нажмите «Нет».")


# --- ответ на просьбу (ASK) токену Hermes: только технический итог (Д-53) ---------------------------------
#
# Текст карточки (`summary`) написан для владельца: в нём формулировка договорённости из переписки, имена людей,
# названия чатов и проектов. Объекты в ответах «ничего не изменилось» (`project`, `person`) — то же самое.
# Маршруты вида ASK не мерятся границей архива, поэтому их ответы токену Hermes сводятся здесь, в одном месте
# (`archive_guard.ask_api`), к итогу без содержимого: «ждёт подтверждения» с номером и сроком, «ничего не
# изменилось», код отказа. Карточку целиком владелец по-прежнему видит в боте. Оператор (доверенная сторона)
# получает ответы как раньше.

DRAFTS_PATH = "/api/outbox/drafts"
PENDING_KEYS = ("status", "action_id", "expires_at", "note")
# Черновик ассистента: его собственный текст ему не возвращается, номера Telegram и опоры — тоже.
DRAFT_KEYS = ("draft_id", "status", "channel", "chat_id", "expires_at", "parts_total", "duplicate", "replayed",
              "text_changed", "cards")
DRAFT_ERROR_KEYS = ("error", "reason", "code", "retry_after")
# Отказы, текст которых неизменен и ничего из архива не несёт: отдаются как есть.
TECHNICAL_CODES = frozenset({"owner_unknown", "too_many_pending", "assistant_stopped", "archive_closed",
                             "own_bot", "hermes_forbidden"})
# Отказы «такого нет / уже не в том состоянии / имя занято»: по ним перебором узнавали бы, что есть в архиве и
# памяти (а в тексте «имя занято» стоит название чужого проекта). Токену Hermes — один одинаковый ответ.
DATA_CODES = frozenset({"not_found", "bad_chat", "exists", "name_taken", "bad_status", "changed_meanwhile", "frozen",
                        "bad_person", "approval_required"})
OWNER_UNKNOWN_TEXT = ("Это действие нужно подтвердить в боте согласований, а бота согласований у сервиса нет или "
                      "владелец к нему не привязан: подтвердить некому. Скажите владельцу.")
TOO_MANY_PENDING_TEXT = ("Слишком много действий уже ждут подтверждения владельца в боте согласований. Не повторяйте "
                         "запрос: скажите владельцу, чтобы ответил на карточки.")
# Отказы черновика «чата нет», «чат исключён владельцем», «служебный чат» — для ассистента одно и то же: по разнице
# он узнавал бы, какие чаты владелец исключил.
DRAFT_CHAT_REASONS = frozenset({"chat_not_found", "chat_excluded", "chat_blocked"})
DRAFT_CHAT_TEXT = "Такого чата нет в архиве или писать в него нельзя."
UNAVAILABLE_TEXT = ("Не сделано: такого нет, это недоступно ассистенту или уже в нужном состоянии. Не перебирайте "
                    "номера и названия; если это нужно владельцу — пусть сделает сам в приложении «Штурман».")
BAD_REQUEST_TEXT = "Запрос не принят: проверьте поля по описанию инструмента."


def ask_answer(path: str, status: int, data: object) -> tuple[int, dict]:
    """Что из ответа маршрута вида ASK получает токен Hermes: (код, тело). Только технический итог."""
    body = data if isinstance(data, dict) else {}
    if status >= 500:
        return status, {"error": "внутренняя ошибка сервиса", "code": "internal"}
    if path == DRAFTS_PATH:
        if status >= 400 and body.get("reason") in DRAFT_CHAT_REASONS:
            return 404, {"error": DRAFT_CHAT_TEXT, "reason": "chat_not_found"}
        keys = DRAFT_KEYS if status < 400 else DRAFT_ERROR_KEYS
        return status, {k: body[k] for k in keys if k in body and not isinstance(body[k], (dict, list))}
    if status == 202 and body.get("status") == "pending_confirmation":
        return 202, {k: body.get(k) for k in PENDING_KEYS}
    if status < 400:
        # Токену ассистента просьба сразу не применяется никогда (`api_core.settle`), так что 200 — это «ничего не
        # изменилось». Его отличие от «нет такого» сказало бы, что запись есть и в каком она состоянии: ответ тот же.
        return 404, {"error": UNAVAILABLE_TEXT, "code": "not_found"}
    code = body.get("code") if isinstance(body.get("code"), str) else None
    error = body.get("error") if isinstance(body.get("error"), str) else ""
    if code in TECHNICAL_CODES:
        return status, {"error": error, "code": code}
    if status == 404 or code in DATA_CODES:
        return 404, {"error": UNAVAILABLE_TEXT, "code": "not_found"}
    # Ошибки самого запроса (поле не того вида, слишком длинно): их тексты — о запросе, а не об архиве. Текст с
    # кавычками-«ёлочками» — это название из архива или памяти: такой не отдаётся.
    if not error or "«" in error or "»" in error:
        error = BAD_REQUEST_TEXT
    out = {"error": error}
    if code:
        out["code"] = code
    return status, out
