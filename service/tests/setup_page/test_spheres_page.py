"""Сферы, папки Telegram и раскладки списка чатов на странице «Переписка» и в приложении «Штурман» (Р-82).

Главное, что здесь проверяется:
  * папки читаются из «Telegram» той же фоновой загрузкой, что список диалогов, и хранятся в базе;
  * раскладки «Папки / Сферы / Активность / Вид» — разделы со счётчиком «читается X из Y» и
    постраничным списком внутри раздела;
  * «Включить все» в разделе — только на странице, с глубиной истории и пропуском исключённых и
    служебных чатов; в приложении массовое включение отвергается, а «Выключить все» работает и
    сообщается владельцу в бот одной строкой; всё — в журнал действий;
  * сферы: завести, переименовать, порядок, удалить (= снять), чаты из строки и из раздела, «Сделать
    сферу из папки», «Связать папку» — новые чаты предлагаются, а не добавляются;
  * новые маршруты не отдаются на адресе дашборда и закрыты без своей сессии.
"""

import json
from datetime import datetime, timedelta, timezone

from setup_fakes import API, API_HASH, ORIGIN, bind_owner, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from conftest import API_AUTH, MCP_AUTH
import exec_fakes  # noqa: E402
from tg_fakes import (C_NEWS, C_SUPER, G_FAMILY, HELPER, IVAN, MARIA, U_BOT, U_IVAN, U_MARIA, U_TELEGRAM,  # noqa: E402
                      World, dialogs_loaded, folder, user, wait_for)
from tg_fakes import msg as tg_msg  # noqa: E402

from test_miniapp import MODULES, OWNER, TOKEN, App, signed  # noqa: E402

APP_API = API + "/app"
CLIENTS = [user(20_000 + i, "Клиент", f"Номер {i}", contact=True) for i in range(40)]


def foldered() -> World:
    """Аккаунт с папками: «Работа» — явные чаты и клиенты-контакты; «Каналы» — правило; общая папка."""
    world = World(HELPER)
    world.authorized = False
    world.dialogs = [U_IVAN, U_MARIA, U_BOT, U_TELEGRAM, G_FAMILY, C_SUPER, C_NEWS] + CLIENTS
    world.add(*(tg_msg(i, ("user", IVAN), f"сообщение {i}", sender=IVAN) for i in range(1, 4)))
    world.folders = [
        folder(2, "Работа", include=[U_IVAN, C_SUPER, *CLIENTS[:5]], emoticon="💼"),
        folder(3, "Каналы", broadcasts=True),
        folder(4, "Тихие группы", groups=True, exclude_muted=True),
    ]
    world.muted = {("chat", G_FAMILY.id)}
    # время последнего сообщения — убывает по списку, как у Telegram
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    for n, entity in enumerate(world.dialogs):
        key = ("user" if entity in (U_IVAN, U_MARIA, U_BOT, U_TELEGRAM) or entity in CLIENTS else
               "chat" if entity is G_FAMILY else "channel", entity.id)
        # Раскладка «по активности» считает от настоящего «сейчас» базы — даты тоже от него, а не от DIALOGS_NOW.
        world.dates[key] = recent - timedelta(days=(0 if n < 2 else 10 if n < 4 else 45 if n < 6 else 400))
    return world


async def ready(stand, conn, world=None, **changes):
    s = await stand(modules=MODULES, bot_token=TOKEN, tg_api_id=1234567, tg_api_hash=API_HASH,
                    world=world or foldered(), **changes)
    await s.page.login(conn)
    await bind_owner(s)
    started = await s.page.post("/tg/login", {"role": "assistant"})
    assert started.status_code == 200, started.text
    s.world.me = HELPER
    s.world.last.scan.set_result(HELPER)
    login_id = started.json()["login_id"]
    await wait_for(lambda: s.manager.flows[login_id].done)
    s.account = (await s.page.get(f"/tg/login/{login_id}")).json()["account_id"]
    await wait_for(lambda: s.manager.runtimes["assistant"].status == "running")
    s.app = App(s.api._transport, ORIGIN)
    assert (await s.app.sign_in(signed())).status_code == 200
    await dialogs_loaded(s.manager, s.account)
    return s


def section(body, sid):
    return next(x for x in body["sections"] if x["id"] == sid)


async def audit_rows(conn, prefix):
    return [(r["action"], r["outcome"], r["detail"]) for r in await conn.fetch(
        "SELECT action, outcome, detail FROM setup_audit WHERE action LIKE $1 ORDER BY id", prefix + "%")]


