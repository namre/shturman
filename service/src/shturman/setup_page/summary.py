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
    running = EXTRAS_KEY in state.extras
    reason = config.setup_reason
    return {
        # Страницей можно пользоваться. false — модуль не запущен либо её внешний адрес совпал
        # с адресом дашборда Hermes (reason = same_origin): тогда снаружи она не отдаётся вовсе.
        "enabled": running and reason != "same_origin",
        # Страницу можно открыть по внешнему адресу, а не только с сервера и через туннель.
        "origin_set": bool(config.setup_external),
        # Внешний адрес страницы (схема, имя, порт) — не секрет; null, если снаружи её нет.
        "origin": config.setup_external or None,
        # Почему внешнего адреса нет: null | "no_origin" (не задан) | "same_origin" (совпал с дашбордом).
        "reason": reason,
        "tg_keys": bool(config.tg_api_id and config.tg_api_hash),
        "accounts": int(row["accounts"]),
        "own_bot": bool(config.own_bot),
        "owner_bound": bool(owner_bound),
        "business_connected": bool(row["business"]),
        "own_model": bool(config.own_llm),
    }
