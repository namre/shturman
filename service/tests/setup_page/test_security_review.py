"""Страница настройки против главного противника — ассистента в Hermes под внедрёнными инструкциями.

По модели угроз (docs/architecture.md, «Открытое ограничение») он может ставить расширения
дашборда, то есть исполнять свой JavaScript на origin дашборда Hermes в браузере владельца.
Независимая проверка показала: когда страница настройки отдавалась с того же origin, такой
скрипт делал от имени владельца всё. Здесь — проверки того, как это закрыто:

  * страница живёт на своём origin (другой порт того же имени или другое имя); при совпадении
    адресов снаружи не отдаётся вовсе; запрос с `Host` дашборда отвергается;
  * сессия не держится на cookie: ключ сессии лежит в `localStorage` origin страницы, которого
    скрипт дашборда не видит, и приходит заголовком; cookie не ставятся и не читаются;
  * запросы `same-site` (соседний порт) и `cross-site` отвергаются для всех маршрутов API,
    включая GET, даже с верным ключом; заголовков CORS нет;
  * сохранённый ключ модели не уходит на новый адрес; адрес модели со страницы — только https и
    только наружу, по результату разрешения имени в момент запроса.

Браузерная часть того же (настоящий `fetch` с соседнего порта, `localStorage`, service worker) —
в `tests/e2e/setup_page.mjs`.
"""

import dataclasses
import logging

import httpx
import pytest

from setup_fakes import (  # noqa: F401, I001 — stand — фикстура; импорт первым добавляет пути tg/executor
    API, API_HASH, DNS, LLM_KEY, PREFIX, PUBLIC_IP, bind_owner, save_bot, stand,
)
from conftest import API_TOKEN, DSN, MCP_TOKEN
from exec_fakes import until
from tg_fakes import G_FAMILY, ME, U_IVAN, U_MARIA, wait_for

from shturman import bridge, netguard
from shturman.config import Config, ConfigError, normalize_origin
from shturman.executor.llm import LlmClient, LlmError
from shturman.setup_page import apply, auth, shield
from shturman.setup_page import secrets_store as ss

DASHBOARD = "https://assistant.example.com"            # адрес дашборда Hermes
SETUP = "https://assistant.example.com:8443"           # страница настройки: то же имя, другой порт
SETUP_HOST = "assistant.example.com:8443"


async def external(stand, **changes):
    """Сервис, у которого страница настройки отдаётся с соседнего порта; владелец вошёл."""
    s = await stand(setup_origin=SETUP, dashboard_origin=DASHBOARD, **changes)
    s.page = s.browser(SETUP)
    await s.page.login(s.conn)
    return s


def dashboard_script(s, *, cookies: dict[str, str] | None = None) -> httpx.AsyncClient:
    """То, чем располагает скрипт на origin дашборда: запросы уходят к странице из браузера
    владельца, поэтому браузер сам ставит `Origin` дашборда и `Sec-Fetch-Site: same-site` и
    приставляет все cookie этого имени узла — в том числе те, что скрипт подложил сам.
    Ключа сессии у скрипта нет: `localStorage` другого origin ему не виден."""
    return httpx.AsyncClient(transport=s.api._transport, base_url=SETUP, cookies=cookies or {},
                             headers={"Origin": DASHBOARD, "Sec-Fetch-Site": "same-site"})


async def connect_owner_account(s) -> int:
    assert (await s.page.put("/tg/keys", {"api_id": "1234567", "api_hash": API_HASH})).status_code == 200
    started = await s.page.post("/tg/login", {"role": "owner", "confirm_owner": True})
    assert started.status_code == 200, started.text
    login_id = started.json()["login_id"]
    s.world.me = ME
    s.world.last.scan.set_result(ME)
    await wait_for(lambda: s.manager.flows[login_id].done)
    done = (await s.page.get(f"/tg/login/{login_id}")).json()
    assert done["status"] == "completed", done
    return done["account_id"]


# =================================================================================================
# Сессия без cookie: скрипту с origin дашборда не на чем «ехать»
# =================================================================================================

async def test_login_sets_no_cookie_and_the_session_key_is_issued_exactly_once(stand, conn, caplog):
    caplog.set_level(logging.DEBUG)
    s = await stand(setup_origin=SETUP, dashboard_origin=DASHBOARD)
    owner = s.browser(SETUP)
    response = await owner.login(conn)
    key = response.json()["key"]
    assert set(response.json()) == {"ok", "key", "expires_at"} and auth.well_formed(key)
    assert "set-cookie" not in response.headers and not owner.http.cookies
    # ключ не повторяется ни в одном другом ответе, не лежит в базе и не попадает в журнал
    seen = [await owner.get("/session"), await owner.get("/state"), await owner.get("/overview"),
            await owner.http.get(PREFIX + "/"), await s.api.get("/api/status")]
    for r in seen:
        assert r.status_code == 200 and key not in r.text and "set-cookie" not in r.headers, r.url
    assert (await owner.get("/session")).json() == {
        "authenticated": True, "code_login": False, "via": "link", "expires_at": response.json()["expires_at"]}
    dump = await conn.fetchval(
        """SELECT string_agg(t, ' ') FROM (SELECT x::text AS t FROM setup_sessions x
             UNION ALL SELECT a::text FROM setup_audit a UNION ALL SELECT l::text FROM setup_links l) q""")
    assert key not in dump and auth.token_hash(key) in dump
    assert key not in caplog.text


