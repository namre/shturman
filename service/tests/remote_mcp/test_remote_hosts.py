"""Equivalent external Host spellings cannot select the local bearer-token gate."""
import contextlib
from types import SimpleNamespace

import httpx
import pytest
from starlette.responses import JSONResponse

from shturman.app import Gate
from shturman.remote_mcp import Gateway, core


HOSTS = ("archive.example", "ARCHIVE.EXAMPLE", "archive.example:443",
         "archive.example.", "archive.example.:443", "archive.example:0443")


class Pool:
    @contextlib.asynccontextmanager
    async def acquire(self):
        yield None


class Inner:
    def __init__(self):
        self.calls = []
        self.state = SimpleNamespace(shturman=SimpleNamespace(pool=Pool()))

    async def __call__(self, scope, receive, send):
        self.calls.append(scope)
        await JSONResponse({"private_inner": True})(scope, receive, send)


def make_gate():
    config = SimpleNamespace(remote_mcp_origin="https://archive.example",
        dashboard_origin="https://dashboard.example", setup_origin="https://setup.example",
        api_token="local-api-secret", mcp_token="local-mcp-secret")
    inner = Inner()
    remote = Gateway(inner, config)
    return Gate(inner, config, setup=inner, remote=remote), inner, remote


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("path,token,status", [
    ("/api/status", "local-api-secret", 404),
    ("/mcp", "local-mcp-secret", 401),
    ("/shturman-setup/", "local-api-secret", 404),
    ("/health", "local-api-secret", 404),
])
async def test_remote_host_alias_never_accepts_local_gate_token(monkeypatch, host, path, token, status):
    async def denied(*args):
        return False
    monkeypatch.setattr(core, "valid_access", denied)
    gate, inner, _ = make_gate()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url="https://archive.example") as client:
        response = await client.get(path, headers={"Host": host, "Authorization": "Bearer " + token})
    assert response.status_code == status
    assert not inner.calls


@pytest.mark.parametrize("host", HOSTS)
async def test_oauth_validated_alias_forwards_canonical_host_to_sdk(monkeypatch, host):
    async def approved(conn, token, resource):
        return token == "oauth-access" and resource == "https://archive.example/mcp"
    monkeypatch.setattr(core, "valid_access", approved)
    gate, inner, _ = make_gate()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url="https://archive.example") as client:
        response = await client.post("/mcp", headers={"Host": host, "Authorization": "Bearer oauth-access",
                                                    "Origin": "https://client.example"})
    assert response.status_code == 200
    headers = dict(inner.calls[0]["headers"])
    assert headers[b"host"] == b"archive.example"
    assert headers[b"authorization"] == b"Bearer local-mcp-secret"
    assert b"origin" not in headers


async def test_duplicate_hosts_including_remote_alias_are_rejected():
    gate, inner, _ = make_gate()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url="https://archive.example") as client:
        response = await client.get("/api/status", headers=[
            ("Host", "localhost"), ("Host", "archive.example:443"),
            ("Authorization", "Bearer local-api-secret")])
    assert response.status_code == 404
    assert not inner.calls


def test_distinct_ports_hosts_and_malformed_authorities_do_not_match():
    _, _, remote = make_gate()
    for host in ("archive.example:8443", "archive.example:0", "evil.example",
                 "archive.example@evil.example", "archive.example/path", "archive.example?x",
                 "archive.example#x", "archive.example:bad", " archive.example"):
        assert not remote.handles("/mcp", host)
    assert Gateway._host_key("[2001:0db8::1]:443") == Gateway._host_key("[2001:db8:0:0:0:0:0:1]")
