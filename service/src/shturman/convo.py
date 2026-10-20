"""Контекст разговора в карточке черновика ответа (решение Р-103).

Владелец оценивает черновик — карточку репетиции, черновик «через бота и помощника», черновик автоответа
доверенным в режиме подготовки — и должен видеть, на что тот отвечает. Над черновиком — свёрнутая цитата
(`expandable_blockquote`, richtext.py): до `BEFORE` сообщений чата до пачки и сама пачка, по строке на
сообщение — «Вы · 14:05: …» или «Анна · 14:06: …», время по поясу владельца.

Что в цитату не попадает никогда — скрытое защитой от внедрённых инструкций: запрос берёт только
`agent_visible` (то же правило, что у справки для модели, `autoreply.gather_context`). Удалённое и служебное
тоже не берётся. Чужой текст — одной строкой, до `LINE` знаков, ссылки, @упоминания и команды обезврежены
так же, как в цитатах защиты (`guard.alerts.quote`). Вложение — пометкой: «[фото: пересказ]»,
«[голосовое: расшифровка]», если пересказ или расшифровка готовы, иначе «[фото]».

Черновик важнее контекста: цитата укладывается в то, что осталось от предела сообщения Telegram после
самого черновика (`fit`), и режется со старых сообщений; не уложилась ни одна строка — цитаты нет.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import asyncpg

from .richtext import utf16_len

BEFORE = 5                  # сообщений до пачки
LINE = 200                  # знаков на одно сообщение
QUOTE_MAX = 1500            # единиц UTF-16 на всю цитату, не больше
ANSWER = 80                 # знаков в строке «Ответ на: …»
PACK_MAX = 10               # сообщений самой пачки в цитате (последние)
HEADER = "Разговор — последние сообщения (нажмите, чтобы развернуть):"

_FIELDS = """m.id, m.chat_id, m.sent_at, m.is_outgoing, m.text, m.media_type, m.media_state, m.transcript_state,
             m.sender_name, m.topic_tg_id"""
_WHAT = {"photo": "фото", "file": "документ", "voice_message": "голосовое", "video_message": "кружок",
         "sticker": "стикер", "animation": "GIF", "video_file": "видео", "audio_file": "аудио"}
_LABEL = re.compile(r"^\[[^\]\n]{1,200}\]\s*")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def rows(conn: asyncpg.Connection, chat_id: int, *, pack_ids: Sequence[int] = (),
               anchor_id: int | None = None, until: datetime | None = None,
               before: int = BEFORE) -> list[asyncpg.Record]:
    """Сообщения для цитаты, от старых к новым: пачка (`pack_ids`, или пачка вокруг `anchor_id`) и до
    `before` сообщений перед ней; без пачки — последние сообщения чата не позже `until`. Только видимое
    ассистенту: скрытое защитой сюда не попадает."""
    if not pack_ids and anchor_id is not None:
        from . import packs
        pack = await packs.around(conn, anchor_id)
        pack_ids = pack.ids if pack is not None else [anchor_id]
    if pack_ids:
        own = await conn.fetch(
            f"""SELECT {_FIELDS} FROM messages m
                WHERE m.id = ANY($1::bigint[]) AND m.chat_id = $2 AND m.kind = 'message' AND m.deleted_at IS NULL
                  AND m.agent_visible
                ORDER BY m.sent_at, m.id""", list(pack_ids), chat_id)
        own = own[-PACK_MAX:]
        if not own:
            return []
        first = own[0]
        earlier = await conn.fetch(
            f"""SELECT {_FIELDS} FROM messages m
                WHERE m.chat_id = $1 AND m.kind = 'message' AND m.deleted_at IS NULL AND m.agent_visible
                  AND m.topic_tg_id IS NOT DISTINCT FROM $2::bigint AND (m.sent_at, m.id) < ($3, $4)
                  AND NOT (m.id = ANY($6::bigint[]))
                ORDER BY m.sent_at DESC, m.id DESC LIMIT $5""",
            chat_id, first["topic_tg_id"], first["sent_at"], first["id"], before, list(pack_ids))
        return list(reversed(earlier)) + list(own)
    latest = await conn.fetch(
        f"""SELECT {_FIELDS} FROM messages m
            WHERE m.chat_id = $1 AND m.kind = 'message' AND m.deleted_at IS NULL AND m.agent_visible
              AND m.sent_at <= $2
            ORDER BY m.sent_at DESC, m.id DESC LIMIT $3""", chat_id, until or _now(), before + 1)
    return list(reversed(latest))


def content(row: Mapping[str, Any], limit: int = LINE) -> str:
    """Что сказано в сообщении — одной обезвреженной строкой; вложение — пометкой с пересказом."""
    from .guard.alerts import quote
    raw = row["text"] or ""
    kind = row["media_type"]
    if not kind:
        return quote(raw, limit) or "(без текста)"
    what = _WHAT.get(kind, "вложение")
    done = row["transcript_state"] == "done" or row["media_state"] == "done"
    body = _LABEL.sub("", raw, count=1) if done else raw
    body = quote(body, limit)
    return f"[{what}: {body}]" if body else f"[{what}]"


def line(row: Mapping[str, Any], tz: Any, now: datetime, *, peer: str | None = None) -> str:
    from .guard.alerts import _name
    from .notify.core import _clock
    who = "Вы" if row["is_outgoing"] is True else _name(row["sender_name"], peer or "Собеседник")
    return f"{who} · {_clock(row['sent_at'], tz, now)}: {content(row)}"


def lines(found: Sequence[Mapping[str, Any]], tz: Any, now: datetime | None = None, *,
          peer: str | None = None) -> list[str]:
    now = now or _now()
    return [line(r, tz, now, peer=peer) for r in found]


def fit(items: Sequence[str], budget: int) -> list[str]:
    """Строки цитаты, которые помещаются в `budget` единиц UTF-16 вместе с заголовком: лишнее режется
    со старых. Не помещается ни одна — пусто."""
    budget = min(int(budget), QUOTE_MAX) - utf16_len(HEADER) - 1
    out = list(items)
    while out and sum(utf16_len(x) + 1 for x in out) > budget:
        out.pop(0)
    return out


def answer_line(row: Mapping[str, Any] | None) -> str | None:
    """«Ответ на: …» — первые слова сообщения собеседника, на которое готовился ответ."""
    if row is None:
        return None
    text = content(row, ANSWER)
    return f"Ответ на: «{text}»" if text else None


def add_quote(rich: Any, items: Sequence[str]) -> None:
    """Заголовок и свёрнутая цитата в сообщении (richtext.Rich). Пустой список — ничего."""
    if not items:
        return
    rich.line(HEADER)
    rich.span("\n".join(items), "expandable_blockquote")


USED = 2                    # сколько найденного в архиве назвать в «Учтено»
USED_LINE = 60


async def used(conn: asyncpg.Connection, chat_id: int, source_keys: Sequence[str] | None,
               hits: Sequence[int] | None) -> list[str]:
    """«Учтено: …» — только по тому, что модель на самом деле получила и сама назвала опорой ответа:
    её ссылки (`source_keys`), которые указывают на найденное в архиве этого чата (`hits` запроса). Обещаний
    и договорённостей в запрос ответа не подаётся — о них здесь не говорится. Скрытое защитой — нет."""
    offered = {int(h) for h in hits or () if isinstance(h, int) and not isinstance(h, bool)}
    ids: list[int] = []
    for key in source_keys or ():
        if isinstance(key, str) and key.startswith("m:") and key[2:].isdigit() and int(key[2:]) in offered:
            if int(key[2:]) not in ids:
                ids.append(int(key[2:]))
    if not ids:
        return []
    found = await conn.fetch(
        f"""SELECT {_FIELDS} FROM messages m
            WHERE m.id = ANY($1::bigint[]) AND m.chat_id = $2 AND m.deleted_at IS NULL AND m.agent_visible
            ORDER BY m.sent_at, m.id""", ids[:USED], chat_id)
    return [content(r, USED_LINE) for r in found]
