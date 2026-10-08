"""Task-bound source broker. Reading a source never authorizes disclosing it to a chat.

Only verified owner entry points call grant_for_task. Agent results and HTTP callers can
request access, but cannot create a grant. Receipts are service-issued and checked again
against current visibility and revisions before a draft is sent.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import bridge, sanitize
from . import mcp_client
from .registry import ID, SourceError

MAX_ITEMS = 10
MAX_CHARS = 12000
_KEYS = {"kind", "source_id", "query", "since", "until", "limit", "max_chars", "reason"}


def revision(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _object(value):
    return json.loads(value) if isinstance(value, str) else value


def _date(value):
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc).isoformat()
    except (AttributeError, TypeError, ValueError):
        raise SourceError("invalid_source_window") from None


def canonical_source_spec(raw: dict) -> dict:
    if not isinstance(raw, dict) or raw.keys() - _KEYS:
        raise SourceError("invalid_source_request")
    kind = raw.get("kind")
    if kind not in {"chat", "memory", "external"}:
        raise SourceError("invalid_source_kind")
    sid = raw.get("source_id")
    if sid is not None:
        if not isinstance(sid, str):
            raise SourceError("invalid_source_id")
        if kind == "chat":
            if not re.fullmatch(r"[1-9]\d{0,17}", sid):
                raise SourceError("invalid_source_id")
        elif kind == "memory":
            if not re.fullmatch(r"person:[1-9]\d{0,17}", sid):
                raise SourceError("invalid_source_id")
        elif not ID.fullmatch(sid):
            raise SourceError("invalid_source_id")
    if kind == "external" and sid is None:
        raise SourceError("source_id_required")
    query = raw.get("query", "")
    reason = raw.get("reason", "Для подготовки ответа в этом чате")
    if not isinstance(query, str) or len(query) > 500 or not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise SourceError("invalid_source_request")
    limit, chars = raw.get("limit", MAX_ITEMS), raw.get("max_chars", MAX_CHARS)
    if type(limit) is not int or not 1 <= limit <= MAX_ITEMS or type(chars) is not int or not 1 <= chars <= MAX_CHARS:
        raise SourceError("invalid_source_limit")
    since, until = _date(raw.get("since")), _date(raw.get("until"))
    if since and until and since >= until:
        raise SourceError("invalid_source_window")
    if kind == "memory" and (since or until):
        raise SourceError("memory_window_not_supported")
    if not query.strip() and sid is None:
        raise SourceError("query_required")
    return {"kind": kind, "source_id": sid, "query": query.strip(), "since": since,
            "until": until, "limit": limit, "max_chars": chars,
            "reason": sanitize.clean_line(reason, 500)}


async def _task(conn, task):
    task_id = task.get("id") if isinstance(task, dict) else task
    if type(task_id) is not int:
        raise SourceError("invalid_task")
    row = await conn.fetchrow("SELECT * FROM reply_tasks WHERE id=$1", task_id)
    if row is None or (isinstance(task, dict) and task.get("chat_id", row["chat_id"]) != row["chat_id"]):
        raise SourceError("invalid_task")
    if row["expires_at"] <= datetime.now(timezone.utc) or row["status"] in {
            "cancelled", "expired", "failed", "sent", "rejected", "declined", "done", "completed"}:
        raise SourceError("inactive_task")
    return dict(row)


async def resolve_request(conn, state, request, task=None):
    spec = canonical_source_spec(request)
    if task is not None:
        await _task(conn, task)
    if spec["kind"] == "external":
        connector = (state.extras.get("source_registry") or {}).get(spec["source_id"])
        if connector is None:
            raise SourceError("source_not_registered")
        if any(spec[k] and k not in connector.search_args for k in ("since", "until")):
            raise SourceError("source_window_not_supported")
    elif spec["kind"] == "chat" and spec["source_id"]:
        if not await conn.fetchval(_CHAT_EXISTS, int(spec["source_id"])):
            raise SourceError("source_not_available")
    return spec


_CHAT_EXISTS = """SELECT EXISTS(SELECT 1 FROM chats c JOIN peers p ON p.id=c.peer_id
    WHERE c.id=$1 AND NOT c.excluded AND NOT EXISTS
    (SELECT 1 FROM control_peers cp WHERE p.class='user' AND cp.tg_id=p.tg_id))"""


async def _owner(conn, owner_id):
    from .. import authority
    owner = await bridge.get_owner(conn)
    principal = authority.get_owner_principal()
    if not principal or (principal.user_id is not None and principal.user_id != owner_id) or not bridge.owns_bot() or owner is None or owner.get("user_id") != owner_id:
        raise SourceError("owner_authority_required")


async def grant_for_task(conn, task, request, *, owner_id: int, mode="read", expires_at=None,
                         persistent=False) -> dict:
    """Call only from verified own-bot/owner-page context, never from a model job result."""
    await _owner(conn, owner_id)
    current = await _task(conn, task)
    spec = canonical_source_spec(request)
    if mode not in {"read", "disclose"} or type(persistent) is not bool:
        raise SourceError("invalid_grant")
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(days=30) if persistent else current["expires_at"]
    if expires_at is not None:
        requested = datetime.fromisoformat(_date(
            expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at))
        deadline = min(deadline, requested)
    if deadline <= now:
        raise SourceError("invalid_grant_expiry")
    if spec["kind"] == "chat" and spec["source_id"] and not await conn.fetchval(_CHAT_EXISTS, int(spec["source_id"])):
        raise SourceError("source_not_available")
    row = await conn.fetchrow("""INSERT INTO source_grants
        (task_id,target_chat_id,target_topic_tg_id,kind,source_id,request,mode,owner_id,expires_at)
        VALUES($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9) RETURNING id,expires_at""",
        None if persistent else current["id"], current["chat_id"], current.get("topic_tg_id"), spec["kind"], spec["source_id"],
        json.dumps(spec), mode, owner_id, deadline)
    return {"id": row["id"], "expires_at": row["expires_at"].isoformat(), "mode": mode}


async def revoke_grant(conn, grant_id: int, *, owner_id: int):
    await _owner(conn, owner_id)
    await conn.execute("UPDATE source_grants SET revoked_at=now() WHERE id=$1", grant_id)


async def _grant(conn, task, spec, mode):
    return await conn.fetchrow("""SELECT * FROM source_grants WHERE
        (task_id=$1 OR task_id IS NULL) AND target_chat_id=$2
        AND (request - 'reason')=($3::jsonb - 'reason')
        AND mode=$4 AND revoked_at IS NULL AND expires_at>clock_timestamp()
        AND target_topic_tg_id IS NOT DISTINCT FROM $5::bigint
        AND owner_id=(SELECT (value->>'user_id')::bigint FROM settings WHERE key='owner')
        ORDER BY id DESC LIMIT 1""",
        task["id"], task["chat_id"], json.dumps(spec), mode, task.get("topic_tg_id"))


_VISIBLE = """m.deleted_at IS NULL AND m.agent_visible AND NOT c.excluded
    AND NOT EXISTS(SELECT 1 FROM control_peers cp WHERE p.class='user' AND cp.tg_id=p.tg_id)
    AND NOT EXISTS(SELECT 1 FROM peers sp JOIN control_peers cp ON cp.tg_id=sp.tg_id
                   WHERE sp.id=m.sender_peer_id AND sp.class='user')"""
_MESSAGES = """SELECT m.id,m.chat_id,m.text,m.sent_at,m.topic_tg_id FROM messages m
    JOIN chats c ON c.id=m.chat_id JOIN peers p ON p.id=c.peer_id WHERE """ + _VISIBLE + """
    AND ($1::bigint IS NULL OR m.chat_id=$1) AND ($2::timestamptz IS NULL OR m.sent_at>=$2)
    AND ($3::timestamptz IS NULL OR m.sent_at<$3)
    AND ($4='' OR m.fts @@ websearch_to_tsquery('russian',$4))
    AND (NOT $6::boolean OR m.topic_tg_id IS NOT DISTINCT FROM $7::bigint)
    ORDER BY m.sent_at DESC,m.id DESC LIMIT $5"""


async def _memory_rows(conn, spec):
    # Refuse a whole derived page when any underlying source lost visibility. Old block
    # text can still contain a removed claim until the asynchronous rebuild catches up.
    return await conn.fetch("""SELECT g.entity_id,b.block,b.text FROM pages g
        JOIN page_blocks b ON b.page_id=g.id JOIN people person ON person.id=g.person_id
        WHERE NOT g.security_quarantined AND NOT g.dirty AND person.merged_into IS NULL
        AND g.problem IS NULL AND b.block<>'head'
        AND ($1::text IS NULL OR g.entity_id=$1)
        AND ($2='' OR b.fts @@ websearch_to_tsquery('russian',$2))
        AND (NOT EXISTS(SELECT 1 FROM person_peers pp WHERE pp.person_id=g.person_id)
          OR EXISTS(SELECT 1 FROM person_peers pp JOIN chats c ON c.peer_id=pp.peer_id
            JOIN peers p ON p.id=c.peer_id WHERE pp.person_id=g.person_id AND NOT c.excluded
            AND NOT EXISTS(SELECT 1 FROM control_peers cp WHERE p.class='user' AND cp.tg_id=p.tg_id))
          OR EXISTS(SELECT 1 FROM person_peers pp JOIN messages m ON m.sender_peer_id=pp.peer_id
            JOIN chats c ON c.id=m.chat_id JOIN peers p ON p.id=c.peer_id
            WHERE pp.person_id=g.person_id AND """ + _VISIBLE + """))
        AND NOT EXISTS(SELECT 1 FROM page_entries e WHERE e.page_id=g.id AND
          (e.removed_at IS NOT NULL OR e.n_sources<>(SELECT count(*) FROM page_entry_sources s
            JOIN messages m ON m.id=s.message_id JOIN chats c ON c.id=m.chat_id
            JOIN peers p ON p.id=c.peer_id WHERE s.entry_id=e.id AND """ + _VISIBLE + """)))
        ORDER BY g.id,b.block LIMIT $3""", spec["source_id"], spec["query"], spec["limit"])


async def read_for_task(conn, state, task, request):
    current = await _task(conn, task)
    spec = canonical_source_spec(request)
    try:
        spec = await resolve_request(conn, state, spec, current)
    except SourceError:
        return {"status": "unavailable", "request": spec, "snippets": [], "source_refs": []}
    same_chat = spec["kind"] == "chat" and spec["source_id"] == str(current["chat_id"])
    grant = None if same_chat else await _grant(conn, current, spec, "read")
    if not same_chat and grant is None:
        return {"status": "access_required", "request": spec, "snippets": [], "source_refs": []}
    snippets, refs, remaining = [], [], spec["max_chars"]
    try:
        if spec["kind"] == "chat":
            rows = await conn.fetch(_MESSAGES, int(spec["source_id"]) if spec["source_id"] else None,
                                    datetime.fromisoformat(spec["since"]) if spec["since"] else None,
                                    datetime.fromisoformat(spec["until"]) if spec["until"] else None,
                                    spec["query"], spec["limit"], same_chat, current.get("topic_tg_id"))
            rows = [{"text": r["text"], "source_id": str(r["chat_id"]), "message_id": r["id"],
                     "topic_tg_id": r["topic_tg_id"]} for r in rows]
        elif spec["kind"] == "memory":
            rows = [{"text": r["text"], "source_id": r["entity_id"], "block": r["block"]}
                    for r in await _memory_rows(conn, spec)]
        else:
            connector = state.extras["source_registry"][spec["source_id"]]
            rows = [{**r, "source_id": spec["source_id"], "connector_revision": connector.revision}
                    for r in await mcp_client.search(connector, spec)]
        # A remote fetch can outlive its lease or owner revocation. Check again before
        # releasing any fragment to the model; transaction time is not wall-clock time.
        await _task(conn, current)
        if grant is not None and not await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM source_grants
            WHERE id=$1 AND revoked_at IS NULL AND expires_at>clock_timestamp()
            AND owner_id=(SELECT (value->>'user_id')::bigint FROM settings WHERE key='owner'))""", grant["id"]):
            raise SourceError("source_access_revoked")
        for row in rows:
            if remaining <= 0:
                break
            raw_text = row.pop("text")
            ref = {**row, "kind": spec["kind"], "revision": revision(raw_text), "visibility": "agent_visible"}
            receipt_id = await conn.fetchval("""INSERT INTO source_reads(task_id,grant_id,request,source_ref)
                VALUES($1,$2,$3::jsonb,$4::jsonb) RETURNING id""", current["id"], grant["id"] if grant else None,
                json.dumps(spec), json.dumps(ref))
            ref["receipt_id"] = receipt_id
            text = sanitize.clean_text(raw_text, min(4000, remaining))
            remaining -= len(text)
            snippets.append({"text": "[untrusted]\n" + text + "\n[/untrusted]", "source_ref": ref})
            refs.append(ref)
    except SourceError:
        return {"status": "unavailable", "request": spec, "snippets": [], "source_refs": []}
    return {"status": "ok", "request": spec, "snippets": snippets, "source_refs": refs}


