"""Защита на настоящей переписке — только числа (Р-99): `shturman guard stats`, `./ops/guard.sh stats`.

Зачем. Все числа в `docs/guard.md` сняты на синтетике. Как защита ведёт себя на переписке владельца,
видно только по ней самой — и по его решениям: доля «Показать ассистенту» среди решённых ПОШТУЧНО и
есть доля ложных срабатываний. Решённое разом (сводкой) считается отдельно: владелец не смотрел
каждое сообщение, и «показать всё» не говорит, что каждое было обычным.

Что считается (без текстов, имён и названий чатов — только счётчики):
  * проверено, скрыто (всего, на 1000 проверенных), ждут решения, не проверено;
  * причина: кто скрыл (модель, правила, оба), корзина оценки модели, коды правил — нынешними
    правилами (`rules.NAME`), то есть «что сказали бы правила сейчас»;
  * тип чата (личный, группа, канал публичный и закрытый), пересланное, живой поток или история;
  * решения владельца: показано, оставлено скрытым — поштучно и сводкой; доля «показано» среди
    решённых поштучно — по каждой причине;
  * «если бы порог был X»: сколько скрыла бы модель (и модель вместе с нынешними правилами), из них
    показано владельцем. Только по сообщениям, у которых оценка модели сохранена: у скрытых одними
    правилами она не записывалась — они видны отдельной строкой;
  * варианты Р-99 на решение владельца: не скрывать публичные каналы, скрывать только сильными
    правилами, только нынешние правила без модели.

Текст сообщений читается внутри процесса — чтобы прогнать по нему нынешние правила — и никуда не
выводится. Порог меняет только владелец (`./ops/guard.sh threshold`); здесь — только расчёт.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import asyncpg

from . import alerts, rules

BUCKETS = ((0.99, "≥0,99"), (0.95, "0,95–0,99"), (0.9, "0,9–0,95"), (0.0, "<0,9"))
THRESHOLDS = (0.9, 0.95, 0.97, 0.99, 0.995, 0.999)
CHAT_KINDS = {"personal_chat": "личный", "bot_chat": "личный (бот)", "private_group": "группа",
              "private_supergroup": "группа", "public_supergroup": "публичная группа",
              "private_channel": "закрытый канал", "public_channel": "публичный канал"}
DECISIONS = ("shown", "kept", "bulk_shown", "bulk_kept", "waiting")

_ROWS = f"""
SELECT m.text, m.guard_label AS label, m.guard_model AS model, m.guard_score AS score, c.type AS chat_type,
       (COALESCE(m.is_forwarded, false) OR m.forwarded_from IS NOT NULL) AS forwarded,
       {alerts.CARD_WORTHY} AS live,
       CASE WHEN m.guard_label IN ('released', 'confirmed')
            THEN (SELECT a.via FROM guard_alerts a WHERE a.text_hash = md5(m.text)) END AS via
FROM messages m JOIN chats c ON c.id = m.chat_id
WHERE m.guard_label IS NOT NULL AND m.is_outgoing IS NOT TRUE AND m.kind = 'message' AND m.text <> ''
  AND m.deleted_at IS NULL
