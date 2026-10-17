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
    # сферы (Р-82): любое изменение через внутренний API ждёт владельца, в том числе «убрать» и «удалить»
    "spheres.create", "spheres.update", "spheres.delete", "spheres.chats", "spheres.project",
    # уведомления владельцу (Р-84): расширение (больше сообщений, текст в уведомлении) ждёт владельца,
    # сужение и время — сразу
    "notify.settings",
    # репетиция ответов на основном аккаунте (Р-85): включить, добавить людей, поднять предел, сократить
    # паузу — ждёт владельца; выключить, убрать людей, снизить предел — сразу. Отправки у неё нет
    "rehearsal.settings",
    # «Продолжить» после «Остановить всё» (Р-87): со страницы и из приложения — карточка в бот, нажать
    # можно только в самом боте (BOT_ONLY), не в приложении; через внутренний API — никак
    "halt.resume",
    # вход на страницу по коду из бота при боте от посредника (Р-88): включить — только нажатием в боте
    "setup.code_login",
    # включение разбора входящих (Р-92): переписка начинает уходить модели — только нажатием в самом боте
    # (BOT_ONLY); страница лишь присылает карточку, через внутренний API — никак; выключение — сразу
    "triage.enable",
    # Р-95: «убрать из доверенных» (сужение — всегда сразу), группы и обращения (разрешить, добавить — ослабление;
    # раньше только командами бота), а также `owner.widen` — ослабление, о котором владелец попросил из приложения
    # или «Вернуть как было», если возврат расширяет: только нажатием в самом боте (BOT_ONLY)
    "outbox.trusted_remove", "outbox.group_scope", "outbox.addressing", "owner.widen",
    # Р-99: решение разом по скрытому защитой — ставит только нажатие владельца под сводкой (маршрута во
    # внутреннем API нет). «Показать все» расширяет видимое ассистенту — только в самом боте (BOT_ONLY);
    # «Оставить все скрытыми» — подтверждение и в приложении
    "guard.release_all", "guard.keep_all",
    # Р-100: разрешение владельца на действие сопровождения сервера (обновление с откатом базы, восстановление,
    # откат, перезапуск без копии, порог защиты, удаление копий) вместо фразы в терминале. Просит скрипт ops/
    # (`POST /api/ops/consents`), выдаёт — только «Разрешить» в самом боте (BOT_ONLY): функция применения
    # отказывает без нажатия под карточкой (`confirm.card_press`). Маршрута, который выдаёт разрешение, нет
    "ops.consent",
}


def test_ops_consent_is_granted_only_by_a_press_in_the_bot():
    assert "ops.consent" in confirm.BOT_ONLY
    assert "ops.consent" not in confirm.OWNER_ASKED       # просит сервер, а не сам владелец: своё предупреждение
    text = confirm._card("Обновление", "ops.consent")
    assert confirm.OPS_CARD in text and confirm.CARD_WARNING not in text
    labels = [b["text"] for row in confirm._card_buttons(1, "n", "ops.consent") for b in row]
    assert labels == ["Разрешить", "Отклонить"]


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
