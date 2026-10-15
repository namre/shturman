"""Настройки разбора входящих и правила владельца (решение Р-90).

Строка `triage.settings` в таблице `settings`. Строки нет — действуют умолчания: разбор **выключен**.

  enabled            включён ли разбор. Включение — ослабление (переписка уходит провайдеру модели):
                     только нажатием владельца в боте согласований (`triage.enable`); выключение — сразу;
  remind_minutes     через сколько минут рабочего времени напомнить о вопросе без ответа (по умолчанию
                     120); рабочее время — вне тихих часов уведомлений (notify.settings);
  important_people   «важные люди» (числовые ID Telegram): порог ниже — о всём, что ждёт вашего ответа,
                     сразу; «срочно» от них — даже в тихие часы;
  quiet_people       «тихие люди»: не тревожить никогда — только напоминание о вопросе без ответа;
  lower_people       «реже беспокоить» — так их отмечает кнопка «Не важно»: сразу — только срочное;
  important_chats, quiet_chats, lower_chats — то же для групп (номера чатов архива, chats.id);
  important_spheres  «важные сферы» (Р-82): чаты этих сфер — как важные люди.

Человек или группа стоит не больше чем в одном перечне: новая отметка снимает прежнюю.

Правила и срок меняет только владелец: на странице «Настройки Штурмана» (сразу) и кнопкой «Не важно»
под карточкой в боте (сразу — это сужение). Внутреннего API для них нет: ассистент и дашборд Hermes ни
включить разбор, ни поменять правила не могут.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

SETTINGS_KEY = "triage.settings"

REMIND_CHOICES = (30, 60, 120, 180, 240, 360, 480)
MAX_IDS = 300
LEVELS = ("important", "lower", "quiet")
PEOPLE = {"important": "important_people", "lower": "lower_people", "quiet": "quiet_people"}
CHATS = {"important": "important_chats", "lower": "lower_chats", "quiet": "quiet_chats"}

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "remind_minutes": 120,
    "important_people": [],
    "lower_people": [],
    "quiet_people": [],
    "important_chats": [],
    "lower_chats": [],
    "quiet_chats": [],
    "important_spheres": [],
}
FIELDS = frozenset(DEFAULTS)
LISTS = frozenset(FIELDS - {"enabled", "remind_minutes"})
# Что владелец меняет на странице (всё, кроме включения: оно — нажатием в боте).
PAGE_FIELDS = FIELDS - {"enabled"}


def _ids(value: Any, field: str) -> list[int]:
    if not isinstance(value, list) or len(value) > MAX_IDS or any(
            isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in value):
        raise ValueError(f"поле {field}: нужен список положительных номеров, не больше {MAX_IDS}")
    return sorted(set(value))


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
        elif key == "remind_minutes":
            if value not in REMIND_CHOICES or isinstance(value, bool):
                raise ValueError("поле remind_minutes: " + ", ".join(str(m) for m in REMIND_CHOICES) + " минут")
        else:
            value = _ids(value, key)
        out[key] = value
    for group in (PEOPLE, CHATS):
        seen: dict[int, str] = {}
        for field in group.values():
            for i in out.get(field, []):
                if i in seen:
                    raise ValueError(f"№ {i} отмечен дважды — в «{seen[i]}» и «{field}»: выберите одно")
                seen[i] = field
    return out


def merge(before: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    """Новые настройки поверх прежних. Новая отметка человека или группы снимает прежнюю."""
    after = {**before, **changes}
    for group in (PEOPLE, CHATS):
        for field in group.values():
            if field not in changes:
                continue
            added = set(changes[field]) - set(before.get(field, []))
            for other in group.values():
                if other != field and other not in changes:
                    after[other] = [i for i in after[other] if i not in added]
    return after


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
    # пересечения перечней (испорченная запись): строже — тише
    for group in (PEOPLE, CHATS):
        taken: set[int] = set()
        for level in ("quiet", "lower", "important"):
            field = group[level]
            out[field] = [i for i in out[field] if i not in taken]
            taken |= set(out[field])
    return out


async def load(conn: Any) -> dict[str, Any]:
    return normalize(await conn.fetchval("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY))


async def save(conn: Any, value: Mapping[str, Any]) -> None:
    await conn.execute(
        """INSERT INTO settings (key, value) VALUES ($1, $2::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        SETTINGS_KEY, json.dumps(dict(value), ensure_ascii=False, sort_keys=True))


async def lock(conn: Any) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "shturman.settings:triage")


def hours(minutes: int) -> str:
    """«2 ч», «30 мин», «1 ч 30 мин»."""
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} ч {m} мин"
    return f"{h} ч" if h else f"{m} мин"


def audit_detail(s: Mapping[str, Any], via: str) -> str:
    """Для журнала действий: только признаки и числа, без номеров и имён людей."""
    return (f"{'включён' if s['enabled'] else 'выключен'}; напоминание через {s['remind_minutes']} мин; "
            f"людей: важных {len(s['important_people'])}, реже {len(s['lower_people'])}, тихих {len(s['quiet_people'])}; "
            f"групп: важных {len(s['important_chats'])}, реже {len(s['lower_chats'])}, тихих {len(s['quiet_chats'])}; "
            f"важных сфер: {len(s['important_spheres'])}; через: {via}")