"""


def _source(model: str | None) -> str:
    """Кто скрыл: model, rules, both; owner — по прежнему решению владельца о таком же тексте."""
    value = model or ""
    if value == "owner":
        return "owner"
    if value.startswith("rules-"):
        return "rules"
    return "both" if "+rules-" in value else "model"


def _bucket(score: float | None) -> str | None:
    if score is None:
        return None
    return next(name for low, name in BUCKETS if score >= low)


def _decision(label: str, via: str | None) -> str:
    if label == "suspect":
        return "waiting"
    bulk = via == "bulk"
    if label == "released":
        return "bulk_shown" if bulk else "shown"
    return "bulk_kept" if bulk else "kept"


def _cell() -> dict[str, int]:
    return {"hidden": 0, **{d: 0 for d in DECISIONS}}


def _put(cell: dict[str, int], decision: str) -> None:
    cell["hidden"] += 1
    cell[decision] = cell.get(decision, 0) + 1


def share_shown(cell: dict[str, int]) -> float | None:
    """Доля «показать» среди решённых поштучно — оценка ложных срабатываний."""
    decided = cell["shown"] + cell["kept"]
    return round(cell["shown"] / decided, 3) if decided else None


async def collect(conn: asyncpg.Connection, *, threshold: float, card_hours: int = alerts.DEFAULT_CARD_HOURS,
                  batch: int = 2000) -> dict[str, Any]:
    labels: dict[str, int] = defaultdict(int)
    by: dict[str, dict[str, dict[str, int]]] = {k: defaultdict(_cell) for k in (
        "source", "bucket", "rule", "chat", "forwarded", "stream")}
    checked_by_chat: dict[str, int] = defaultdict(int)
    total = _cell()
    remembered = 0
    # «если бы порог был X»: оценки модели (сохранены у тех, где модель решала или ответила «обычное»)
    scored: list[tuple[float, bool, str]] = []   # (оценка, нынешние правила, решение)
    rules_only_unknown = _cell()                 # скрыты одними правилами: оценка модели не сохранена
    variants = {"no_public_channels": _cell(), "strong_rules_only": _cell(), "rules_only": _cell()}

    async with conn.transaction():
        async for row in conn.cursor(_ROWS, card_hours, prefetch=batch):
            label, source = row["label"], _source(row["model"])
            labels[label] += 1
            chat = CHAT_KINDS.get(row["chat_type"], row["chat_type"])
            checked_by_chat[chat] += 1
            fired = rules.explain(row["text"] or "")
            now_rules = rules.score_one(row["text"] or "") >= rules.THRESHOLD
            strong = any(r.weight >= rules.HIGH_WEIGHT for r in fired)
            decision = _decision(label, row["via"]) if label != "ok" else None
            flagged = label in ("suspect", "released", "confirmed") and source != "owner"
            if source == "owner":
                remembered += 1
            if source in ("model", "both") and row["score"] is not None:
                scored.append((float(row["score"]), now_rules, decision or "ok"))
            if flagged and source == "rules":
                _put(rules_only_unknown, decision)
            if flagged:
                _put(total, decision)
                _put(by["source"][source], decision)
                if source in ("model", "both"):
                    _put(by["bucket"][_bucket(row["score"]) or "—"], decision)
                for rule in fired:
                    _put(by["rule"][rule.code], decision)
                if not now_rules:
                    _put(by["rule"]["— нынешние правила не скрыли бы"], decision)
                _put(by["chat"][chat], decision)
                _put(by["forwarded"]["пересланное" if row["forwarded"] else "не пересланное"], decision)
                _put(by["stream"]["живой поток" if row["live"] else "история"], decision)
                if row["chat_type"] != "public_channel":
                    _put(variants["no_public_channels"], decision)
            # Варианты «только правила» считаются по всем проверенным: нынешние правила могли бы
            # скрыть и то, что модель сочла обычным.
            if flagged or label == "ok":
                if strong:
                    _put(variants["strong_rules_only"], decision or "ok_now")
                if now_rules:
                    _put(variants["rules_only"], decision or "ok_now")

    checked = sum(labels.values())
    unchecked = int(await conn.fetchval(
        """SELECT count(*) FROM messages m WHERE m.guard_label IS NULL AND m.is_outgoing IS NOT TRUE
           AND m.kind = 'message' AND m.text <> '' AND m.deleted_at IS NULL"""))

    def per_1000(part: int, whole: int) -> float:
        return round(part * 1000 / whole, 1) if whole else 0.0

    chats = {}
    for name, n in sorted(checked_by_chat.items(), key=lambda kv: -kv[1]):
        cell = dict(by["chat"].get(name) or _cell())
        chats[name] = {"checked": n, **cell, "per_1000": per_1000(cell["hidden"], n), "shown_share": share_shown(cell)}

    def table(name: str) -> dict[str, Any]:
        return {k: {**v, "shown_share": share_shown(v)}
                for k, v in sorted(by[name].items(), key=lambda kv: -kv[1]["hidden"])}

    what_if = []
    for x in sorted(set(THRESHOLDS) | {round(threshold, 4)}):
        model_cell, both_cell = _cell(), _cell()
        for score, now_rules, decision in scored:
            if score >= x:
                _put(model_cell, decision if decision != "ok" else "ok_now")
            if score >= x or now_rules:
                _put(both_cell, decision if decision != "ok" else "ok_now")
        what_if.append({"threshold": x, "model": _clean(model_cell), "model_or_rules": _clean(both_cell),
                        "note": "оценки ниже нынешнего порога сохранены только у сообщений, которые модель "
                                "сочла обычными" if x < threshold else None})

    return {
        "rules": rules.NAME, "threshold": threshold, "card_hours": card_hours,
        "checked": checked, "unchecked": unchecked, "labels": dict(labels),
        "hidden": _clean(total), "hidden_per_1000": per_1000(total["hidden"], checked),
        "remembered": remembered, "shown_share": share_shown(total),
        "by_source": table("source"), "by_bucket": table("bucket"), "by_rule": table("rule"),
        "by_chat": chats, "by_forwarded": table("forwarded"), "by_stream": table("stream"),
        "rules_only_without_model_score": _clean(rules_only_unknown),
        "what_if_threshold": what_if,
        "variants": {k: _clean(v) for k, v in variants.items()},
    }


def _clean(cell: dict[str, int]) -> dict[str, int]:
    """Счётчики ячейки; ok_now — сообщения, которые сейчас видны («обычное» по проверке), но были бы скрыты."""
    out = {k: int(v) for k, v in cell.items()}
    out["shown_share"] = share_shown(out)
    return out


# --- вывод словами ---

WORDS = {"model": "модель", "rules": "правила", "both": "модель и правила", "owner": "по прежнему решению"}


def _line(title: str, cell: dict[str, Any]) -> str:
    share = cell.get("shown_share")
    decided = cell.get("shown", 0) + cell.get("kept", 0)
    parts = [f"скрыто {cell['hidden']}"]
    if cell.get("ok_now"):
        parts.append(f"сейчас видны, но были бы скрыты {cell['ok_now']}")
    parts.append(f"ждут {cell.get('waiting', 0)}")
    parts.append(f"поштучно: показано {cell.get('shown', 0)}, оставлено {cell.get('kept', 0)}"
                 + (f" — ложных ≈ {share:.0%} из {decided}" if share is not None else ""))
    if cell.get("bulk_shown") or cell.get("bulk_kept"):
        parts.append(f"сводкой: показано {cell.get('bulk_shown', 0)}, оставлено {cell.get('bulk_kept', 0)}")
    return f"  {title}: " + "; ".join(parts)


def render(stats: dict[str, Any]) -> str:
    out = [
        f"Защита на вашей переписке (правила {stats['rules']}, порог модели {stats['threshold']}).",
        f"Проверено входящих: {stats['checked']}; не проверено (очередь): {stats['unchecked']}.",
        f"Скрыто проверкой: {stats['hidden']['hidden']} — {stats['hidden_per_1000']} на 1000 проверенных; "
        f"ждут решения: {stats['hidden']['waiting']}; скрыто по прежнему решению о таком же тексте: "
        f"{stats['remembered']}.",
        _line("Всего", stats["hidden"]),
        "",
        "Доля «показано» среди решённых поштучно — это и есть ложные срабатывания на вашей переписке. "
        "Решённое сводкой («показать все») сюда не входит.",
        "",
        "Кто скрыл:",
        *[_line(WORDS.get(k, k), v) for k, v in stats["by_source"].items()],
        "Оценка модели:",
        *[_line(k, v) for k, v in stats["by_bucket"].items()],
        f"Правила (нынешние, {stats['rules']}; у одного сообщения может быть несколько):",
        *[_line(k, v) for k, v in stats["by_rule"].items()],
        "Тип чата (проверено → скрыто на 1000):",
        *[_line(f"{k} ({v['checked']} → {v['per_1000']})", v) for k, v in stats["by_chat"].items()],
        "Пересланное:",
        *[_line(k, v) for k, v in stats["by_forwarded"].items()],
        f"Живой поток или история (свежее — {stats['card_hours']} ч):",
        *[_line(k, v) for k, v in stats["by_stream"].items()],
        "",
        "Если бы порог модели был X (только сообщения с сохранённой оценкой модели):",
    ]
    for item in stats["what_if_threshold"]:
        mark = " ← сейчас" if abs(item["threshold"] - stats["threshold"]) < 1e-9 else ""
        out.append(_line(f"X={item['threshold']}{mark}, одна модель", item["model"]))
        out.append(_line(f"X={item['threshold']}{mark}, модель или нынешние правила", item["model_or_rules"]))
    unknown = stats["rules_only_without_model_score"]
    if unknown["hidden"]:
        out.append(f"  Скрыто одними правилами (оценка модели не сохранена, ниже порога): {unknown['hidden']}.")
    variants = stats["variants"]
    out += [
        "",
        "Варианты на решение владельца (Р-99, docs/guard.md), что было бы скрыто:",
        _line("(а) не скрывать публичные каналы", variants["no_public_channels"]),
        _line("(б) скрывать только сильными правилами, модель — второе мнение", variants["strong_rules_only"]),
        _line("(г) без модели, только нынешние правила", variants["rules_only"]),
        "",
        "Порог меняет только владелец: ./ops/guard.sh threshold ЧИСЛО (подтверждение в его терминале).",
    ]
    return "\n".join(out)
