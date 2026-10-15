"""Признаки готовности для `GET /api/status` (объект `setup`) — по ним мастер в Hermes и
`ops/doctor.sh` показывают, что уже настроено на странице сервиса.

Только признаки и числа: ни имён, ни идентификаторов, ни значений. Внутренний API доступен
ассистенту в Hermes, поэтому сюда не попадает ничего, чего он не должен знать.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from .. import listen
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
    # Вход по коду из бота (Р-88): включён ли и сколько входов за 7 суток — без кодов и адресов.
    from . import auth, code_login
    code = await code_login.setting(conn, bot.bot_id if bot is not None else None)
    logins = await auth.logins_by_code(conn)
    # С Hermes вход по коду — только через сокет обратного прокси (service._via_proxy): без сокета
    # снаружи он не работает.
    no_socket = bool(config.dashboard_origin) and listen.state() != "listening"
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
        # Unix-сокет для обратного прокси (listen.py, Р-76): код состояния — listening, off,
        # dir_missing, dir_owner, dir_mode, not_socket, error. Пути и имён здесь нет.
        "socket": listen.state(),
        # Вход по коду: enabled — можно ли войти (бот, владелец и сам выключатель); reason — почему нет:
        # managed (бот от посредника, по умолчанию выключено), off (выключил владелец), no_bot.
        "code_login": {"enabled": bool(code["enabled"] and owner_bound and not no_socket),
                       "reason": ("no_bot" if code["reason"] == "no_bot" else "no_owner" if not owner_bound
                                  else code["reason"] or ("no_socket" if no_socket else None)),
                       "logins_7d": logins},
    }
