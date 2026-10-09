"""Проекты: объекты, сделки, направления работы, о которых ведётся страница памяти.

Откуда берутся (docs/memory.md, решение Р-66):
  * владелец заводит сам — на странице настройки (экран «Память») или фразой ассистенту; во втором
    случае ассистент просит через внутренний API, и владелец подтверждает карточкой в боте;
  * модель предлагает — владелец решает кнопкой ✓/✗ (модуль `pj`). Предложение появляется, если
    о проекте, которого ещё нет, говорят не меньше чем в трёх сообщениях двух разных эпизодов за
    30 дней, или если в групповом чате без проекта за 30 дней не меньше 20 сообщений (тогда
    название проекта — название чата). Отклонённое не предлагается снова никогда.

Статусы: proposed — ждёт владельца; active — ведётся; archived — страница остаётся, сводка больше
не пересобирается; rejected — владелец отказался.

Обязательству проект ставится при записи: по чатам проекта, иначе по названию, которое назвала
модель (сопоставление с названиями и алиасами действующих проектов).

Функции для экрана «Память» (страница настройки; изменения — в контексте владельца, authority):

  list_projects(conn, status=None) -> list[dict]
      status None — все, кроме отклонённых; иначе proposed | active | archived | rejected.
  get_project(conn, project_id) -> dict | None
  create_project(conn, title, chat_ids=(), aliases=(), origin="owner", *, description=None) -> dict
      origin="owner" — только владелец; проект сразу действует. Если такой же проект уже
      предложен моделью, он принимается. origin="model" — предложение (для обработки).
  set_project_chats(conn, project_id, chat_ids) -> dict          только владелец
  archive_project(conn, project_id) -> dict                      только владелец
  decide_project_proposal(conn, project_id, accept) -> dict
      accept=True — только владелец; accept=False — отказаться можно и без него.
  pending_approvals(conn) -> dict
      {"projects": [...], "owner_facts": [...], "pages": [...], "commitments": [...]} — всё, что
      ждёт решения владельца: предложенные проекты, факты профиля, страницы людей, обязательства.
      У каждого пункта id (для страниц — person_id), заголовок и короткий текст. Тексты взяты из
      переписки: показывать как данные (textContent), не как разметку.

Без владельца любая из «только владелец» бросает `confirm.Refused` (403, owner_required).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import asyncpg

from .. import authority, bridge
from ..sanitize import clean_line
from . import extract, facts

CALLBACK_MODULE = "pj"
HANDLER_DIGEST = "projects.digest"
STATUSES = ("proposed", "active", "archived", "rejected")
TITLE_LIMIT = 80
MAX_CHATS = 50
MAX_ALIASES = 20

PROPOSE_DAYS = 30
PROPOSE_MENTIONS = 3
PROPOSE_EPISODES = 2
PROPOSE_GROUP_MESSAGES = 20
DIGEST_MAX_ITEMS = 10
DIGEST_MAX_SENDS = 3
SIMILAR = 90               # похожесть названий, с которой метка модели — этот проект

# Групповые чаты: из них предлагаются проекты по второму правилу.
GROUP_TYPES = ("private_group", "private_supergroup", "public_supergroup")
UNTRUSTED_FIELDS = ["title", "aliases[]", "chats[].title", "participants[]"]


class ProjectsError(ValueError):
    """Действие с проектом невозможно; текст — для владельца, `code` — для ответа API."""

    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.code = code


norm = extract.norm_title


def clean_title(value: Any) -> str:
    """Название проекта от владельца или из переписки: одна строка, с буквами, до 80 знаков."""
    if not isinstance(value, str):
        raise ProjectsError("Название проекта: нужна строка.")
    title = clean_line(value, 400).strip(" .,;:—-«»\"'“”„")
    if not norm(title) or not extract._LETTER_RE.search(title):
        raise ProjectsError("В названии проекта должны быть буквы.")
    if len(title) > TITLE_LIMIT:
        raise ProjectsError(f"Название проекта — не длиннее {TITLE_LIMIT} знаков.")
    return title


# --- чтение ------------------------------------------------------------------------------------------

async def _chats(conn: asyncpg.Connection, project_id: int) -> list[dict[str, Any]]:
    """Чаты проекта. Исключённые владельцем не показываются вовсе."""
    rows = await conn.fetch(
        """SELECT c.id, c.title, c.type, pc.origin FROM project_chats pc JOIN chats c ON c.id = pc.chat_id
           WHERE pc.project_id = $1 AND NOT c.excluded ORDER BY pc.added_at, c.id""", project_id)
    return [{"id": r["id"], "title": r["title"], "type": r["type"], "origin": r["origin"]} for r in rows]


async def _aliases(conn: asyncpg.Connection, project_id: int) -> list[str]:
    return [r["alias"] for r in await conn.fetch(
        "SELECT alias FROM project_aliases WHERE project_id = $1 ORDER BY id", project_id)]


async def participants(conn: asyncpg.Connection, project_id: int, limit: int = 30) -> list[dict[str, Any]]:
    """Люди с сообщениями в чатах проекта (видимыми), кроме владельца: по числу сообщений."""
    rows = await conn.fetch(
        """SELECT p.id, p.display_name, count(*) AS n FROM project_chats pc
           JOIN chats c ON c.id = pc.chat_id AND NOT c.excluded
           JOIN messages m ON m.chat_id = c.id AND m.deleted_at IS NULL AND m.agent_visible AND m.kind = 'message'
           JOIN person_peers pp ON pp.peer_id = m.sender_peer_id
           JOIN people p ON p.id = pp.person_id AND NOT p.is_owner AND p.merged_into IS NULL
           WHERE pc.project_id = $1 GROUP BY p.id ORDER BY n DESC, p.id LIMIT $2""", project_id, limit)
    return [{"person_id": r["id"], "name": r["display_name"], "messages": r["n"]} for r in rows]


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def _to_dict(conn: asyncpg.Connection, row: asyncpg.Record, *, full: bool = False) -> dict[str, Any]:
    page = await conn.fetchrow("SELECT id, path, updated FROM pages WHERE project_id = $1", row["id"])
    counts = await conn.fetchrow(
        f"""SELECT (SELECT count(*) FROM commitments c JOIN chats ch ON ch.id = c.chat_id
                    JOIN messages m ON m.id = c.source_message_id
                    WHERE c.project_id = $1 AND c.status = 'open' AND NOT ch.excluded
                      AND m.deleted_at IS NULL AND m.agent_visible) AS commitments_open,
                   (SELECT count(*) {facts._FROM} WHERE {facts.VISIBLE} AND f.project_id = $1
                      AND f.status = 'active' AND f.valid_to IS NULL AND f.kind = 'fact') AS facts,
                   (SELECT count(*) {facts._FROM} WHERE {facts.VISIBLE} AND f.project_id = $1
                      AND f.status = 'active' AND f.kind = 'decision') AS decisions""", row["id"])
    out = {
        "id": row["id"], "title": row["title"], "status": row["status"], "origin": row["origin"],
        "aliases": await _aliases(conn, row["id"]), "chats": await _chats(conn, row["id"]),
        "commitments_open": counts["commitments_open"], "facts": counts["facts"],
        "decisions": counts["decisions"],
        "page": ({"page_id": page["id"], "entity_id": f"project:{row['id']}", "path": page["path"],
                  "updated": page["updated"].isoformat() if page["updated"] else None} if page else None),
        "created_at": row["created_at"].isoformat(),
        "decided_at": row["decided_at"].isoformat() if row["decided_at"] else None,
        "untrusted_fields": list(UNTRUSTED_FIELDS),
    }
    if full:
        reason = _loads(row["reason"]) or {}
        out.update(description=row["description"], reason={
            k: v for k, v in reason.items() if k in ("mentions", "episodes", "messages", "chats")},
            participants=[p["name"] for p in await participants(conn, row["id"])])
    return out


async def list_projects(conn: asyncpg.Connection, status: str | None = None) -> list[dict[str, Any]]:
    """Проекты по названию. status None — все, кроме отклонённых."""
    if status is not None and status not in STATUSES:
        raise ProjectsError("Статус проекта: proposed, active, archived или rejected.")
    rows = await conn.fetch(
        """SELECT * FROM projects WHERE ($1::text IS NULL AND status <> 'rejected') OR status = $1
           ORDER BY lower(title), id LIMIT 500""", status)
    return [await _to_dict(conn, r) for r in rows]


async def get_project(conn: asyncpg.Connection, project_id: int) -> dict[str, Any] | None:
    row = await conn.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
    return await _to_dict(conn, row, full=True) if row is not None else None


# --- сопоставление названий ---------------------------------------------------------------------------

async def resolve(conn: asyncpg.Connection, label: str | None, *, statuses: Sequence[str] = ("active",)) -> int | None:
    """Проект по названию из переписки: точное совпадение ключа названия или алиаса, иначе
    единственный достаточно похожий. Решает код; неоднозначное — None."""
    key = norm(label)
    if not key:
        return None
    rows = await conn.fetch(
        """SELECT p.id, p.title_norm AS name FROM projects p WHERE p.status = ANY($1::text[])
           UNION ALL
           SELECT p.id, a.alias_norm FROM project_aliases a JOIN projects p ON p.id = a.project_id
           WHERE p.status = ANY($1::text[])""", list(statuses))
    exact = {r["id"] for r in rows if r["name"] == key}
    if len(exact) == 1:
        return exact.pop()
    if exact:
        return None
    from rapidfuzz import fuzz

    best: dict[int, int] = {}
    for r in rows:
        score = int(fuzz.token_set_ratio(key, r["name"])) if min(len(key), len(r["name"])) >= 4 else 0
        best[r["id"]] = max(best.get(r["id"], 0), score)
    good = sorted((score, pid) for pid, score in best.items() if score >= SIMILAR)
    if len(good) == 1 or (len(good) > 1 and good[-1][0] > good[-2][0]):
        return good[-1][1]
    return None


async def for_chat(conn: asyncpg.Connection, chat_id: int) -> list[int]:
    """Действующие проекты, к которым относится чат."""
    return [r["project_id"] for r in await conn.fetch(
        """SELECT pc.project_id FROM project_chats pc JOIN projects p ON p.id = pc.project_id
           WHERE pc.chat_id = $1 AND p.status = 'active' ORDER BY pc.project_id""", chat_id)]


async def for_commitment(conn: asyncpg.Connection, chat_id: int, label: str | None) -> int | None:
    """Проект обязательства: по чатам проекта (если чат относится ровно к одному), иначе по метке модели."""
    linked = await for_chat(conn, chat_id)
    if len(linked) == 1:
        return linked[0]
    found = await resolve(conn, label)
    if found is not None and (not linked or found in linked):
        return found
    return None


async def active_titles(conn: asyncpg.Connection, limit: int = 30) -> list[str]:
    return [r["title"] for r in await conn.fetch(
        "SELECT title FROM projects WHERE status = 'active' ORDER BY updated_at DESC, id LIMIT $1", limit)]


async def record_mentions(
    conn: asyncpg.Connection, title: str, episode: extract.Episode, *, run_id: int | None,
) -> int:
    """Записывает сообщения эпизода, в которых названо ещё не заведённое название проекта."""
    key = norm(title)
    if not key or await _known_name(conn, key):
        return 0
    padded, added = f" {key} ", 0
    episode_key = f"{episode.chat_id}:{episode.first_id}-{episode.last_id}"
    for message in episode.messages:
        if message.is_source and padded in f" {norm(message.text)} ":
            done = await conn.execute(
                """INSERT INTO project_mentions (title, title_norm, chat_id, message_id, episode, run_id)
                   VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (title_norm, message_id) DO NOTHING""",
                title, key, episode.chat_id, message.id, episode_key, run_id)
            added += int(done.endswith("1"))
    return added


async def _known_name(conn: asyncpg.Connection, key: str) -> bool:
    """Название уже занято проектом в любом статусе (в том числе отклонённым) или его алиасом."""
    return bool(await conn.fetchval(
        """SELECT 1 FROM projects WHERE title_norm = $1
           UNION ALL SELECT 1 FROM project_aliases WHERE alias_norm = $1 LIMIT 1""", key))


# --- изменения ------------------------------------------------------------------------------------------

async def _visible_chats(conn: asyncpg.Connection, chat_ids: Iterable[Any]) -> list[int]:
    ids = []
    for value in chat_ids:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ProjectsError("Чаты проекта: нужен список номеров чатов архива.")
        if value not in ids:
            ids.append(value)
    if len(ids) > MAX_CHATS:
        raise ProjectsError(f"У проекта не больше {MAX_CHATS} чатов.")
    found = {r["id"] for r in await conn.fetch(
        "SELECT id FROM chats WHERE id = ANY($1::bigint[]) AND NOT excluded", ids)}
    if len(found) != len(ids):
        raise ProjectsError("Такого чата нет в архиве.", "bad_chat")
    return ids


def _clean_aliases(values: Iterable[Any], title_key: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for value in values:
        alias = clean_title(value)
        key = norm(alias)
        if key != title_key and key not in [k for _, k in out]:
            out.append((alias, key))
    if len(out) > MAX_ALIASES:
        raise ProjectsError(f"У проекта не больше {MAX_ALIASES} других названий.")
    return out


async def _link_commitments(conn: asyncpg.Connection, project_id: int, chat_ids: Sequence[int]) -> None:
    """Обязательства из чатов проекта, ещё не отнесённые ни к какому проекту, относятся к нему."""
    if chat_ids:
        await conn.execute(
            """UPDATE commitments SET project_id = $1, updated_at = now()
               WHERE project_id IS NULL AND chat_id = ANY($2::bigint[])""", project_id, list(chat_ids))


def _approval(principal: authority.AuthorityPrincipal) -> tuple[str, str]:
    return str(authority.current_owner_id()), principal.source


async def _activate(conn: asyncpg.Connection, project_id: int, principal: authority.AuthorityPrincipal) -> None:
    from . import pages_build
    by, via = _approval(principal)
    await conn.execute(
        """UPDATE projects SET status = 'active', decided_at = now(), updated_at = now(),
                  approved_by = $2, approved_via = $3 WHERE id = $1""", project_id, by, via)
    chats = [r["chat_id"] for r in await conn.fetch(
        "SELECT chat_id FROM project_chats WHERE project_id = $1", project_id)]
    await _link_commitments(conn, project_id, chats)
    await pages_build.ensure_project_page(conn, project_id)


async def create_project(
    conn: asyncpg.Connection, title: str, chat_ids: Iterable[int] = (), aliases: Iterable[str] = (),
    origin: str = "owner", *, description: str | None = None, reason: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Заводит проект (см. описание модуля). Возвращает {"ok", "project", "created"}."""
    if origin not in ("owner", "model"):
        raise ProjectsError("Неизвестное происхождение проекта.")
    principal = authority.requires_owner() if origin == "owner" else None
    title = clean_title(title)
    key = norm(title)
    chat_list = list(chat_ids or ())
    alias_list = _clean_aliases(aliases or (), key)
    if description is not None:
        if not isinstance(description, str) or len(description) > 2000:
            raise ProjectsError("Описание проекта: строка не длиннее 2000 знаков.")
        description = clean_line(description, 2000) or None
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.projects'))")
        chats = await _visible_chats(conn, chat_list)
        same = await conn.fetchrow(
            "SELECT id, status FROM projects WHERE title_norm = $1 AND status <> 'rejected' FOR UPDATE", key)
        created = same is None
        if same is not None and (origin == "model" or same["status"] != "proposed"):
            raise ProjectsError("Проект с таким названием уже есть.", "exists")
        if same is None:
            project_id = await conn.fetchval(
                """INSERT INTO projects (title, title_norm, description, status, origin, reason)
                   VALUES ($1, $2, $3, 'proposed', $4, $5::jsonb) RETURNING id""",
                title, key, description, origin, json.dumps(reason or {}))
        else:
            project_id = same["id"]
            if description:
                await conn.execute("UPDATE projects SET description = $2 WHERE id = $1", project_id, description)
        await conn.executemany(
            """INSERT INTO project_chats (project_id, chat_id, origin) VALUES ($1, $2, $3)
               ON CONFLICT DO NOTHING""", [(project_id, c, origin) for c in chats])
        await conn.executemany(
            """INSERT INTO project_aliases (project_id, alias, alias_norm, origin) VALUES ($1, $2, $3, $4)
               ON CONFLICT DO NOTHING""", [(project_id, a, k, origin) for a, k in alias_list])
        if principal is not None:
            await _activate(conn, project_id, principal)
    if principal is not None:
        _wake()
    return {"ok": True, "created": created, "project": await get_project(conn, project_id)}


