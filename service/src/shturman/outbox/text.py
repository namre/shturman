"""Работа с текстом в шлюзе отправки: чистка, показ владельцу, вставка в промпт, разрезание.

Три разных задачи — три разных функции:
  clean_outgoing  — текст, который уйдёт собеседнику. Чистится один раз при создании
                    черновика, и дальше хранится, показывается и отправляется одна и та же строка;
  one_line        — чужая строка (имя чата, отрывок сообщения) в сообщении владельцу;
  for_prompt      — чужой текст внутри запроса к модели.
"""

# normalize_text и content_hash основаны на Luan-X/hermes-telegram-business (MIT),
# screening.py@6d50b89 — добавлены «ё → е» и расширен набор невидимых символов.

from __future__ import annotations

import hashlib
import re
import unicodedata

# Соединители, без которых ломаются составные эмодзи и письмо некоторых языков.
# Порядок чтения они не меняют, поэтому остаются.
_KEEP_FORMAT = frozenset({"‌", "‍"})
_BLANK_RUN = re.compile(r"\n{3,}")
_SPACE_RUN = re.compile(r"\s+")
_ZERO_WIDTH = re.compile(r"[­​-‏‪-‮⁠-⁩﻿]")

TELEGRAM_LIMIT = 4096   # знаков в одном сообщении Telegram
SPLIT_LIMIT = 3500      # длиннее — режем по абзацам


def utf16_len(text: str) -> int:
    """Длина так, как её считает Telegram: в единицах UTF-16 (эмодзи — две)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _harmless(ch: str) -> bool:
    cat = unicodedata.category(ch)
    if cat in ("Cc", "Cs"):
        return False
    if cat == "Cf" and ch not in _KEEP_FORMAT:
        return False  # невидимые и меняющие направление письма
    return True


def clean_outgoing(text: str) -> str:
    """Приводит текст к виду, в котором он будет и показан владельцу, и отправлен.

    Убирает управляющие, невидимые и меняющие направление письма символы, приводит переводы
    строк к одному виду, схлопывает пустые строки (не больше одной подряд).
    """
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", "\n").replace(" ", "\n\n")
    out = []
    for ch in text:
        if ch == "\n":
            out.append(ch)
        elif ch == "\t":
            out.append(" ")
        elif _harmless(ch):
            out.append(ch)
    lines = [line.rstrip() for line in "".join(out).split("\n")]
    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()


def one_line(text: str | None, limit: int = 80) -> str:
    """Чужая строка для показа владельцу: одна строка, без невидимых символов, с пределом длины."""
    chars = []
    for ch in str(text or ""):
        cat = unicodedata.category(ch)
        chars.append(" " if cat in ("Cc", "Cf", "Cs", "Zl", "Zp") else ch)
    value = _SPACE_RUN.sub(" ", "".join(chars)).strip()
    return value if len(value) <= limit else value[: max(1, limit - 1)].rstrip() + "…"


def for_prompt(text: str | None, limit: int = 2000) -> str:
    """Чужой текст для вставки в запрос к модели: без невидимых символов, с пределом длины."""
    value = clean_outgoing(text or "")
    return value if len(value) <= limit else value[:limit].rstrip() + " […обрезано]"


def normalize_text(text: str) -> str:
    """Вид текста для сравнения: без регистра, «ё» как «е», без невидимых символов и лишних пробелов."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = _ZERO_WIDTH.sub("", value).lower().replace("ё", "е")
    return _SPACE_RUN.sub(" ", value).strip()


def content_hash(text: str) -> str:
    """Отпечаток текста: одинаков для текстов, которые отличаются только регистром и пробелами."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def _cut(text: str, limit: int) -> int:
    """Место разреза: конец абзаца, иначе строки, иначе слова, иначе — по пределу длины."""
    end = len(text)
    # предел задан в единицах UTF-16; подбираем соответствующую границу в знаках
    if utf16_len(text) > limit:
        end = limit
        while utf16_len(text[:end]) > limit:
            end -= 1
    window = text[:end]
    for sep in ("\n\n", "\n", " "):
        pos = window.rfind(sep)
        if pos > end // 4:
            return pos
    return end


def split_text(text: str, limit: int = SPLIT_LIMIT) -> list[str]:
    """Режет длинный текст на части не длиннее `limit`. Ничего не выбрасывает, кроме пробелов на стыках."""
    parts: list[str] = []
    rest = text.strip()
    while rest and utf16_len(rest) > limit:
        pos = _cut(rest, limit)
        head, rest = rest[:pos].rstrip(), rest[pos:].lstrip()
        if head:
            parts.append(head)
    if rest:
        parts.append(rest)
    return parts
