"""Мост к Hermes: что сервис просит сделать плагин и как разбирает ответы.

Плагин умеет ровно пять вещей (виды заданий) и ничего не знает о том, зачем они нужны:

  llm.structured  {instructions, input, json_schema, schema_name, task, max_tokens}
                  -> {parsed: {...} | null, text, model}
  llm.text        {messages: [{role, content}], task, max_tokens}
                  -> {text, model}
  notify.owner    {text, buttons: [[{text, data}]] | null, silent: bool}
                  -> {message_id}
  notify.edit     {message_id, text, remove_buttons: bool}
                  -> {}
  business.send   {business_connection_id, chat_id, text, reply_to_message_id | null}
                  -> {message_id}

Про business.send: если Telegram точно отказал (сообщение не ушло), исполнитель сообщает
неудачу с текстом, начинающимся на «not_sent:». Любая другая неудача значит «исход неизвестен»:
сообщение могло уйти, и повторять отправку без владельца нельзя.

И одно умение в обратную сторону: нажатие кнопки под сообщением владельцу плагин пересылает
в сервис как есть (`data` кнопки и идентификатор нажавшего), сервис решает, что это значит.

Модули сервиса регистрируют разбор результата (`on_result`) и разбор нажатий (`on_callback`).
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import asyncpg

from . import jobs

LLM_STRUCTURED = "llm.structured"
LLM_TEXT = "llm.text"
NOTIFY_OWNER = "notify.owner"
NOTIFY_EDIT = "notify.edit"
BUSINESS_SEND = "business.send"
EXECUTOR_KINDS = (LLM_STRUCTURED, LLM_TEXT, NOTIFY_OWNER, NOTIFY_EDIT, BUSINESS_SEND)
NOT_SENT_PREFIX = "not_sent:"

# Все кнопки сервиса начинаются с этого префикса: он не пересекается с префиксами ядра Hermes
# (ea: sc: cl: cp: mp: mm: mc: gt: update_prompt:) и плагина бизнес-режима (bd:).
CALLBACK_PREFIX = "sh:"
CALLBACK_DATA_LIMIT = 64  # байт — предел Telegram
MESSAGE_LIMIT = 4096      # предел сообщения Telegram — в единицах UTF-16, а не в знаках


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def fit_message(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """Обрезает текст до предела Telegram (эмодзи и редкие знаки считаются за два)."""
    if utf16_len(text) <= limit:
        return text
    cut = text[: limit - 1]
    while utf16_len(cut) > limit - 1:
        cut = cut[:-1]
    return cut + "…"

ResultHandler = Callable[[asyncpg.Connection, dict[str, Any], dict[str, Any]], Awaitable[None]]
FailureHandler = Callable[[asyncpg.Connection, dict[str, Any], str], Awaitable[None]]
CallbackHandler = Callable[[asyncpg.Connection, str, int], Awaitable[dict[str, Any]]]

_result_handlers: dict[str, ResultHandler] = {}
_failure_handlers: dict[str, FailureHandler] = {}
_callback_handlers: dict[str, CallbackHandler] = {}


def on_result(handler_name: str) -> Callable[[ResultHandler], ResultHandler]:
    """Регистрирует разбор результата: fn(conn, job, result). Вызывается в транзакции закрытия задания."""
    def deco(fn: ResultHandler) -> ResultHandler:
        _result_handlers[handler_name] = fn
        return fn
    return deco


def on_failure(handler_name: str) -> Callable[[FailureHandler], FailureHandler]:
    """Регистрирует реакцию на окончательную неудачу задания: fn(conn, job, error)."""
    def deco(fn: FailureHandler) -> FailureHandler:
        _failure_handlers[handler_name] = fn
        return fn
    return deco


def on_callback(module: str) -> Callable[[CallbackHandler], CallbackHandler]:
    """Регистрирует разбор нажатий кнопок с данными вида `sh:<module>:<остальное>`.

    fn(conn, остальное, tg_id нажавшего) -> {"answer": "короткий текст-подсказка",
                                              "edit_text": "новый текст сообщения" | None,
                                              "remove_buttons": bool}
    Нажатие приходит только от владельца: это проверено до вызова.
    """
    def deco(fn: CallbackHandler) -> CallbackHandler:
        _callback_handlers[module] = fn
        return fn
    return deco


def callback_data(module: str, rest: str) -> str:
    data = f"{CALLBACK_PREFIX}{module}:{rest}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:
        raise ValueError(f"данные кнопки длиннее {CALLBACK_DATA_LIMIT} байт: {data!r}")
    return data


def button(text: str, module: str, rest: str) -> dict[str, str]:
    return {"text": text, "data": callback_data(module, rest)}


# --- владелец ---

OwnerChangeHandler = Callable[[asyncpg.Connection, int], Awaitable[None]]
_owner_change_handlers: list[OwnerChangeHandler] = []


def on_owner_change(fn: OwnerChangeHandler) -> OwnerChangeHandler:
    """Регистрирует реакцию на смену или сброс владельца: fn(conn, новый user_id или 0)."""
    _owner_change_handlers.append(fn)
    return fn


async def _owner_changed(conn: asyncpg.Connection, new_user_id: int) -> None:
    # Бизнес-подключения прежнего владельца перестают принимать сообщения и отправлять.
    await conn.execute(
        """UPDATE business_connections SET enabled = false, updated_at = now()
           WHERE account_id IN (SELECT id FROM accounts WHERE tg_user_id <> $1)""",
        new_user_id,
    )
    for fn in _owner_change_handlers:
        await fn(conn, new_user_id)


async def clear_owner(conn: asyncpg.Connection) -> None:
    """Владелец отвязан (ссылка восстановления): кнопки никто не нажмёт, отправки останавливаются."""
    async with conn.transaction():
        removed = await conn.fetchval("DELETE FROM settings WHERE key = 'owner' RETURNING key")
        if removed:
            await _owner_changed(conn, 0)


async def set_owner(conn: asyncpg.Connection, user_id: int, chat_id: int) -> None:
    async with conn.transaction():
        previous = await get_owner(conn)
        await _set_owner(conn, user_id, chat_id)
        if previous is not None and int(previous["user_id"]) != int(user_id):
            await _owner_changed(conn, int(user_id))
        if previous is None or int(previous["user_id"]) != int(user_id):
            # Владелец привязан (заново): его собственные бизнес-подключения снова принимают
            # сообщения. Если Telegram подключение отключил, отправка через него всё равно откажет.
            await conn.execute(
                """UPDATE business_connections SET enabled = true, updated_at = now()
                   WHERE NOT enabled AND account_id IN (SELECT id FROM accounts WHERE tg_user_id = $1)""",
                int(user_id),
            )


async def _set_owner(conn: asyncpg.Connection, user_id: int, chat_id: int) -> None:
    await conn.execute(
        """INSERT INTO settings (key, value) VALUES ('owner', $1::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        json.dumps({"user_id": int(user_id), "chat_id": int(chat_id)}),
    )


