"""Operator-owned connector configuration. No API route accepts URLs, tools or credentials.

The file is read by the service only; names/opaque IDs are the only configuration exposed
to the model. MCP resources follow a small explicit structured output contract.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import netguard, sanitize

ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}")
TOOL = re.compile(r"[a-zA-Z0-9_.:-]{1,120}")
VARIABLES = frozenset({"query", "since", "until", "limit", "resource_id"})


class SourceError(Exception):
    """Safe public failure; never carries raw connector response or credentials."""


@dataclass(frozen=True)
class Connector:
    source_id: str
    name: str
    url: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    search_tool: str = ""
    read_tool: str = ""
    search_args: dict[str, str] = field(default_factory=lambda: {"query": "query", "limit": "limit"})
    read_args: dict[str, str] = field(default_factory=lambda: {"resource_id": "id"})
    fixed_args: dict[str, Any] = field(default_factory=dict, repr=False)
    revision: str = "1"


def _mapping(value: Any, default: dict[str, str]) -> dict[str, str]:
    mapping = default if value is None else value
    if not isinstance(mapping, dict) or not mapping or any(
            key not in VARIABLES or not isinstance(arg, str) or not TOOL.fullmatch(arg)
            for key, arg in mapping.items()):
        raise SourceError("invalid_source_configuration")
    return dict(mapping)


def load_registry(path: str | Path | None) -> dict[str, Connector]:
    if not path:
        return {}
    file = Path(path)
    info = file.stat()
    # Credentials must not be readable/writable by arbitrary local users or the agent.
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid not in (0, os.getuid()):
        raise SourceError("unsafe_source_configuration_permissions")
    if info.st_size > 64 * 1024:
        raise SourceError("invalid_source_configuration")
    try:
        raw = json.loads(file.read_text())
        items = raw["sources"]
        if not isinstance(items, list) or len(items) > 30:
            raise ValueError()
        registry = {}
        for item in items:
            sid = item["source_id"]
            if not isinstance(sid, str) or not ID.fullmatch(sid) or sid in registry:
                raise ValueError()
            netguard.check_url(item["url"])
            search, read = item["search_tool"], item["read_tool"]
            if not TOOL.fullmatch(search) or not TOOL.fullmatch(read):
                raise ValueError()
            headers = item.get("headers", {})
            if not isinstance(headers, dict) or any(
                    k.lower() not in {"authorization", "x-api-key"} or not isinstance(v, str)
                    or "\n" in v or "\r" in v for k, v in headers.items()):
                raise ValueError()
            fixed = item.get("fixed_args", {})
            if not isinstance(fixed, dict) or len(json.dumps(fixed)) > 4000:
                raise ValueError()
            name = sanitize.clean_line(item.get("name", sid), 100)
            registry[sid] = Connector(
                sid, name or sid, item["url"], dict(headers), search, read,
                _mapping(item.get("search_args"), {"query": "query", "limit": "limit"}),
                _mapping(item.get("read_args"), {"resource_id": "id"}), dict(fixed),
                str(item.get("revision", "1"))[:80])
        return registry
    except (KeyError, TypeError, ValueError, netguard.Blocked):
        raise SourceError("invalid_source_configuration") from None


def public_sources(registry: dict[str, Connector]) -> list[dict[str, str]]:
    return [{"source_id": c.source_id, "name": c.name, "kind": "external"}
            for c in registry.values()]
