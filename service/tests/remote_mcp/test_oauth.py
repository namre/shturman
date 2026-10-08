"""OAuth protocol flow: fake external MCP app + synthetic verified Telegram approval."""
import contextlib
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.responses import JSONResponse

from shturman import authority, bridge
from shturman.remote_mcp import Gateway, core

ORIGIN = "https://archive.example"
RESOURCE = ORIGIN + "/mcp"
REDIRECT = "https://client.example/callback?configured=value"
OWNER = 1000
VERIFIER = "p" * 43


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.conn


class Archive:
    def __init__(self, conn, config):
        self.state = SimpleNamespace(shturman=SimpleNamespace(pool=Pool(conn), config=config))
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append(scope)
        await JSONResponse({"read_only": True})(scope, receive, send)


def config(origin=ORIGIN, **kw):
    return SimpleNamespace(remote_mcp_origin=origin, remote_mcp_allow_loopback=False,
                           mcp_token="local-archive-secret", dashboard_origin="https://dashboard.example",
                           setup_origin="https://setup.example", **kw)


@pytest.mark.parametrize("value", [["http://client.example/cb"], ["https://client.example/#"],
    ["https://u:p@client.example/cb"], ["https://client.example/cb#fragment"], ["https://client.example\\evil/cb"],
    [], ["https://client.example/ bad"], ["javascript:alert(1)"], ["http://localhost:9000/cb"]])
def test_redirect_validation_rejects_unsafe(value):
    with pytest.raises(core.OAuthError):
        core.redirects(value)


def test_redirect_exactness_and_loopback_test_option():
    assert core.redirects([REDIRECT]) == [REDIRECT]
    assert core.redirects(["http://127.0.0.1:9000/cb"], allow_loopback=True)
    with pytest.raises(core.OAuthError):
        core.redirects(["http://not-local.example/cb"], allow_loopback=True)


def test_pkce_rfc7636_vector():
    assert core.pkce("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    with pytest.raises(core.OAuthError):
        core.pkce("short")


def test_gateway_disabled_and_separate_https_origin():
    assert not Gateway(None, config(origin="")).handles("/mcp", "archive.example")
    with pytest.raises(ValueError):
        Gateway(None, config(origin="http://archive.example"))
    with pytest.raises(ValueError):
        Gateway(None, config(origin="https://dashboard.example"))
    gate = Gateway(None, config())
    assert gate.handles("/api", "archive.example")
    assert not gate.handles("/mcp", "evil.example")


@pytest.mark.asyncio
async def test_no_agent_authority_can_approve():
    assert (await core.decision(None, "y:1:nonce", OWNER))["answer"] == "Кнопка недоступна."


@pytest.fixture
async def oauth(conn, own_bot):
    await bridge.set_owner(conn, OWNER, OWNER)
    cfg = config()
    app = Archive(conn, cfg)
    gate = Gateway(app, cfg)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url=ORIGIN,
                                follow_redirects=False) as client:
        yield client, app


async def begin(client):
    reg = await client.post("/oauth/register", json={"client_name": "Synthetic browser client", "redirect_uris": [REDIRECT]})
    assert reg.status_code == 201, reg.text
    cid = reg.json()["client_id"]
    response = await client.get("/oauth/authorize", params={"client_id": cid, "redirect_uri": REDIRECT,
        "response_type": "code", "scope": "archive:read", "resource": RESOURCE, "state": "exact + / ? state",
        "code_challenge_method": "S256", "code_challenge": core.pkce(VERIFIER)})
    assert response.status_code == 303, response.text
    assert "Secure" in response.headers["set-cookie"] and "HttpOnly" in response.headers["set-cookie"]
    return cid


async def approve(conn, yes=True):
    payload = await conn.fetchval("SELECT payload FROM jobs WHERE kind=$1 ORDER BY id DESC LIMIT 1", bridge.NOTIFY_OWNER)
    import json
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert "archive:read" in payload["text"] and REDIRECT in payload["text"]
    data = payload["buttons"][0][0 if yes else 1]["data"]
    with authority.owner_context(OWNER, chat_id=OWNER):
        response = await bridge.dispatch_callback(conn, data, OWNER)
        assert response["remove_buttons"] is True
        assert (await bridge.dispatch_callback(conn, data, OWNER))["answer"] == "Запрос уже закрыт или срок вышел."


async def exchange(client, cid, code, **changes):
    data = {"grant_type": "authorization_code", "client_id": cid, "code": code,
            "redirect_uri": REDIRECT, "resource": RESOURCE, "code_verifier": VERIFIER}
    data.update(changes)
    return await client.post("/oauth/token", data=data)