async def get_owner(conn: asyncpg.Connection) -> dict[str, int] | None:
    raw = await conn.fetchval("SELECT value FROM settings WHERE key = 'owner'")
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


# --- постановка заданий ---

async def notify_owner(
    conn: asyncpg.Connection, text: str, *, buttons: list[list[dict[str, str]]] | None = None,
    silent: bool = False, dedup_key: str | None = None, handler: str | None = None,
    context: dict[str, Any] | None = None,
) -> int | None:
    """Сообщение владельцу в управляющий чат. Текст — без разметки, не длиннее 4096 знаков."""
    return await jobs.enqueue(
        conn, NOTIFY_OWNER, {"text": fit_message(text), "buttons": buttons, "silent": silent},
        handler=handler, context=context, dedup_key=dedup_key,
    )


async def edit_owner_message(
    conn: asyncpg.Connection, message_id: int, text: str, *, remove_buttons: bool = True,
) -> int | None:
    """Меняет текст ранее отправленного владельцу сообщения (например, карточку устаревшего черновика)."""
    return await jobs.enqueue(
        conn, NOTIFY_EDIT,
        {"message_id": int(message_id), "text": fit_message(text), "remove_buttons": remove_buttons},
        max_attempts=2,
    )


async def request_structured(
    conn: asyncpg.Connection, *, handler: str, instructions: str, input: str,
    json_schema: dict[str, Any], schema_name: str, task: str = "shturman_extract",
    max_tokens: int = 2000, context: dict[str, Any] | None = None, dedup_key: str | None = None,
) -> int | None:
    """Просит модель вернуть JSON по схеме. Результат придёт в `on_result(handler)`."""
    return await jobs.enqueue(
        conn, LLM_STRUCTURED,
        {"instructions": instructions, "input": input, "json_schema": json_schema,
         "schema_name": schema_name, "task": task, "max_tokens": max_tokens},
        handler=handler, context=context, dedup_key=dedup_key,
    )


