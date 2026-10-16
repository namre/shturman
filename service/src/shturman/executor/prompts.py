"""Просьба написать текст следующим сообщением в боте согласований (ForceReply).

Два случая (пакет 2 ревизии удобства):
  * «Ответить» под вопросом ассистента (П1, Б4) — ответ уходит тем же путём, что команда
    `/replytask N answer текст` (`replies.owner.answer`);
  * «Почти — поправил бы» у карточки репетиции (Б14) — свой вариант владельца сохраняется у строки
    репетиции (`rehearsal.core.save_variant`).

Ждущая просьба — одна (новая заменяет прежнюю), в `executor_state.prompt`: вид, номер, метка карточки,
номер сообщения бота с просьбой и срок. Текст принимается только так же, как команды: от привязанного
владельца, в его личном чате с ботом (`bot.py` проверяет это до разбора), и только пока просьба ждёт:
ответом на сообщение с просьбой либо следующим сообщением без ответа. Ответ на другое сообщение
бота просьбу не забирает. Команда (текст с «/») — всегда команда, просьба остаётся ждать.

Входящие сообщения переписки сюда не попадают никак: бот принимает только личный чат владельца.
Остановка ассистента (Р-87) снимает ждущую просьбу.
"""

from __future__ import annotations

import time
from typing import Any

import asyncpg

from . import binding

STATE_KEY = "prompt"
KINDS = frozenset({"reply", "variant", "alias"})   # alias — «Добавить обращение» (Р-95, owner_settings.py)
MAX_TTL = 30 * 60           # секунд — дольше просьба не ждёт, даже если срок карточки больше

# Часы. Тесты подменяют.
now = time.time


def _clean(prompt: dict[str, Any]) -> dict[str, Any] | None:
    kind, item, nonce = prompt.get("kind"), prompt.get("id"), prompt.get("nonce")
    if kind not in KINDS or isinstance(item, bool) or not isinstance(item, int) or not isinstance(nonce, str):
        return None
    until = prompt.get("until")
    until = float(until) if isinstance(until, (int, float)) and not isinstance(until, bool) else now() + MAX_TTL
    return {"kind": kind, "id": item, "nonce": nonce[:64], "until": min(until, now() + MAX_TTL)}


async def remember(conn: asyncpg.Connection, prompt: dict[str, Any], message_id: int | None) -> bool:
    """Запоминает просьбу, отправленную сообщением `message_id`. Прежняя просьба забывается."""
    clean = _clean(prompt)
    if clean is None:
        return False
    clean["message_id"] = message_id
    await binding.put_state(conn, STATE_KEY, clean)
    return True


async def clear(conn: asyncpg.Connection) -> None:
    await conn.execute("DELETE FROM executor_state WHERE key = $1", STATE_KEY)


async def take(conn: asyncpg.Connection, *, reply_to: int | None) -> dict[str, Any] | None:
    """Просьба, на которую отвечает это сообщение владельца, — и она больше не ждёт. None — сообщение
    не ответ на просьбу (просьбы нет, срок вышел или владелец ответил на другое сообщение бота)."""
    async with conn.transaction():
        row = await conn.fetchval("SELECT value FROM executor_state WHERE key = $1 FOR UPDATE", STATE_KEY)
        if row is None:
            return None
        import json
        prompt = json.loads(row) if isinstance(row, str) else row
        if not isinstance(prompt, dict) or _clean(prompt) is None:
            await clear(conn)
            return None
        if reply_to is not None and reply_to != prompt.get("message_id"):
            return None
        await clear(conn)
        if float(prompt.get("until") or 0) <= now():
            return {**prompt, "expired": True}
        return prompt
