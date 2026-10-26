"""Проверки памяти для `GET /api/status` и строки `memory` в `./ops/doctor.sh` (Р-111).

Только числа — ни текстов фактов, ни имён. Два вида:

* **ссылки** (`memory_broken`) — должно быть 0: «сменено» указывает на недействующий факт или факт другого
  субъекта или ключа; факты об объединённом человеке или об отклонённом проекте; у одного ключа с одним уточнением открыто несколько значений; статус проекта без уточнения
  всё ещё в цепочке. Не 0 — WARN: это ошибка кода или ручная правка базы;
* **на разбор владельцу** — признаки того, что в память попало не то. Ошибкой они не являются:
  * `memory_paraphrases` — пары действующих фактов одного субъекта с почти одинаковым текстом;
  * `memory_project_outside` — действующие факты проекта из чатов, не привязанных к проекту;
  * `memory_empty_projects` — действующие проекты без фактов, решений, обязательств и чатов;
  * `memory_quotes_changed` — цитата-основание не находится в тексте сообщения дословно (сообщение изменено
    после записи либо совпадало только после выравнивания букв и знаков: ё, кавычки, тире);
  * `memory_stale` — положение дел старше `facts.OBSERVATION_DAYS` дней: оно уже только в хронологии.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from . import facts

# Похожесть текстов (pg_trgm), с которой два факта одного субъекта считаются пересказом друг друга.
PARAPHRASE_SIMILARITY = 0.6

_BROKEN = """
SELECT
  (SELECT count(*) FROM facts f LEFT JOIN facts g ON g.id = f.superseded_by
    WHERE f.superseded_by IS NOT NULL AND f.status = 'active'
      AND (g.id IS NULL OR g.status <> 'active'
           OR (f.subject_type, f.person_id, f.project_id) IS DISTINCT FROM (g.subject_type, g.person_id, g.project_id)
           OR (f.chained AND (f.slot, f.slot_detail_norm) IS DISTINCT FROM (g.slot, g.slot_detail_norm))))
+ (SELECT count(*) FROM facts f JOIN people p ON p.id = f.person_id
    WHERE f.status IN ('active', 'proposed') AND p.merged_into IS NOT NULL)
+ (SELECT count(*) FROM facts f JOIN projects p ON p.id = f.project_id
    WHERE f.status IN ('active', 'proposed') AND p.status IN ('proposed', 'rejected'))
+ (SELECT count(*) FROM (SELECT 1 FROM facts f
    WHERE f.status = 'active' AND f.valid_to IS NULL AND f.chained AND f.slot IS NOT NULL AND f.nature <> 'event'
    GROUP BY f.subject_type, f.person_id, f.project_id, f.slot, f.slot_detail_norm HAVING count(*) > 1) x)
+ (SELECT count(*) FROM facts f
    WHERE f.subject_type = 'project' AND f.slot = 'статус' AND f.slot_detail_norm = '' AND f.chained
      AND f.status = 'active')
"""

_PARAPHRASES = f"""
SELECT count(*) FROM facts a JOIN facts b
  ON b.id > a.id AND a.subject_type = b.subject_type AND a.person_id IS NOT DISTINCT FROM b.person_id
 AND a.project_id IS NOT DISTINCT FROM b.project_id AND a.kind = b.kind
JOIN messages am ON am.id = a.source_message_id JOIN chats ac ON ac.id = am.chat_id
JOIN messages bm ON bm.id = b.source_message_id JOIN chats bc ON bc.id = bm.chat_id
WHERE {facts.CURRENT.replace('f.', 'a.')} AND {facts.CURRENT.replace('f.', 'b.')}
  AND am.deleted_at IS NULL AND am.agent_visible AND NOT ac.excluded
  AND bm.deleted_at IS NULL AND bm.agent_visible AND NOT bc.excluded
  AND similarity(a.text_norm, b.text_norm) >= $1
"""

_SIMPLE = {
    "memory_facts_current": f"SELECT count(*) {facts._FROM} WHERE {facts.VISIBLE} AND {facts.CURRENT}",
    "memory_project_outside": f"""SELECT count(*) {facts._FROM} WHERE {facts.VISIBLE} AND {facts.CURRENT}
        AND f.subject_type = 'project'
        AND NOT EXISTS (SELECT 1 FROM project_chats pc WHERE pc.project_id = f.project_id AND pc.chat_id = fm.chat_id)""",
    "memory_empty_projects": f"""SELECT count(*) FROM projects p WHERE p.status = 'active'
        AND NOT EXISTS (SELECT 1 FROM project_chats pc WHERE pc.project_id = p.id)
        AND NOT EXISTS (SELECT 1 FROM commitments c WHERE c.project_id = p.id)
        AND NOT EXISTS (SELECT 1 {facts._FROM} WHERE {facts.VISIBLE} AND f.project_id = p.id
                        AND f.status IN ('active', 'proposed'))""",
    # Сравнение без учёта регистра и пробелов. Цитата записывается только найденной в сообщении
    # (extract.grounded — с выравниванием ё, кавычек и тире), поэтому несовпадение здесь — сообщение
    # изменено после записи либо совпадало только после выравнивания.
    "memory_quotes_changed": f"""SELECT count(*) {facts._FROM} WHERE {facts.VISIBLE} AND {facts.CURRENT}
        AND position(regexp_replace(lower(f.source_quote), '\\s+', ' ', 'g')
                     IN regexp_replace(lower(fm.text || ' ' || coalesce(fm.transcript, '')), '\\s+', ' ', 'g')) = 0""",
    "memory_stale": f"""SELECT count(*) {facts._FROM} WHERE {facts.VISIBLE} AND f.status = 'active'
        AND f.valid_to IS NULL AND {facts.STALE}""",
}


async def overview(conn: asyncpg.Connection) -> dict[str, Any]:
    """Счётчики для `/api/status` (плоские поля `memory_*`, только числа)."""
    out: dict[str, Any] = {key: int(await conn.fetchval(sql) or 0) for key, sql in _SIMPLE.items()}
    out["memory_broken"] = int(await conn.fetchval(_BROKEN) or 0)
    out["memory_paraphrases"] = int(await conn.fetchval(_PARAPHRASES, PARAPHRASE_SIMILARITY) or 0)
    return out
