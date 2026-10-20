"""Заготовки тестов памяти этапа 2: факты, проекты, профиль владельца."""

from datetime import timedelta

from shturman import authority, bridge
from shturman.processing import pipeline

from proc_helpers import OWNER, T0, TZ, answer, buttons_of, claim, press

NOW = T0 + timedelta(hours=1)


def fact(message, quote, about, text, *, slot=None, kind="fact", project=None):
    return {"message": message, "source_quote": quote, "about": about, "text": text, "slot": slot,
            "kind": kind, "project": project}


def owner():
    """Проверенный владелец: нажатие в боте согласований или страница настройки."""
    return authority.owner_context(OWNER, chat_id=OWNER, action="test.memory")


async def plan(conn, **kw):
    return await pipeline.plan_run(conn, tz=TZ, now=kw.pop("now", NOW), **kw)


async def run_with(conn, reply, **plan_kw):
    """Прогон обработки: на каждый запрос извлечения отвечает reply(задание) -> разобранный ответ."""
    out = await plan(conn, **plan_kw)
    for job in await claim(conn):
        if job["payload"].get("schema_name") == "commitments":
            parsed = reply(job)
            assert await answer(conn, job, parsed)
    return out


async def notifications(conn):
    """Сообщения владельцу, ещё не забранные исполнителем, в порядке постановки."""
    return sorted(await claim(conn, bridge.NOTIFY_OWNER), key=lambda job: job["id"])


def buttons(jobs, module):
    return [data for job in jobs for _, data in buttons_of(job) if data.startswith(f"sh:{module}:")]


async def press_button(conn, data):
    return await press(conn, data)


# --- MCP ------------------------------------------------------------------------------------------

MCP_JSON = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def mcp_call(client, tool, **arguments):
    """Вызов инструмента архива; возвращает структурированный ответ (ошибка инструмента — провал)."""
    import json

    from conftest import MCP_AUTH

    response = await client.post("/mcp", headers={**MCP_AUTH, **MCP_JSON}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result.get("isError") is not True, result
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result["structuredContent"]


async def mcp_tools(client):
    from conftest import MCP_AUTH

    response = await client.post("/mcp", headers={**MCP_AUTH, **MCP_JSON},
                                 json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return {t["name"]: t for t in response.json()["result"]["tools"]}
