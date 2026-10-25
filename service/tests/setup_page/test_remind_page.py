"""Круг напоминаний исполнителям на странице настройки (`setup_page/remind.py`): владелец меняет сразу, в журнал —
признаки и числа, без имён и номеров; внутреннего API у настройки нет."""

from setup_fakes import ORIGIN, bind_owner, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути

from shturman import store  # noqa: E402
from shturman.brief import remind  # noqa: E402
from shturman.records import ChatRecord  # noqa: E402

from test_miniapp import OWNER, TOKEN  # noqa: E402

MODULES = ("shturman.api_core", "shturman.executor.service", "shturman.setup_page.service")
IVAN = 2001


async def test_page_sets_who_gets_the_remind_button(stand, conn):
    s = await stand(modules=MODULES, bot_token=TOKEN)
    await s.page.login(conn)
    await bind_owner(s)
    acc = await store.ensure_account(conn, OWNER["id"], "Владелец", "owner")
    await store.ensure_peer(conn, "user", IVAN, name="Иван Петров")
    group, _ = await store.ensure_chat(conn, acc, ChatRecord("channel", 5001, "private_supergroup", "Стройка: Северный"))
    view = (await s.page.get("/remind")).json()
    assert view["settings"] == {"enabled": True, "who": "all", "people": [], "groups": []}
    out = await s.page.put("/remind", {"who": "chosen", "people": [IVAN], "groups": [group]})
    assert out.status_code == 200
    body = out.json()
    assert body["people"] == [{"tg_id": IVAN, "name": "Иван Петров", "username": None}]
    assert body["groups"][0]["title"] == "Стройка: Северный"
    assert (await remind.load(conn))["who"] == "chosen"
    detail = await conn.fetchval("SELECT detail FROM setup_audit WHERE action = 'remind.settings'")
    assert detail == "предлагать: да; кому: выбранным; людей: 1; групп: 1"
    assert (await s.page.put("/remind", {"who": "everyone"})).status_code == 400
    assert (await s.page.put("/remind", {})).status_code == 400
    # внутреннего API нет
    assert (await s.api.get("/api/remind")).status_code == 404
