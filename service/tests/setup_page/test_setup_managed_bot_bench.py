"""Стенд «бот в одно нажатие» целиком: настоящая служба посредника (relay/relayd.py 0.4.0, HTTP на
127.0.0.1, поток опроса бота-менеджера), подставной Bot API Telegram посредника (настоящий HTTP-сервер,
relay/tests/fake_telegram.py) и сервис со страницей настройки. Сервис ходит к посреднику настоящим
httpx по сети; браузер владельца — только к странице.

Чего здесь нет: настоящего Telegram (окна создания бота у владельца, его реальных полей и
ограничений) — это проверяется вживую по списку в docs/approvals-bot.md.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from setup_fakes import TOKEN, bind_owner, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from exec_fakes import BOT_ID, OWNER, OWNER_USER, until

from shturman.executor import binding
from shturman.executor import service as executor_service

RELAY_DIR = Path(__file__).resolve().parents[3] / "relay"
for _p in (RELAY_DIR, RELAY_DIR / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import relayd  # noqa: E402
from fake_telegram import MANAGER_TOKEN, FakeTelegram  # noqa: E402


async def test_bench_relayd_and_service_end_to_end(stand, conn, tmp_path, caplog):
    tg = FakeTelegram()
    base = tg.start()
    ca = tmp_path / "frps.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    settings = relayd.Settings(db=str(tmp_path / "relay.db"), domain="sh.example.test", ca_file=str(ca), frp_token="t",
                               tg_api=base, manager_poll=1)
    relayd.write_manager_token(settings.manager_path, MANAGER_TOKEN)
    _, api = relayd.make_servers(settings, ("127.0.0.1", 0), ("127.0.0.1", 0))
    stop = threading.Event()
    threading.Thread(target=api.serve_forever, daemon=True).start()
    threading.Thread(target=api.manager.run, args=(stop,), daemon=True).start()
    try:
        await until(lambda: api.manager.ready, timeout=10)
        s = await stand(bot_manager_api=f"http://127.0.0.1:{api.server_address[1]}")
        executor_service.TEST_OVERRIDES.update(botpair_poll=0.05)
        await s.page.login(conn)
        started = await s.page.post("/bot/managed")
        assert started.status_code == 200, started.text
        view = started.json()["attempt"]
        username = view["username"]
        assert view["link"].startswith(f"https://t.me/newbot/shturman_manager_bot/{username}?name=")
        assert len(api.manager.pairs) == 1
        # Владелец подтвердил создание в Telegram. Подставной Telegram сервиса знает этот же токен.
        s.telegram.me["username"] = username
        tg.create_managed_bot(username, "Согласования", {**OWNER_USER, "username": "evgeny_t"}, bot_id=BOT_ID, token=TOKEN)
        page = s.state.extras["setup_page"]
        await until(lambda: page.managed.status == "confirm", timeout=15)
        assert s.state.config.bot_token == ""                          # без подтверждения владельца — не сохранён
        creator = (await s.page.get("/state")).json()["bot"]["managed"]["attempt"]["creator"]
        assert creator == {"id": OWNER, "username": "evgeny_t", "name": "Евгений Тестов"}
        assert (await s.page.post("/bot/managed/confirm", {"accept": True})).status_code == 200
        await until(lambda: s.state.config.bot_token == TOKEN, timeout=15)
        await until(lambda: s.state.extras["executor"].bot is not None and s.state.extras["executor"].bot.identity)
        assert tg.managed[BOT_ID]["restricted"] is True                 # посредник закрыл бота для посторонних
        assert api.manager.pairs == {}                                   # посредник пару забыл
        assert ("getManagedBotToken", {"user_id": BOT_ID}) in tg.calls
        state = (await s.page.get("/state")).json()["bot"]
        assert state["created_via"] == "managed" and state["restricted"] is True
        assert await binding.required_creator(conn, BOT_ID) == OWNER
        await bind_owner(s)                                             # создатель — владелец
        assert (await s.page.get("/state")).json()["bot"]["owner_bound"] is True
        assert TOKEN not in caplog.text and MANAGER_TOKEN not in caplog.text
        assert TOKEN not in json.dumps(state)
    finally:
        stop.set()
        api.shutdown()
        api.server_close()
        tg.stop()