async def told(s, words: str) -> list[str]:
    def found():
        return [m["text"] for m in s.telegram.sent() if words in m["text"] and m["chat_id"] == OWNER["id"]]
    await exec_fakes.until(found)
    return found()


# --- папки и раскладки ------------------------------------------------------------------------------------

async def test_folders_are_read_with_the_dialog_list_and_laid_out(stand, conn):
    s = await ready(stand, conn)
    stored = await conn.fetch("SELECT folder_id, title, kind, flags FROM tg_folders ORDER BY position")
    assert [(r["folder_id"], r["title"], r["kind"]) for r in stored] == [(2, "Работа", "filter"), (3, "Каналы", "filter"),
                                                                          (4, "Тихие группы", "filter")]
    asked = [type(r).__name__ for c in s.world.clients for r in c.requests]
    assert asked.count("GetDialogFiltersRequest") == 1           # одним запросом той же загрузкой

    body = (await s.page.get(f"/tg/accounts/{s.account}/sections", params={"layout": "folders"})).json()
    work = section(body, "folder:2")
    assert (work["title"], work["emoticon"], work["total"], work["enabled"]) == ("Работа", "💼", 7, 0)
    assert section(body, "folder:3")["total"] == 1                         # канал «Стройка: новости»
    quiet = section(body, "folder:4")
    assert quiet["total"] == 1 and any("общие настройки" in n for n in quiet["notes"])   # «Семья» без звука — отсеяна
    assert quiet["total"] == 1                                              # «Стройка: чат» — и в «Работе»
    assert section(body, "folder:none")["total"] == 47 - 7 - 1             # чат может быть в двух папках
    # постранично внутри раздела
    page1 = (await s.page.get(f"/tg/accounts/{s.account}/dialogs", params={"section": "folder:2", "limit": 3})).json()
    assert page1["total"] == 7 and [i["title"] for i in page1["items"]] == ["Иван Петров", "Стройка: чат", "Клиент Номер 0"]
    page2 = (await s.page.get(f"/tg/accounts/{s.account}/dialogs",
                              params={"section": "folder:2", "limit": 3, "offset": 3})).json()
    assert [i["title"] for i in page2["items"]] == ["Клиент Номер 1", "Клиент Номер 2", "Клиент Номер 3"]
    # поиск действует и на разделы
    body = (await s.page.get(f"/tg/accounts/{s.account}/sections", params={"layout": "folders", "q": "клиент"})).json()
    assert section(body, "folder:2")["total"] == 5

    activity = (await s.page.get(f"/tg/accounts/{s.account}/sections", params={"layout": "activity"})).json()
    assert [x["id"] for x in activity["sections"]][0] == "activity:week"
    assert sum(x["total"] for x in activity["sections"]) == 47
    kinds = {x["id"]: x["total"] for x in
             (await s.page.get(f"/tg/accounts/{s.account}/sections", params={"layout": "kind"})).json()["sections"]}
    assert kinds == {"kind:personal": 43, "kind:group": 2, "kind:channel": 1, "kind:bot": 1}
    assert (await s.page.get(f"/tg/accounts/{s.account}/sections", params={"layout": "x"})).status_code == 400
    assert (await s.page.get(f"/tg/accounts/{s.account}/dialogs", params={"section": "folder:99"})).status_code == 404


