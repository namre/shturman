"""Бот с изменённым адресом: код подтверждения пары (посредник 0.4.1, setup_page/bot_setup.py).

Владелец поменял адрес бота в окне создания — посредник не узнал бота по имени. Страница просит
вписать новый адрес и отправить боту `/start <код>`; посредник присоединяет бота и сообщает
`claimed: true`. Сервис принимает токен, только если адрес совпал и с вписанным, и с `getMe`;
подтверждение «Это ваш аккаунт?» остаётся.
"""

from __future__ import annotations

import json

from setup_fakes import TOKEN, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from exec_fakes import BOT_ID, until

from shturman.executor import binding
from test_setup_managed_bot import CLAIM, PAIR, SECRET, audit_rows, managed_stand, pairing, until_status

RENAMED = "shturman_erman_bot"


async def renamed_stand(stand, conn):
    s = await managed_stand(stand, conn)
    s.telegram.me["username"] = RENAMED             # getMe: бот с тем адресом, который выбрал владелец
    return s


async def test_renamed_bot_is_found_by_the_claim_and_still_needs_the_owner(stand, conn):
    s = await renamed_stand(stand, conn)
    view = (await s.page.post("/bot/managed")).json()["attempt"]
    assert view["claim"] == CLAIM and view["claim_link"] is None
    got = await s.page.post("/bot/managed/claim", {"username": "@Shturman_Erman_Bot"})
    assert got.status_code == 200, got.text
    view = got.json()["attempt"]
    assert view["claimed_username"] == "Shturman_Erman_Bot"
    assert view["claim_link"] == f"https://t.me/Shturman_Erman_Bot?start={CLAIM}"
    assert ("bot.managed_claim", "ok", "указан адрес @Shturman_Erman_Bot") in await audit_rows(conn)
    s.relay.create(username=RENAMED, claimed=True)
    await until_status(s, "confirm")
    assert s.state.config.bot_token == ""                              # «Это ваш аккаунт?» — по-прежнему
    a = (await s.page.get("/state")).json()["bot"]["managed"]["attempt"]
    assert a["bot_username"] == RENAMED and a["claim"] is None
    assert (await s.page.post("/bot/managed/confirm", {"accept": True})).status_code == 200
    await until(lambda: s.state.config.bot_token == TOKEN)
    assert (await binding.bot_origin(conn, BOT_ID))["created_via"] == "managed"
    assert SECRET not in json.dumps(a)


async def test_claimed_bot_must_match_the_typed_address(stand, conn):
    s = await renamed_stand(stand, conn)
    await s.page.post("/bot/managed")
    s.relay.create(username=RENAMED, claimed=True)                     # адрес на странице не вписан
    await until_status(s, "failed")
    assert pairing(s).code == "claim_mismatch" and pairing(s).token == ""
    assert not s.telegram.calls("getMe")
    # Вписан другой адрес — тоже отказ.
    from test_setup_managed_bot import FakeRelay
    from shturman.executor import service as executor_service
    s.relay = FakeRelay()
    executor_service.TEST_OVERRIDES["botpair_transport"] = s.relay.transport()
    await s.page.post("/bot/managed")
    await s.page.post("/bot/managed/claim", {"username": "other_one_bot"})
    s.relay.create(username=RENAMED, claimed=True)
    await until_status(s, "failed")
    assert pairing(s).code == "claim_mismatch"
    # Вписан верный адрес, но getMe говорит о другом имени — отказ.
    s.relay = FakeRelay()
    executor_service.TEST_OVERRIDES["botpair_transport"] = s.relay.transport()
    s.telegram.me["username"] = "yet_another_bot"
    await s.page.post("/bot/managed")
    await s.page.post("/bot/managed/claim", {"username": RENAMED})
    s.relay.create(username=RENAMED, claimed=True)
    await until_status(s, "failed")
    assert pairing(s).code == "wrong_bot" and s.state.config.bot_token == ""


async def test_typed_address_is_checked_and_only_while_waiting(stand, conn):
    s = await renamed_stand(stand, conn)
    assert (await s.page.post("/bot/managed/claim", {"username": RENAMED})).json()["code"] == "not_pending"
    await s.page.post("/bot/managed")
    for bad in ("", "erman", "@1abc_bot", "a" * 40 + "bot", "evil.bot", "ivan_bot?start=x", None, 5):
        got = await s.page.post("/bot/managed/claim", {"username": bad})
        assert got.status_code == 422 and got.json()["code"] == "bad_username", bad
    s.telegram.me["username"] = "shturman_" + PAIR + "_bot"
    s.relay.create()                                                   # бот с адресом из ссылки — без кода
    await until_status(s, "confirm")
    assert (await s.page.post("/bot/managed/claim", {"username": RENAMED})).json()["code"] == "not_pending"


async def test_relay_without_claims_explains(stand, conn):
    s = await renamed_stand(stand, conn)
    await s.page.post("/bot/managed")
    pairing(s).claim = None                                            # посредник 0.4.0 кода не даёт
    got = await s.page.post("/bot/managed/claim", {"username": RENAMED})
    assert got.status_code == 422 and got.json()["code"] == "no_claim"


async def test_cancel_tells_the_relay(stand, conn):
    s = await renamed_stand(stand, conn)
    await s.page.post("/bot/managed")
    assert (await s.page.delete("/bot/managed")).status_code == 200
    await until(lambda: (f"/api/botpair/{PAIR}/cancel", {"secret": SECRET}) in s.relay.requests)
