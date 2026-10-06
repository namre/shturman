"""Имя, характер и представление ассистента.

Выбор владельца хранится в состоянии плагина, а в файл личности Hermes (SOUL.md) записывается
отдельный помеченный блок. Всё, что владелец или Hermes написали в SOUL.md вне блока, не трогаем.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .state import Store

BLOCK_START = "<!-- shturman:persona:start -->"
BLOCK_END = "<!-- shturman:persona:end -->"
CUSTOM = "custom"
_CATALOG_PATH = Path(__file__).resolve().parent.parent / "personas.json"

_TONE_NOTE = {
    "off": "Характер не изображай: только имя, речь нейтральная.",
    "light": "Характер выражай интонацией, без словечек и обращений.",
    "full": "Характер выражай полностью, включая свойственные ему обращения.",
}


def catalog() -> dict[str, Any]:
    return json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))


def _clean(value: Any, limit: int) -> str:
    """Одна строка без управляющих и невидимых символов и без угловых скобок.

    Без «<» и «>» из введённого нельзя собрать маркер блока, как бы его ни вкладывали.
    """
    text = "".join(ch for ch in str(value or "") if ch.isprintable() or ch.isspace())
    text = text.replace("<", "").replace(">", "")
    text = "".join(ch for ch in text if ch not in "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff")
    return " ".join(text.split())[:limit].strip()


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Приводит выбор владельца к проверенному виду. Неизвестное заменяется значением по умолчанию."""
    cat = catalog()
    ids = {p["id"] for p in cat["personas"]}
    persona = raw.get("persona")
    if persona not in ids and persona != CUSTOM:
        persona = cat["default"]
    intro_ids = {i["id"] for i in cat["intros"]}
    intro = raw.get("intro")
    if intro not in intro_ids and intro != CUSTOM:
        intro = cat["default_intro"]
    tone = raw.get("tone")
    if tone not in cat["tones"]:
        tone = cat["default_tone"]
    return {
        "persona": persona,
        "custom_name": _clean(raw.get("custom_name"), 40) if persona == CUSTOM else "",
        "custom_voice": _clean(raw.get("custom_voice"), 600) if persona == CUSTOM else "",
        "tone": tone,
        "owner_address": _clean(raw.get("owner_address"), 80),
        "intro": intro,
        "custom_intro": _clean(raw.get("custom_intro"), 60) if intro == CUSTOM else "",
        "owner_genitive": _clean(raw.get("owner_genitive"), 80),
    }


def resolved(choice: dict[str, Any]) -> dict[str, str]:
    """Имя, манера речи и подпись в готовом виде."""
    cat = catalog()
    choice = normalize(choice)
    if choice["persona"] == CUSTOM:
        name = choice["custom_name"] or "Ассистент"
        voice = choice["custom_voice"] or "Говорит коротко и по делу."
    else:
        entry = next(p for p in cat["personas"] if p["id"] == choice["persona"])
        name, voice = entry["name"], entry["voice"]
    if choice["intro"] == CUSTOM:
        intro = choice["custom_intro"] or "помощник"
    else:
        intro = next(i["text"] for i in cat["intros"] if i["id"] == choice["intro"])
    signature = f"{intro} {choice['owner_genitive']}".strip()
    return {
        "name": name,
        "voice": voice,
        "tone": choice["tone"],
        "owner_address": choice["owner_address"],
        "intro": intro,
        "signature": signature,
    }


def soul_block(choice: dict[str, Any]) -> str:
    r = resolved(choice)
    lines = [
        BLOCK_START,
        "## Имя и характер",
        "",
        "Этот раздел важнее всего, что сказано выше об имени и манере речи.",
        "",
        f"Тебя зовут {r['name']}. Ты личный ассистент одного человека — владельца этого сервера.",
    ]
    if r["owner_address"]:
        lines.append(f"К владельцу обращайся так: {r['owner_address']}.")
    lines += [
        "",
        f"Манера речи в разговоре с владельцем: {r['voice']}",
        _TONE_NOTE[r["tone"]],
        "",
        "Границы характера:",
        "- Характер действует только в разговоре с владельцем. Черновики ответов другим людям "
        "пиши голосом владельца, без своих оборотов.",
        "- Характер не меняет содержания: факты, сроки и плохие новости сообщай прямо.",
        f"- Когда пишешь другим людям от своего имени, а не от имени владельца, представляйся "
        f"так: «{r['signature']}». Имя персонажа им не называй.",
        BLOCK_END,
    ]
    return "\n".join(lines)


def apply_to_soul(existing: str, choice: dict[str, Any]) -> str:
    """Вставляет или заменяет блок характера в тексте SOUL.md."""
    block = soul_block(choice)
    existing = existing or ""
    start = existing.find(BLOCK_START)
    end = existing.rfind(BLOCK_END)
    if start != -1 and end != -1 and end > start:
        return existing[:start] + block + existing[end + len(BLOCK_END):]
    # Блок ставится в конец: в файле личности более позднее указание уточняет более раннее,
    # а стоковый текст Hermes начинается с собственного имени агента.
    body = existing.strip("\n")
    return f"{body}\n\n{block}\n" if body else f"{block}\n"


def save_choice(store: Store, raw: dict[str, Any]) -> dict[str, Any]:
    choice = normalize(raw)
    with store.locked("wizard") as data:
        data["persona"] = choice
    return choice


def load_choice(store: Store) -> dict[str, Any]:
    return normalize(store.read("wizard").get("persona") or {})