async def request_text(
    conn: asyncpg.Connection, *, handler: str, messages: list[dict[str, str]],
    task: str = "shturman_reply", max_tokens: int = 1500,
    context: dict[str, Any] | None = None, dedup_key: str | None = None,
) -> int | None:
    return await jobs.enqueue(
        conn, LLM_TEXT, {"messages": messages, "task": task, "max_tokens": max_tokens},
        handler=handler, context=context, dedup_key=dedup_key,
    )


async def request_business_send(
    conn: asyncpg.Connection, *, handler: str, business_connection_id: str, chat_id: int,
    text: str, reply_to_message_id: int | None = None,
    context: dict[str, Any] | None = None, dedup_key: str | None = None,
) -> int | None:
    """Отправка от имени владельца через бизнес-бота. Ставится только после согласования."""
    return await jobs.enqueue(
        conn, BUSINESS_SEND,
        {"business_connection_id": business_connection_id, "chat_id": int(chat_id),
         "text": text, "reply_to_message_id": reply_to_message_id},
        handler=handler, context=context, dedup_key=dedup_key, max_attempts=1,
    )


# --- разбор ответов ---

async def deliver_result(conn: asyncpg.Connection, job_id: int, result: dict[str, Any]) -> bool:
    """Закрывает задание и передаёт результат модулю-владельцу. Одна транзакция."""
    async with conn.transaction():
        job = await jobs.complete(conn, job_id, result)
        if job is None:
            return False
        fn = _result_handlers.get(job["handler"] or "")
        if fn is not None:
            await fn(conn, job, result)
    return True


async def deliver_failure(conn: asyncpg.Connection, job_id: int, error: str, *, retry_in: int | None) -> str:
    async with conn.transaction():
        status = await jobs.fail(conn, job_id, error, retry_in=retry_in)
        if status == "failed":
            job = await jobs.get(conn, job_id)
            fn = _failure_handlers.get((job or {}).get("handler") or "")
            if fn is not None and job is not None:
                await fn(conn, job, error)
    return status


async def reap_lost(conn: asyncpg.Connection) -> int:
    """Закрывает задания, на которые исполнитель так и не ответил, и сообщает модулям-владельцам."""
    async with conn.transaction():
        lost = await jobs.reap(conn) + await jobs.expire_queued(conn)
        for job in lost:
            fn = _failure_handlers.get(job.get("handler") or "")
            if fn is not None:
                await fn(conn, job, job.get("error") or "исполнитель не ответил")
    return len(lost)


async def dispatch_callback(conn: asyncpg.Connection, data: str, from_user_id: int) -> dict[str, Any]:
    """Разбирает нажатие кнопки. Чужие нажатия и неизвестные кнопки отклоняются без подробностей."""
    refused = {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
    if not data.startswith(CALLBACK_PREFIX):
        return refused
    owner = await get_owner(conn)
    if owner is None or int(from_user_id) != int(owner["user_id"]):
        return refused
    module, _, rest = data[len(CALLBACK_PREFIX):].partition(":")
    fn = _callback_handlers.get(module)
    if fn is None:
        return refused
    async with conn.transaction():
        out = await fn(conn, rest, int(from_user_id))
    return {"answer": str(out.get("answer") or "")[:190], "edit_text": out.get("edit_text"),
            "remove_buttons": bool(out.get("remove_buttons"))}