async def test_enable_all_on_the_page_uses_the_depth_and_skips_excluded_chats(stand, conn):
    s = await ready(stand, conn)
    # «Семья» исключена из архива; служебный чат Telegram (777000) исключён всегда
    assert (await s.page.post(f"/tg/accounts/{s.account}/exclude",
                              {"peer_class": "chat", "tg_id": G_FAMILY.id, "excluded": True})).status_code == 200
    kinds = (await s.page.get(f"/tg/accounts/{s.account}/sections", params={"layout": "kind"})).json()
    groups = section(kinds, "kind:group")
    assert (groups["total"], groups["can_enable"]) == (2, 1)
    got = await s.page.post(f"/tg/accounts/{s.account}/bulk", {"section": "kind:group", "enabled": True, "months": 6})
    assert got.status_code == 200, got.text
    assert (got.json()["changed"], got.json()["skipped"]) == (1, 1)
    enabled = await conn.fetch("SELECT peer_class, tg_id, backfill_since FROM tg_sync_chats WHERE enabled")
    assert [(r["peer_class"], r["tg_id"]) for r in enabled] == [("channel", C_SUPER.id)]
    assert enabled[0]["backfill_since"] is not None
    personal = await s.page.post(f"/tg/accounts/{s.account}/bulk", {"section": "kind:personal", "enabled": True})
    assert personal.json()["skipped"] == 1                                    # 777000
    assert not await conn.fetchval("SELECT enabled FROM tg_sync_chats WHERE tg_id = 777000")
    off = await s.page.post(f"/tg/accounts/{s.account}/bulk", {"section": "kind:group", "enabled": False})
    assert off.json()["changed"] == 1
    rows = await audit_rows(conn, "tg.bulk")
    assert rows[0] == ("tg.bulk_on", "ok", "раздел kind:group; чатов: 1; пропущено исключённых: 1; глубина: 6 месяцев")
    assert rows[-1] == ("tg.bulk_off", "ok", "раздел kind:group; чатов: 1")
    assert "Стройка" not in json.dumps(rows, ensure_ascii=False)
    assert await conn.fetchval("SELECT count(*) FROM pending_actions") == 0


async def test_in_the_app_enable_all_is_refused_and_disable_all_is_told_to_the_owner(stand, conn):
    s = await ready(stand, conn)
    assert (await s.page.post(f"/tg/accounts/{s.account}/bulk", {"section": "folder:2", "enabled": True})).json()["changed"] == 7
    refused = await s.app.post(f"/chats/{s.account}/bulk", {"section": "folder:3", "enabled": True})
    assert refused.status_code == 400 and refused.json()["code"] == "page_only"
    assert not await conn.fetchval("SELECT enabled FROM tg_sync_chats WHERE tg_id = $1", C_NEWS.id)
    sections = (await s.app.get(f"/chats/{s.account}/sections", params={"layout": "folders"})).json()
    assert section(sections, "folder:2")["enabled"] == 7
    off = await s.app.post(f"/chats/{s.account}/bulk", {"section": "folder:2", "enabled": False, "q": "клиент"})
    assert off.status_code == 200 and off.json()["changed"] == 5             # только видимые по поиску
    assert await conn.fetchval("SELECT count(*) FROM tg_sync_chats WHERE enabled") == 2
    (text,) = await told(s, "Из приложения выключено чтение всех чатов раздела")
    assert text == "Из приложения выключено чтение всех чатов раздела «Работа»: 5."
    assert await audit_rows(conn, "app.bulk") == [
        ("app.bulk_on", "refused", "массовое включение — только на странице"),
        ("app.bulk_off", "ok", "раздел folder:2; чатов: 5")]