async def _owned(conn: asyncpg.Connection, project_id: int, statuses: Sequence[str]) -> asyncpg.Record:
    row = await conn.fetchrow("SELECT * FROM projects WHERE id = $1 FOR UPDATE", project_id)
    if row is None:
        raise ProjectsError("Такого проекта нет.", "not_found")
    if row["status"] not in statuses:
        raise ProjectsError("Нельзя: проект " + {"proposed": "ещё не заведён", "active": "действует",
                                                  "archived": "в архиве", "rejected": "отклонён"}[row["status"]] + ".",
                            "bad_status")
    return row


async def set_project_chats(conn: asyncpg.Connection, project_id: int, chat_ids: Iterable[int]) -> dict[str, Any]:
    """Заменяет перечень чатов проекта. Обязательства из добавленных чатов относятся к проекту,
    из убранных — перестают. Только владелец."""
    authority.requires_owner()
    async with conn.transaction():
        await _owned(conn, project_id, ("active", "archived"))
        chats = await _visible_chats(conn, list(chat_ids or ()))
        before = {r["chat_id"] for r in await conn.fetch(
            "SELECT chat_id FROM project_chats WHERE project_id = $1", project_id)}
        removed = sorted(before - set(chats))
        added = [c for c in chats if c not in before]
        await conn.execute("DELETE FROM project_chats WHERE project_id = $1 AND chat_id = ANY($2::bigint[])",
                           project_id, removed)
        await conn.executemany("INSERT INTO project_chats (project_id, chat_id, origin) VALUES ($1, $2, 'owner')",
                               [(project_id, c) for c in added])
        if removed:
            await conn.execute(
                """UPDATE commitments SET project_id = NULL, updated_at = now()
                   WHERE project_id = $1 AND chat_id = ANY($2::bigint[])""", project_id, removed)
        await _link_commitments(conn, project_id, added)
        await conn.execute("UPDATE projects SET updated_at = now() WHERE id = $1", project_id)
        await conn.execute("UPDATE pages SET dirty = true WHERE project_id = $1", project_id)
    _wake()
    return {"ok": True, "changed": bool(added or removed), "added": added, "removed": removed,
            "project": await get_project(conn, project_id)}


