"""MCP 2.x client for an operator's explicit read-only tool pair.

No tool, URL, header or MCP permission is supplied by the language model. The SDK gets
no sampling/elicitation/root callbacks. Every connection is TLS and DNS-pinned by netguard.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any

import httpx2  # MCP 2.x transport dependency
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .. import netguard
from .registry import Connector, SourceError

MAX_RESPONSE = 1024 * 1024
TIMEOUT = 20


class _BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream):
        self.inner = inner

    async def __aiter__(self):
        size = 0
        async for chunk in self.inner:
            size += len(chunk)
            if size > MAX_RESPONSE:
                raise SourceError("source_response_too_large")
            yield chunk

    async def aclose(self):
        await self.inner.aclose()


class SourceTransport(netguard.PinnedTransport):
    def __init__(self, endpoint: str, inner=None):
        super().__init__(inner or httpx2.AsyncHTTPTransport(retries=0))
        self.endpoint = httpx2.URL(endpoint)

    async def handle_async_request(self, request):
        # MCP SDK may follow within-origin redirects. Restrict them to the configured
        # endpoint path as well, before credentials can be forwarded anywhere else.
        if (request.url.scheme, request.url.host, request.url.port, request.url.path) != (
                self.endpoint.scheme, self.endpoint.host, self.endpoint.port, self.endpoint.path):
            raise SourceError("source_endpoint_changed")
        response = await super().handle_async_request(request)
        response.stream = _BoundedStream(response.stream)
        return response


@asynccontextmanager
async def _session(connector: Connector):
    netguard.check_url(connector.url)
    async with httpx2.AsyncClient(
            transport=SourceTransport(connector.url), headers=connector.headers,
            timeout=TIMEOUT, follow_redirects=False, trust_env=False) as client:
        async with streamable_http_client(connector.url, http_client=client,
                                          max_sse_event_size=65536) as streams:
            async with ClientSession(streams[0], streams[1], read_timeout_seconds=TIMEOUT) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                for configured in {connector.search_tool, connector.read_tool}:
                    tool = next((t for t in tools if t.name == configured), None)
                    annotations = tool.annotations if tool else None
                    if not annotations or annotations.read_only_hint is not True or annotations.destructive_hint is True:
                        raise SourceError("source_tool_not_read_only")
                yield session


def _args(connector: Connector, spec: dict, *, resource_id: str | None = None) -> dict:
    mapping = connector.read_args if resource_id is not None else connector.search_args
    values = {**spec, "resource_id": resource_id}
    return {**connector.fixed_args, **{argument: values[key] for key, argument in mapping.items()
                                      if values.get(key) is not None}}


def _items(result: Any, limit: int) -> list[dict[str, str]]:
    # No images, embedded resources, unbounded text content or model-selected URLs.
    if getattr(result, "is_error", False):
        raise SourceError("source_unavailable")
    data = getattr(result, "structured_content", None)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SourceError("source_invalid_response")
    out = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            raise SourceError("source_invalid_response")
        rid, text = item.get("id"), item.get("text")
        if not isinstance(rid, str) or not 1 <= len(rid) <= 200 or "://" in rid:
            raise SourceError("source_invalid_response")
        if not isinstance(text, str) or len(text) > MAX_RESPONSE:
            raise SourceError("source_invalid_response")
        date = item.get("date")
        if date is not None and (not isinstance(date, str) or len(date) > 80):
            raise SourceError("source_invalid_response")
        out.append({"resource_id": rid, "text": text,
                    "remote_revision": str(item.get("revision", ""))[:200],
                    "date": date})
    return out


def _no_credentials(connector, items):
    # A broken connector can echo request headers even in a successful result. Do not
    # release those values to the model, provenance receipts or an owner card.
    rendered = "\n".join(value for item in items for value in item.values() if isinstance(value, str))
    for value in connector.headers.values():
        secrets = {value}
        if value.lower().startswith("bearer "):
            secrets.add(value.split(" ", 1)[1])
        if any(secret and secret in rendered for secret in secrets):
            raise SourceError("source_invalid_response")
    return items


async def search(connector: Connector, spec: dict) -> list[dict[str, str]]:
    try:
        async with asyncio.timeout(TIMEOUT):
            async with _session(connector) as session:
                result = await session.call_tool(connector.search_tool, _args(connector, spec))
                items = _no_credentials(connector, _items(result, spec["limit"]))
                if spec.get("since") or spec.get("until"):
                    for item in items:
                        try:
                            date = datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
                            if date.tzinfo is None:
                                raise ValueError()
                            date = date.astimezone(timezone.utc)
                            if (spec.get("since") and date < datetime.fromisoformat(spec["since"])) or (
                                    spec.get("until") and date >= datetime.fromisoformat(spec["until"])):
                                raise ValueError()
                        except (AttributeError, TypeError, ValueError):
                            raise SourceError("source_window_not_honored") from None
                return items
    except SourceError:
        raise
    except Exception as exc:
        # SDK errors can contain credentials, resource contents or response bodies.
        raise _safe_failure(exc) from None


async def read(connector: Connector, resource_id: str) -> dict[str, str] | None:
    try:
        async with asyncio.timeout(TIMEOUT):
            async with _session(connector) as session:
                result = await session.call_tool(connector.read_tool,
                                                 _args(connector, {}, resource_id=resource_id))
                items = _no_credentials(connector, _items(result, 1))
                return items[0] if items and items[0]["resource_id"] == resource_id else None
    except SourceError:
        raise
    except Exception as exc:
        raise _safe_failure(exc) from None


def _safe_failure(exc):
    if isinstance(exc, SourceError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            found = _safe_failure(child)
            if str(found) != "source_unavailable":
                return found
    return SourceError("source_unavailable")