async def test_spheres_on_the_page_folders_offers_and_projects(stand, conn):
    s = await ready(stand, conn)
    made = await s.page.post("/spheres/folders/make", {"account_id": s.account, "folder_id": 2})
    assert made.status_code == 200, made.text
    sphere = made.json()["sphere"]
    assert (sphere["title"], made.json()["created"], made.json()["added"]) == ("Работа", True, 7)
    assert await conn.fetchval("SELECT count(*) FROM chats") == 0           # сфера не заводит записей архива
    linked = await s.page.post(f"/spheres/{sphere['id']}/folders", {"account_id": s.account, "folder_id": 2, "link": True})
    assert linked.status_code == 200 and linked.json()["proposed"] == 0
    # в папке в Telegram появился новый чат — после обновления списка он предложен, но не добавлен
    newcomer = user(30_001, "Новый", "Заказчик", contact=True)
    s.world.dialogs.insert(0, newcomer)
    s.world.dates[("user", newcomer.id)] = s.world.dates[("user", IVAN)] + timedelta(minutes=1)
    s.world.folders[0] = folder(2, "Работа", include=[U_IVAN, C_SUPER, *CLIENTS[:5], newcomer], emoticon="💼")
    await dialogs_loaded(s.manager, s.account)
    overview = (await s.page.get("/spheres")).json()
    (offer,) = overview["proposals"]
    assert (offer["sphere_id"], offer["count"], offer["chats"][0]["title"]) == (sphere["id"], 1, "Новый Заказчик")
    assert "keys" not in offer
    assert overview["spheres"][0]["chats"] == 7
    folders = (await s.page.get(f"/tg/accounts/{s.account}/sections", params={"layout": "folders"})).json()
    assert section(folders, "folder:2")["proposals"] == [{"sphere_id": sphere["id"], "sphere_title": "Работа", "count": 1}]
    accepted = await s.page.post(f"/spheres/{sphere['id']}/proposals",
                                 {"account_id": s.account, "folder_id": 2, "accept": True})
    assert accepted.json()["added"] == 1 and (await s.page.get("/spheres")).json()["proposals"] == []

    # сферы из строки чата: чат в нескольких сферах
    home = (await s.page.post("/spheres", {"title": "Личное", "description": "семья"})).json()["sphere"]
    both = await s.page.put("/spheres/chat", {"account_id": s.account, "peer_class": "user", "tg_id": IVAN,
                                              "sphere_ids": [sphere["id"], home["id"]]})
    assert both.json()["spheres"] == [sphere["id"], home["id"]]
    listed = (await s.page.get(f"/tg/accounts/{s.account}/dialogs", params={"q": "иван"})).json()["items"]
    assert listed[0]["spheres"] == [sphere["id"], home["id"]]
    by_sphere = (await s.page.get(f"/tg/accounts/{s.account}/sections", params={"layout": "spheres"})).json()
    assert [(x["title"], x["total"]) for x in by_sphere["sections"]] == [("Работа", 8), ("Личное", 1), ("Без сферы", 40)]
    # раздел сферы: чаты, убрать, добавить, переименовать, порядок, удалить
    chats = (await s.page.get(f"/spheres/{home['id']}/chats")).json()["chats"]
    assert [(c["title"], c["enabled"]) for c in chats] == [("Иван Петров", False)]
    assert (await s.page.post(f"/spheres/{home['id']}/chats", {"account_id": s.account, "add": True,
                                                               "chats": [{"peer_class": "chat", "tg_id": G_FAMILY.id}]})).json()["changed"] == 1
    assert (await s.page.post(f"/spheres/{home['id']}/chats", {"account_id": s.account, "add": False,
                                                               "chats": [{"peer_class": "user", "tg_id": IVAN}]})).json()["changed"] == 1
    assert (await s.page.put(f"/spheres/{home['id']}", {"title": "Семья и дом"})).json()["sphere"]["title"] == "Семья и дом"
    assert (await s.page.put(f"/spheres/{home['id']}", {"title": "работа"})).status_code == 409
    order = (await s.page.put("/spheres/order", {"ids": [home["id"], sphere["id"]]})).json()["spheres"]
    assert [x["id"] for x in order] == [home["id"], sphere["id"]]

    # проект → сфера; удаление сферы снимает её с проекта и чатов, но не трогает их
    project_id = await conn.fetchval(
        """INSERT INTO projects (title, title_norm, status, origin) VALUES ('ЖК Северный', 'жк северный', 'active', 'owner')
           RETURNING id""")
    assert (await s.page.put(f"/memory/projects/{project_id}/sphere", {"sphere_id": sphere["id"]})).json()["changed"]
    assert (await s.page.get(f"/memory/projects/{project_id}")).json()["sphere"] == "Работа"
    assert (await s.page.put(f"/memory/projects/{project_id}/sphere", {"sphere_id": 999999})).status_code == 404
    gone = await s.page.delete(f"/spheres/{sphere['id']}")
    assert gone.status_code == 200 and (gone.json()["chats"], gone.json()["projects"]) == (8, 1)
    assert await conn.fetchval("SELECT (status, sphere_id) FROM projects WHERE id = $1", project_id) == ("active", None)
    assert await conn.fetchval("SELECT count(*) FROM tg_dialogs") == 48

    rows = await audit_rows(conn, "sphere.")
    assert [r[0] for r in rows] == ["sphere.folder", "sphere.folder", "sphere.chats", "sphere.create", "sphere.chats",
                                    "sphere.chats", "sphere.chats", "sphere.update", "sphere.update", "sphere.project",
                                    "sphere.delete"]
    dumped = json.dumps(rows, ensure_ascii=False)
    assert "Работа" not in dumped and "Иван" not in dumped and "Семья" not in dumped