async def test_cookie_alone_gives_nothing_not_even_with_the_real_key_in_it(stand, conn):
    """Cookie браузер шлёт на любой порт имени, и соседний порт может подложить свою. Поэтому
    cookie не значит ничего: ни чтения, ни действия, ни получения ключа."""
    s = await external(stand)
    key = s.page.key
    for cookies in ({"shturman_setup": key}, {"__Host-shturman_setup": key}, {"X-Shturman-Session": key},
                    {"shturman-setup-session": key}):
        guest = httpx.AsyncClient(transport=s.api._transport, base_url=SETUP, cookies=cookies)
        try:
            same_origin = {"Origin": SETUP, "Sec-Fetch-Site": "same-origin", "X-Shturman-Setup": "1"}
            assert (await guest.get(API + "/session", headers=same_origin)).json() == {
                "authenticated": False, "code_login": False}
            for path in ("/state", "/overview", "/imports"):
                got = await guest.get(API + path, headers=same_origin)
                assert got.status_code == 401 and key not in got.text
            for method, path in (("POST", "/bot/bind"), ("PUT", "/tg/keys"), ("POST", "/logout-all"), ("DELETE", "/llm")):
                got = await guest.request(method, API + path, json={}, headers=same_origin)
                assert got.status_code == 401 and got.json()["code"] == "unauthenticated", (method, path)
        finally:
            await guest.aclose()
    assert (await s.page.get("/state")).status_code == 200          # сессия владельца цела


async def test_session_lives_seven_days_and_ends_on_logout_and_logout_everywhere(stand, conn):
    s = await external(stand)
    ttl = await conn.fetchval("SELECT extract(epoch FROM expires_at - now()) FROM setup_sessions")
    assert 7 * 86400 - 60 < ttl <= 7 * 86400
    # перерыв в обращениях сессию не завершает: вернувшись через шесть дней, владелец всё ещё вошёл
    await conn.execute("UPDATE setup_sessions SET last_seen_at = now() - interval '6 days'")
    assert (await s.page.get("/state")).status_code == 200
    await conn.execute("UPDATE setup_sessions SET expires_at = now() - interval '1 second'")
    assert (await s.page.get("/state")).status_code == 401

    a, b = s.browser(SETUP), s.browser(SETUP)
    await a.login(conn)
    b.key, _ = await auth.create_session(conn, "code")
    assert (await b.get("/state")).status_code == 200
    assert (await a.post("/logout")).json() == {"ok": True}
    assert (await a.get("/state")).status_code == 401 and (await b.get("/state")).status_code == 200
    await a.login(conn)                     # вход по новой ссылке завершает прежние сессии
    assert (await b.get("/state")).status_code == 401
    c = s.browser(SETUP)
    c.key, _ = await auth.create_session(conn, "code")
    assert (await a.post("/logout-all")).json() == {"ok": True, "sessions": 2}
    assert (await a.get("/state")).status_code == 401 and (await c.get("/state")).status_code == 401


# =================================================================================================
# Скрипт с origin дашборда ничего не читает и ничего не меняет
# =================================================================================================

async def test_dashboard_script_cannot_issue_the_owner_bind_link(stand, conn):
    """Было: скрипт выпускал ссылку привязки владельца (кто её откроет, станет владельцем)."""
    s = await external(stand)
    await save_bot(s)
    await bind_owner(s)
    before = await conn.fetchval("SELECT count(*) FROM setup_audit WHERE action = 'bot.bind_link'")
    script = dashboard_script(s, cookies={"shturman_setup": "0" * 43})
    for extra in ({}, {"X-Shturman-Setup": "1"}, {"X-Shturman-Setup": "1", "X-Shturman-Session": "0" * 43}):
        bind = await script.post(API + "/bot/bind", json={}, headers=extra)
        assert bind.status_code == 403 and bind.json()["code"] == "bad_origin" and "link" not in bind.json()
    assert await conn.fetchval("SELECT count(*) FROM setup_audit WHERE action = 'bot.bind_link'") == before
    await script.aclose()


