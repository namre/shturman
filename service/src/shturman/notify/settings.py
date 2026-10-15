"""Настройки уведомлений владельцу: умолчания, проверка, что расширяет и что сужает (решение Р-84).

Хранятся строкой `notify.settings` в таблице `settings`. Строки нет — действуют умолчания
(`DEFAULTS`): после обновления уведомления включены — личные «от всех» и прямые обращения в группах,
«сразу», не чаще раза на чат за 10 минут, тихие часы 23:00–08:00.

Что считается расширением, решается по значению, а не по имени поля (как у правил отправки):
  * больше сообщений попадает в уведомления — личные: «выкл» → люди/сферы → «от всех»; новые люди
    или сферы в перечне; включить прямые обращения в группах; новое обращение по имени;
  * в уведомлении появляется текст сообщения — «показывать начало сообщения».
Сужение — обратное. Время и частота (сразу или пачкой, через сколько минут, тихие часы) — не про то,
что и о ком уходит владельцу, а про то, когда: такие правки ни расширением, ни сужением не считаются
и применяются сразу.

Через внутренний API (ассистент) расширение ждёт нажатия владельца в боте согласований, сужение и
время — сразу. Со страницы настройки — сразу. В приложении «Штурман» — только сужение и время:
войти в приложение может и держатель токена бота согласований (Р-80), а уведомления приходят
в чат этого бота.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from ..outbox import direct_address

SETTINGS_KEY = "notify.settings"

PERSONAL = ("off", "people", "spheres", "all")
MODES = ("instant", "digest")
DIGEST_MINUTES = (15, 30, 60)
GAP_RANGE = (1, 240)            # «сразу»: не чаще раза на чат за N минут
MAX_PEOPLE = 200
MAX_SPHERES = 50
MAX_MUTED = 500
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

DEFAULTS: dict[str, Any] = {
    "personal": "all",          # off | people | spheres | all
    "people": [],               # числовые ID собеседников (personal = people)
    "spheres": [],              # номера сфер (personal = spheres)
    "groups": True,             # прямые обращения в группах
    "aliases": [],              # как к вам обращаются в начале реплики: «Евгений, …»
    "preview": True,            # показывать начало сообщения
    "mode": "instant",          # instant — сразу; digest — пачкой
    "gap_minutes": 10,
    "digest_minutes": 30,
    "quiet": True,
    "quiet_from": "23:00",
    "quiet_to": "08:00",
    "muted_chats": [],          # чаты, о которых не сообщать (номера chats.id): «Не сообщать об этом чате»
}
FIELDS = frozenset(DEFAULTS)
# Время и частота: не расширение и не сужение (см. шапку).
TIMING = frozenset({"mode", "gap_minutes", "digest_minutes", "quiet", "quiet_from", "quiet_to"})


def _ids(value: Any, field: str, limit: int) -> list[int]:
    if not isinstance(value, list) or len(value) > limit or any(
            isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in value):
        raise ValueError(f"поле {field}: нужен список положительных номеров, не больше {limit}")
    return sorted(set(value))


def validate(data: Mapping[str, Any]) -> dict[str, Any]:
    """Проверяет изменения настроек. Незнакомое поле или неверное значение — ValueError с объяснением."""
    if not isinstance(data, Mapping):
        raise ValueError("нужен объект с настройками")
    unknown = set(data) - FIELDS
    if unknown:
        raise ValueError("нет таких настроек: " + ", ".join(sorted(unknown)))
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "personal":
            if value not in PERSONAL:
                raise ValueError("поле personal: off, people, spheres или all")
        elif key == "people":
            value = _ids(value, key, MAX_PEOPLE)
        elif key == "spheres":
            value = _ids(value, key, MAX_SPHERES)
        elif key == "muted_chats":
            value = _ids(value, key, MAX_MUTED)
        elif key in ("groups", "preview", "quiet"):
            if not isinstance(value, bool):
                raise ValueError(f"поле {key}: true или false")
        elif key == "aliases":
            try:
                value = direct_address.validate_aliases(value)
            except ValueError as exc:
                raise ValueError(str(exc).replace("aliases:", "поле aliases:")) from None
        elif key == "mode":
            if value not in MODES:
                raise ValueError("поле mode: instant или digest")
        elif key == "gap_minutes":
            if isinstance(value, bool) or not isinstance(value, int) or not GAP_RANGE[0] <= value <= GAP_RANGE[1]:
                raise ValueError(f"поле gap_minutes: целое число минут от {GAP_RANGE[0]} до {GAP_RANGE[1]}")
        elif key == "digest_minutes":
            if value not in DIGEST_MINUTES:
                raise ValueError("поле digest_minutes: 15, 30 или 60")
        elif key in ("quiet_from", "quiet_to"):
            if not isinstance(value, str) or not _TIME.match(value):
                raise ValueError(f"поле {key}: время вида 23:00")
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


async def stored(conn: Any) -> bool:
    """Записал ли владелец (или ассистент с его согласия) настройки хоть раз."""
    return bool(await conn.fetchval("SELECT 1 FROM settings WHERE key = $1", SETTINGS_KEY))


async def save(conn: Any, value: Mapping[str, Any]) -> None:
    await conn.execute(
        """INSERT INTO settings (key, value) VALUES ($1, $2::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        SETTINGS_KEY, json.dumps(dict(value), ensure_ascii=False, sort_keys=True))


