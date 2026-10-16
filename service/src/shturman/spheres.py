"""Сферы: данные и правила (решение Р-82, docs/spheres.md). Только функции над соединением с базой.

Сфера — часть работы или жизни владельца, которую он отделяет от остального: «Работа»,
«Личное», «Хобби». Заводит и меняет сферы только владелец: на странице
настройки, в приложении «Штурман» — сразу, а через ассистента (внутренний API) — карточкой в боте
согласований (`spheres_service.py`).

Модель данных (миграция `0032_spheres.sql`):
  * `spheres` — название, короткое описание, порядок; `settings` — место под настройки шагов 2–4
    (своя обработка, своя модель, строгая граница ответов), в шаге 1 не читается и не пишется;
  * `sphere_chats` — чат в сфере, многие-ко-многим: чат может быть в нескольких сферах или ни в одной.
    Ключ — диалог аккаунта (account_id, peer_class, tg_id), тот же, что у `tg_dialogs` и
    `tg_sync_chats`: так сферу можно назначить и чату, который ещё не читается (запись в `chats` для
    него не заводится — иначе он стал бы виден ассистенту). С архивом ключ сходится через
    chats.account_id + peers (class, tg_id) — `CHAT_KEY_JOIN`;
  * `projects.sphere_id` — проект не больше чем в одной сфере;
  * `tg_folders`, `sphere_folders`, `sphere_declined` — папки Telegram, связь сферы с папкой и чаты,
    которые владелец не хочет видеть в сфере (их связанная папка больше не предлагает).

Удаление сферы в шаге 1 — только снятие её с чатов и проектов: чаты, архив сообщений, проекты и их
страницы не трогаются. Полное удаление сферы с её данными и выгрузка — шаг 4.

Для ассистента сфера — мягкая граница (шаг 1): инструменты помечают каждый результат сферами его
чата и сужают поиск по параметру `spheres`; по умолчанию он ищет в сфере, о которой разговор, —
это правило в описаниях инструментов и в блоке SOUL.md, а не принуждение в коде.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

import asyncpg

from .sanitize import clean_line
from .tg import dialogs as dl

PeerKey = tuple[str, int]

TITLE_LIMIT = 60
DESCRIPTION_LIMIT = 300
MAX_SPHERES = 50
MAX_KEYS = 5000                 # чатов за одно изменение (вся папка, весь раздел)

# Как ассистент просит «искать везде» и «вне сфер».
ALL_WORDS = frozenset({"все", "всё", "all", "*", "везде", "everywhere", "any"})
NONE_WORDS = frozenset({"без сферы", "вне сфер", "none", "no sphere", "unassigned"})

# Чат архива (c — chats, p — peers) и его строки в сферах.
CHAT_KEY_JOIN = ("JOIN sphere_chats sc ON sc.account_id = c.account_id AND sc.peer_class = p.class "
                 "AND sc.tg_id = p.tg_id")


class SpheresError(Exception):
    """Отказ с текстом для владельца или ассистента. code — для ответа маршрута."""

    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.message, self.code = message, code


# --- названия ------------------------------------------------------------------------------------

def norm(title: str | None) -> str:
    """Ключ сравнения названий: нижний регистр, ё → е, пробелы схлопнуты, без кавычек по краям."""
    text = " ".join(str(title or "").casefold().replace("ё", "е").split())
    return text.strip("«»\"'“”„ ")


def clean_title(value: Any) -> str:
    if not isinstance(value, str):
        raise SpheresError("Название сферы: нужна строка.")
    title = clean_line(value, 400).strip()
    if not norm(title) or not re.search(r"\w", title):
        raise SpheresError("Впишите название сферы — например, «Работа» или «Личное».")
    if len(title) > TITLE_LIMIT:
        raise SpheresError(f"Название сферы — не длиннее {TITLE_LIMIT} знаков.")
    if norm(title) in ALL_WORDS or norm(title) in NONE_WORDS:
        raise SpheresError("Это слово ассистент понимает как «все сферы» или «без сферы» — выберите другое название.")
    return title


def clean_description(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SpheresError("Описание сферы: нужна строка.")
    text = clean_line(value, 2000).strip()
    if len(text) > DESCRIPTION_LIMIT:
        raise SpheresError(f"Описание сферы — не длиннее {DESCRIPTION_LIMIT} знаков.")
    return text or None


def _keys(value: Iterable[Any]) -> list[PeerKey]:
    out: list[PeerKey] = []
    for key in value:
        cls, tg_id = key
        if cls not in ("user", "chat", "channel") or isinstance(tg_id, bool) or not isinstance(tg_id, int):
            raise SpheresError("Чат указан неверно.")
        if (cls, tg_id) not in out:
            out.append((cls, tg_id))
    if len(out) > MAX_KEYS:
        raise SpheresError(f"Слишком много чатов за раз: не больше {MAX_KEYS}.")
    return out


# --- сферы ---------------------------------------------------------------------------------------

async def _row(conn: asyncpg.Connection, sphere_id: int) -> asyncpg.Record:
    row = await conn.fetchrow("SELECT * FROM spheres WHERE id = $1", sphere_id)
    if row is None:
        raise SpheresError("Такой сферы нет.", "not_found")
    return row


async def list_spheres(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Сферы по порядку: название, описание, сколько чатов (всего и уже в архиве), проектов, папки."""
    rows = await conn.fetch(
        """SELECT s.id, s.title, s.description, s.position, s.created_at, s.updated_at,
                  (SELECT count(*) FROM sphere_chats sc WHERE sc.sphere_id = s.id) AS chats,
                  (SELECT count(DISTINCT c.id) FROM sphere_chats sc
                     JOIN peers p ON p.class = sc.peer_class AND p.tg_id = sc.tg_id
                     JOIN chats c ON c.account_id = sc.account_id AND c.peer_id = p.id AND NOT c.excluded
                    WHERE sc.sphere_id = s.id) AS archived_chats,
                  (SELECT count(*) FROM projects pr WHERE pr.sphere_id = s.id
                     AND pr.status IN ('active', 'archived')) AS projects
           FROM spheres s ORDER BY s.position, s.id""")
    links = await conn.fetch(
        """SELECT f.sphere_id, f.account_id, f.folder_id, f.propose, COALESCE(t.title, f.folder_title) AS title,
                  t.folder_id IS NOT NULL AS present
           FROM sphere_folders f LEFT JOIN tg_folders t ON t.account_id = f.account_id AND t.folder_id = f.folder_id
           ORDER BY f.linked_at, f.folder_id""")
    by_sphere: dict[int, list[dict[str, Any]]] = {}
    for link in links:
        by_sphere.setdefault(link["sphere_id"], []).append({
            "account_id": link["account_id"], "folder_id": link["folder_id"], "propose": link["propose"],
            "title": link["title"], "present": link["present"]})
    return [{"id": r["id"], "title": r["title"], "description": r["description"], "position": r["position"],
             "chats": int(r["chats"]), "archived_chats": int(r["archived_chats"]), "projects": int(r["projects"]),
             "folders": by_sphere.get(r["id"], []),
             "updated_at": r["updated_at"].isoformat()} for r in rows]