async def test_spheres_in_the_app_are_logged_from_the_app(stand, conn):
    s = await ready(stand, conn)
    created = await s.app.post("/spheres", {"title": "Работа"})
    assert created.status_code == 200, created.text
    sphere_id = created.json()["sphere"]["id"]
    assert (await s.app.put("/spheres/chat", {"account_id": s.account, "peer_class": "user", "tg_id": MARIA,
                                              "sphere_ids": [sphere_id]})).json()["spheres"] == [sphere_id]
    made = await s.app.post("/spheres/folders/make", {"account_id": s.account, "folder_id": 3})
    assert made.json()["created"] and made.json()["sphere"]["title"] == "Каналы"
    folders = (await s.app.get("/spheres/folders")).json()["folders"]
    assert [(f["folder_id"], f["count"], [x["title"] for x in f["spheres"]]) for f in folders] == [
        (2, 7, []), (3, 1, ["Каналы"]), (4, 1, [])]
    assert (await s.app.delete(f"/spheres/{sphere_id}")).status_code == 200
    rows = await audit_rows(conn, "app.sphere")
    assert [r[0] for r in rows] == ["app.sphere_create", "app.sphere_chats", "app.sphere_folder", "app.sphere_delete"]
    overview = (await s.page.get("/overview")).json()
    assert overview["audit_key"][0]["title"] == "Из приложения в Telegram: сфера удалена (снята с чатов и проектов)"
    # изменения сфер чтения не трогают: сообщений владельцу о них нет
    assert not [m for m in s.telegram.sent() if "сфер" in m["text"].lower()]
    # проектов в приложении нет
    assert (await s.app.put("/memory/projects/1/sphere", {"sphere_id": None})).status_code in (401, 404, 405)


async def test_new_routes_need_their_own_session_and_are_not_on_the_dashboard(stand, conn):
    s = await ready(stand, conn, setup_origin="https://assistant.example.com:8443",
                    dashboard_origin="https://assistant.example.com")
    page_paths = [("GET", "/spheres", None), ("POST", "/spheres", {"title": "x"}), ("GET", "/spheres/folders", None),
                  ("PUT", "/spheres/chat", {}), ("GET", f"/tg/accounts/{s.account}/sections?layout=kind", None),
                  ("POST", f"/tg/accounts/{s.account}/bulk", {"section": "kind:group", "enabled": True}),
                  ("PUT", "/memory/projects/1/sphere", {"sphere_id": None})]
    app_paths = [("GET", "/spheres", None), ("POST", "/spheres", {"title": "x"}),
                 ("GET", f"/chats/{s.account}/sections?layout=kind", None),
                 ("POST", f"/chats/{s.account}/bulk", {"section": "kind:group", "enabled": False})]
    for host in ("assistant.example.com", "assistant.example.com:443"):
        for method, path, body in page_paths:
            got = await s.page.http.request(method, API + path, json=body, headers={**s.page.headers(), "Host": host})
            assert got.status_code == 421, (host, path)
        for method, path, body in app_paths:
            got = await s.app.http.request(method, APP_API + path, json=body, headers={**s.app.headers(), "Host": host})
            assert got.status_code == 421, (host, path)
    # без своей сессии — закрыто; ключ приложения не открывает страницу, и наоборот; токены API — тоже нет
    for headers in ({}, API_AUTH, MCP_AUTH, {"X-Shturman-App": s.app.key}):
        for method, path, body in page_paths:
            got = await s.api.request(method, API + path, json=body, headers={
                "Origin": ORIGIN, "X-Shturman-Setup": "1", "Sec-Fetch-Site": "same-origin", **headers})
            assert got.status_code in (401, 421), (path, headers, got.status_code)
    for headers in ({}, API_AUTH, {"X-Shturman-Session": s.page.key}):
        for method, path, body in app_paths:
            got = await s.api.request(method, APP_API + path, json=body, headers={
                "Origin": ORIGIN, "X-Shturman-Setup": "1", "Sec-Fetch-Site": "same-origin", **headers})
            assert got.status_code in (401, 421), (path, headers, got.status_code)
    assert await conn.fetchval("SELECT count(*) FROM spheres") == 0
    assert await conn.fetchval("SELECT count(*) FROM tg_sync_chats WHERE enabled") == 0


def test_audit_titles_for_spheres():
    from shturman.setup_page import audit

    for action in ("app.sphere_create", "app.sphere_update", "app.sphere_delete", "app.sphere_chats",
                   "app.sphere_folder", "app.bulk_off", "app.bulk_on"):
        assert audit.ACTIONS[action].startswith("Из приложения") and action in audit.IMPORTANT
    for action in ("sphere.create", "sphere.update", "sphere.delete", "sphere.chats", "sphere.folder",
                   "sphere.project", "tg.bulk_on", "tg.bulk_off"):
        assert action in audit.ACTIONS