async def test_dashboard_script_cannot_read_chat_names_or_turn_reading_on(stand, conn):
    """Было: скрипт читал названия чатов владельца и включал чтение всех личных чатов."""
    s = await stand(setup_origin=SETUP, dashboard_origin=DASHBOARD)
    s.world.dialogs = [U_IVAN, U_MARIA, G_FAMILY]
    s.world.authorized = False
    s.page = s.browser(SETUP)
    await s.page.login(conn)
    account_id = await connect_owner_account(s)
    assert (await s.page.get(f"/tg/accounts/{account_id}/dialogs")).json()["total"] == 3   # владелец список видит

    script = dashboard_script(s)
    dialogs = await script.get(API + f"/tg/accounts/{account_id}/dialogs?limit=5")
    assert dialogs.status_code == 403 and "items" not in dialogs.json() and "Иван" not in dialogs.text
    sync = await script.post(API + f"/tg/accounts/{account_id}/sync", json={"enabled": True, "kind": "personal"},
                             headers={"X-Shturman-Setup": "1"})
    assert sync.status_code == 403
    state = await script.get(API + "/state")
    assert state.status_code == 403 and "accounts" not in state.text
    assert await conn.fetchval("SELECT count(*) FROM tg_sync_chats WHERE enabled") == 0
    await script.aclose()


@pytest.mark.parametrize("site, origin", [
    ("same-site", DASHBOARD),                 # дашборд на соседнем порту
    ("same-site", SETUP),
    ("cross-site", "https://evil.example"),   # чужой сайт
    ("cross-site", SETUP),
    ("none", None),                           # адрес API, набранный в адресной строке
])
async def test_only_same_origin_requests_reach_the_page_api_even_with_the_right_key(stand, conn, site, origin):
    """Вторая линия за ключом сессии: даже если ключ утёк, запрос с соседнего порта или с чужого
    сайта не проходит — ни чтение (GET), ни действие."""
    s = await external(stand)
    headers = {"Sec-Fetch-Site": site, "X-Shturman-Setup": "1", "X-Shturman-Session": s.page.key}
    if origin:
        headers["Origin"] = origin
    client = httpx.AsyncClient(transport=s.api._transport, base_url=SETUP)
    try:
        for path in ("/session", "/state", "/overview", "/imports", "/tg/accounts/1/dialogs", "/tg/login/x", "/imports/x/scan"):
            got = await client.get(API + path, headers=headers)
            assert got.status_code == 403 and got.json()["code"] == "bad_origin", path
            assert not [h for h in got.headers if h.startswith("access-control-")]
        for method, path in (("POST", "/logout-all"), ("PUT", "/tg/keys"), ("POST", "/bot/token"), ("POST", "/bot/bind"),
                             ("DELETE", "/bot/token"), ("PUT", "/llm"), ("POST", "/imports"), ("POST", "/login/code/request"),
                             ("POST", "/tg/login")):
            got = await client.request(method, API + path, json={}, headers=headers)
            assert got.status_code == 403 and got.json()["code"] == "bad_origin", (method, path)
        token, _ = await auth.create_link(conn)
        login = await client.post(API + "/login/link", json={"token": token}, headers=headers)
        assert login.status_code == 403 and "key" not in login.json()
        assert await conn.fetchval("SELECT used_at IS NULL FROM setup_links WHERE token_hash = $1", auth.token_hash(token))
    finally:
        await client.aclose()
    assert (await s.page.get("/state")).status_code == 200          # сессия цела: «выйти везде» не выполнилось


async def test_page_gives_no_cors_headers_and_refuses_preflight_and_service_workers(stand, conn):
    s = await external(stand)
    client = httpx.AsyncClient(transport=s.api._transport, base_url=SETUP)
    try:
        preflight = await client.request("OPTIONS", API + "/state", headers={
            "Origin": DASHBOARD, "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-shturman-session", "Sec-Fetch-Site": "same-site"})
        assert preflight.status_code == 405
        # файл service worker'а браузер запрашивает с этим заголовком: под префиксом его не получить
        for path in (PREFIX + "/static/setup.js", PREFIX + "/", API + "/state"):
            worker = await client.get(path, headers={"Service-Worker": "script", "Sec-Fetch-Site": "same-origin",
                                                     "X-Shturman-Session": s.page.key})
            assert worker.status_code == 404, path
        # скрипты и стили страницы соседнему порту и чужому сайту не отдаются
        for site in ("same-site", "cross-site"):
            got = await client.get(PREFIX + "/static/setup.js", headers={"Sec-Fetch-Site": site})
            assert got.status_code == 403 and "ShturmanQR" not in got.text
        page = await client.get(PREFIX + "/", headers={"Sec-Fetch-Site": "same-site"})    # переход по ссылке из дашборда
        assert page.status_code == 200
        ok = await s.page.get("/state")
        for r in (preflight, page, ok, worker, got):
            names = {h.lower() for h in r.headers}
            assert not [h for h in names if h.startswith("access-control-")] and "service-worker-allowed" not in names
            assert "set-cookie" not in names
            assert r.headers["cross-origin-resource-policy"] == "same-origin"
            assert r.headers["cross-origin-opener-policy"] == "same-origin"
            assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
            assert r.headers["origin-agent-cluster"] == "?1"
    finally:
        await client.aclose()


