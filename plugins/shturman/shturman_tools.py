"""Что плагин добавляет агенту Hermes: инструменты, вспомогательные задачи модели и скиллы.

Логика инструментов — в `shturman_core/tools.py`; здесь только регистрация штатными вызовами
Hermes 0.21.5 (hermes_cli/plugins.py): `register_tool` (:456), `register_auxiliary_task` (:863),
`register_skill` (:993).

Инструменты видны агенту, только когда сервис переписки подключён (`check_fn`).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from shturman_core import service_client, service_routes, tools
from shturman_core.executor import AUX_TASKS

logger = logging.getLogger("shturman.tools")

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
SKILLS: dict[str, str] = {
    "morning-brief": "Утренняя сводка владельцу по архиву переписки: сроки на сегодня, просроченное, "
                     "неотвеченное со вчерашнего вечера, предложения, ждущие решения.",
    "weekly-review": "Обзор недели владельцу по архиву переписки: что закрыто, что открыто и просрочено, "
                     "что ждёт ответа, что впереди.",
}
EMOJI = {
    "shturman_draft_message": "✍️",
    "shturman_commitments": "📋",
    "shturman_commitment_update": "✅",
    "shturman_people": "👤",
}


def _client() -> service_client.ServiceClient | None:
    try:
        return service_client.ServiceClient.from_env(service_routes.TOOLS)
    except ValueError:
        return None


def _handler(name: str):
    def handle(args: dict, **kwargs: Any) -> str:
        return json.dumps(tools.run(name, _client(), args), ensure_ascii=False)

    handle.__name__ = f"handle_{name}"
    return handle


def register_tools(ctx: Any) -> list[str]:
    done = []
    for name, schema in tools.SCHEMAS.items():
        ctx.register_tool(
            name=name, toolset=tools.TOOLSETS[name], schema=schema, handler=_handler(name),
            check_fn=service_client.configured, description=schema["description"], emoji=EMOJI.get(name, ""),
        )
        done.append(name)
    return done


def register_auxiliary_tasks(ctx: Any) -> list[str]:
    done = []
    for key, (display_name, description) in AUX_TASKS.items():
        # timeout — запас для длинных разборов; модель и провайдера владелец выбирает в настройках Hermes.
        ctx.register_auxiliary_task(key, display_name=display_name, description=description,
                                    defaults={"timeout": 90})
        done.append(key)
    return done


def register_skills(ctx: Any) -> list[str]:
    done = []
    for name, description in SKILLS.items():
        ctx.register_skill(name, SKILLS_DIR / name / "SKILL.md", description=description)
        done.append(name)
    return done


def register(ctx: Any) -> None:
    """Каждая часть регистрируется отдельно: сбой одной не должен лишать остальных."""
    for label, step in (("вспомогательные задачи", register_auxiliary_tasks),
                        ("инструменты", register_tools), ("скиллы", register_skills)):
        try:
            step(ctx)
        except Exception:
            logger.warning("shturman: %s не зарегистрированы", label, exc_info=True)
