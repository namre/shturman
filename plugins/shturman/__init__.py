"""Плагин «Штурман» для стокового Hermes Agent.

Подключается только штатными механизмами:
  * ctx.register_dashboard_auth_provider — свой способ входа в дашборд;
  * ctx.register_telegram_handler — привязка владельца и защита от сообщений бизнес-режима;
  * dashboard/manifest.json — вкладка «Штурман» с мастером настройки.

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

    try:
        from shturman_telegram import wire

        ctx.register_telegram_handler(wire)
    except Exception:
        logger.warning("shturman: обработчики Telegram не зарегистрированы", exc_info=True)