async def get_sphere(conn: asyncpg.Connection, sphere_id: int) -> dict[str, Any]:
    await _row(conn, sphere_id)
    return next(s for s in await list_spheres(conn) if s["id"] == sphere_id)


async def _check_title(conn: asyncpg.Connection, title: str, *, sphere_id: int | None = None) -> None:
    taken = await conn.fetchval("SELECT id FROM spheres WHERE title_norm = $1", norm(title))
    if taken is not None and taken != sphere_id:
        raise SpheresError("Сфера с таким названием уже есть.", "exists")


async def create_sphere(conn: asyncpg.Connection, title: Any, description: Any = None) -> dict[str, Any]:
    title, about = clean_title(title), clean_description(description)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.spheres'))")
        if await conn.fetchval("SELECT count(*) FROM spheres") >= MAX_SPHERES:
            raise SpheresError(f"Сфер уже {MAX_SPHERES} — больше завести нельзя.", "too_many")
        await _check_title(conn, title)
        sphere_id = await conn.fetchval(
            """INSERT INTO spheres (title, title_norm, description, position)
               VALUES ($1, $2, $3, COALESCE((SELECT max(position) + 1 FROM spheres), 0)) RETURNING id""",
            title, norm(title), about)
    return await get_sphere(conn, sphere_id)


KEEP = object()


async def update_sphere(conn: asyncpg.Connection, sphere_id: int, *, title: Any = KEEP,
                        description: Any = KEEP) -> dict[str, Any]:
    row = await _row(conn, sphere_id)
    new_title = row["title"] if title is KEEP else clean_title(title)
    about = row["description"] if description is KEEP else clean_description(description)
    if new_title != row["title"]:
        await _check_title(conn, new_title, sphere_id=sphere_id)
    if new_title != row["title"] or about != row["description"]:
        await conn.execute(
            """UPDATE spheres SET title = $2, title_norm = $3, description = $4, updated_at = now()
               WHERE id = $1""", sphere_id, new_title, norm(new_title), about)
    return await get_sphere(conn, sphere_id)


async def reorder(conn: asyncpg.Connection, ids: Sequence[int]) -> list[dict[str, Any]]:
    """Новый порядок сфер: `ids` — все сферы, по одному разу."""
    current = {r["id"] for r in await conn.fetch("SELECT id FROM spheres")}
    if len(ids) != len(set(ids)) or set(ids) != current:
        raise SpheresError("Порядок: нужен перечень всех сфер, каждая по одному разу.", "changed_meanwhile")
    await conn.execute(
        """UPDATE spheres s SET position = o.n - 1, updated_at = now()
           FROM unnest($1::bigint[]) WITH ORDINALITY AS o (id, n) WHERE s.id = o.id""", list(ids))
    return await list_spheres(conn)


async def delete_sphere(conn: asyncpg.Connection, sphere_id: int) -> dict[str, Any]:
    """Шаг 1: сфера снимается с чатов и проектов. Чаты, архив сообщений, проекты и их страницы
    остаются как были. Возвращает, со скольких чатов и проектов она снята."""
    row = await _row(conn, sphere_id)
    async with conn.transaction():
        chats = await conn.fetchval("SELECT count(*) FROM sphere_chats WHERE sphere_id = $1", sphere_id)
        projects = await conn.fetchval("SELECT count(*) FROM projects WHERE sphere_id = $1", sphere_id)
        await conn.execute("UPDATE projects SET sphere_id = NULL, updated_at = now() WHERE sphere_id = $1", sphere_id)
        await conn.execute("DELETE FROM spheres WHERE id = $1", sphere_id)   # чаты в сфере, папки, отказы — каскадом
    return {"id": sphere_id, "title": row["title"], "chats": int(chats), "projects": int(projects)}


