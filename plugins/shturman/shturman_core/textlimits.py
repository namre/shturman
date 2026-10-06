"""Длина текста так, как её считает Telegram, и проверка ответа модели по схеме.

Telegram меряет сообщение в единицах UTF-16: знак вне основной плоскости (большинство эмодзи)
занимает две. Строка из 4096 знаков Python с эмодзи в предел Telegram уже не помещается.
"""

from __future__ import annotations

from typing import Any

TELEGRAM_TEXT_LIMIT = 4096        # единиц UTF-16 в одном сообщении


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def cut_utf16(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Обрезает текст до `limit` единиц UTF-16, не разрывая знак пополам."""
    if len(text) * 2 <= limit or utf16_len(text) <= limit:
        return text
    used, out = 0, []
    for ch in text:
        size = 2 if ord(ch) > 0xFFFF else 1
        if used + size > limit - 1:          # одна единица — под многоточие
            break
        out.append(ch)
        used += size
    return "".join(out) + "…"


_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    expected = _TYPES.get(name)
    return expected is None or isinstance(value, expected)


def matches_schema(value: Any, schema: Any, _depth: int = 0) -> bool:
    """Подходит ли значение под схему — для отметки `schema_valid` в результате задания.

    Понимает то, чем пользуются схемы сервиса: type, enum, required, properties,
    additionalProperties: false, items. Остальные слова схемы не проверяются. Решения по этой
    отметке никто не принимает: сервис проверяет ответ модели сам и терпимее.
    """
    if not isinstance(schema, dict) or _depth > 32:
        return True
    kind = schema.get("type")
    if isinstance(kind, str) and not _is_type(value, kind):
        return False
    if isinstance(kind, list) and not any(isinstance(k, str) and _is_type(value, k) for k in kind):
        return False
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        return False
    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        if any(key not in value for key in required):
            return False
        if schema.get("additionalProperties") is False and any(key not in properties for key in value):
            return False
        return all(matches_schema(value[key], sub, _depth + 1) for key, sub in properties.items() if key in value)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return all(matches_schema(item, schema["items"], _depth + 1) for item in value)
    return True