async def test_page_body_is_the_same_for_everyone_and_carries_the_marker(stand, conn):
    s = await external(stand)
    guest = await s.browser(SETUP).http.get(PREFIX + "/")
    owner = await s.page.http.get(PREFIX + "/", headers={"X-Shturman-Session": s.page.key})
    local = await s.api.get(PREFIX + "/")                             # локальное имя сервиса
    assert guest.status_code == owner.status_code == local.status_code == 200
    assert guest.content == owner.content == local.content
    assert guest.headers["x-shturman-setup-page"] == "1"
    assert "x-shturman-setup-page" not in (await s.page.get("/state")).headers


# =================================================================================================
# Свой origin: при совпадении адресов страница снаружи не отдаётся; Host дашборда отвергается
# =================================================================================================

@pytest.mark.parametrize("setup, dashboard, reason", [
    ("", "https://assistant.example.com", "no_origin"),
    ("https://assistant.example.com:8443", "https://assistant.example.com", None),       # другой порт
    ("https://setup.example.com", "https://assistant.example.com", None),                # другое имя
    ("https://assistant.example.com:8443", "", None),                                    # дашборда нет (режим без Hermes)
    ("http://assistant.example.com", "https://assistant.example.com", None),             # другая схема — другой origin
    ("https://assistant.example.com", "https://assistant.example.com", "same_origin"),
    ("https://assistant.example.com", "https://ASSISTANT.example.com:443/", "same_origin"),     # регистр, порт по умолчанию
    ("https://assistant.example.com:443", "https://assistant.example.com./dashboard?x=1", "same_origin"),   # точка, путь
    ("https://пример.example:8443", "https://xn--e1afmkfd.example:8443", "same_origin"),         # punycode
    ("http://assistant.example.com:80", "http://assistant.example.com", "same_origin"),
    ("https://[2001:db8::1]:8443", "https://[2001:DB8:0::1]:8443/", None),   # разная запись IPv6: сравнение строгое
])
def test_setup_origin_is_compared_with_the_dashboard_origin_after_normalising(tmp_path, setup, dashboard, reason):
    cfg = Config(dsn=DSN, api_token=API_TOKEN, mcp_token=MCP_TOKEN, data_dir=tmp_path,
                 setup_origin=normalize_origin(setup, "SHTURMAN_SETUP_ORIGIN"),
                 dashboard_origin=normalize_origin(dashboard, "SHTURMAN_DASHBOARD_ORIGIN", strict=False))
    assert cfg.setup_reason == reason
    table = shield.origins(cfg)
    if reason is None:
        assert cfg.setup_external and table[cfg.setup_external.split("://")[1]] == cfg.setup_external
    else:
        assert cfg.setup_external == "" and set(table) == {"127.0.0.1:8765", "localhost:8765"}