# --- чаты в сферах -------------------------------------------------------------------------------

async def add_chats(conn: asyncpg.Connection, sphere_id: int, account_id: int, keys: Iterable[PeerKey], *,
                    origin: str = "owner") -> int:
    """Относит чаты аккаунта к сфере. Уже отнесённые не трогаются. Чат, добавленный вручную, снова
    может предлагаться связанной папкой (отметка «не предлагать» снимается). Сколько добавлено."""
    await _row(conn, sphere_id)
    keys = _keys(keys)
    if not keys:
        return 0
    if not await conn.fetchval("SELECT 1 FROM accounts WHERE id = $1", account_id):
        raise SpheresError("Такого аккаунта нет.", "not_found")
    classes, ids = [k[0] for k in keys], [k[1] for k in keys]
    async with conn.transaction():
        done = await conn.execute(
            """INSERT INTO sphere_chats (sphere_id, account_id, peer_class, tg_id, origin)
               SELECT $1, $2, k.c, k.i, $5 FROM unnest($3::text[], $4::bigint[]) AS k (c, i)
               ON CONFLICT DO NOTHING""", sphere_id, account_id, classes, ids, origin)
        await conn.execute(
            """DELETE FROM sphere_declined WHERE sphere_id = $1 AND account_id = $2
                 AND (peer_class, tg_id) IN (SELECT * FROM unnest($3::text[], $4::bigint[]))""",
            sphere_id, account_id, classes, ids)
        await conn.execute("UPDATE spheres SET updated_at = now() WHERE id = $1", sphere_id)
    return int(done.split()[-1])


async def remove_chats(conn: asyncpg.Connection, sphere_id: int, account_id: int, keys: Iterable[PeerKey]) -> int:
    """Убирает чаты из сферы. Связанная папка их больше не предлагает (`sphere_declined`). Сам чат,
    его чтение и сообщения не трогаются. Сколько убрано."""
    await _row(conn, sphere_id)
    keys = _keys(keys)
    if not keys:
        return 0
    classes, ids = [k[0] for k in keys], [k[1] for k in keys]
    async with conn.transaction():
        done = await conn.execute(
            """DELETE FROM sphere_chats WHERE sphere_id = $1 AND account_id = $2
                 AND (peer_class, tg_id) IN (SELECT * FROM unnest($3::text[], $4::bigint[]))""",
            sphere_id, account_id, classes, ids)
        if await conn.fetchval("SELECT 1 FROM accounts WHERE id = $1", account_id):
            await conn.execute(
                """INSERT INTO sphere_declined (sphere_id, account_id, peer_class, tg_id)
                   SELECT $1, $2, k.c, k.i FROM unnest($3::text[], $4::bigint[]) AS k (c, i)
                   ON CONFLICT DO NOTHING""", sphere_id, account_id, classes, ids)
        await conn.execute("UPDATE spheres SET updated_at = now() WHERE id = $1", sphere_id)
    return int(done.split()[-1])


async def set_chat_spheres(conn: asyncpg.Connection, account_id: int, key: PeerKey,
                           sphere_ids: Iterable[int]) -> dict[str, list[int]]:
    """Весь перечень сфер одного чата (выбор в строке чата): добавляет недостающие, убирает лишние."""
    wanted = list(dict.fromkeys(int(i) for i in sphere_ids))
    known = {r["id"] for r in await conn.fetch("SELECT id FROM spheres WHERE id = ANY($1::bigint[])", wanted)}
    if set(wanted) - known:
        raise SpheresError("Такой сферы нет.", "not_found")
    current = set((await spheres_of_keys(conn, account_id, [key])).get(key, []))
    added = [i for i in wanted if i not in current]
    removed = [i for i in current if i not in wanted]
    async with conn.transaction():
        for sphere_id in added:
            await add_chats(conn, sphere_id, account_id, [key])
        for sphere_id in removed:
            await remove_chats(conn, sphere_id, account_id, [key])
    return {"added": added, "removed": removed}


async def keys_of(conn: asyncpg.Connection, sphere_id: int, account_id: int) -> list[PeerKey]:
    rows = await conn.fetch(
        """SELECT peer_class, tg_id FROM sphere_chats WHERE sphere_id = $1 AND account_id = $2
           ORDER BY added_at, peer_class, tg_id""", sphere_id, account_id)
    return [(r["peer_class"], int(r["tg_id"])) for r in rows]


async def spheres_of_keys(conn: asyncpg.Connection, account_id: int,
                          keys: Iterable[PeerKey] | None = None) -> dict[PeerKey, list[int]]:
    """Сферы диалогов аккаунта: {ключ: [номера сфер по порядку сфер]}. keys None — все."""
    args: list[Any] = [account_id]
    where = "sc.account_id = $1"
    if keys is not None:
        keys = list(keys)
        args += [[k[0] for k in keys], [k[1] for k in keys]]
        where += " AND (sc.peer_class, sc.tg_id) IN (SELECT * FROM unnest($2::text[], $3::bigint[]))"
    rows = await conn.fetch(
        f"""SELECT sc.peer_class, sc.tg_id, s.id FROM sphere_chats sc JOIN spheres s ON s.id = sc.sphere_id
            WHERE {where} ORDER BY s.position, s.id""", *args)
    out: dict[PeerKey, list[int]] = {}
    for r in rows:
        out.setdefault((r["peer_class"], int(r["tg_id"])), []).append(r["id"])
    return out


