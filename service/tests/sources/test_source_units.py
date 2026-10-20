import json
import os
from dataclasses import replace

import httpx2
import pytest

from shturman.sources import broker, mcp_client
from shturman.sources.registry import Connector, SourceError, load_registry, public_sources


def test_scope_is_exact_and_bounded():
    spec = broker.canonical_source_spec({"kind": "chat", "source_id": "12", "query": "смета",
        "since": "2026-10-08T04:00:00+03:00", "until": "2026-10-09T00:00:00Z", "limit": 3})
    assert spec["since"] == "2026-10-08T01:00:00+00:00"
    assert spec["limit"] == 3 and spec["max_chars"] == 12000
    for change in ({"url": "https://evil.example/mcp"}, {"limit": 100}, {"limit": True},
                   {"source_id": "https://example.org"}, {"kind": "write"},
                   {"since": "2026-10-08"}, {"until": "2025-01-01T00:00:00Z"}):
        with pytest.raises(SourceError):
            broker.canonical_source_spec({**spec, **change})
    with pytest.raises(SourceError):
        broker.canonical_source_spec({"kind": "external"})


def test_registry_is_private_and_public_catalog_has_no_credentials(tmp_path):
    path = tmp_path / "sources.json"
    data = {"sources": [{"source_id": "documents", "name": "Документы", "url": "https://docs.example.org/mcp",
                         "search_tool": "search_docs", "read_tool": "read_doc",
                         "headers": {"Authorization": "Bearer private-secret"}}]}
    path.write_text(json.dumps(data))
    os.chmod(path, 0o644)
    with pytest.raises(SourceError, match="permissions"):
        load_registry(path)
    os.chmod(path, 0o600)
    registry = load_registry(path)
    assert public_sources(registry) == [{"source_id": "documents", "name": "Документы", "kind": "external"}]
    assert "private-secret" not in repr(registry)
    for url in ("http://docs.example.org/mcp", "https://127.0.0.1/mcp", "https://docs.example.org/mcp?token=abc"):
        data["sources"][0]["url"] = url
        path.write_text(json.dumps(data))
        with pytest.raises(SourceError):
            load_registry(path)


@pytest.fixture
def fake_mcp(monkeypatch):
    calls = []
    settings = {"read_only": True, "text": "Смета: 100 рублей", "error": False, "date": "2026-10-08T10:00:00Z"}

    async def pin(host, port):
        return "93.184.216.34"

    async def handle(request):
        if request.method == "GET":
            return httpx2.Response(405)
        if request.method == "DELETE":
            return httpx2.Response(200)
        body = json.loads(request.content)
        calls.append(body)
        method = body["method"]
        if "id" not in body:
            return httpx2.Response(202)
        if method == "initialize":
            result = {"protocolVersion": body["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "bounded-fixture", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "inputSchema": {"type": "object"},
                                  "annotations": {"readOnlyHint": settings["read_only"], "destructiveHint": False}}
                                 for name in ("search_docs", "read_doc", "send_email")]}
        elif method == "tools/call":
            result = {"content": [], "isError": settings["error"], "structuredContent": {
                "items": [{"id": "doc-1", "text": settings["text"], "revision": "v1", "date": settings["date"]}]}}
        else:
            raise AssertionError(method)
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    real_transport = mcp_client.SourceTransport
    monkeypatch.setattr(mcp_client.netguard, "pin", pin)
    monkeypatch.setattr(mcp_client, "SourceTransport", lambda endpoint: real_transport(endpoint, httpx2.MockTransport(handle)))
    return calls, settings


def connector():
    return Connector("docs", "Документы", "https://docs.example.org/mcp", {"Authorization": "Bearer secret"},
                     "search_docs", "read_doc")


async def test_real_sdk_calls_only_configured_read_tools_and_keeps_credentials_out_of_results(fake_mcp):
    calls, settings = fake_mcp
    spec = broker.canonical_source_spec({"kind": "external", "source_id": "docs", "query": "смета", "limit": 1})
    items = await mcp_client.search(connector(), spec)
    assert items[0]["text"] == settings["text"]
    assert (await mcp_client.read(connector(), "doc-1"))["resource_id"] == "doc-1"
    tool_calls = [c["params"] for c in calls if c["method"] == "tools/call"]
    assert [c["name"] for c in tool_calls] == ["search_docs", "read_doc"]
    assert tool_calls[0]["arguments"] == {"query": "смета", "limit": 1}
    assert tool_calls[1]["arguments"] == {"id": "doc-1"}
    assert "secret" not in json.dumps(items)


async def test_mcp_deny_write_hints_and_scrub_remote_errors(fake_mcp):
    calls, settings = fake_mcp
    settings["read_only"] = False
    spec = broker.canonical_source_spec({"kind": "external", "source_id": "docs", "query": "смета"})
    with pytest.raises(SourceError, match="read_only"):
        await mcp_client.search(connector(), spec)
    assert not any(c["method"] == "tools/call" for c in calls)
    settings["read_only"] = True
    settings["error"] = True
    settings["text"] = "Bearer secret-and-private-document"
    with pytest.raises(SourceError) as error:
        await mcp_client.search(connector(), spec)
    assert "secret" not in str(error.value)


async def test_response_limit_and_remote_date_window_are_enforced(fake_mcp):
    _, settings = fake_mcp
    spec = broker.canonical_source_spec({"kind": "external", "source_id": "docs", "query": "смета",
                                        "since": "2026-10-09T00:00:00Z"})
    conn = replace(connector(), search_args={"query": "query", "limit": "limit", "since": "since"})
    with pytest.raises(SourceError, match="window"):
        await mcp_client.search(conn, spec)
    settings["text"] = "x" * (mcp_client.MAX_RESPONSE + 100)
    with pytest.raises(SourceError):
        await mcp_client.search(conn, {**spec, "since": None})


async def test_successful_connector_cannot_echo_service_credential_to_model(fake_mcp):
    _, settings = fake_mcp
    settings["text"] = "Request credential was secret"
    spec = broker.canonical_source_spec({"kind": "external", "source_id": "docs", "query": "смета"})
    with pytest.raises(SourceError) as error:
        await mcp_client.search(connector(), spec)
    assert "secret" not in str(error.value)


async def test_endpoint_changes_rejected_before_transport_receives_credentials():
    received = []
    transport = mcp_client.SourceTransport("https://docs.example.org/mcp", httpx2.MockTransport(lambda r: received.append(r)))
    for url in ("https://evil.example.org/mcp", "https://docs.example.org/admin", "http://docs.example.org/mcp"):
        with pytest.raises(SourceError):
            await transport.handle_async_request(httpx2.Request("POST", url, headers={"Authorization": "secret"}))
    assert received == []
