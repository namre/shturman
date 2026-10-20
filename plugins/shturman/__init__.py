"""Плагин «Штурман» для стокового Hermes Agent.

Подключается только штатными механизмами:
  * ctx.register_dashboard_auth_provider — свой способ входа в дашборд;
  * ctx.register_telegram_handler — привязка владельца и защита от сообщений бизнес-режима;
  * dashboard/manifest.json — вкладка «Штурман» с мастером настройки;
  * ctx.register_tool, ctx.register_auxiliary_task, ctx.register_skill, ctx.llm — мост к сервису
    переписки: инструменты агента, обращения сервиса к модели, скиллы сводок.

Сервис переписки необязателен: без SHTURMAN_API_TOKEN всё, что на нём построено, молчит,
а вход, мастер и защита бизнес-режима работают как прежде.

Проверено с Hermes Agent 0.21.5 (образ v2026.9.24).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    # Общие модули импортируются под одним именем и из этого файла, и из dashboard/plugin_api.py,
    # который Hermes загружает отдельно. Иначе получились бы две копии с разным состоянием.
    sys.path.insert(0, str(_HERE))

logger = logging.getLogger("shturman")


def register(ctx) -> None:
    try:
        from shturman_provider import build_provider

        ctx.register_dashboard_auth_provider(build_provider())
    except Exception:
        # Провайдер нужен только процессу дашборда; в остальных процессах его отсутствие не ошибка.
        logger.warning("shturman: провайдер входа не зарегистрирован", exc_info=True)

    # Мост настраивается до обработчиков Telegram: фабрика запускает исполнитель заданий,
    # и к этому моменту ему нужен доступ к модели Hermes (ctx.llm).
    try:
        import shturman_bridge

        bridge = shturman_bridge.runtime()
        bridge.configure(ctx)
        on_unload = getattr(ctx, "on_unload", None)
        if callable(on_unload):
            on_unload(bridge.shutdown)
    except Exception:
        logger.warning("shturman: мост к сервису переписки не настроен", exc_info=True)

    try:
        from shturman_telegram import wire

        ctx.register_telegram_handler(wire)
    except Exception:
        logger.warning("shturman: обработчики Telegram не зарегистрированы", exc_info=True)

    try:
        import shturman_tools

        shturman_tools.register(ctx)
    except Exception:
        logger.warning("shturman: инструменты и скиллы не зарегистрированы", exc_info=True)