async def sphere_chats(conn: asyncpg.Connection, sphere_id: int) -> list[dict[str, Any]]:
    """Чаты сферы для раздела «Сферы» страницы и приложения: название — из списка диалогов аккаунта,
    а если его там нет (вышли из группы, аккаунт отключён) — из архива; ещё — читается ли чат."""
    await _row(conn, sphere_id)
    rows = await conn.fetch(
        """SELECT sc.account_id, sc.peer_class, sc.tg_id, sc.origin,
                  COALESCE(d.title, c.title, p.name) AS title, COALESCE(d.chat_type, c.type) AS chat_type,
                  COALESCE(s.enabled, false) AS enabled, COALESCE(c.excluded, false) AS excluded,
                  a.label AS account_label, a.role AS account_role
           FROM sphere_chats sc
           JOIN accounts a ON a.id = sc.account_id
           LEFT JOIN tg_dialogs d ON d.account_id = sc.account_id AND d.peer_class = sc.peer_class AND d.tg_id = sc.tg_id
           LEFT JOIN peers p ON p.class = sc.peer_class AND p.tg_id = sc.tg_id
           LEFT JOIN chats c ON c.account_id = sc.account_id AND c.peer_id = p.id
           LEFT JOIN tg_sync_chats s ON s.account_id = sc.account_id AND s.peer_class = sc.peer_class AND s.tg_id = sc.tg_id
           WHERE sc.sphere_id = $1
           ORDER BY lower(COALESCE(d.title, c.title, p.name, '')), sc.tg_id""", sphere_id)
    return [{"account_id": r["account_id"], "peer_class": r["peer_class"], "tg_id": int(r["tg_id"]),
             "title": clean_line(r["title"], 80) or None, "type": r["chat_type"], "origin": r["origin"],
             "enabled": r["enabled"], "excluded": r["excluded"],
             "account_label": r["account_label"], "account_role": r["account_role"]} for r in rows]


# --- архив: какие сферы у чата, какие чаты в сферах ------------------------------------------------

async def titles_by_chat(conn: asyncpg.Connection, chat_ids: Iterable[int]) -> dict[int, list[str]]:
    """{chats.id: [названия сфер по порядку]} — пометка результатов для ассистента."""
    ids = list({int(i) for i in chat_ids if i is not None})
    if not ids:
        return {}
    rows = await conn.fetch(
        f"""SELECT c.id AS chat_id, s.title FROM chats c JOIN peers p ON p.id = c.peer_id {CHAT_KEY_JOIN}
            JOIN spheres s ON s.id = sc.sphere_id WHERE c.id = ANY($1::bigint[])
            ORDER BY s.position, s.id""", ids)
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["chat_id"], []).append(r["title"])
    return out


PERSON_RECENT = 2000            # по стольким последним сообщениям человека ищутся его сферы


async def titles_by_peer(conn: asyncpg.Connection, peer_ids: Iterable[int]) -> dict[int, list[str]]:
    """{peers.id: [названия сфер]} — сферы чатов, где у человека личный чат или где он писал (по
    `PERSON_RECENT` последним его видимым сообщениям). Исключённые чаты не считаются."""
    ids = list({int(i) for i in peer_ids if i is not None})
    if not ids:
        return {}
    rows = await conn.fetch(
        f"""SELECT DISTINCT x.peer_id, s.id, s.position, s.title
            FROM unnest($1::bigint[]) AS x (peer_id)
            JOIN LATERAL (
                SELECT t.chat_id FROM (SELECT m.chat_id FROM messages m
                                       WHERE m.sender_peer_id = x.peer_id AND m.deleted_at IS NULL AND m.agent_visible
                                       ORDER BY m.sent_at DESC LIMIT $2) t
                UNION SELECT dc.id FROM chats dc WHERE dc.peer_id = x.peer_id
            ) ch ON true
            JOIN chats c ON c.id = ch.chat_id AND NOT c.excluded
            JOIN peers p ON p.id = c.peer_id {CHAT_KEY_JOIN}
            JOIN spheres s ON s.id = sc.sphere_id
            ORDER BY x.peer_id, s.position, s.id""", ids, PERSON_RECENT)
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["peer_id"], []).append(r["title"])
    return out


async def titles_by_person(conn: asyncpg.Connection, person_ids: Iterable[int]) -> dict[int, list[str]]:
    """{people.id: [названия сфер]} по всем учётным записям человека из реестра."""
    ids = list({int(i) for i in person_ids if i is not None})
    if not ids:
        return {}
    links = await conn.fetch("SELECT person_id, peer_id FROM person_peers WHERE person_id = ANY($1::bigint[])", ids)
    by_peer = await titles_by_peer(conn, [r["peer_id"] for r in links])
    order = {r["title"]: (r["position"], r["id"]) for r in await conn.fetch("SELECT id, title, position FROM spheres")}
    out: dict[int, list[str]] = {}
    for r in links:
        for title in by_peer.get(r["peer_id"], []):
            if title not in out.setdefault(r["person_id"], []):
                out[r["person_id"]].append(title)
    return {k: sorted(v, key=lambda t: order.get(t, (0, 0))) for k, v in out.items()}


