"""OAuth: public clients, S256, точные адреса, одноразовые коды, отзыв семейства токенов."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from urllib.parse import urlsplit

from .. import authority, bridge

SCOPE = "archive:read"
ACCESS_TTL = 900
GRANT_TTL = 30 * 86400
AUTH_TTL = 600
CODE_TTL = 120


class OAuthError(Exception):
    def __init__(self, error="invalid_request", status=400):
        self.error, self.status = error, status


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def secret() -> str:
    return secrets.token_urlsafe(32)


def pkce(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", value):
        raise OAuthError("invalid_grant")
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode("ascii")).digest()).rstrip(b"=").decode()


def redirects(values, allow_loopback=False):
    if not isinstance(values, list) or not 1 <= len(values) <= 8:
        raise OAuthError("invalid_client_metadata")
    out = []
    for value in values:
        if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
            raise OAuthError("invalid_redirect_uri")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise OAuthError("invalid_redirect_uri") from None
        local = allow_loopback and parsed.hostname in ("127.0.0.1", "::1", "localhost")
        if not parsed.hostname or parsed.username or parsed.password or parsed.fragment \
                or parsed.scheme != "https" and not (local and parsed.scheme == "http") \
                or port == 0 or "\\" in value or "#" in value or any(ord(c)<32 or ord(c)==127 for c in value):
            raise OAuthError("invalid_redirect_uri")
        out.append(value)
    return list(dict.fromkeys(out))


async def register(conn, data, *, allow_loopback=False, client_id=None):
    if not isinstance(data, dict):
        raise OAuthError("invalid_client_metadata")
    if data.get("token_endpoint_auth_method", "none") != "none" \
            or data.get("grant_types", ["authorization_code", "refresh_token"]) != ["authorization_code", "refresh_token"] \
            or data.get("response_types", ["code"]) != ["code"] \
            or data.get("scope", SCOPE) != SCOPE:
        raise OAuthError("invalid_client_metadata")
    name = data.get("client_name", "Клиент MCP")
    if not isinstance(name, str) or not 1 <= len(name) <= 100 or any(ord(c) < 32 for c in name):
        raise OAuthError("invalid_client_metadata")
    uris = redirects(data.get("redirect_uris"), allow_loopback)
    if await conn.fetchval("SELECT count(*) FROM remote_mcp_clients") >= 500:
        raise OAuthError("temporarily_unavailable", 429)
    cid = client_id or secret()
    await conn.execute("INSERT INTO remote_mcp_clients(id,name,redirect_uris,expires_at) VALUES($1,$2,$3::jsonb,"
                       "CASE WHEN $4 THEN NULL ELSE now()+interval '30 days' END)",
                       cid, name, json.dumps(uris), client_id is not None)
    return {"client_id": cid, "client_name": name, "redirect_uris": uris,
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": SCOPE}


async def throttle(conn, ip, route):
    # Не доверяем X-Forwarded-For. За прокси общий лимит консервативно применяется ко всем.
    await conn.execute("DELETE FROM remote_mcp_limits WHERE bucket < floor(extract(epoch FROM now())/60)-60")
    for key, cap in (("global:"+route, 200), (digest(ip)+":"+route, 3 if route=="register" else 10 if route=="authorize" else 120)):
        n = await conn.fetchval("INSERT INTO remote_mcp_limits(key,bucket,n) VALUES($1,floor(extract(epoch FROM now())/60),1) "
                                "ON CONFLICT(key,bucket) DO UPDATE SET n=remote_mcp_limits.n+1 RETURNING n", key)
        if n > cap:
            return False
    return True


async def authorize(conn, params, resource):
    owner = await bridge.get_owner(conn)
    if not bridge.owns_bot() or owner is None:
        raise OAuthError("temporarily_unavailable", 503)
    client = await conn.fetchrow("SELECT * FROM remote_mcp_clients WHERE id=$1 AND (expires_at IS NULL OR expires_at>now())",
                                 params.get("client_id", ""))
    if client is None:
        raise OAuthError("invalid_client")
    uris = json.loads(client["redirect_uris"]) if isinstance(client["redirect_uris"], str) else client["redirect_uris"]
    if params.get("redirect_uri") not in uris:
        raise OAuthError("invalid_redirect_uri")
    if params.get("response_type") != "code" or params.get("code_challenge_method") != "S256" \
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.get("code_challenge", "")) \
            or not params.get("state") or len(params["state"]) > 512 \
            or params.get("scope", SCOPE) != SCOPE or params.get("resource") != resource:
        raise OAuthError()
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.remote_mcp.pending'))")
    if await conn.fetchval("SELECT count(*) FROM remote_mcp_requests WHERE status='pending' AND expires_at>now()") >= 10:
        raise OAuthError("temporarily_unavailable", 429)
    cookie, nonce = secret(), secrets.token_hex(8)
    rid = await conn.fetchval(
        """INSERT INTO remote_mcp_requests(client_id,cookie_hash,nonce_hash,redirect_uri,state,challenge,resource,owner_id,expires_at)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8,now()+make_interval(secs=>$9)) RETURNING id""",
        client["id"], digest(cookie), digest(nonce), params["redirect_uri"], params["state"],
        params["code_challenge"], resource, owner["user_id"], AUTH_TTL)
    # Имя DCR не удостоверено: показываем реальный точный URI, а не доверяем вывеске.
    text = ("Облачный клиент просит читать архив и страницы памяти через MCP. "
            "Это постоянный доступ только на чтение на 30 дней, без согласования каждого запроса.\n"
            "Название заявлено клиентом и не удостоверено: " + client["name"] + "\n"
            "Адрес возврата: " + params["redirect_uri"] + "\nРазрешение: archive:read. "
            "Отправка и управление настройками недоступны. "
            f"Номер запроса: {rid}. Сверьте номер с открытой страницей входа MCP. "
            "Разрешить только если вы сейчас подключаете этот клиент.")
    await bridge.notify_owner(conn, text, buttons=[[
        bridge.button("Разрешить чтение", "oa", f"y:{rid}:{nonce}"),
        bridge.button("Отклонить", "oa", f"n:{rid}:{nonce}")]])
    return cookie


@bridge.on_callback("oa")
async def decision(conn, rest, from_user_id):
    refused = {"answer": "Кнопка недоступна.", "remove_buttons": False}
    if not authority.is_owner() or authority.current_owner_id()!=from_user_id or not bridge.owns_bot():
        return refused
    parts = rest.split(":")
    if len(parts) != 3 or parts[0] not in ("y", "n") or not parts[1].isdigit():
        return refused
    row = await conn.fetchrow("SELECT * FROM remote_mcp_requests WHERE id=$1 FOR UPDATE", int(parts[1]))
    if row is None or row["owner_id"] != from_user_id or not hmac.compare_digest(row["nonce_hash"], digest(parts[2])):
        return refused
    valid = await conn.fetchval("SELECT status='pending' AND expires_at>now() FROM remote_mcp_requests WHERE id=$1", row["id"])
    if not valid:
        return {"answer": "Запрос уже закрыт или срок вышел.", "remove_buttons": True}
    await conn.execute("UPDATE remote_mcp_requests SET status=$2 WHERE id=$1", row["id"], "approved" if parts[0]=="y" else "denied")
    return {"answer": "Чтение разрешено." if parts[0]=="y" else "Отклонено.", "remove_buttons": True,
            "edit_text": "MCP: чтение разрешено на 30 дней." if parts[0]=="y" else "MCP: доступ отклонён."}


async def continuation(conn, cookie):
    row = await conn.fetchrow("SELECT * FROM remote_mcp_requests WHERE cookie_hash=$1 AND expires_at>now() FOR UPDATE", digest(cookie))
    if row is None:
        raise OAuthError("invalid_request", 410)
    owner = await bridge.get_owner(conn)
    if owner is None or row["owner_id"] != owner["user_id"]:
        raise OAuthError("access_denied", 403)
    if row["status"] == "pending":
        return {"pending": True, "request_id": row["id"], "redirect_uri": row["redirect_uri"]}
    if row["status"] == "delivered":
        raise OAuthError("invalid_request", 410)
    await conn.execute("UPDATE remote_mcp_requests SET status='delivered' WHERE id=$1", row["id"])
    if row["status"] == "denied":
        return row["redirect_uri"], {"error": "access_denied", "state": row["state"]}
    code = secret()
    await conn.execute("INSERT INTO remote_mcp_codes(hash,request_id,expires_at) VALUES($1,$2,now()+make_interval(secs=>$3))",
                       digest(code), row["id"], CODE_TTL)
    return row["redirect_uri"], {"code": code, "state": row["state"]}


async def _tokens(conn, grant_id):
    access, refresh = secret(), secret()
    await conn.execute("INSERT INTO remote_mcp_tokens(hash,grant_id,kind,expires_at) VALUES "
                       "($1,$3,'access',now()+make_interval(secs=>$4)),($2,$3,'refresh',now()+make_interval(secs=>$5))",
                       digest(access), digest(refresh), grant_id, ACCESS_TTL, GRANT_TTL)
    return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": ACCESS_TTL, "scope": SCOPE}


async def token(conn, data, resource):
    if not bridge.owns_bot():
        raise OAuthError("temporarily_unavailable", 503)
    if data.get("resource") != resource or data.get("scope", SCOPE) != SCOPE:
        raise OAuthError("invalid_target")
    if data.get("grant_type") == "authorization_code":
        row = await conn.fetchrow(
            """SELECT c.hash,c.used_at,r.* FROM remote_mcp_codes c JOIN remote_mcp_requests r ON r.id=c.request_id
               WHERE c.hash=$1 AND c.expires_at>now() FOR UPDATE OF c""", digest(data.get("code", "")))
        if row is None or row["used_at"] is not None or row["client_id"] != data.get("client_id") \
                or row["redirect_uri"] != data.get("redirect_uri") or row["resource"] != resource \
                or not hmac.compare_digest(row["challenge"], pkce(data.get("code_verifier", ""))):
            raise OAuthError("invalid_grant")
        owner = await bridge.get_owner(conn)
        if owner is None or owner["user_id"] != row["owner_id"]:
            raise OAuthError("invalid_grant")
        await conn.execute("UPDATE remote_mcp_codes SET used_at=now() WHERE hash=$1", row["hash"])
        grant = await conn.fetchval("INSERT INTO remote_mcp_grants(client_id,owner_id,resource,expires_at) "
                                    "VALUES($1,$2,$3,now()+make_interval(secs=>$4)) RETURNING id",
                                    row["client_id"], row["owner_id"], resource, GRANT_TTL)
        return await _tokens(conn, grant)
    if data.get("grant_type") != "refresh_token":
        raise OAuthError("unsupported_grant_type")
    row = await conn.fetchrow(
        """SELECT t.hash,t.used_at,g.* FROM remote_mcp_tokens t JOIN remote_mcp_grants g ON g.id=t.grant_id
           WHERE t.hash=$1 AND t.kind='refresh' AND t.expires_at>now() AND g.expires_at>now()
             AND g.revoked_at IS NULL FOR UPDATE OF g,t""", digest(data.get("refresh_token", "")))
    if row is None or row["client_id"] != data.get("client_id") or row["resource"] != resource:
        raise OAuthError("invalid_grant")
    owner = await bridge.get_owner(conn)
    if owner is None or owner["user_id"] != row["owner_id"]:
        raise OAuthError("invalid_grant")
    if row["used_at"] is not None:
        await conn.execute("UPDATE remote_mcp_grants SET revoked_at=now() WHERE id=$1", row["id"])
        return {"error": "invalid_grant"}  # сохранить отзыв при повторном использовании
    await conn.execute("UPDATE remote_mcp_tokens SET used_at=now() WHERE hash=$1 OR (grant_id=$2 AND kind='access')",
                       row["hash"], row["id"])
    return await _tokens(conn, row["id"])


async def valid_access(conn, value, resource):
    row = await conn.fetchrow(
        """SELECT g.owner_id FROM remote_mcp_tokens t JOIN remote_mcp_grants g ON g.id=t.grant_id
           WHERE t.hash=$1 AND t.kind='access' AND t.used_at IS NULL AND t.expires_at>now()
             AND g.expires_at>now() AND g.revoked_at IS NULL AND g.resource=$2""", digest(value), resource)
    owner = await bridge.get_owner(conn)
    return bridge.owns_bot() and row is not None and owner is not None and row["owner_id"] == owner["user_id"]


async def revoke(conn, value, client_id):
    await conn.execute("UPDATE remote_mcp_grants SET revoked_at=now() WHERE client_id=$2 AND id IN "
                       "(SELECT grant_id FROM remote_mcp_tokens WHERE hash=$1)", digest(value), client_id)


@bridge.on_owner_change
async def owner_changed(conn, new_user_id):
    await conn.execute("UPDATE remote_mcp_grants SET revoked_at=now() WHERE revoked_at IS NULL")
    await conn.execute("UPDATE remote_mcp_requests SET status='denied' WHERE status IN ('pending','approved')")


async def list_grants(conn):
    authority.requires_owner()
    rows = await conn.fetch("SELECT g.id,c.name,c.redirect_uris,g.created_at,g.expires_at,g.revoked_at "
                            "FROM remote_mcp_grants g JOIN remote_mcp_clients c ON c.id=g.client_id ORDER BY g.id")
    return [dict(row) for row in rows]


async def revoke_grant(conn, grant_id):
    authority.requires_owner()
    if not isinstance(grant_id, int) or isinstance(grant_id, bool) or grant_id<=0:
        raise ValueError("Нужен номер подключения MCP")
    return await conn.fetchval("UPDATE remote_mcp_grants SET revoked_at=now() WHERE id=$1 RETURNING true", grant_id) is True
