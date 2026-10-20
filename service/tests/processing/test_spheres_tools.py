"""Сферы для ассистента (Р-82): инструменты архива помечают результаты сферами и сужают поиск по `spheres`;
`list_spheres`; изменения сфер через внутренний API ждут нажатия владельца в боте согласований."""

import json

from shturman import spheres

from conftest import MCP_AUTH
from memory_helpers import MCP_JSON, mcp_call, mcp_tools, owner
from pages_helpers import seed
from proc_helpers import chat, say

IVAN, MARIA = 2001, 2002

MODULES = ("shturman.api_core", "shturman.mcp_server", "shturman.processing.service",
           "shturman.processing.pages_service", "shturman.processing.memory_service", "shturman.processing.mcp_tools",
           "shturman.spheres_service")
BRIGADE = 5001


async def mcp_error(client, tool, **arguments) -> str:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    got = await client.post("/mcp", headers={**MCP_AUTH, **MCP_JSON}, json=body)
    result = got.json()["result"]
    assert result["isError"] is True, result
    return result["content"][0]["text"]


async def world(conn):
    """Иван — в «Работе» и в «Личном», Мария — только в «Личном», группа бригады — в «Работе»."""
    w = await seed(conn)
    w.group = await chat(conn, w.account, BRIGADE, "Стройка: бригада", type_="private_supergroup", cls="channel")
    w.group_msgs = await say(conn, w.group, [(IVAN, "Иван Петров", "смета по кровле готова")], first_id=500)
    w.work = await spheres.create_sphere(conn, "Работа", "стройка")
    w.home = await spheres.create_sphere(conn, "Личное")
    await spheres.add_chats(conn, w.work["id"], w.account, [("user", IVAN), ("channel", BRIGADE)])
    await spheres.add_chats(conn, w.home["id"], w.account, [("user", IVAN), ("user", MARIA)])
    return w