async def titles_by_project(conn: asyncpg.Connection, project_ids: Iterable[int]) -> dict[int, str]:
    ids = list({int(i) for i in project_ids if i is not None})
    if not ids:
        return {}
    rows = await conn.fetch(
        """SELECT pr.id, s.title FROM projects pr JOIN spheres s ON s.id = pr.sphere_id
           WHERE pr.id = ANY($1::bigint[])""", ids)
    return {r["id"]: r["title"] for r in rows}


@dataclass(frozen=True)
class Scope:
    """Сужение по сферам, о котором попросил ассистент: какие сферы и брать ли чаты вне сфер."""

    sphere_ids: tuple[int, ...] = ()
    titles: tuple[str, ...] = ()
    without: bool = False             # «без сферы» — чаты, не отнесённые ни к одной сфере

    def allows(self, names: Sequence[str]) -> bool:
        """Подходит ли результат с такими сферами (названия, как в пометке)."""
        if not names:
            return self.without
        keys = {norm(t) for t in self.titles}
        return any(norm(n) in keys for n in names)


async def resolve_scope(conn: asyncpg.Connection, value: Any) -> Scope | None:
    """Параметр `spheres` от ассистента: None, пусто или «все» — без сужения (None); иначе — названия
    сфер (без учёта регистра и «ё») и, по желанию, «без сферы». Незнакомое название — SpheresError с
    перечнем известных: молча расширять или сужать поиск нельзя."""
    if value is None:
        return None
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)) or not all(isinstance(i, str) for i in items):
        raise SpheresError("`spheres`: a list of sphere names, or \"all\".")
    words = [norm(clean_line(i, 120)) for i in items if norm(clean_line(i, 120))]
    if not words or any(w in ALL_WORDS for w in words):
        return None
    known = await conn.fetch("SELECT id, title, title_norm FROM spheres ORDER BY position, id")
    by_norm = {r["title_norm"]: r for r in known}
    ids, titles, without, unknown = [], [], False, []
    for w in words:
        if w in NONE_WORDS:
            without = True
        elif w in by_norm:
            if by_norm[w]["id"] not in ids:
                ids.append(by_norm[w]["id"])
                titles.append(by_norm[w]["title"])
        else:
            unknown.append(w)
    if unknown:
        listed = ", ".join(f"\"{r['title']}\"" for r in known) or "none yet"
        raise SpheresError(f"Unknown sphere name in `spheres`. The owner's spheres: {listed}. Pass exact names, "
                           "\"без сферы\" for chats outside any sphere, or \"all\".", "unknown_sphere")
    return Scope(tuple(ids), tuple(titles), without)


def query_names(params: Any) -> list[str] | None:
    """Параметр `spheres` маршрута внутреннего API: повторяющийся (?spheres=Работа&spheres=Личное) или
    одной строкой с переводом строки между названиями (так передаёт плагин). Нет параметра — None."""
    raw = params.getlist("spheres") if hasattr(params, "getlist") else [params.get("spheres")]
    names = [part for value in raw if value for part in str(value).split("\n") if part.strip()]
    return names or None


async def chat_ids_in(conn: asyncpg.Connection, scope: Scope) -> list[int]:
    """Чаты архива (`chats.id`) в этих сферах; с `without` — и чаты, не отнесённые ни к одной сфере."""
    rows = await conn.fetch(
        f"""SELECT c.id FROM chats c JOIN peers p ON p.id = c.peer_id
            WHERE EXISTS (SELECT 1 FROM sphere_chats sc WHERE sc.account_id = c.account_id
                          AND sc.peer_class = p.class AND sc.tg_id = p.tg_id AND sc.sphere_id = ANY($1::bigint[]))
               OR ($2 AND NOT EXISTS (SELECT 1 FROM sphere_chats sc WHERE sc.account_id = c.account_id
                                      AND sc.peer_class = p.class AND sc.tg_id = p.tg_id))""",
        list(scope.sphere_ids), scope.without)
    return [r["id"] for r in rows]


async def keys_by_chat_ids(conn: asyncpg.Connection, chat_ids: Iterable[int]) -> dict[int, tuple[int, PeerKey]]:
    """{chats.id: (аккаунт, ключ диалога)} — для изменений по номерам чатов архива (внутренний API)."""
    ids = list({int(i) for i in chat_ids})
    rows = await conn.fetch(
        """SELECT c.id, c.account_id, p.class, p.tg_id FROM chats c JOIN peers p ON p.id = c.peer_id
           WHERE c.id = ANY($1::bigint[]) AND NOT c.excluded""", ids)
    return {r["id"]: (r["account_id"], (r["class"], int(r["tg_id"]))) for r in rows}


# --- проекты -------------------------------------------------------------------------------------

async def set_project_sphere(conn: asyncpg.Connection, project_id: int, sphere_id: int | None) -> dict[str, Any]:
    row = await conn.fetchrow("SELECT id, status, sphere_id FROM projects WHERE id = $1 FOR UPDATE", project_id)
    if row is None or row["status"] not in ("active", "archived"):
        raise SpheresError("Такого проекта нет.", "not_found")
    if sphere_id is not None:
        await _row(conn, sphere_id)
    if row["sphere_id"] != sphere_id:
        await conn.execute("UPDATE projects SET sphere_id = $2, updated_at = now() WHERE id = $1", project_id, sphere_id)
    return {"project_id": project_id, "sphere_id": sphere_id, "changed": row["sphere_id"] != sphere_id}