def test_origins_come_from_the_environment_and_garbage_stops_the_service(monkeypatch, tmp_path):
    for name in ("SHTURMAN_SETUP_ORIGIN", "SHTURMAN_DASHBOARD_ORIGIN", "SHTURMAN_ALLOWED_HOSTS", "SHTURMAN_PORT",
                 "SHTURMAN_BOT_TOKEN", "SHTURMAN_LLM_API_KEY", "SHTURMAN_LLM_BASE_URL", "SHTURMAN_LLM_MODEL",
                 "TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SHTURMAN_DSN", DSN)
    monkeypatch.setenv("SHTURMAN_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("SHTURMAN_MCP_TOKEN", MCP_TOKEN)
    monkeypatch.setenv("SHTURMAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHTURMAN_SETUP_ORIGIN", "https://Assistant.example.com:8443/")
    monkeypatch.setenv("SHTURMAN_DASHBOARD_ORIGIN", "https://assistant.example.com/")
    cfg = Config.from_env()
    assert (cfg.setup_origin, cfg.dashboard_origin, cfg.setup_reason) == (SETUP, DASHBOARD, None)
    monkeypatch.setenv("SHTURMAN_DASHBOARD_ORIGIN", "https://assistant.example.com:8443")
    assert Config.from_env().setup_reason == "same_origin"          # сервис запускается: архив должен работать
    for bad in ("assistant.example.com", "ftp://assistant.example.com", "https://u:p@assistant.example.com", "https://"):
        monkeypatch.setenv("SHTURMAN_DASHBOARD_ORIGIN", bad)
        with pytest.raises(ConfigError):
            Config.from_env()


async def test_when_addresses_coincide_the_page_is_not_served_outside_at_all(stand, conn, caplog):
    caplog.set_level(logging.INFO)
    s = await stand(setup_origin=DASHBOARD, dashboard_origin=DASHBOARD)
    assert "совпадает с адресом дашборда" in caplog.text and "НЕ отдаётся" in caplog.text
    outside = httpx.AsyncClient(transport=s.api._transport, base_url=DASHBOARD)
    try:
        token, _ = await auth.create_link(conn)
        for method, path, body in (("GET", PREFIX + "/", None), ("GET", PREFIX, None), ("GET", PREFIX + "/static/setup.js", None),
                                   ("GET", API + "/session", None), ("POST", API + "/login/link", {"token": token})):
            got = await outside.request(method, path, json=body, headers={
                "Origin": DASHBOARD, "Sec-Fetch-Site": "same-origin", "X-Shturman-Setup": "1"})
            assert got.status_code == 421 and got.json() == {"error": "misdirected"}, path
            assert "x-shturman-setup-page" not in got.headers
        assert await conn.fetchval("SELECT used_at IS NULL FROM setup_links")       # ссылка не израсходована
    finally:
        await outside.aclose()
    status = (await s.api.get("/api/status")).json()["setup"]
    assert (status["enabled"], status["reason"], status["origin"], status["origin_set"]) == (False, "same_origin", None, False)
    # архив и внутренний API работают, а под локальным именем работает и страница
    assert (await s.api.get("/api/status")).status_code == 200
    assert (await s.page.http.get(PREFIX + "/")).status_code == 200
    await s.page.login(conn)
    assert (await s.page.get("/state")).json()["origin_set"] is False


@pytest.mark.parametrize("setup, dashboard, own_host, foreign_hosts", [
    (SETUP, DASHBOARD, SETUP_HOST,
     ["assistant.example.com", "assistant.example.com:443", "assistant.example.com:9443", "assistant.example.com.:8443",
      "evil.example:8443", "assistant.example.com:8443.evil.example"]),
    ("https://setup.example.com", DASHBOARD, "setup.example.com",
     ["assistant.example.com", "setup.example.com:8443", "setup.example.com.", "assistant.example.com:8443"]),
])
async def test_request_with_the_dashboard_host_is_refused_even_if_a_proxy_let_it_through(
        stand, conn, setup, dashboard, own_host, foreign_hosts):
    s = await stand(setup_origin=setup, dashboard_origin=dashboard)
    own = await s.page.http.get(PREFIX + "/", headers={"Host": own_host})
    assert own.status_code == 200 and own.headers["x-shturman-setup-page"] == "1"
    token, _ = await auth.create_link(conn)
    for host in foreign_hosts:
        for method, path, body in (("GET", PREFIX + "/", None), ("GET", API + "/session", None),
                                   ("POST", API + "/login/link", {"token": token})):
            got = await s.page.http.request(method, path, json=body, headers={
                "Host": host, "Origin": f"https://{host}", "X-Shturman-Setup": "1",
                "X-Forwarded-Host": own_host, "Forwarded": f"host={own_host}"})
            assert got.status_code == 421 and got.json() == {"error": "misdirected"}, (host, path)
            assert "x-shturman-setup-page" not in got.headers
    assert await conn.fetchval("SELECT used_at IS NULL FROM setup_links")
    status = (await s.api.get("/api/status")).json()["setup"]
    assert (status["enabled"], status["reason"], status["origin"], status["origin_set"]) == (True, None, setup, True)


async def test_status_tells_the_wizard_where_the_page_is_and_why_not(stand):
    s = await stand()                                    # внешний адрес не задан
    status = (await s.api.get("/api/status")).json()["setup"]
    assert (status["enabled"], status["reason"], status["origin"], status["origin_set"]) == (True, "no_origin", None, False)
    assert set(status) == {"enabled", "origin_set", "origin", "reason", "tg_keys", "accounts", "own_bot", "owner_bound",
                           "business_connected", "own_model"}


# =================================================================================================
# Ключ своей модели не уходит на новый адрес
# =================================================================================================

async def test_stored_model_key_is_never_sent_to_a_new_address(stand, conn):
    s = await external(stand)
    saved = await s.page.put("/llm", {"api_key": LLM_KEY, "model": "gpt-test", "base_url": "https://llm.example/v1"})
    assert saved.status_code == 200, saved.text
    before = len(s.llm.requests)
    for url in ("https://attacker.example/v1", "https://llm.example/v2", "https://llm.example:8443/v1", "",
                "https://LLM.example.evil.example/v1"):
        swap = await s.page.put("/llm", {"model": "gpt-test", "base_url": url})
        assert swap.status_code == 422 and swap.json()["code"] == "key_required", (url, swap.text)
        assert "заново" in swap.json()["error"]
    assert len(s.llm.requests) == before                                 # ни одного запроса никуда не ушло
    assert s.state.config.llm_base_url == "https://llm.example/v1" and s.state.config.llm_api_key == LLM_KEY
    assert ("llm.save", "refused", "смена адреса без ввода ключа") in [
        (r["action"], r["outcome"], r["detail"]) for r in await conn.fetch("SELECT * FROM setup_audit")]
    # тот же адрес (в любой записи) — не смена: модель меняется без ключа
    same = await s.page.put("/llm", {"model": "gpt-other", "base_url": "HTTPS://LLM.Example/v1/"})
    assert same.status_code == 200 and s.state.config.llm_model == "gpt-other"
    assert s.llm.headers[-1]["authorization"] == f"Bearer {LLM_KEY}" and s.llm.headers[-1]["host"] == "llm.example"
    # новый адрес вместе с ключом, введённым заново, — можно
    moved = await s.page.put("/llm", {"api_key": "sk-NEW-key-for-the-new-address-000", "model": "gpt-test",
                                      "base_url": "https://other.example/v1"})
    assert moved.status_code == 200 and s.state.config.llm_base_url == "https://other.example/v1"
    assert s.llm.headers[-1]["authorization"] == "Bearer sk-NEW-key-for-the-new-address-000"
    assert not any(h.get("authorization") == f"Bearer {LLM_KEY}" and h.get("host") != "llm.example" for h in s.llm.headers)


# =================================================================================================
# Адрес модели со страницы: только https и только наружу
# =================================================================================================

INNER = [
    "http://169.254.169.254/latest/v1", "https://169.254.169.254/v1",          # метаданные облака
    "http://127.0.0.1:9119/v1", "https://127.0.0.1:9119/v1", "https://127.0.0.1:5432/v1",   # дашборд, база
    "http://localhost/v1", "https://localhost/v1", "https://LOCALHOST./v1", "https://sub.localhost/v1",
    "https://10.0.0.5/v1", "https://172.16.0.1/v1", "https://192.168.1.10/v1",   # частные сети
    "https://100.64.0.1/v1", "https://100.100.100.200/v1",                       # CGNAT, метаданные
    "https://224.0.0.1/v1", "https://0.0.0.0/v1", "https://255.255.255.255/v1",
    "https://[::1]/v1", "https://[fe80::1]/v1", "https://[fc00::1]/v1", "https://[fd00:ec2::254]/v1", "https://[ff02::1]/v1",
    "https://[::ffff:127.0.0.1]/v1", "https://[::ffff:169.254.169.254]/v1", "https://[::ffff:a00:1]/v1",   # IPv4-mapped
    "https://[::127.0.0.1]/v1", "https://[64:ff9b::7f00:1]/v1", "https://[2002:7f00:1::]/v1",              # вложенный IPv4
    "https://2130706433/v1", "https://0x7f.0.0.1/v1", "https://0177.0.0.1/v1", "https://127.1/v1",         # иные записи
    "https://hermes:9119/v1", "https://postgres/v1", "https://db.internal/v1", "https://nas.local/v1",     # соседи
    "https://metadata.google.internal/v1",
]


@pytest.mark.parametrize("url", INNER)
def test_inner_addresses_are_refused_by_how_they_are_written(url):
    with pytest.raises(apply.Invalid) as refused:
        apply.check_llm_url(url)
    assert refused.value.code in ("blocked_base_url", "bad_base_url")
    assert "169.254" not in refused.value.message and "127.0.0.1" not in refused.value.message


@pytest.mark.parametrize("url", ["ftp://llm.example/v1", "https://u:p@llm.example/v1", "https://@llm.example/v1",
                                 "https://llm.example/v1?x=1", "https://llm.example/v1#x", "https://llm.example:0/v1",
                                 "https://llm.example:99999/v1", "//llm.example/v1", "llm.example/v1", "https:///v1",
                                 "https://llm.example\\@127.0.0.1/v1", "file:///etc/passwd", "gopher://llm.example"])
def test_malformed_model_addresses_are_refused(url):
    with pytest.raises(apply.Invalid):
        apply.check_llm_url(url)


def test_public_https_addresses_pass_and_come_back_in_one_form():
    assert apply.check_llm_url("") == ss.DEFAULT_LLM_BASE_URL == apply.check_llm_url(None)
    assert apply.check_llm_url(" HTTPS://OpenRouter.ai/api/v1/ ") == "https://openrouter.ai/api/v1"
    assert apply.check_llm_url("https://llm.example:8443/V1") == "https://llm.example:8443/V1"
    assert apply.check_llm_url("https://8.8.8.8/v1") == "https://8.8.8.8/v1"
    assert apply.check_llm_url("https://[2606:4700::1111]/v1") == "https://[2606:4700::1111]/v1"


@pytest.mark.parametrize("ip, closed", [
    ("127.0.0.1", True), ("10.1.2.3", True), ("172.31.255.255", True), ("192.168.0.1", True), ("169.254.169.254", True),
    ("100.64.0.1", True), ("100.127.255.254", True), ("224.0.0.251", True), ("0.0.0.0", True), ("192.0.0.192", True),
    ("198.18.0.1", True), ("240.0.0.1", True), ("::1", True), ("::", True), ("fe80::1%eth0", True), ("fc00::1", True),
    ("fd00:ec2::254", True), ("ff02::1", True), ("::ffff:10.0.0.1", True), ("::ffff:7f00:1", True), ("::10.0.0.1", True),
    ("64:ff9b::a9fe:a9fe", True), ("2002:c0a8:101::", True), ("2001:0:4136:e378:8000:63bf:3fff:fdd2", True),
    ("не адрес", True), ("", True),
    ("8.8.8.8", False), ("93.184.216.34", False), ("2606:4700::1111", False), ("::ffff:8.8.8.8", False),
    ("64:ff9b::808:808", False),
])
def test_closed_networks(ip, closed):
    assert netguard.blocked_ip(ip) is closed


async def test_page_refuses_inner_addresses_before_any_request_is_made(stand, conn):
    s = await external(stand)
    for url in INNER:
        got = await s.page.put("/llm", {"api_key": LLM_KEY, "model": "gpt-test", "base_url": url})
        assert got.status_code == 422 and got.json()["code"] in ("blocked_base_url", "bad_base_url"), (url, got.text)
        assert LLM_KEY not in got.text
    assert s.llm.requests == [] and s.state.config.own_llm is False
    assert not ss.SecretStore(s.config.data_dir).path.exists()


@pytest.mark.parametrize("answers", [
    ["127.0.0.1"], ["169.254.169.254"], ["10.0.0.7"], ["::1"], ["::ffff:127.0.0.1"], ["fd00::7"],
    [PUBLIC_IP, "127.0.0.1"],            # два адреса: проверку прошёл бы один, соединение пошло бы на другой
    ["127.0.0.1", PUBLIC_IP], [],
])
async def test_name_that_resolves_inside_is_refused_at_request_time(stand, conn, answers):
    """Подменённый резолвер: имя выглядит обычным, а ведёт внутрь сервера."""
    s = await external(stand)
    DNS["llm.example"] = answers
    got = await s.page.put("/llm", {"api_key": LLM_KEY, "model": "gpt-test", "base_url": "https://llm.example/v1"})
    assert got.status_code == 422 and got.json()["code"] == ("blocked_address" if answers else "dns_failed"), got.text
    assert s.llm.requests == [] and s.state.config.own_llm is False       # запрос с ключом никуда не ушёл
    assert LLM_KEY not in got.text


async def test_dns_rebinding_after_saving_does_not_reach_inner_addresses(stand, conn):
    """Адрес сохранён честным, а потом запись DNS сменили на внутренний адрес: в обычной работе
    клиент модели проверяет адрес при каждом запросе и внутрь не идёт."""
    s = await external(stand)
    saved = await s.page.put("/llm", {"api_key": LLM_KEY, "model": "gpt-test", "base_url": "https://llm.example/v1"})
    assert saved.status_code == 200 and s.state.config.llm_url_from_page is True
    job = await bridge.request_text(conn, handler="x", messages=[{"role": "user", "content": "привет"}])
    await until(lambda: conn.fetchval("SELECT status = 'done' FROM jobs WHERE id = $1", job))
    sent = len(s.llm.requests)

    DNS["llm.example"] = ["169.254.169.254"]
    job = await bridge.request_text(conn, handler="x", messages=[{"role": "user", "content": "ещё раз"}])
    await until(lambda: conn.fetchval("SELECT status <> 'queued' AND status <> 'running' FROM jobs WHERE id = $1", job))
    row = await conn.fetchrow("SELECT status, error FROM jobs WHERE id = $1", job)
    assert row["status"] == "failed" and "blocked_address" in (row["error"] or "")
    assert len(s.llm.requests) == sent
    state = (await s.page.get("/state")).json()["llm"]
    assert "внутрь сервера" in state["problem_text"]


async def test_redirects_are_not_followed(stand, conn):
    s = await external(stand)
    s.llm.script = [httpx.Response(307, headers={"Location": "http://169.254.169.254/latest/meta-data/"})] * 3
    got = await s.page.put("/llm", {"api_key": LLM_KEY, "model": "gpt-test", "base_url": "https://llm.example/v1"})
    assert got.status_code == 422 and got.json()["code"] == "http_307" and "перенаправлен" in got.json()["error"]
    assert len(s.llm.requests) == 1 and s.state.config.own_llm is False


async def test_address_from_the_server_settings_is_not_filtered_and_cannot_be_changed_on_the_page(stand, conn):
    """Локальную модель задаёт оператор в окружении сервиса: фильтр её не касается, а страница
    этот адрес не меняет."""
    s = await stand(setup_origin=SETUP, dashboard_origin=DASHBOARD, llm_base_url="http://ollama:11434/v1",
                    locked=frozenset({ss.LLM_BASE_URL}))
    s.page = s.browser(SETUP)
    await s.page.login(conn)
    DNS["ollama"] = ["172.18.0.5"]
    saved = await s.page.put("/llm", {"api_key": LLM_KEY, "model": "llama3", "base_url": "https://attacker.example/v1"})
    assert saved.status_code == 200, saved.text
    assert s.llm.headers[-1]["host"] == "ollama:11434"                 # запрос ушёл по адресу оператора
    assert s.state.config.llm_base_url == "http://ollama:11434/v1" and s.state.config.llm_url_from_page is False
    state = (await s.page.get("/state")).json()["llm"]
    assert state["base_url"] == {"set": True, "source": "server", "editable": False, "value": "http://ollama:11434/v1"}
    assert ss.LLM_BASE_URL not in ss.SecretStore(s.config.data_dir).load()


async def test_pinned_transport_connects_to_the_checked_address_and_keeps_the_name_for_tls(monkeypatch):
    seen: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "да"}}]})

    calls: list[tuple[str, int]] = []

    async def resolve(host: str, port: int) -> list[str]:
        calls.append((host, port))
        return {"llm.example": [PUBLIC_IP], "v6.example": ["2606:4700::1111"], "inner.example": ["10.0.0.1"]}[host]

    monkeypatch.setattr(netguard, "resolver", resolve)
    async with httpx.AsyncClient(transport=netguard.PinnedTransport(httpx.MockTransport(handle))) as client:
        await client.post("https://llm.example:8443/v1/chat/completions", json={})
        assert str(seen[-1].url) == f"https://{PUBLIC_IP}:8443/v1/chat/completions"
        assert seen[-1].headers["host"] == "llm.example:8443" and seen[-1].extensions["sni_hostname"] == "llm.example"
        await client.get("https://v6.example/v1")
        assert seen[-1].url.host == "2606:4700::1111" and seen[-1].extensions["sni_hostname"] == "v6.example"
        assert calls == [("llm.example", 8443), ("v6.example", 443)]       # имя разрешается при каждом запросе
        await client.get("https://8.8.8.8/v1")                             # адрес цифрами — без разрешения имени
        assert seen[-1].url.host == "8.8.8.8" and "sni_hostname" not in seen[-1].extensions
        for url, code in (("https://inner.example/v1", "blocked_address"), ("http://llm.example/v1", "not_https"),
                          ("https://127.0.0.1/v1", "blocked_address"), ("https://localhost/v1", "blocked_address")):
            with pytest.raises(netguard.Blocked) as blocked:
                await client.get(url)
            assert blocked.value.code == code
        assert len(seen) == 3
    # за прокси HTTP адрес проверяется так же, но в запросе остаётся имя (его разрешает прокси)
    by_name = netguard.PinnedTransport(httpx.MockTransport(handle), connect_by_name=True)
    async with httpx.AsyncClient(transport=by_name) as client:
        await client.get("https://llm.example/v1")
        assert seen[-1].url.host == "llm.example"
        with pytest.raises(netguard.Blocked):
            await client.get("https://inner.example/v1")
    assert netguard.proxy_resolves_names("http://proxy.example:3128") and not netguard.proxy_resolves_names("socks5://p:1080")
    assert not netguard.proxy_resolves_names("")


