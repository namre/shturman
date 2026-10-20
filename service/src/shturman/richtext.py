"""Оформление сообщений владельцу без разметки в тексте: цитата, свёрнутая цитата, моноширинный блок (Р-103).

Сообщения бота согласований уходят обычным текстом (`executor/botapi.py`): в них есть чужой текст, и
разметка в самом тексте (HTML, Markdown) позволила бы чужому сообщению подделать вид карточки. Поэтому
оформление задаётся отдельно — списком `entities` Bot API (вид, начало и длина в единицах UTF-16), который
собирает только код сервиса. Чужой текст здесь — всегда содержимое, никогда не разметка: какие бы знаки
в нём ни были, оформление не меняется.

Виды (`MessageEntity.type`, Bot API 10.3, сверено по core.telegram.org/bots/api 2026-10-19):
  blockquote, expandable_blockquote — цитата и свёрнутая цитата (свёрнутая — с Bot API 7.3);
  pre, code — моноширинный блок и строка: нажатие в Telegram копирует текст.

Задание `notify.owner` несёт их в поле `entities`. Исполнитель сервиса передаёт их Telegram, исполнитель
плагина в Hermes это поле не читает — тогда то же сообщение приходит обычным текстом, ничего не теряя.
"""

from __future__ import annotations

from typing import Any, Iterable


def utf16_len(text: str) -> int:
    """Длина так, как её считает Telegram: в единицах UTF-16 (эмодзи — две)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2

KINDS = frozenset({"blockquote", "expandable_blockquote", "pre", "code", "bold", "italic"})
MAX_ENTITIES = 20


class Rich:
    """Текст сообщения и его оформление: строки добавляются по порядку, у каждого оформленного куска —
    свой вид. Пустой кусок не оформляется (Telegram отверг бы оформление нулевой длины)."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._len = 0
        self.entities: list[dict[str, Any]] = []

    def add(self, text: str) -> "Rich":
        self._parts.append(text)
        self._len += utf16_len(text)
        return self

    def line(self, text: str = "") -> "Rich":
        return self.add(text + "\n")

    def lines(self, items: Iterable[str]) -> "Rich":
        for item in items:
            self.line(item)
        return self

    def span(self, text: str, kind: str, *, newline: bool = True) -> "Rich":
        if kind not in KINDS:
            raise ValueError(f"неизвестный вид оформления: {kind}")
        if text:
            self.entities.append({"type": kind, "offset": self._len, "length": utf16_len(text)})
        self.add(text)
        if newline:
            self.add("\n")
        return self

    @property
    def size(self) -> int:
        return self._len

    def build(self) -> tuple[str, list[dict[str, Any]]]:
        """Текст без хвостовых пустых строк и оформление, которое в нём помещается."""
        text = "".join(self._parts).rstrip()
        return text, clip(text, self.entities)


def clip(text: str, entities: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Оформление, которое помещается в этот текст (после обрезки до предела Telegram): что вышло за
    конец — укорачивается, что началось за концом — выбрасывается."""
    total = utf16_len(text)
    out = []
    for e in entities or ():
        offset, length = int(e["offset"]), int(e["length"])
        if offset >= total or length <= 0:
            continue
        out.append({"type": e["type"], "offset": offset, "length": min(length, total - offset)})
    return out[:MAX_ENTITIES]


def valid(text: str, entities: Any) -> list[dict[str, Any]] | None:
    """Проверка поля `entities` задания исполнителем: только известные виды, целые числа, внутри текста.
    None — поле задано неверно."""
    if entities is None:
        return []
    if not isinstance(entities, list) or len(entities) > MAX_ENTITIES:
        return None
    total = utf16_len(text)
    out = []
    for e in entities:
        if not isinstance(e, dict) or e.get("type") not in KINDS:
            return None
        offset, length = e.get("offset"), e.get("length")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (offset, length)):
            return None
        if offset < 0 or length <= 0 or offset + length > total:
            return None
        out.append({"type": e["type"], "offset": offset, "length": length})
    return out