@pytest.mark.asyncio
async def test_complete_authorization_rotation_reuse_and_remote_isolation(oauth, conn):
    client, app = oauth
    response = await client.post("/mcp", headers={"Authorization": "Bearer local-archive-secret"})
    assert response.status_code == 401 and "resource_metadata" in response.headers["www-authenticate"]
    assert not app.calls
    assert (await client.get("/api/chats")).status_code == 404
    metadata = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert metadata["resource"] == RESOURCE
    cid = await begin(client)
    assert (await client.get("/oauth/continue")).status_code == 200
    await approve(conn)
    response = await client.get("/oauth/continue")
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == ["exact + / ? state"] and query["configured"] == ["value"]
    code = query["code"][0]
    assert (await exchange(client, cid, code, code_verifier="x"*43)).status_code == 400
    assert (await exchange(client, cid, code, redirect_uri="https://attacker.example/cb")).status_code == 400
    result = await exchange(client, cid, code)
    assert result.status_code == 200, result.text
    tokens = result.json()
    assert (await exchange(client, cid, code)).status_code == 400
    assert (await client.get("/oauth/continue")).status_code == 410
    assert await conn.fetchval("SELECT EXISTS(SELECT 1 FROM remote_mcp_tokens WHERE hash=$1)", core.digest(tokens["access_token"]))
    assert not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM remote_mcp_tokens WHERE hash=$1)", tokens["access_token"])
    access = {"Authorization": "Bearer "+tokens["access_token"], "Origin": "https://client.example"}
    response = await client.post("/mcp", headers=access)
    assert response.status_code == 200 and response.headers["access-control-allow-origin"] == "*"
    assert dict(app.calls[-1]["headers"])[b"authorization"] == b"Bearer local-archive-secret"
    assert b"origin" not in dict(app.calls[-1]["headers"])
    assert (await client.post("/api/owner", headers=access)).status_code == 404
    rotated = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": cid,
                        "refresh_token": tokens["refresh_token"], "resource": RESOURCE})
    assert rotated.status_code == 200
    assert (await client.post("/mcp", headers=access)).status_code == 401
    new = rotated.json()
    assert (await client.post("/mcp", headers={"Authorization": "Bearer "+new["access_token"]})).status_code == 200
    assert (await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": cid,
            "refresh_token": tokens["refresh_token"], "resource": RESOURCE})).status_code == 400
    assert (await client.post("/mcp", headers={"Authorization": "Bearer "+new["access_token"]})).status_code == 401


@pytest.mark.asyncio
async def test_denied_owner_approval_never_issues_code(oauth, conn):
    client, _ = oauth
    await begin(client)
    await approve(conn, yes=False)
    response = await client.get("/oauth/continue")
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["access_denied"]
    assert await conn.fetchval("SELECT count(*) FROM remote_mcp_codes") == 0


@pytest.mark.asyncio
async def test_strict_parameters_cors_quota_and_no_http_grant(oauth):
    client, _ = oauth
    assert (await client.post("/oauth/grant")).status_code == 404
    reg = await client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    cid = reg.json()["client_id"]
    params = {"client_id": cid, "redirect_uri": REDIRECT, "response_type": "code", "state": "s",
              "scope": "archive:write", "code_challenge_method": "plain", "code_challenge": "x"*43, "resource": RESOURCE}
    assert (await client.get("/oauth/authorize", params=params)).status_code == 400
    preflight = await client.options("/oauth/token", headers={"Origin": "https://client.example"})
    assert preflight.status_code == 204 and preflight.headers["access-control-allow-origin"] == "*"
    for i in range(3):
        response = await client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    assert response.status_code == 429


