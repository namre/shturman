"""Изолированный ASGI вход: внешнему OAuth-токену доступен только путь /mcp."""
from __future__ import annotations

import contextlib
from urllib.parse import parse_qsl, urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import core

COOKIE = "shturman_oauth_flow"
PUBLIC = {"/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp",
          "/.well-known/oauth-authorization-server", "/oauth/register", "/oauth/token", "/oauth/revoke"}


def parameters(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise core.OAuthError()
        out[key] = value
    return out


class Gateway:
    def __init__(self, inner_app, config):
        from ..config import normalize_origin
        self.app, self.config = inner_app, config
        self.origin = normalize_origin(getattr(config, "remote_mcp_origin", ""), "remote_mcp_origin")
        self.host = urlsplit(self.origin).netloc.lower()
        self.resource = self.origin + "/mcp"
        if self.origin and (urlsplit(self.origin).scheme != "https" or urlsplit(self.origin).path \
                            or urlsplit(self.origin).query or urlsplit(self.origin).fragment \
                            or urlsplit(self.origin).username or urlsplit(self.origin).password \
                            or not self.host \
                            or self.origin in tuple(normalize_origin(getattr(config, field, ""), field, strict=False)
                                                    for field in ("dashboard_origin", "setup_origin"))):
            raise ValueError("remote_mcp_origin requires a separate HTTPS origin")

    def handles(self, path, host):
        # На этом отдельном адресе перехватываем ВСЁ; /api не должен попасть во внутренний Gate.
        return bool(self.origin) and host.lower() == self.host

    @property
    def state(self):
        return getattr(self.app, "inner", self.app).state.shturman

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        headers = scope.get("headers", [])
        hosts = [v.decode("latin1") for k, v in headers if k.lower() == b"host"]
        if scope["type"] != "http" or len(hosts) != 1 or not self.handles(path, hosts[0]):
            await JSONResponse({"error": "not_found"}, 404)(scope, receive, send)
            return
        request = Request(scope, receive)
        cors = path in PUBLIC or path == "/mcp"
        extra = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
                 "X-Content-Type-Options": "nosniff"}
        if cors:
            extra.update({"Access-Control-Allow-Origin": "*", "Access-Control-Expose-Headers": "WWW-Authenticate, MCP-Session-Id",
                          "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                          "Access-Control-Allow-Headers": "Authorization, Content-Type, MCP-Protocol-Version, MCP-Session-Id"})
        if request.method == "OPTIONS" and cors:
            await Response(status_code=204, headers=extra)(scope, receive, send)
            return
        try:
            if path == "/mcp":
                values = [v.decode("latin1") for k, v in headers if k.lower() == b"authorization"]
                token = values[0][7:] if len(values)==1 and values[0].startswith("Bearer ") else ""
                async with self.state.pool.acquire() as conn:
                    valid = bool(token) and await core.valid_access(conn, token, self.resource)
                if not valid:
                    extra["WWW-Authenticate"] = f'Bearer resource_metadata="{self.origin}/.well-known/oauth-protected-resource", scope="{core.SCOPE}"'
                    raise core.OAuthError("invalid_token", 401)
                # Сам SDK получает только уже проверенный токен и ожидаемый origin.
                forwarded = dict(scope)
                forwarded["headers"] = [(k,v) for k,v in headers if k.lower() not in (b"authorization", b"origin")]
                forwarded["headers"].append((b"authorization", f"Bearer {self.config.mcp_token}".encode()))
                async def cors_send(message):
                    if message["type"] == "http.response.start":
                        message = dict(message)
                        message["headers"] = list(message.get("headers", [])) + [(k.lower().encode(), v.encode()) for k,v in extra.items()]
                    await send(message)
                await self.app(forwarded, receive, cors_send)
                return
            response = await self.endpoint(request)
        except core.OAuthError as exc:
            response = JSONResponse({"error": exc.error}, exc.status)
        for key, value in extra.items():
            response.headers[key] = value
        await response(scope, receive, send)

    async def endpoint(self, request):
        path, method = request.url.path, request.method
        if path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp") and method == "GET":
            return JSONResponse({"resource": self.resource, "authorization_servers": [self.origin],
                                 "scopes_supported": [core.SCOPE], "bearer_methods_supported": ["header"]})
        if path == "/.well-known/oauth-authorization-server" and method == "GET":
            return JSONResponse({"issuer": self.origin, "authorization_endpoint": self.origin+"/oauth/authorize",
                "token_endpoint": self.origin+"/oauth/token", "registration_endpoint": self.origin+"/oauth/register",
                "revocation_endpoint": self.origin+"/oauth/revoke", "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"], "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": [core.SCOPE], "code_challenge_methods_supported": ["S256"],
                "client_id_metadata_document_supported": False})
        allowed = {"/oauth/authorize": "GET", "/oauth/continue": "GET", "/oauth/register": "POST",
                   "/oauth/token": "POST", "/oauth/revoke": "POST"}
        if path not in allowed:
            raise core.OAuthError("not_found", 404)
        if method != allowed[path]:
            raise core.OAuthError("method_not_allowed", 405)
        # Ограничение применяется отдельной транзакцией: неверные запросы тоже расходуют квоту.
        async with self.state.pool.acquire() as conn:
            if not await core.throttle(conn, request.client.host if request.client else "unknown", path.rsplit("/",1)[-1]):
                raise core.OAuthError("temporarily_unavailable", 429)
        async with self.state.pool.acquire() as conn, conn.transaction():
            if path == "/oauth/authorize":
                if len(request.scope.get("query_string", b"")) > 8192:
                    raise core.OAuthError()
                cookie = await core.authorize(conn, parameters(request.query_params.multi_items()), self.resource)
                response = RedirectResponse(self.origin+"/oauth/continue", 303)
                response.set_cookie(COOKIE, cookie, max_age=core.AUTH_TTL, path="/oauth/continue",
                                    secure=True, httponly=True, samesite="lax")
                return response
            if path == "/oauth/continue":
                result = await core.continuation(conn, request.cookies.get(COOKIE, ""))
                if isinstance(result, dict) and result.get("pending"):
                    from html import escape
                    return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8">'
                        '<meta http-equiv="refresh" content="3"><title>Подтверждение MCP</title>'
                        '<p>Подтвердите подключение клиента в своём боте Telegram. Эта страница обновится сама.</p>'
                        f'<p>Номер запроса: {result["request_id"]}. Сверьте его с карточкой в боте.</p>'
                        f'<p>Адрес клиента: {escape(result["redirect_uri"])}</p></html>',
                        headers={"Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"})
                uri, params = result
                separator = "&" if urlsplit(uri).query else "?"
                response = RedirectResponse(uri+separator+urlencode(params), 303)
                response.delete_cookie(COOKIE, path="/oauth/continue", secure=True, httponly=True, samesite="lax")
                return response
            body = await bounded_body(request)
            if path == "/oauth/register":
                import json
                if request.headers.get("content-type", "").split(";",1)[0] != "application/json":
                    raise core.OAuthError()
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeError):
                    raise core.OAuthError() from None
                return JSONResponse(await core.register(conn, data,
                    allow_loopback=getattr(self.config, "remote_mcp_allow_loopback", False)), 201)
            if request.headers.get("content-type", "").split(";",1)[0] != "application/x-www-form-urlencoded":
                raise core.OAuthError()
            try:
                data = parameters(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
            except UnicodeError:
                raise core.OAuthError() from None
            if path == "/oauth/revoke":
                await core.revoke(conn, data.get("token", ""), data.get("client_id", ""))
                return Response(status_code=200)
            result = await core.token(conn, data, self.resource)
            return JSONResponse(result, 400 if "error" in result else 200)


async def bounded_body(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body)>16384:
            raise core.OAuthError("invalid_request", 413)
    return bytes(body)


@contextlib.asynccontextmanager
async def lifespan(state):
    if getattr(state.config, "remote_mcp_origin", ""):
        async with state.pool.acquire() as conn, conn.transaction():
            for client in getattr(state.config, "remote_mcp_clients", ()):
                existing = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM remote_mcp_clients WHERE id=$1)", client.get("client_id"))
                if not existing:
                    await core.register(conn, client, allow_loopback=getattr(state.config, "remote_mcp_allow_loopback", False),
                                        client_id=client["client_id"])
    yield
