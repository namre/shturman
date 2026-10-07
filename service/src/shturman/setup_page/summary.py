"""Признаки готовности для `GET /api/status` (объект `setup`) — по ним мастер в Hermes и
`ops/doctor.sh` показывают, что уже настроено на странице сервиса.

Только признаки и числа: ни имён, ни идентификаторов, ни значений. Внутренний API доступен
ассистенту в Hermes, поэтому сюда не попадает ничего, чего он не должен знать.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from ..executor import binding

EXTRAS_KEY = "setup_page"     # под этим ключом работающий модуль страницы лежит в state.extras


async def overview(conn: asyncpg.Connection, state: Any) -> dict[str, Any]:
    config = state.config
    runtime = state.extras.get("executor")
    bot = getattr(runtime, "bot", None)
    owner_bound = bot is not None and await binding.bound_owner(conn, bot.bot_id) is not None
    row = await conn.fetchrow(
        """SELECT (SELECT count(*) FROM tg_sessions) AS accounts,
                  (SELECT EXISTS (SELECT 1 FROM business_connections
                                  WHERE via = 'service' AND enabled)) AS business""")
    return {
        # страница включена в этот сервис (модуль запущен)
        "enabled": EXTRAS_KEY in state.extras,
        # задан внешний адрес: страницу можно открыть не только с сервера и через туннель
        "origin_set": bool(config.setup_origin),
        "tg_keys": bool(config.tg_api_id and config.tg_api_hash),
        "accounts": int(row["accounts"]),
        "own_bot": bool(config.own_bot),
        "owner_bound": bool(owner_bound),
        "business_connected": bool(row["business"]),
        "own_model": bool(config.own_llm),
    }
