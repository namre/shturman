"""Измерение договорённостей «было / стало» (Р-96): только агрегаты, без текста переписки.

Что считается:
  * сколько найдено по статусам: предложено (ждёт), принято, отклонено, снято без владельца, «В команде»,
    закрыто — и отдельно по версии инструкции модели (`prompt_version`: до 4 — прежние правила);
  * снятое без владельца — по причинам (`quiet_reason`): сделано или отменено до показа, до решения,
    заменено перепроверкой, без ответа, «В команде» давно;
  * «Не так» — по причинам, которые назвал владелец (журнал `commitment_events.details.reason`);
  * доля причин срока (`due_reason`): дата определена, быстрое обещание, событие, не разобрано…;
  * доля «между другими» (others) и быстрых обещаний (immediate);
  * сколько времени от обещания до показа владельцу (среднее и медиана, часы) — по сводке в боте и
    по записи в базу (с этого момента предложение видно в приложении).

Берут: `shturman commitments stats` (в контейнере сервиса), `./ops/commitments.sh stats`,
`service/tools/commitments_stats.py` (база разработчика) и строка `commitments` в ./ops/doctor.sh.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from .extract import PROMPT_VERSION


def _share(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


async def collect(conn: asyncpg.Connection) -> dict[str, Any]:
    total = int(await conn.fetchval("SELECT count(*) FROM commitments"))
    by_status = {r["status"]: int(r["n"]) for r in await conn.fetch(
        "SELECT status, count(*) AS n FROM commitments GROUP BY status")}
    by_version: dict[str, dict[str, int]] = {}
    for r in await conn.fetch(
            """SELECT CASE WHEN prompt_version = $1 THEN $1 ELSE 'before' END AS v, status, count(*) AS n
               FROM commitments GROUP BY 1, 2""", PROMPT_VERSION):
        by_version.setdefault(r["v"], {})[r["status"]] = int(r["n"])
    quiet = {r["quiet_reason"]: int(r["n"]) for r in await conn.fetch(
        "SELECT quiet_reason, count(*) AS n FROM commitments WHERE quiet_reason IS NOT NULL GROUP BY 1")}
    wrong = {r["reason"]: int(r["n"]) for r in await conn.fetch(
        """SELECT details->>'reason' AS reason, count(DISTINCT commitment_id) AS n FROM commitment_events
           WHERE action IN ('rejected', 'marked_wrong', 'wrong_reason') AND details ? 'reason' GROUP BY 1""")}
    due = {r["due_reason"]: int(r["n"]) for r in await conn.fetch(
        "SELECT due_reason, count(*) AS n FROM commitments GROUP BY 1")}
    direction = {r["direction"]: int(r["n"]) for r in await conn.fetch(
        "SELECT direction, count(*) AS n FROM commitments GROUP BY 1")}
    decided = {r["action"]: int(r["n"]) for r in await conn.fetch(
        """SELECT action, count(DISTINCT commitment_id) AS n FROM commitment_events
           WHERE actor = 'owner' AND action IN ('accepted', 'rejected', 'marked_wrong', 'hidden', 'closed')
           GROUP BY 1""")}
    lag = await conn.fetchrow(
        """SELECT avg(extract(epoch FROM c.notified_at - m.sent_at)) / 3600 AS shown_avg,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM c.notified_at - m.sent_at)) / 3600
                      AS shown_median,
                  avg(extract(epoch FROM c.created_at - m.sent_at)) / 3600 AS found_avg,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM c.created_at - m.sent_at)) / 3600
                      AS found_median,
                  count(c.notified_at) AS shown
           FROM commitments c JOIN messages m ON m.id = c.source_message_id""")

    def hours(value: Any) -> float | None:
        return round(float(value), 1) if value is not None else None

    return {
        "total": total, "prompt_version": PROMPT_VERSION,
        "by_status": by_status, "by_version": by_version,
        "quiet_reasons": quiet, "wrong_reasons": wrong,
        "owner_decisions": decided,
        "due_reasons": due, "due_shares": {k: _share(v, total) for k, v in due.items()},
        "directions": direction,
        "others_share": _share(direction.get("others", 0), total),
        "immediate_share": _share(due.get("immediate", 0), total),
        "hours_promise_to_digest": {"avg": hours(lag["shown_avg"]), "median": hours(lag["shown_median"]),
                                    "items": int(lag["shown"])},
        "hours_promise_to_found": {"avg": hours(lag["found_avg"]), "median": hours(lag["found_median"])},
    }


STATUS_WORDS = {"proposed": "ждут решения", "open": "приняты", "done": "выполнены", "cancelled": "отменены",
                "rejected": "отклонены или скрыты", "expired": "сняты без владельца", "team": "«В команде»"}
QUIET_WORDS = {"done_before_shown": "сделано до показа", "cancelled_before_shown": "отменено до показа",
               "done_before_decision": "сделано до решения", "cancelled_before_decision": "отменено до решения",
               "recheck": "заменено перепроверкой", "unanswered": "без ответа 7 дней",
               "team_done": "«В команде»: сделано", "team_cancelled": "«В команде»: отменено",
               "team_stale": "«В команде»: старше 14 дней"}


def render(stats: dict[str, Any]) -> str:
    """Те же числа словами — для терминала."""
    def line(title: str, data: dict[str, int], words: dict[str, str] | None = None) -> str:
        items = sorted(data.items(), key=lambda kv: -kv[1])
        return f"{title}: " + (", ".join(f"{(words or {}).get(k, k)} — {v}" for k, v in items) or "нет")

    out = [f"Договорённостей всего: {stats['total']} (инструкция модели — версия {stats['prompt_version']})",
           line("По статусам", stats["by_status"], STATUS_WORDS)]
    for version, data in sorted(stats["by_version"].items()):
        title = "найдены прежними правилами" if version == "before" else f"найдены правилами версии {version}"
        out.append(line(f"  {title}", data, STATUS_WORDS))
    out += [line("Сняты без владельца, почему", stats["quiet_reasons"], QUIET_WORDS),
            line("«Не так», причины владельца", stats["wrong_reasons"]),
            line("Решения владельца", stats["owner_decisions"]),
            line("Срок", stats["due_reasons"]),
            f"Между другими людьми: {stats['others_share']:.0%}; быстрые обещания: {stats['immediate_share']:.0%}"]
    shown, found = stats["hours_promise_to_digest"], stats["hours_promise_to_found"]
    if shown["avg"] is not None:
        out.append(f"От обещания до сводки в боте: в среднем {shown['avg']} ч, медиана {shown['median']} ч "
                   f"(показано пунктов: {shown['items']})")
    if found["avg"] is not None:
        out.append(f"От обещания до записи (видно в приложении): в среднем {found['avg']} ч, медиана {found['median']} ч")
    return "\n".join(out)


async def status_fields(conn: asyncpg.Connection) -> dict[str, int]:
    """Числа для `GET /api/status` и строки `commitments` в ./ops/doctor.sh."""
    row = await conn.fetchrow(
        """SELECT count(*) FILTER (WHERE status = 'proposed') AS proposed,
                  count(*) FILTER (WHERE status = 'open') AS open,
                  count(*) FILTER (WHERE status = 'team') AS team,
                  count(*) FILTER (WHERE status = 'expired' AND quiet_reason IS DISTINCT FROM 'unanswered') AS quiet,
                  count(*) FILTER (WHERE status = 'rejected') AS rejected
           FROM commitments""")
    return {f"commitments_{k}": int(v) for k, v in dict(row).items()}