async def _receipt(conn, task, ref):
    rid = ref.get("receipt_id")
    if type(rid) is not int:
        return None
    row = await conn.fetchrow("SELECT * FROM source_reads WHERE id=$1 AND task_id=$2", rid, task["id"])
    if row is None or {k: v for k, v in ref.items() if k != "receipt_id"} != _object(row["source_ref"]):
        return None
    if row["grant_id"] is not None and not await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM source_grants
        WHERE id=$1 AND revoked_at IS NULL AND expires_at>clock_timestamp()
        AND owner_id=(SELECT (value->>'user_id')::bigint FROM settings WHERE key='owner'))""", row["grant_id"]):
        return None
    return row


async def validate_refs(conn, state, task, refs) -> bool:
    try:
        current = await _task(conn, task)
        if not isinstance(refs, list) or len(refs) > 30:
            return False
        for ref in refs:
            if not isinstance(ref, dict):
                return False
            receipt = await _receipt(conn, current, ref)
            if receipt is None:
                # Compatibility with service-built initial context references: exact same
                # chat only, revision checked below. Model-provided refs are never accepted
                # by workflow unless present in the service's initial context.
                if ref.get("receipt_id") is not None or ref.get("kind") != "chat" or ref.get("source_id") != str(current["chat_id"]):
                    return False
            if ref["kind"] == "chat":
                row = await conn.fetchrow("SELECT m.text,m.chat_id,m.topic_tg_id FROM messages m JOIN chats c ON c.id=m.chat_id "
                    "JOIN peers p ON p.id=c.peer_id WHERE m.id=$1 AND " + _VISIBLE, ref.get("message_id"))
                if row is None or str(row["chat_id"]) != ref["source_id"] or revision(row["text"]) != ref["revision"]:
                    return False
                if receipt is None and row["topic_tg_id"] != current.get("topic_tg_id"):
                    return False
                if "topic_tg_id" in ref and row["topic_tg_id"] != ref["topic_tg_id"]:
                    return False
            elif ref["kind"] == "memory":
                spec = {**_object(receipt["request"]), "source_id": ref["source_id"], "query": ""}
                rows = await _memory_rows(conn, spec)
                if not any(r["block"] == ref.get("block") and revision(r["text"]) == ref["revision"] for r in rows):
                    return False
            elif ref["kind"] == "external":
                connector = (state.extras.get("source_registry") or {}).get(ref["source_id"])
                if connector is None or connector.revision != ref.get("connector_revision"):
                    return False
                row = await mcp_client.read(connector, ref["resource_id"])
                if row is None or row["remote_revision"] != ref.get("remote_revision") or revision(row["text"]) != ref["revision"]:
                    return False
            else:
                return False
        return True
    except (SourceError, KeyError, TypeError, ValueError):
        return False


refs_valid = validate_refs


@bridge.on_owner_change
async def _owner_changed(conn, new_user_id):
    # Clearing/rebinding the owner never revives a previous principal's policies.
    await conn.execute("UPDATE source_grants SET revoked_at=now() WHERE revoked_at IS NULL AND owner_id<>$1",
                       new_user_id)


async def disclosure_allowed(conn, task, refs) -> bool:
    """Automatic sending only: owner-reviewed exact drafts may disclose their shown text.

This never replaces validate_refs (revoked read permission invalidates a reviewed draft).
"""
    try:
        current = await _task(conn, task)
        for ref in refs:
            if ref.get("kind") == "chat" and ref.get("source_id") == str(current["chat_id"]):
                topic = await conn.fetchval("SELECT topic_tg_id FROM messages WHERE id=$1", ref.get("message_id"))
                if topic == current.get("topic_tg_id"):
                    continue
            receipt = await _receipt(conn, current, ref)
            if receipt is None or await _grant(conn, current, _object(receipt["request"]), "disclose") is None:
                return False
        return True
    except (SourceError, KeyError, TypeError, ValueError):
        return False