# --- папки Telegram ------------------------------------------------------------------------------
#
# Состав папки сервис вычисляет сам по списку диалогов, повторяя правило клиентов Telegram (Telegram
# Desktop, Data::ChatFilter::contains): чат из «никогда» (exclude) — не в папке; из «всегда» (include
# и закреплённые) — в папке; иначе — если подходит под вид (контакты, не контакты, группы, каналы,
# боты) и не отсеян правилами «без звука», «прочитанные», «архив». Что из этого точно, а что нет —
# `FOLDER_NOTES` (и docs/spheres.md).

FOLDER_NOTES = {
    "exclude_muted": "«Без звука» — по выключенным уведомлениям самого чата; общие настройки Telegram "
                     "(«все группы без звука») сервису не видны, поэтому такие чаты могут попасть сюда.",
    "exclude_read": "«Прочитанные» — по состоянию на последнее обновление списка из Telegram.",
    "contacts": "«Контакты» — по отметке Telegram на момент обновления списка.",
    "non_contacts": "«Не контакты» — по отметке Telegram на момент обновления списка.",
}
MISSING_NOTE = "Часть чатов папки не найдена в списке диалогов (например, вы вышли из группы) — их здесь нет."


def _ref(key: PeerKey) -> str:
    return f"{key[0]}:{key[1]}"


def _kind_flag(d: dl.DialogRow) -> str:
    if d.chat.peer_class == "user":
        if d.chat.is_bot or d.chat.type == "bot_chat":
            return "bots"
        return "contacts" if (d.is_contact or d.chat.type == "saved_messages") else "non_contacts"
    if d.chat.type in ("private_channel", "public_channel"):
        return "broadcasts"
    return "groups"


def folder_members(folder: dl.FolderRow, dialogs: Sequence[dl.DialogRow], *, now: datetime) -> list[PeerKey]:
    """Чаты папки из списка диалогов, в его порядке."""
    never, always, flags = set(folder.exclude), set(folder.pinned) | set(folder.include), set(folder.flags)
    out: list[PeerKey] = []
    for d in dialogs:
        ref = _ref(d.key)
        if ref in never:
            continue
        if ref in always:
            out.append(d.key)
            continue
        if folder.kind == "chatlist" or _kind_flag(d) not in flags:
            continue
        if "exclude_muted" in flags and d.muted_until is not None and d.muted_until > now:
            continue
        if "exclude_read" in flags and not d.unread:
            continue
        if "exclude_archived" in flags and d.folder_id == 1:
            continue
        out.append(d.key)
    return out


def folder_notes(folder: dl.FolderRow, dialogs: Sequence[dl.DialogRow]) -> list[str]:
    """Что в составе папки вычислено не точно."""
    notes = [FOLDER_NOTES[f] for f in folder.flags if f in FOLDER_NOTES]
    present = {_ref(d.key) for d in dialogs}
    if any(ref not in present for ref in set(folder.include) | set(folder.pinned)):
        notes.append(MISSING_NOTE)
    return notes


async def _now(conn: asyncpg.Connection) -> datetime:
    return await conn.fetchval("SELECT now()")


async def folder(conn: asyncpg.Connection, account_id: int, folder_id: int) -> dl.FolderRow:
    found = next((f for f in await dl.folders(conn, account_id) if f.folder_id == folder_id), None)
    if found is None:
        raise SpheresError("Такой папки нет в Telegram (или список ещё не обновлялся).", "not_found")
    return found


async def folder_keys(conn: asyncpg.Connection, account_id: int, folder_id: int) -> list[PeerKey]:
    found = await folder(conn, account_id, folder_id)
    return folder_members(found, await dl.rows(conn, account_id), now=await _now(conn))


async def _unique_title(conn: asyncpg.Connection, title: str) -> str:
    base = clean_line(title, TITLE_LIMIT - 4).strip() or "Папка"
    if norm(base) in ALL_WORDS or norm(base) in NONE_WORDS:
        base = f"Папка «{base}»"[:TITLE_LIMIT - 4]
    candidate, n = base, 2
    while await conn.fetchval("SELECT 1 FROM spheres WHERE title_norm = $1", norm(candidate)):
        candidate, n = f"{base} {n}", n + 1
    return candidate