class MemoryConnection:
    """Протокольный стенд без PG; SQL-контракты отдельно проверяет фикстура conn."""
    def __init__(self):
        self.clients, self.requests, self.codes, self.grants, self.tokens, self.cards, self.limits = {}, {}, {}, {}, {}, [], {}

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql, *args):
        if sql.startswith("INSERT INTO remote_mcp_clients"):
            self.clients[args[0]] = {"id": args[0], "name": args[1], "redirect_uris": args[2]}
        elif sql.startswith("INSERT INTO remote_mcp_codes"):
            self.codes[args[0]] = {"hash": args[0], "request_id": args[1], "used_at": None}
        elif sql.startswith("INSERT INTO remote_mcp_tokens"):
            for value, kind in ((args[0], "access"), (args[1], "refresh")):
                self.tokens[value] = {"hash": value, "grant_id": args[2], "kind": kind, "used_at": None}
        elif sql.startswith("UPDATE remote_mcp_requests"):
            self.requests[args[0]]["status"] = args[1] if len(args)>1 else "delivered"
        elif sql.startswith("UPDATE remote_mcp_codes"):
            self.codes[args[0]]["used_at"] = True
        elif sql.startswith("UPDATE remote_mcp_tokens"):
            for value in self.tokens.values():
                if value["hash"]==args[0] or value["grant_id"]==args[1] and value["kind"]=="access":
                    value["used_at"] = True
        elif sql.startswith("UPDATE remote_mcp_grants"):
            self.grants[args[0]]["revoked_at"] = True

    async def fetchval(self, sql, *args):
        if sql.startswith("INSERT INTO remote_mcp_limits"):
            self.limits[args[0]] = self.limits.get(args[0], 0)+1
            return self.limits[args[0]]
        if sql.startswith("SELECT count(*) FROM remote_mcp_clients"):
            return len(self.clients)
        if sql.startswith("SELECT count(*) FROM remote_mcp_requests"):
            return sum(row["status"]=="pending" for row in self.requests.values())
        if sql.startswith("INSERT INTO remote_mcp_requests"):
            rid = len(self.requests)+1
            self.requests[rid] = dict(zip(("client_id","cookie_hash","nonce_hash","redirect_uri","state","challenge","resource","owner_id"),args[:8])) | {"id": rid, "status": "pending"}
            return rid
        if sql.startswith("SELECT status='pending'"):
            return self.requests[args[0]]["status"]=="pending"
        if sql.startswith("INSERT INTO remote_mcp_grants"):
            gid = len(self.grants)+1
            self.grants[gid] = {"id": gid,"client_id": args[0],"owner_id": args[1],"resource": args[2],"revoked_at": None}
            return gid
        if "SELECT payload FROM jobs" in sql:
            return self.cards[-1]
        if "SELECT EXISTS(SELECT 1 FROM remote_mcp_tokens" in sql:
            return args[0] in self.tokens
        if "SELECT count(*) FROM remote_mcp_codes" in sql:
            return len(self.codes)
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if "SELECT * FROM remote_mcp_clients" in sql:
            return self.clients.get(args[0])
        if "SELECT * FROM remote_mcp_requests WHERE id" in sql:
            return self.requests.get(args[0])
        if "SELECT * FROM remote_mcp_requests WHERE cookie_hash" in sql:
            return next((r for r in self.requests.values() if r["cookie_hash"]==args[0]),None)
        if "FROM remote_mcp_codes c" in sql:
            row = self.codes.get(args[0])
            return row | self.requests[row["request_id"]] if row else None
        if "FROM remote_mcp_tokens t" in sql:
            row = self.tokens.get(args[0])
            if row is None:
                return None
            grant = self.grants[row["grant_id"]]
            if grant["revoked_at"] is not None:
                return None
            if "SELECT g.owner_id" in sql:
                return {"owner_id": grant["owner_id"]} if row["kind"]=="access" and row["used_at"] is None and grant["resource"]==args[1] else None
            return row | grant if row["kind"]=="refresh" else None
        raise AssertionError(sql)


@pytest.mark.asyncio
async def test_fake_end_to_end_without_database(monkeypatch, own_bot):
    conn = MemoryConnection()
    async def owner(c):
        return {"user_id": OWNER, "chat_id": OWNER}
    async def notify(c, text, **kwargs):
        c.cards.append({"text": text, "buttons": kwargs["buttons"]})
        return 1
    monkeypatch.setattr(bridge, "get_owner", owner)
    monkeypatch.setattr(bridge, "notify_owner", notify)
    app = Archive(conn, config())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=Gateway(app, config())),base_url=ORIGIN) as client:
        await test_complete_authorization_rotation_reuse_and_remote_isolation((client,app),conn)


@pytest.mark.asyncio
async def test_real_mcp_sdk_initialize_and_read_tool_listing_without_database(monkeypatch, own_bot, config):
    import dataclasses
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from starlette.applications import Starlette
    from shturman import mcp_server
    conn = MemoryConnection()
    async def owner(c):
        return {"user_id": OWNER, "chat_id": OWNER}
    async def notify(c, text, **kwargs):
        c.cards.append({"text": text, "buttons": kwargs["buttons"]})
        return 1
    monkeypatch.setattr(bridge, "get_owner", owner)
    monkeypatch.setattr(bridge, "notify_owner", notify)
    cfg = dataclasses.replace(config, remote_mcp_origin=ORIGIN)
    state = SimpleNamespace(pool=Pool(conn), config=cfg)
    app = Starlette(routes=mcp_server.routes())
    app.state.shturman = state
    gate = Gateway(app, cfg)
    async with mcp_server.lifespan(state):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate),base_url=ORIGIN) as browser:
            cid = await begin(browser)
            await approve(conn)
            result = await browser.get("/oauth/continue")
            code = parse_qs(urlsplit(result.headers["location"]).query)["code"][0]
            tokens = (await exchange(browser,cid,code)).json()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate),
                headers={"Authorization": "Bearer "+tokens["access_token"]}) as authenticated:
            async with streamable_http_client(RESOURCE, http_client=authenticated) as streams:
                async with ClientSession(streams[0],streams[1]) as session:
                    initialized = await session.initialize()
                    assert initialized.model_dump(by_alias=True)["serverInfo"]["name"] == "shturman-archive"
                    tools = await session.list_tools()
                    assert "search_messages" in {tool.name for tool in tools.tools}
                    assert all(tool.annotations.model_dump(by_alias=True)["readOnlyHint"] is True for tool in tools.tools)