def _audience(s: Mapping[str, Any]) -> tuple[int, frozenset]:
    """Кого охватывают личные уведомления: ступень и перечень (для людей и сфер)."""
    rank = {"off": 0, "people": 1, "spheres": 1, "all": 2}[s["personal"]]
    items = frozenset(s["people"]) if s["personal"] == "people" else \
        frozenset(s["spheres"]) if s["personal"] == "spheres" else frozenset()
    return rank, items


def widening(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Что в новых настройках расширяет уведомления по сравнению с прежними: коды полей (пусто — нет)."""
    out: list[str] = []
    (r0, i0), (r1, i1) = _audience(before), _audience(after)
    if r1 > r0 or (r1 == r0 == 1 and (before["personal"] != after["personal"] or i1 - i0)):
        out.append("personal")
    if after["groups"] and not before["groups"]:
        out.append("groups")
    if {a.casefold() for a in after["aliases"]} - {a.casefold() for a in before["aliases"]}:
        out.append("aliases")
    if after["preview"] and not before["preview"]:
        out.append("preview")
    if set(before["muted_chats"]) - set(after["muted_chats"]):
        out.append("muted")          # вернуть чат в уведомления — расширение
    return out


def narrows(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Сужает ли правка (что-то стало уже), не расширяя ничего."""
    if widening(before, after):
        return False
    return before != after and any(before[k] != after[k] for k in FIELDS - TIMING)


PERSONAL_WORDS = {"off": "выключены", "people": "только от выбранных людей", "spheres": "только из выбранных сфер",
                  "all": "от всех"}


def describe(s: Mapping[str, Any], *, sphere_titles: Mapping[int, str] | None = None) -> list[str]:
    """Настройки строками простыми словами — для карточки в боте и сообщения из приложения.
    Имён людей здесь нет: только их число."""
    lines = [f"• личные сообщения: {PERSONAL_WORDS[s['personal']]}"]
    if s["personal"] == "people":
        lines[-1] += f" ({len(s['people'])})"
    elif s["personal"] == "spheres":
        names = [f"«{sphere_titles.get(i, f'№ {i}')}»" for i in s["spheres"]] if sphere_titles is not None \
            else [f"№ {i}" for i in s["spheres"]]
        lines[-1] += ": " + (", ".join(names) or "сферы не выбраны")
    lines.append("• прямые обращения в группах: " + ("да" if s["groups"] else "нет"))
    if s["aliases"]:
        lines.append("• обращения по имени: " + ", ".join(f"«{a}»" for a in s["aliases"]))
    lines.append("• начало сообщения в уведомлении: " + ("показывать" if s["preview"] else "не показывать"))
    lines.append("• как присылать: " + (f"сразу, не чаще раза на чат за {s['gap_minutes']} мин"
                                         if s["mode"] == "instant" else f"пачкой раз в {s['digest_minutes']} мин"))
    lines.append("• тихие часы: " + (f"{s['quiet_from']}–{s['quiet_to']}" if s["quiet"] else "нет"))
    if s["muted_chats"]:
        lines.append(f"• не сообщать о чатах: {len(s['muted_chats'])}")
    return lines


def audit_detail(s: Mapping[str, Any]) -> str:
    """Подробности для журнала действий: только коды и числа, без имён и названий."""
    parts = [f"личные: {s['personal']}"]
    if s["personal"] == "people":
        parts.append(f"людей: {len(s['people'])}")
    if s["personal"] == "spheres":
        parts.append("сферы № " + ",".join(str(i) for i in s["spheres"]))
    parts += [f"группы: {'да' if s['groups'] else 'нет'}", f"обращений: {len(s['aliases'])}",
              f"текст: {'да' if s['preview'] else 'нет'}",
              f"{s['mode']} {s['gap_minutes'] if s['mode'] == 'instant' else s['digest_minutes']} мин",
              f"тихие: {s['quiet_from']}–{s['quiet_to']}" if s["quiet"] else "тихие: нет"]
    if s["muted_chats"]:
        parts.append(f"без уведомлений чатов: {len(s['muted_chats'])}")
    return "; ".join(parts)