async def test_list_spheres_tool_and_rule_in_descriptions(make_client, conn):
    client, _ = await make_client(*MODULES)
    assert (await mcp_call(client, "list_spheres"))["status"] == "not_found"
    w = await world(conn)
    out = await mcp_call(client, "list_spheres")
    assert [(s["name"], s.get("description"), s["chats"]) for s in out["spheres"]] == [
        ("Работа", "стройка", 2), ("Личное", None, 2)]
    assert "soft boundary" in out["rule"]
    tools = await mcp_tools(client)
    assert tools["list_spheres"]["annotations"]["readOnlyHint"] is True
    for name in ("search_messages", "list_chats", "get_chat_history", "find_person", "list_commitments",
                 "search_pages", "list_projects"):
        assert "spheres" in tools[name]["inputSchema"]["properties"], name
    description = " ".join(tools["list_spheres"]["description"].split())
    assert "By default look only in the sphere the conversation is about" in description
    assert "nothing enforces it" in description
    assert "Spheres:" in (await client.post("/mcp", headers={**MCP_AUTH, **MCP_JSON}, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    ).json()["result"]["instructions"]
    # лишний аргумент — ошибка, а не молчаливый пропуск
    assert "extra" in (await mcp_error(client, "list_spheres", sphere="Работа")).lower()
    assert w.work["id"]


async def test_tools_label_results_and_narrow_by_spheres(make_client, conn):
    client, _ = await make_client(*MODULES)
    w = await world(conn)

    found = await mcp_call(client, "search_messages", query="смета")
    labels = {h["chat_id"]: h.get("spheres") for h in found["hits"]}
    assert labels[w.ivan_chat] == ["Работа", "Личное"] and labels[w.group] == ["Работа"]
    only_work = await mcp_call(client, "search_messages", query="смета", spheres=["работа"])
    assert {h["chat_id"] for h in only_work["hits"]} == {w.ivan_chat, w.group}
    home = await mcp_call(client, "search_messages", query="смета", spheres="Личное")
    assert {h["chat_id"] for h in home["hits"]} == {w.ivan_chat}
    assert (await mcp_call(client, "search_messages", query="смета", spheres="без сферы"))["hits"] == []
    assert {h["chat_id"] for h in (await mcp_call(client, "search_messages", query="смета", spheres=["all"]))["hits"]} \
        == {w.ivan_chat, w.group}
    # незнакомое название — ошибка с перечнем сфер, а не поиск «где-нибудь»
    error = await mcp_error(client, "search_messages", query="смета", spheres=["Учёба"])
    assert '"Работа", "Личное"' in error

    chats = await mcp_call(client, "list_chats")
    assert {c["id"]: c.get("spheres") for c in chats["chats"]} == {
        w.ivan_chat: ["Работа", "Личное"], w.maria_chat: ["Личное"], w.group: ["Работа"]}
    assert [c["id"] for c in (await mcp_call(client, "list_chats", spheres=["Личное"]))["chats"]] \
        and {c["id"] for c in (await mcp_call(client, "list_chats", spheres=["Личное"]))["chats"]} == {w.ivan_chat, w.maria_chat}

    history = await mcp_call(client, "get_chat_history", limit=50)
    assert {m["chat_id"]: m.get("spheres") for m in history["messages"]}[w.maria_chat] == ["Личное"]
    narrowed = await mcp_call(client, "get_chat_history", spheres=["Работа"], limit=50)
    assert {m["chat_id"] for m in narrowed["messages"]} == {w.ivan_chat, w.group}
    one = await mcp_call(client, "get_chat_history", chat=w.group)
    assert one["chat"]["spheres"] == ["Работа"] and "spheres" not in one["messages"][0]

    around = await mcp_call(client, "get_context", message_id=w.maria_msgs[0])
    assert around["chat"]["spheres"] == ["Личное"]

    people = await mcp_call(client, "find_person", name="Мария")
    assert people["matches"][0]["spheres"] == ["Личное"]
    assert (await mcp_call(client, "find_person", name="Мария", spheres=["Работа"]))["matches"] == []
    ivan = await mcp_call(client, "find_person", name="Иван Петров", spheres=["Работа"])
    assert ivan["matches"][0]["spheres"] == ["Работа", "Личное"]

    # обязательства — сферами своего чата
    await conn.execute(
        """INSERT INTO commitments (chat_id, source_message_id, direction, what, source_quote, status)
           VALUES ($1, $2, 'owed_to_owner', 'прислать смету', 'смета', 'open'),
                  ($3, $4, 'owed_to_owner', 'подписать акт', 'акт', 'open')""",
        w.group, w.group_msgs[0], w.maria_chat, w.maria_msgs[0])
    items = (await mcp_call(client, "list_commitments"))["items"]
    assert {i["chat_id"]: i.get("spheres") for i in items} == {w.group: ["Работа"], w.maria_chat: ["Личное"]}
    assert [i["chat_id"] for i in (await mcp_call(client, "list_commitments", spheres=["Работа"]))["items"]] == [w.group]
    one = (await mcp_call(client, "get_commitment", commitment_id=items[0]["id"]))["items"][0]
    assert one["spheres"]

    # проекты: своя сфера
    project_id = await conn.fetchval(
        """INSERT INTO projects (title, title_norm, status, origin) VALUES ('ЖК Северный', 'жк северный', 'active', 'owner')
           RETURNING id""")
    await conn.execute("INSERT INTO projects (title, title_norm, status, origin) VALUES ('Дача', 'дача', 'active', 'owner')")
    await spheres.set_project_sphere(conn, project_id, w.work["id"])
    listed = (await mcp_call(client, "list_projects"))["projects"]
    assert {p["title"]: p.get("sphere") for p in listed} == {"ЖК Северный": "Работа", "Дача": None}
    assert [p["title"] for p in (await mcp_call(client, "list_projects", spheres=["Работа"]))["projects"]] == ["ЖК Северный"]
    assert [p["title"] for p in (await mcp_call(client, "list_projects", spheres=["без сферы"]))["projects"]] == ["Дача"]


async def test_plugin_routes_label_and_narrow_too(make_client, conn):
    """Инструменты плагина ходят во внутренний API: те же пометки и то же сужение (`?spheres=`)."""
    client, _ = await make_client(*MODULES)
    w = await world(conn)
    await conn.execute(
        """INSERT INTO commitments (chat_id, source_message_id, direction, what, source_quote, status)
           VALUES ($1, $2, 'owed_to_owner', 'прислать смету', 'смета', 'open')""", w.group, w.group_msgs[0])
    got = (await client.get("/api/commitments")).json()["commitments"]
    assert [c["spheres"] for c in got] == [["Работа"]]
    assert (await client.get("/api/commitments", params={"spheres": "Личное"})).json()["commitments"] == []
    assert (await client.get("/api/commitments", params={"spheres": "Нет такой"})).status_code == 400
    people = (await client.get("/api/people", params={"query": "Мария"})).json()["people"]
    assert people and people[0]["spheres"] == ["Личное"]
    assert (await client.get("/api/people", params={"query": "Мария", "spheres": "Работа"})).json()["people"] == []
    # несколько сфер — одной строкой через перевод строки (так передаёт плагин) или повтором параметра
    both = (await client.get("/api/people", params={"query": "Мария", "spheres": "Работа\nЛичное"})).json()["people"]
    assert both and both[0]["spheres"] == ["Личное"]
    assert (await client.get("/api/spheres")).json()["spheres"][0]["title"] == "Работа"


async def test_sphere_changes_through_the_api_wait_for_the_owner(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    w = await world(conn)
    project_id = await conn.fetchval(
        """INSERT INTO projects (title, title_norm, status, origin) VALUES ('ЖК Северный', 'жк северный', 'active', 'owner')
           RETURNING id""")

    async def titles():
        return [s["title"] for s in await spheres.list_spheres(conn)]

    await approvals.gate(lambda: client.post("/api/spheres", json={"title": "Учёба", "description": "курсы"}), titles,
                         ["Работа", "Личное"], ["Работа", "Личное", "Учёба"])
    study = (await spheres.list_spheres(conn))[2]["id"]
    action = approvals.waiting(await client.put(f"/api/spheres/{study}", json={"title": "Хобби"}))
    assert "«Учёба»" in await approvals.card(action) and "«Хобби»" in await approvals.card(action)
    await approvals.press(action)
    assert (await titles())[2] == "Хобби"

    async def work_chats():
        return sorted(k[1] for k in await spheres.keys_of(conn, w.work["id"], w.account))

    await approvals.gate(lambda: client.post(f"/api/spheres/{w.work['id']}/chats", json={"add": [w.maria_chat]}),
                         work_chats, [IVAN, BRIGADE], [IVAN, MARIA, BRIGADE])
    action = approvals.waiting(await client.post(f"/api/spheres/{w.work['id']}/chats", json={"remove": [w.group]}))
    assert "убрать «Стройка: бригада»" in await approvals.card(action)
    await approvals.press(action)
    assert await work_chats() == [IVAN, MARIA]

    async def project_sphere():
        return await conn.fetchval("SELECT sphere_id FROM projects WHERE id = $1", project_id)

    await approvals.gate(lambda: client.post(f"/api/projects/{project_id}/sphere", json={"sphere_id": w.work["id"]}),
                         project_sphere, None, w.work["id"])
    # удалить — тоже ждёт: в следующих шагах снятие сферы ослабит границу
    action = approvals.waiting(await client.delete(f"/api/spheres/{w.home['id']}"))
    assert "Сами чаты, сохранённые сообщения и проекты останутся" in await approvals.card(action)
    assert len(await titles()) == 3
    await approvals.press(action)
    assert await titles() == ["Работа", "Хобби"]
    assert await conn.fetchval("SELECT count(*) FROM chats") == 3

    # заведомо неисполнимое — отказ до карточки
    for method, path, body, status in (("POST", "/api/spheres", {"title": "работа"}, 409),
                                       ("POST", "/api/spheres", {"title": "все"}, 400),
                                       ("POST", f"/api/spheres/{w.work['id']}/chats", {"add": [999999]}, 404),
                                       ("POST", f"/api/spheres/{w.work['id']}/chats", {}, 400),
                                       ("PUT", "/api/spheres/999999", {"title": "x"}, 404),
                                       ("POST", f"/api/projects/{project_id}/sphere", {"sphere_id": True}, 400)):
        got = await client.request(method, path, json=body)
        assert got.status_code == status, (path, got.text)
    assert await approvals.pending() == 0
    # токен архива внутренний API не открывает
    for method, path in (("GET", "/api/spheres"), ("POST", "/api/spheres"), ("DELETE", f"/api/spheres/{study}")):
        assert (await client.request(method, path, headers=MCP_AUTH)).status_code == 401


async def test_owner_context_applies_at_once(make_client, conn, own_bot, approvals):
    """Подтверждённый владелец (нажатие в боте, страница) — сразу, без второй карточки."""
    client, _ = await make_client(*MODULES)
    await world(conn)
    with owner():
        got = await client.post("/api/spheres", json={"title": "Учёба"})
    assert got.status_code == 200 and got.json()["sphere"]["title"] == "Учёба"
    assert await approvals.pending() == 0
    assert json.dumps(got.json(), ensure_ascii=False).count("Учёба") == 1