async def make_from_folder(conn: asyncpg.Connection, account_id: int, folder_id: int, *,
                           title: Any = None, sphere_id: int | None = None) -> dict[str, Any]:
    """«Сделать сферу из папки»: заводит сферу (по умолчанию — с названием папки) и относит к ней все
    чаты папки. Повторно для той же папки — дополняет сделанную из неё сферу. `sphere_id` — отнести
    чаты папки к уже существующей сфере. Возвращает сферу, создана ли она и сколько чатов добавлено."""
    found = await folder(conn, account_id, folder_id)
    keys = folder_members(found, await dl.rows(conn, account_id), now=await _now(conn))
    created = False
    async with conn.transaction():
        if sphere_id is None:
            sphere_id = await conn.fetchval(
                """SELECT f.sphere_id FROM sphere_folders f JOIN spheres s ON s.id = f.sphere_id
                   WHERE f.account_id = $1 AND f.folder_id = $2 ORDER BY f.linked_at, f.sphere_id LIMIT 1""",
                account_id, folder_id)
        if sphere_id is None:
            name = clean_title(title) if title is not None else await _unique_title(conn, found.title)
            sphere_id = (await create_sphere(conn, name))["id"]
            created = True
        else:
            await _row(conn, sphere_id)
        # отказы владельца от чатов этой сферы уважаются и здесь: «дополнить» не возвращает убранное
        declined = {(r["peer_class"], int(r["tg_id"])) for r in await conn.fetch(
            "SELECT peer_class, tg_id FROM sphere_declined WHERE sphere_id = $1 AND account_id = $2",
            sphere_id, account_id)}
        added = await add_chats(conn, sphere_id, account_id, [k for k in keys if k not in declined], origin="folder")
        await conn.execute(
            """INSERT INTO sphere_folders (sphere_id, account_id, folder_id, folder_title)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (sphere_id, account_id, folder_id) DO UPDATE SET folder_title = EXCLUDED.folder_title""",
            sphere_id, account_id, folder_id, found.title)
    return {"sphere": await get_sphere(conn, sphere_id), "created": created, "added": added,
            "folder_chats": len(keys), "skipped": len(keys) - added}


async def link_folder(conn: asyncpg.Connection, sphere_id: int, account_id: int, folder_id: int, *,
                      propose: bool) -> dict[str, Any]:
    """«Связать папку со сферой» (propose=True): новые чаты папки будут предлагаться в сферу. propose=False —
    связь снята (сфера и её чаты остаются)."""
    await _row(conn, sphere_id)
    if propose:
        found = await folder(conn, account_id, folder_id)
        await conn.execute(
            """INSERT INTO sphere_folders (sphere_id, account_id, folder_id, propose, folder_title)
               VALUES ($1, $2, $3, true, $4)
               ON CONFLICT (sphere_id, account_id, folder_id) DO UPDATE SET propose = true,
                   folder_title = EXCLUDED.folder_title""", sphere_id, account_id, folder_id, found.title)
    else:
        await conn.execute("DELETE FROM sphere_folders WHERE sphere_id = $1 AND account_id = $2 AND folder_id = $3",
                           sphere_id, account_id, folder_id)
    return await get_sphere(conn, sphere_id)


async def proposals(conn: asyncpg.Connection, sphere_id: int | None = None) -> list[dict[str, Any]]:
    """Чаты связанных папок, которых нет в сфере и от которых владелец не отказывался: «новые в
    папке: N, добавить в сферу?». Сервис их не добавляет сам — только показывает."""
    links = await conn.fetch(
        """SELECT f.sphere_id, f.account_id, f.folder_id, s.title AS sphere_title
           FROM sphere_folders f JOIN spheres s ON s.id = f.sphere_id
           WHERE f.propose AND ($1::bigint IS NULL OR f.sphere_id = $1)
           ORDER BY s.position, s.id, f.folder_id""", sphere_id)
    if not links:
        return []
    now = await _now(conn)
    cache: dict[int, tuple[list[dl.FolderRow], list[dl.DialogRow]]] = {}
    out = []
    for link in links:
        account_id = link["account_id"]
        if account_id not in cache:
            cache[account_id] = (await dl.folders(conn, account_id), await dl.rows(conn, account_id))
        folders_, dialogs = cache[account_id]
        found = next((f for f in folders_ if f.folder_id == link["folder_id"]), None)
        if found is None:
            continue
        members = folder_members(found, dialogs, now=now)
        have = set(await keys_of(conn, link["sphere_id"], account_id))
        declined = {(r["peer_class"], int(r["tg_id"])) for r in await conn.fetch(
            "SELECT peer_class, tg_id FROM sphere_declined WHERE sphere_id = $1 AND account_id = $2",
            link["sphere_id"], account_id)}
        new = [k for k in members if k not in have and k not in declined]
        if not new:
            continue
        titles = {d.key: d.chat.name for d in dialogs}
        out.append({"sphere_id": link["sphere_id"], "sphere_title": link["sphere_title"], "account_id": account_id,
                    "folder_id": found.folder_id, "folder_title": found.title, "count": len(new),
                    "chats": [{"peer_class": k[0], "tg_id": k[1], "title": clean_line(titles.get(k), 80) or None}
                              for k in new[:50]],
                    "keys": new})
    return out


async def decide_proposals(conn: asyncpg.Connection, sphere_id: int, account_id: int, folder_id: int, *,
                           accept: bool, keys: Iterable[PeerKey] | None = None) -> dict[str, int]:
    """Решение по предложенным чатам папки: принять — в сферу; отказаться — больше не предлагать.
    keys None — все предложенные сейчас."""
    found = next((p for p in await proposals(conn, sphere_id)
                  if p["account_id"] == account_id and p["folder_id"] == folder_id), None)
    offered = found["keys"] if found else []
    chosen = offered if keys is None else [k for k in _keys(keys) if k in offered]
    if not chosen:
        return {"added": 0, "declined": 0}
    if accept:
        return {"added": await add_chats(conn, sphere_id, account_id, chosen, origin="folder"), "declined": 0}
    await conn.execute(
        """INSERT INTO sphere_declined (sphere_id, account_id, peer_class, tg_id)
           SELECT $1, $2, k.c, k.i FROM unnest($3::text[], $4::bigint[]) AS k (c, i) ON CONFLICT DO NOTHING""",
        sphere_id, account_id, [k[0] for k in chosen], [k[1] for k in chosen])
    return {"added": 0, "declined": len(chosen)}