async def test_model_client_filters_only_addresses_that_came_from_the_page(monkeypatch):
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "да"}}]})

    async def resolve(host: str, port: int) -> list[str]:
        return ["192.168.1.50"]

    monkeypatch.setattr(netguard, "resolver", resolve)
    from_page = LlmClient(base_url="https://llm.example/v1", api_key="k", model="m", restricted=True,
                          transport=httpx.MockTransport(handle))
    with pytest.raises(LlmError) as failed:
        await from_page.chat([{"role": "user", "content": "?"}], max_tokens=4)
    assert failed.value.code == "blocked_address" and failed.value.final is True
    await from_page.aclose()
    from_env = LlmClient(base_url="http://ollama:11434/v1", api_key="k", model="m", transport=httpx.MockTransport(handle))
    assert (await from_env.chat([{"role": "user", "content": "?"}], max_tokens=4))[0] == "да"
    await from_env.aclose()


def test_page_value_marks_the_model_address_as_restricted(tmp_path):
    from shturman.config import with_page_values

    base = Config(dsn=DSN, api_token=API_TOKEN, mcp_token=MCP_TOKEN, data_dir=tmp_path)
    assert with_page_values(base, {}).llm_url_from_page is False                        # адрес по умолчанию
    ss.SecretStore(tmp_path).update({ss.LLM_API_KEY: LLM_KEY, ss.LLM_MODEL: "m", ss.LLM_BASE_URL: "https://llm.example/v1"})
    assert with_page_values(base, {}).llm_url_from_page is True
    env = {"SHTURMAN_LLM_BASE_URL": "http://ollama:11434/v1"}
    cfg = with_page_values(dataclasses.replace(base, llm_base_url="http://ollama:11434/v1"), env)
    assert cfg.llm_url_from_page is False and cfg.llm_base_url == "http://ollama:11434/v1"   # окружение главнее