async def archive_project(conn: asyncpg.Connection, project_id: int) -> dict[str, Any]:
    """Проект в архив: страница остаётся, сводка больше не пересобирается. Только владелец."""
    authority.requires_owner()
    async with conn.transaction():
        row = await _owned(conn, project_id, ("active", "archived"))
        if row["status"] == "archived":
            return {"ok": True, "changed": False, "project": await get_project(conn, project_id)}
        await conn.execute("UPDATE projects SET status = 'archived', updated_at = now() WHERE id = $1", project_id)
        await conn.execute("UPDATE pages SET dirty = true WHERE project_id = $1", project_id)
    _wake()
    return {"ok": True, "changed": True, "project": await get_project(conn, project_id)}


async def decide_project_proposal(conn: asyncpg.Connection, project_id: int, accept: bool) -> dict[str, Any]:
    """Решение владельца о предложенном проекте. Согласие — только по проверенному владельцу."""
    principal = authority.requires_owner() if accept else None
    async with conn.transaction():
        row = await conn.fetchrow("SELECT * FROM projects WHERE id = $1 FOR UPDATE", project_id)
        if row is None:
            raise ProjectsError("Такого проекта нет.", "not_found")
        target = "active" if accept else "rejected"
        if row["status"] != "proposed":
            if row["status"] == target or (accept and row["status"] == "archived"):
                return {"ok": True, "changed": False, "status": row["status"],
                        "project": await get_project(conn, project_id)}
            raise ProjectsError("Уже решено.", "bad_status")
        if accept:
            await _activate(conn, project_id, principal)
        else:
            await conn.execute(
                "UPDATE projects SET status = 'rejected', decided_at = now(), updated_at = now() WHERE id = $1",
                project_id)
    if accept:
        _wake()
    return {"ok": True, "changed": True, "status": target, "project": await get_project(conn, project_id)}


