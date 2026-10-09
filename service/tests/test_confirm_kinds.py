"""Перечень действий, которые ждут владельца: новый вид действия должен появиться здесь осознанно."""

from shturman import confirm
from shturman.app import MODULES

# Виды действий и модуль, который их применяет. Если тест упал — в сервисе появилось новое
# действие через confirm: проверьте, что его маршрут отличает расширение от ужесточения по
# значению, что функция применения вызывает confirm.must_not_widen, и допишите его сюда,
# в таблицу docs/service.md и в тесты «ждёт владельца / применяется сразу».
EXPECTED = {
    "outbox.policy", "outbox.account_drafting", "outbox.chat_drafting", "outbox.autoreply",
    "outbox.trusted_add", "watch.rule_create", "watch.rule_update", "watch.rule_delete",
    "archive.chat_exclude", "archive.chat_include", "archive.chat_purge", "archive.import_run",
    "tg.login", "tg.resume", "tg.options", "tg.sync", "tg.forget",
    "commitments.decide", "people.merge", "people.split", "people.alias",
    "pages.owner_block", "pages.accept",
    "projects.create", "projects.chats", "projects.archive", "projects.accept", "facts.retract",
}


async def test_every_confirmable_action_is_known(make_client):
    await make_client(*MODULES)
    registered = {kind for kind in confirm.kinds() if not kind.startswith("t.")}    # t.* — виды из других тестов
    assert registered == EXPECTED


async def test_status_of_an_action_is_its_description_and_outcome(make_client, conn, own_bot, approvals):
    from shturman import bridge

    client, _ = await make_client("shturman.api_core", "shturman.outbox.service")
    await bridge.set_owner(conn, 1000, 1000)
    action = approvals.waiting(await client.post("/api/outbox/trusted", json={"tg_user_id": 2001, "note": "тайна"}))
    one = (await client.get(f"/api/confirmations/{action}")).json()
    assert set(one) == {"id", "kind", "status", "summary", "error", "created_at", "expires_at", "decided_at"}
    assert one["kind"] == "outbox.trusted_add" and one["status"] == "pending"
    await approvals.lapse(action)
    assert (await client.get(f"/api/confirmations/{action}")).json()["status"] == "expired"   # срок вышел — уже не ждёт
