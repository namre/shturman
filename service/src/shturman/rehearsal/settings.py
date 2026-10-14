"""Настройки репетиции ответов: умолчания, проверка, что расширяет и что сужает (решение Р-85).

Хранятся строкой `rehearsal.settings` в таблице `settings`. Строки нет — действуют умолчания:
репетиция **выключена**, людей в списке нет.

Список людей — свой, не список доверенных автоответа (`outbox_trusted`, команда `/trust`). Доверенный
автоответа — это «сервис отвечает ему сам, без вашего согласования», как только где-то включён
автоответ и отправка. Человек в списке репетиции — только «показывайте мне, как ассистент ответил бы».
Общий список смешал бы два разных решения: добавив человека в репетицию, владелец незаметно для себя
разрешил бы ему и настоящий автоответ. Перенести людей из одного списка в другой — отдельное решение
владельца на следующем шаге (выбор способа доставки).

Что считается расширением, решается по значению: включить репетицию; новый человек в списке; выше
дневной предел; короче пауза между репетициями. Сужение — обратное. Со страницы и командой бота —
сразу (это сам владелец); через внутренний API расширение ждёт нажатия в боте согласований
(`rehearsal.settings`), сужение — сразу, с сообщением владельцу.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

SETTINGS_KEY = "rehearsal.settings"
MAX_PEOPLE = 100
DAILY_CAP = (1, 300)          # карточек репетиции в сутки
PAUSE = (0, 3600)             # секунд между двумя репетициями

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "people": [],             # числовые ID собеседников в Telegram
    "daily_cap": 30,
    "pause_seconds": 5,
}
FIELDS = frozenset(DEFAULTS)


def _int(value: Any, field: str, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not bounds[0] <= value <= bounds[1]:
        raise ValueError(f"поле {field}: целое число от {bounds[0]} до {bounds[1]}")
    return value


def validate(data: Mapping[str, Any]) -> dict[str, Any]:
    """Проверяет изменения. Незнакомое поле или неверное значение — ValueError с объяснением."""
    if not isinstance(data, Mapping):
        raise ValueError("нужен объект с настройками")
    unknown = set(data) - FIELDS
    if unknown:
        raise ValueError("нет таких настроек: " + ", ".join(sorted(unknown)))
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "enabled":
            if not isinstance(value, bool):
                raise ValueError("поле enabled: true или false")
        elif key == "people":
            if not isinstance(value, list) or len(value) > MAX_PEOPLE or any(
                    isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in value):
                raise ValueError(f"поле people: список числовых ID в Telegram (не имён), не больше {MAX_PEOPLE}")
            value = sorted(set(value))
        elif key == "daily_cap":
            value = _int(value, key, DAILY_CAP)
        elif key == "pause_seconds":
            value = _int(value, key, PAUSE)
        out[key] = value
    return out


def normalize(raw: Any) -> dict[str, Any]:
    """Сохранённые настройки поверх умолчаний. Испорченное значение заменяется умолчанием."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    out = dict(DEFAULTS)
    if isinstance(raw, Mapping):
        for key in FIELDS & set(raw):
            try:
                out.update(validate({key: raw[key]}))
            except ValueError:
                pass
    return out


async def load(conn: Any) -> dict[str, Any]:
    return normalize(await conn.fetchval("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY))


async def save(conn: Any, value: Mapping[str, Any]) -> None:
    await conn.execute(
        """INSERT INTO settings (key, value) VALUES ($1, $2::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        SETTINGS_KEY, json.dumps(dict(value), ensure_ascii=False, sort_keys=True))


def widening(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Что в новых настройках расширяет репетицию по сравнению с прежними: коды полей (пусто — нет)."""
    out: list[str] = []
    if after["enabled"] and not before["enabled"]:
        out.append("enabled")
    if set(after["people"]) - set(before["people"]):
        out.append("people")
    if after["daily_cap"] > before["daily_cap"]:
        out.append("daily_cap")
    if after["pause_seconds"] < before["pause_seconds"]:
        out.append("pause_seconds")
    return out


def split(before: Mapping[str, Any], changes: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Изменения → (сужения, расширения): сужения применяются сразу, расширения ждут владельца."""
    tighter: dict[str, Any] = {}
    looser: dict[str, Any] = {}
    for key, value in changes.items():
        if value == before[key]:
            continue
        probe = {**before, key: value}
        (looser if widening(before, probe) else tighter)[key] = value
    return tighter, looser


WIDEN_WORDS = {"enabled": "включить репетицию ответов", "people": "новые люди в списке репетиции",
               "daily_cap": "больше карточек в сутки", "pause_seconds": "короче пауза между репетициями"}


def describe(s: Mapping[str, Any]) -> list[str]:
    """Настройки строками простыми словами — для карточки в боте и сообщения владельцу.
    Имён людей здесь нет: только их число."""
    return [
        "• репетиция ответов: " + ("включена" if s["enabled"] else "выключена"),
        f"• людей в списке: {len(s['people'])}",
        f"• не больше {s['daily_cap']} карточек в сутки, пауза между ними — {s['pause_seconds']} с",
        "• ничего не отправляется: карточка с предложенным ответом приходит только вам",
    ]


def audit_detail(s: Mapping[str, Any], via: str | None = None) -> str:
    """Подробности для журнала действий: только признаки и числа, без имён и номеров людей."""
    parts = [f"включена: {'да' if s['enabled'] else 'нет'}", f"людей: {len(s['people'])}",
             f"в сутки: {s['daily_cap']}", f"пауза: {s['pause_seconds']} с"]
    if via:
        parts.append(f"через: {via}")
    return "; ".join(parts)