def _wake() -> None:
    from . import pages_build
    pages_build.wake()


# --- предложения ---------------------------------------------------------------------------------------

async def propose(conn: asyncpg.Connection, *, now: datetime | None = None,
                  run_id: int | None = None) -> dict[str, int]:
    """Заводит предложения по двум правилам (см. описание модуля). Владельцу ничего не шлёт."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=PROPOSE_DAYS)
    stats = {"projects_proposed": 0}
    rows = await conn.fetch(
        """SELECT pm.title_norm, count(*) AS mentions, count(DISTINCT pm.episode) AS episodes,
                  (array_agg(pm.title ORDER BY pm.id DESC))[1] AS title,
                  array_agg(DISTINCT pm.chat_id ORDER BY pm.chat_id) AS chats
           FROM project_mentions pm
           JOIN messages m ON m.id = pm.message_id AND m.deleted_at IS NULL AND m.agent_visible
           JOIN chats c ON c.id = pm.chat_id AND NOT c.excluded
           WHERE m.sent_at >= $1
           GROUP BY pm.title_norm HAVING count(*) >= $2 AND count(DISTINCT pm.episode) >= $3
           ORDER BY count(*) DESC, pm.title_norm LIMIT 50""",
        since, PROPOSE_MENTIONS, PROPOSE_EPISODES)
    for r in rows:
        if await _known_name(conn, r["title_norm"]):
            continue
        try:
            await create_project(conn, r["title"], origin="model", reason={
                "mentions": r["mentions"], "episodes": r["episodes"], "chats": list(r["chats"])[:10]})
        except ProjectsError:
            continue
        stats["projects_proposed"] += 1
    groups = await conn.fetch(
        """SELECT c.id, c.title, count(m.id) AS n FROM chats c
           JOIN messages m ON m.chat_id = c.id AND m.deleted_at IS NULL AND m.agent_visible
                          AND m.kind = 'message' AND m.sent_at >= $1
           WHERE c.type = ANY($2::text[]) AND NOT c.excluded
             AND NOT EXISTS (SELECT 1 FROM project_chats pc WHERE pc.chat_id = c.id)
           GROUP BY c.id HAVING count(m.id) >= $3 ORDER BY count(m.id) DESC, c.id LIMIT 50""",
        since, list(GROUP_TYPES), PROPOSE_GROUP_MESSAGES)
    for r in groups:
        try:
            title = clean_title(r["title"] or "")
        except ProjectsError:
            continue
        if await _known_name(conn, norm(title)):
            continue
        try:
            await create_project(conn, title, [r["id"]], origin="model",
                                 reason={"chats": [r["id"]], "messages": r["n"]})
        except ProjectsError:
            continue
        stats["projects_proposed"] += 1
    return stats


def _reason_text(reason: dict[str, Any]) -> str:
    if reason.get("messages"):
        return f"групповой чат: сообщений за {PROPOSE_DAYS} дн. — {int(reason['messages'])}"
    if reason.get("mentions"):
        return (f"упоминаний за {PROPOSE_DAYS} дн.: {int(reason['mentions'])} "
                f"в {int(reason.get('episodes') or 0)} разговорах")
    return "предложено по переписке"


async def send_digest(conn: asyncpg.Connection, *, run_id: int | str, max_items: int = DIGEST_MAX_ITEMS) -> int:
    """Одно сообщение владельцу с предложенными проектами и кнопками ✓ завести / ✗ не нужно."""
    rows = await conn.fetch(
        """SELECT id, title, reason FROM projects
           WHERE status = 'proposed' AND batch IS NULL AND digest_attempts < $2 ORDER BY id LIMIT $1""",
        max_items, DIGEST_MAX_SENDS)
    if not rows:
        return 0
    batch = f"pj{run_id}"
    lines, buttons = [], []
    for pos, r in enumerate(rows, start=1):
        lines.append(f"{pos}. {clean_line(r['title'], TITLE_LIMIT) or 'без названия'} — "
                     f"{_reason_text(_loads(r['reason']) or {})}")
        buttons.append([bridge.button(f"{pos} ✓ завести", CALLBACK_MODULE, f"a:{r['id']}"),
                        bridge.button(f"{pos} ✗ не нужно", CALLBACK_MODULE, f"r:{r['id']}")])
        await conn.execute(
            """UPDATE projects SET batch = $2, pos = $3, notified_at = now(), digest_attempts = digest_attempts + 1
               WHERE id = $1""", r["id"], batch, pos)
    waiting = await conn.fetchval(
        "SELECT count(*) FROM projects WHERE status = 'proposed' AND batch IS NULL AND digest_attempts < $1",
        DIGEST_MAX_SENDS)
    text = ("Проекты из переписки: завести страницу проекта? ✓ — завести, ✗ — не нужно. "
            "Отклонённое больше не предлагается.\n\n" + "\n".join(lines))
    if waiting:
        text += f"\n\nЕщё ждут решения: {waiting}. Придут со следующим прогоном."
    await bridge.notify_owner(conn, bridge.fit_message(text), buttons=buttons, handler=HANDLER_DIGEST,
                              context={"batch": batch}, dedup_key=f"pj-digest:{batch}")
    return len(rows)


async def unmark_batch(conn: asyncpg.Connection, batch: str) -> int:
    done = await conn.execute(
        """UPDATE projects SET batch = NULL, pos = NULL, notified_at = NULL
           WHERE batch = $1 AND status = 'proposed' AND digest_attempts < $2""", batch, DIGEST_MAX_SENDS)
    return int(done.split()[-1])


async def batch_summary(conn: asyncpg.Connection, batch: str) -> tuple[bool, str]:
    rows = await conn.fetch("SELECT pos, status, title FROM projects WHERE batch = $1 ORDER BY pos", batch)
    marks = {"active": "✓ заведён", "archived": "✓ заведён", "rejected": "✗ не нужен", "proposed": "… ждёт"}
    lines = [f"{r['pos']}. {clean_line(r['title'], TITLE_LIMIT) or 'без названия'} — {marks[r['status']]}"
             for r in rows]
    return bool(rows) and all(r["status"] != "proposed" for r in rows), "Проекты — решено:\n" + "\n".join(lines)


@bridge.on_result(HANDLER_DIGEST)
async def on_digest_sent(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    await conn.execute("UPDATE jobs SET payload = '{}'::jsonb, result = NULL WHERE id = $1", job["id"])


@bridge.on_failure(HANDLER_DIGEST)
async def on_digest_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    """Сообщение до владельца не дошло: нерешённые предложения уйдут со следующим прогоном."""
    await conn.execute("UPDATE jobs SET payload = '{}'::jsonb, result = NULL WHERE id = $1", job["id"])
    batch = (job.get("context") or {}).get("batch")
    if isinstance(batch, str):
        await unmark_batch(conn, batch)


@bridge.on_callback(CALLBACK_MODULE)
async def on_button(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    """Нажатие под предложениями: a:<проект> — завести, r:<проект> — не нужно."""
    refused = {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
    action, _, raw = rest.partition(":")
    if action not in ("a", "r") or not raw.isdigit() or len(raw) > 18:
        return refused
    row = await conn.fetchrow("SELECT status, batch FROM projects WHERE id = $1", int(raw))
    if row is None:
        return refused
    if row["status"] != "proposed":
        answer = "Уже решено."
    else:
        try:
            await decide_project_proposal(conn, int(raw), accept=action == "a")
            answer = "Проект заведён." if action == "a" else "Не заводим."
        except ProjectsError as exc:
            answer = str(exc)
    if row["batch"]:
        done, text = await batch_summary(conn, row["batch"])
        if done:
            return {"answer": answer, "edit_text": text, "remove_buttons": True}
    return {"answer": answer, "edit_text": None, "remove_buttons": False}


async def scrub_closed_jobs(conn: asyncpg.Connection) -> int:
    done = await conn.execute(
        """UPDATE jobs SET payload = '{}'::jsonb, result = NULL
           WHERE handler = $1 AND status IN ('done', 'failed') AND payload <> '{}'::jsonb""", HANDLER_DIGEST)
    return int(done.split()[-1])


async def after_run(conn: asyncpg.Connection, run_id: int) -> dict[str, int]:
    """Конец прогона обработки: новые предложения проектов и сообщения владельцу (проекты,
    факты профиля). Вызывается в транзакции завершения прогона."""
    stats = await propose(conn, run_id=run_id)
    stats["projects_shown"] = await send_digest(conn, run_id=run_id)
    stats["owner_facts_shown"] = await facts.send_owner_digest(conn, run_id=run_id)
    return stats


# --- исключение чата и удаление ------------------------------------------------------------------------------

async def purge_orphans(conn: asyncpg.Connection) -> int:
    """Чат исключён владельцем: он перестаёт быть чатом проекта, его упоминания забываются,
    страницы проектов ждут перерисовки."""
    rows = await conn.fetch(
        """DELETE FROM project_chats pc USING chats c WHERE c.id = pc.chat_id AND c.excluded
           RETURNING pc.project_id""")
    await conn.execute(
        """DELETE FROM project_mentions pm USING chats c, messages m
           WHERE c.id = pm.chat_id AND m.id = pm.message_id AND (c.excluded OR m.deleted_at IS NOT NULL)""")
    projects_ = sorted({r["project_id"] for r in rows})
    if projects_:
        await conn.execute("UPDATE pages SET dirty = true WHERE project_id = ANY($1::bigint[])", projects_)
        _wake()
    return len(rows)


async def purge_for_messages(conn: asyncpg.Connection, message_ids: Sequence[int]) -> int:
    if not message_ids:
        return 0
    done = await conn.execute("DELETE FROM project_mentions WHERE message_id = ANY($1::bigint[])", list(message_ids))
    return int(done.split()[-1])


# --- что ждёт решения владельца ------------------------------------------------------------------------------

def _short(text: str | None, limit: int) -> str:
    text = " ".join(clean_line(text or "", 4 * limit).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def pending_approvals(conn: asyncpg.Connection, *, limit: int = 100) -> dict[str, list[dict[str, Any]]]:
    """Всё, что ждёт решения владельца (см. описание модуля)."""
    from . import commitments

    out: dict[str, list[dict[str, Any]]] = {}
    out["projects"] = [
        {"id": r["id"], "title": r["title"], "text": _reason_text(_loads(r["reason"]) or {}),
         "created_at": r["created_at"].isoformat()}
        for r in await conn.fetch(
            "SELECT id, title, reason, created_at FROM projects WHERE status = 'proposed' ORDER BY id LIMIT $1",
            limit)]
    out["owner_facts"] = [
        {"id": r["id"], "title": r["slot"] or "о вас", "text": _short(r["text"], 240),
         "quote": _short(r["source_quote"], 200), "valid_from": r["valid_from"].isoformat(),
         "fingerprint": facts.fingerprint(r), "created_at": r["created_at"].isoformat()}
        for r in await conn.fetch(
            f"""{facts._SELECT} AND f.subject_type = 'owner' AND f.status = 'proposed' ORDER BY f.id LIMIT $1""",
            limit)]
    out["pages"] = [
        {"id": r["person_id"], "person_id": r["person_id"], "title": r["display_name"],
         "text": ", ".join(f"{k}: {int(v)}" for k, v in (_loads(r["reason"]) or {}).items() if isinstance(v, int)),
         "created_at": r["created_at"].isoformat()}
        for r in await conn.fetch(
            """SELECT x.person_id, x.reason, x.created_at, p.display_name FROM page_proposals x
               JOIN people p ON p.id = x.person_id AND p.merged_into IS NULL
               WHERE x.status = 'pending' ORDER BY x.created_at, x.person_id LIMIT $1""", limit)]
    out["commitments"] = [
        {"id": item["id"], "title": _short(commitments.who_line(item), 120), "text": _short(item["what"], 240),
         "due": item["due_date"] or item["due_expression"], "fingerprint": item["approval_fingerprint"],
         "created_at": item["created_at"]}
        for item in await commitments.list_commitments(conn, view="proposed", today=facts.today("UTC"), limit=limit)]
    return out
