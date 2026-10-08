"""Прямое обращение — числовой адресат Telegram или точное обращение по решению владельца.

Чужой пересланный текст, цитата и код не дают права отвечать. Это только детектор
кандидата: выключатель отправки, роль аккаунта и правила приватности проверяет сервис.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

MAX_ALIASES = 16
MAX_ALIAS_LENGTH = 64
_BLOCKED = frozenset({"code", "pre", "blockquote", "expandable_blockquote"})
_QUOTE_LINE = re.compile(r"(?m)^[ \t]*>[^\n]*")


def _quoted_ranges(text: str) -> list[tuple[int, int]]:
    """Quotation/code spans in UTF-16; punctuation elsewhere does not hide an address."""
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + (2 if ord(char) > 0xFFFF else 1))
    ranges = [(offsets[m.start()], offsets[m.end()]) for m in _QUOTE_LINE.finditer(text)]
    opening = {'"': '"', "'": "'", '«': '»', '“': '”', '‘': '’'}
    stack: list[str] = []
    start = 0
    i = 0
    while i < len(text):
        char = text[i]
        end = i + 1
        token = char
        if char in ("'", "’") and i > 0 and end < len(text) \
                and text[i - 1].isalnum() and text[end].isalnum():
            i = end
            continue  # Apostrophes within words are not quotation delimiters.
        if char == '`':
            while end < len(text) and text[end] == '`':
                end += 1
            token = text[i:end]
        if stack and stack[-1] in ('"', "'") and token == stack[-1]:
            backslashes = 0
            at = i - 1
            while at >= 0 and text[at] == '\\':
                backslashes += 1
                at -= 1
            if backslashes % 2:
                i = end
                continue  # An escaped inner delimiter does not end the outer quotation.
        if stack and token == stack[-1]:
            stack.pop()
            if not stack:
                ranges.append((offsets[start], offsets[end]))
        elif stack and (stack[-1].startswith('`') or stack[-1] in ('"', "'")):
            pass  # Content inside literal quotation/code cannot open a new span.
        elif char in opening or char == '`':
            if not stack:
                start = i
            stack.append(token if char == '`' else opening[char])
        elif char in '»”’' and not stack:
            # A closing delimiter without an opening one is an incomplete quotation.
            ranges.append((0, offsets[end]))
        i = end
    if stack:
        ranges.append((offsets[start], offsets[-1]))
    return ranges


def validate_aliases(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_ALIASES:
        raise ValueError("aliases: нужен список не более 16 обращений")
    out = []
    for alias in value:
        if not isinstance(alias, str) or not alias.strip() or len(alias) > MAX_ALIAS_LENGTH \
                or any(c in alias for c in '\n\r\t,;:!?`"«»“”<>'):
            raise ValueError("aliases: обращение должно быть коротким именем без разметки")
        alias = alias.strip()
        if alias.startswith("@"):
            if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", alias):
                raise ValueError("aliases: неверное имя Telegram")
        elif not re.fullmatch(r"[^\W\d_][\w -]*", alias, flags=re.UNICODE):
            raise ValueError("aliases: неверное имя")
        if alias.casefold() not in {a.casefold() for a in out}:
            out.append(alias)
    return out


def _entities(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def _range(ent: Mapping[str, Any], text: str) -> tuple[int, int] | None:
    start, length = ent.get("offset"), ent.get("length")
    if isinstance(start, bool) or isinstance(length, bool) or not isinstance(start, int) \
            or not isinstance(length, int) or start < 0 or length <= 0:
        return None
    units = text.encode("utf-16-le")
    raw = units[start * 2:(start + length) * 2]
    if len(raw) != length * 2:
        return None
    try:
        fragment = raw.decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    return (start, start + length) if fragment == ent.get("text") else None


def is_direct_address(message: Mapping[str, Any], user_ids: Iterable[int],
                      aliases: Iterable[str] = (), *, reply_sender_tg_id: int | None = None) -> bool:
    """Надёжное обращение в нормализованном сообщении; неизвестная метаинформация — отказ.

    reply_sender_tg_id передаётся только после проверки реальной исходной строки того же
    чата/темы. Имя отправителя, флаг mentioned и номер ответа сами по себе ничего не доказывают.
    """
    if message.get("is_forwarded") is not False:
        return False
    known = {uid for uid in user_ids if isinstance(uid, int) and not isinstance(uid, bool) and uid > 0}
    if reply_sender_tg_id in known:
        return True
    text = message.get("text")
    if not isinstance(text, str) or not text:
        return False
    ents = _entities(message.get("telegram_entities"))
    blocked = _quoted_ranges(text) + [span for ent in ents if ent.get("type") in _BLOCKED
                                      if (span := _range(ent, text)) is not None]
    def usable(span):
        return not any(span[0] < end and start < span[1] for start, end in blocked)
    for ent in ents:
        span = _range(ent, text)
        if span is None or not usable(span):
            continue
        uid = ent.get("user_id")
        if ent.get("type") == "mention_name" and isinstance(uid, int) and not isinstance(uid, bool) \
                and uid in known:
            return True
        if ent.get("type") == "text_link" and isinstance(ent.get("href"), str):
            match = re.fullmatch(r"tg://user\?id=([1-9][0-9]*)", ent["href"])
            if match and int(match[1]) in known:
                return True
    # Обычные имена — только в начале реплики с явным разделителем обращения.
    leading = len(text) - len(text.lstrip())
    for alias in aliases:
        suffix = r"(?=\s|[,;:!?]|$)" if alias.startswith("@") else r"(?=\s*[,;:!?])"
        match = re.match(re.escape(alias) + suffix, text[leading:], flags=re.IGNORECASE)
        if match:
            start = len(text[:leading].encode("utf-16-le")) // 2
            end = start + len(match[0].encode("utf-16-le")) // 2
            if usable((start, end)):
                return True
    return False