# --- раскладки списка чатов ----------------------------------------------------------------------
#
# Раздел — набор диалогов аккаунта. Раскладки: папки Telegram, сферы, активность (по времени последнего
# сообщения из списка диалогов — текст не читается), вид. Состав считается здесь, по списку диалогов из
# базы; страница раздела — тем же запросом, что весь список (`dialogs.page` с `keys`).

LAYOUTS = ("folders", "spheres", "activity", "kind")
ACTIVITY = (("week", "За неделю", 7), ("month", "За месяц", 30), ("quarter", "За 3 месяца", 90),
            ("silent", "Давно молчат", None))
KIND_SECTIONS = (("personal", "Личные", ("personal_chat", "saved_messages")),
                 ("group", "Группы", ("private_group", "private_supergroup", "public_supergroup")),
                 ("channel", "Каналы", ("private_channel", "public_channel")),
                 ("bot", "Боты", ("bot_chat",)))
_SECTION = re.compile(r"(folder|sphere):(\d{1,10}|none)|activity:(week|month|quarter|silent)"
                      r"|kind:(personal|group|channel|bot)")


@dataclass
class Section:
    id: str
    title: str
    keys: list[PeerKey]
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def _activity(d: dl.DialogRow, now: datetime) -> str:
    at = d.last_message_at
    if at is None:
        return "silent"
    for name, _, days in ACTIVITY:
        if days is not None and at >= now - timedelta(days=days):
            return name
    return "silent"


def _kind(d: dl.DialogRow) -> str:
    for name, _, types_ in KIND_SECTIONS:
        if d.chat.type in types_:
            return name
    return "bot" if d.chat.is_bot else "personal"


async def sections(conn: asyncpg.Connection, account_id: int, layout: str,
                   dialogs: Sequence[dl.DialogRow] | None = None) -> list[Section]:
    """Разделы раскладки — с составом (ключи в порядке списка Telegram). Пустые разделы папок и сфер
    тоже показываются (папка пуста, в сферу ещё ничего не отнесено); пустые разделы активности и вида —
    нет."""
    if layout not in LAYOUTS:
        raise SpheresError("Раскладка: folders, spheres, activity или kind.")
    dialogs = list(dialogs) if dialogs is not None else await dl.rows(conn, account_id)
    now = await _now(conn)
    out: list[Section] = []
    if layout == "folders":
        placed: set[PeerKey] = set()
        links = await conn.fetch(
            """SELECT f.folder_id, s.id, s.title, f.propose FROM sphere_folders f JOIN spheres s ON s.id = f.sphere_id
               WHERE f.account_id = $1 ORDER BY s.position, s.id""", account_id)
        for f in await dl.folders(conn, account_id):
            keys = folder_members(f, dialogs, now=now)
            placed.update(keys)
            out.append(Section(f"folder:{f.folder_id}", f.title, keys, folder_notes(f, dialogs), {
                "emoticon": f.emoticon, "kind": f.kind,
                "spheres": [{"id": r["id"], "title": r["title"], "propose": r["propose"]}
                            for r in links if r["folder_id"] == f.folder_id]}))
        out.append(Section("folder:none", "Не в папках", [d.key for d in dialogs if d.key not in placed]))
    elif layout == "spheres":
        by_key = await spheres_of_keys(conn, account_id)
        for s in await conn.fetch("SELECT id, title, description FROM spheres ORDER BY position, id"):
            out.append(Section(f"sphere:{s['id']}", s["title"], [d.key for d in dialogs if s["id"] in by_key.get(d.key, [])],
                               extra={"description": s["description"]}))
        out.append(Section("sphere:none", "Без сферы", [d.key for d in dialogs if not by_key.get(d.key)]))
    elif layout == "activity":
        for name, title, _ in ACTIVITY:
            keys = [d.key for d in dialogs if _activity(d, now) == name]
            if keys:
                out.append(Section(f"activity:{name}", title, keys))
    else:
        for name, title, _ in KIND_SECTIONS:
            keys = [d.key for d in dialogs if _kind(d) == name]
            if keys:
                out.append(Section(f"kind:{name}", title, keys))
    return out


async def section_keys(conn: asyncpg.Connection, account_id: int, section: str) -> tuple[Section, list[dl.DialogRow]]:
    """Один раздел по его номеру ('folder:3', 'sphere:none', 'activity:week', 'kind:bot')."""
    found = _SECTION.fullmatch(section or "")
    if found is None:
        raise SpheresError("Раздел указан неверно.")
    layout = {"folder": "folders", "sphere": "spheres", "activity": "activity", "kind": "kind"}[section.split(":")[0]]
    dialogs = await dl.rows(conn, account_id)
    for s in await sections(conn, account_id, layout, dialogs):
        if s.id == section:
            return s, dialogs
    if layout in ("activity", "kind"):
        return Section(section, "", []), dialogs        # пустой раздел активности или вида
    raise SpheresError("Такого раздела нет — обновите страницу.", "not_found")
