"""Плагин в настоящем процессе Hermes 0.21.5 (без токена бота и без модели): что зарегистрировалось.

Запуск (интерпретатором, в котором установлен Hermes; HERMES_HOME — каталог стенда, см. README.md):

    HERMES_HOME=/путь/к/каталогу/стенда python bridge_hermes.py

В каталоге стенда появятся две задачи по расписанию (сводка и обзор недели).
"""
import asyncio
import json
import os
import sys
import types

print("HERMES_HOME =", os.environ.get("HERMES_HOME"))
import hermes_cli
from importlib.metadata import version
print("hermes-agent", version("hermes-agent"))

from hermes_cli.plugins import discover_plugins, get_plugin_manager, get_plugin_auxiliary_tasks

discover_plugins()
pm = get_plugin_manager()
for p in pm.list_plugins():
    if p.get("name") == "shturman":
        print("plugin:", {k: p.get(k) for k in ("name", "version", "enabled", "error", "source")})
loaded = [lp for key, lp in pm._plugins.items() if lp.manifest.name == "shturman"][0]
print("tools_registered:", sorted(loaded.tools_registered))
print("aux tasks:", [(e["key"], e["plugin"], e["defaults"]["timeout"]) for e in get_plugin_auxiliary_tasks()])
print("skills:", pm.list_plugin_skills("shturman"))
print("telegram factories:", [(getattr(f, "__qualname__", f), n) for f, n in pm.get_platform_handler_factories("telegram")])

from tools.registry import registry
for name in sorted(loaded.tools_registered):
    e = registry.get_entry(name)
    print(f"  tool {name}: toolset={e.toolset} check_fn={e.check_fn()} emoji={e.emoji!r}")

from toolsets import resolve_toolset
print("resolve shturman_read:", resolve_toolset("shturman_read"))
print("resolve shturman_assist:", resolve_toolset("shturman_assist"))

from tools.skills_tool import skill_view
for skill in ("shturman:morning-brief", "shturman:weekly-review"):
    payload = json.loads(skill_view(skill))
    print("skill_view", skill, "success=", payload.get("success"), "chars=", len(payload.get("content") or ""),
          "setup_needed=", payload.get("setup_needed"))

# --- инструменты видны агенту только с подключённым сервисом ---
import model_tools
def visible():
    registry_mod = sys.modules["tools.registry"]
    for cache in ("_check_fn_cache", "_check_fn_last_good"):
        getattr(registry_mod, cache).clear()
    getattr(registry_mod, "_check_fn_ever_good").clear()
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        defs = model_tools.get_tool_definitions(enabled_toolsets=["shturman_read", "shturman_assist"], quiet_mode=False)
    return sorted(d["function"]["name"] for d in defs)
print("tool definitions without service:", visible())
os.environ["SHTURMAN_API_TOKEN"] = "x" * 40
print("tool definitions with service token:", visible())
handler = registry.get_entry("shturman_commitments").handler
os.environ["SHTURMAN_SERVICE_URL"] = "http://127.0.0.1:9"
print("tool call, service down ->", registry.dispatch("shturman_commitments", {"view": "today"}))
del os.environ["SHTURMAN_API_TOKEN"], os.environ["SHTURMAN_SERVICE_URL"]
print("tool call, not configured ->", registry.dispatch("shturman_commitments", {}))

# --- по умолчанию группы плагина включены на платформах ---
from hermes_cli.config import load_config
from hermes_cli.tools_config import _get_platform_tools
for platform in ("telegram", "cron", "cli"):
    enabled = _get_platform_tools(load_config(), platform)
    print(f"platform {platform}: shturman_read={'shturman_read' in enabled} shturman_assist={'shturman_assist' in enabled}")

# --- почему группа не называется «shturman» ---
noop = lambda args, **kw: "{}"
schema = {"name": "x", "description": "probe", "parameters": {"type": "object", "properties": {}}}
registry.register(name="mcp__shturman__search_messages", toolset="mcp-shturman", schema=schema, handler=noop)
registry.register_toolset_alias("shturman", "mcp-shturman")
print("MCP alias 'shturman' resolves to:", resolve_toolset("shturman"))
registry.register(name="probe_plugin_tool", toolset="shturman", schema=schema, handler=noop)
print("after a plugin toolset literally named 'shturman':", resolve_toolset("shturman"))
registry.deregister("probe_plugin_tool") if hasattr(registry, "deregister") else None

# --- настоящий ctx.llm с подставным провайдером: формы аргументов исполнителя ---
import shturman_bridge
from shturman_core.executor import Executor, LLM_STRUCTURED, LLM_TEXT
ctx = shturman_bridge.runtime()._ctx
print("ctx.plugin_id:", ctx.plugin_id, "llm:", type(ctx.llm).__name__)
import agent.auxiliary_client as ac
seen = {}
async def fake_async_call_llm(**kw):
    seen.update(kw)
    content = '{"commitments": [{"who": "owner"}]}' if kw.get("extra_body") else "Добрый день!"
    msg = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], model="stub-model", usage=None)
ac.async_call_llm = fake_async_call_llm
async def nothing(*a, **k):
    return {}
ex = Executor(nothing, llm=ctx.llm, bot=None, owner=lambda: {})
out = asyncio.run(ex.execute(LLM_STRUCTURED, {
    "instructions": "Найди обязательства", "input": "Пришлю смету в пятницу",
    "json_schema": {"type": "object", "properties": {"commitments": {"type": "array"}}, "required": ["commitments"]},
    "schema_name": "commitments", "task": "shturman_extract", "max_tokens": 700}))
print("llm.structured via real ctx.llm ->", out)
print("  routed task:", seen.get("task"), "max_tokens:", seen.get("max_tokens"), "timeout:", seen.get("timeout"),
      "response_format:", (seen.get("extra_body") or {}).get("response_format", {}).get("type"))
print("  user content blocks:", [p["type"] for p in seen["messages"][-1]["content"]])
out = asyncio.run(ex.execute(LLM_TEXT, {"messages": [{"role": "user", "content": "Привет"}], "task": "shturman_reply"}))
print("llm.text via real ctx.llm ->", out, "task:", seen.get("task"))
out = asyncio.run(ex.execute(LLM_TEXT, {"messages": [{"role": "user", "content": "Привет"}], "task": "compression"}))
print("llm.text with a built-in task name ->", out.ok, "routed task:", seen.get("task"))
try:
    asyncio.run(ctx.llm.acomplete([{"role": "user", "content": "x"}], task="compression"))
except Exception as exc:
    print("direct built-in task without trust ->", type(exc).__name__)

# --- задачи по расписанию ---
os.environ["TELEGRAM_HOME_CHANNEL"] = "42"
import importlib.util
from pathlib import Path
plugin_dir = Path(loaded.manifest.path)
spec = importlib.util.spec_from_file_location("plugin_api_smoke", plugin_dir / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec); sys.modules["plugin_api_smoke"] = api; spec.loader.exec_module(api)
print("cron status before:", api._cron_status_sync())
first = api._cron_install_sync()
print("cron install #1:", first)
second = api._cron_install_sync(morning_at="07:00")
print("cron install #2:", second)
from cron.jobs import list_jobs
jobs = list_jobs(include_disabled=True)
for j in jobs:
    print("  job:", {k: j.get(k) for k in ("id", "name", "schedule_display", "deliver", "skills", "enabled_toolsets", "enabled", "next_run_at")})
from cron.scheduler_prompt import _build_job_prompt
from cron.scheduler_delivery import _resolve_delivery_targets
for j in jobs:
    built = _build_job_prompt(j)
    text = built[0] if isinstance(built, tuple) else built
    print("  prompt for", j["name"], "-> chars:", len(text), "| skill loaded:", "Утренняя сводка" in text or "Обзор недели" in text,
          "| skipped notice:", "could not be found" in text or "skipped" in text.lower())
    print("  delivery targets:", [{k: t.get(k) for k in ("platform", "chat_id")} for t in _resolve_delivery_targets(j)])
from cron.scheduler_preflight import _preflight_check_skills
print("preflight skills:", [_preflight_check_skills(j) for j in jobs])
