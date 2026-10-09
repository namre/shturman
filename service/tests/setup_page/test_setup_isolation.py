"""Префикс /shturman-setup/ отделён от внутреннего API и архива; чужой сайт и чужое имя не проходят."""

import pytest

from setup_fakes import API, ORIGIN, PREFIX, raw_request, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from conftest import API_AUTH, API_TOKEN, MCP_AUTH, MCP_TOKEN

from shturman.setup_page import auth, shield


async def test_page_and_its_files_are_served_without_login_and_carry_no_data(stand):
    s = await stand()
    index = await s.page.http.get(PREFIX + "/")
    assert index.status_code == 200 and index.headers["content-type"].startswith("text/html")
    assert "Настройка Telegram и архива" in index.text
    # ни встроенных скриптов, ни встроенных стилей, ни чужих адресов в разметке
    assert "<script>" not in index.text and " style=" not in index.text and "onclick=" not in index.text
    assert 'src="http' not in index.text and 'href="http' not in index.text.replace('href="#"', "")
    for name, kind in (("setup.css", "text/css"), ("setup.js", "text/javascript"), ("qr.js", "text/javascript")):
        got = await s.page.http.get(f"{PREFIX}/static/{name}")
        assert got.status_code == 200 and got.headers["content-type"].startswith(kind)
        assert "http://" not in got.text.replace("http://www.w3.org/2000/svg", "") or name != "qr.js"
    for name in ("index.html", "../service.py", "setup.js.map", "secrets.json"):
        assert (await s.page.http.get(f"{PREFIX}/static/{name}")).status_code == 404
    bare = await s.page.http.get(PREFIX)
    assert bare.status_code == 308 and bare.headers["location"] == PREFIX + "/"


async def test_every_response_under_the_prefix_has_strict_headers(stand, conn):
    s = await stand()
    await s.page.login(conn)
    responses = [
        await s.page.http.get(PREFIX + "/"), await s.page.http.get(PREFIX + "/static/setup.js"),
        await s.page.http.get(PREFIX + "/nothing-here"), await s.browser().get("/state"),
        await s.page.get("/state"), await s.page.http.get(PREFIX + "/", headers={"Host": "evil.example"}),
        await s.page.http.request("OPTIONS", PREFIX + "/"), await s.page.http.get(PREFIX),
    ]
    assert [r.status_code for r in responses] == [200, 200, 404, 401, 200, 421, 405, 308]
    for r in responses:
        csp = r.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "*" not in csp
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
        assert r.headers["cross-origin-opener-policy"] == "same-origin"


async def test_internal_api_and_archive_are_not_reachable_through_the_prefix(stand, conn):
    s = await stand()
    await s.page.login(conn)                       # даже вошедшему владельцу страница не открывает /api и /mcp
    for path in ("/api/status", "/api/chats", "/api/tg/accounts", "/api/owner", "/api/jobs/claim", "/mcp",
                 "/mcp/", "/api/executor/status", "/api/confirmations"):
        for headers in ({}, API_AUTH, MCP_AUTH):
            for method in ("GET", "POST"):
                got = await s.page.http.request(method, PREFIX + path, headers={**s.page.headers(), **headers})
                assert got.status_code in (404, 405), (method, path, got.status_code)
    # обходные записи пути не выводят из-под префикса
    for path in ("/../api/status", "/%2e%2e/api/status", "/..%2fapi/status", "//api/status", "/./api/state",
                 "/api/../../api/status", "/static/..%2f..%2fapi%2fstatus", "/%2E%2E/mcp"):
        status, body = await raw_request(s, (PREFIX + path).encode(), API_AUTH)
        assert status == 404, (path, status)
        assert b"messages" not in body
    # сам внутренний API на месте и по-прежнему требует своего токена
    assert (await s.page.http.get("/api/status")).status_code == 401
    assert (await s.page.http.get("/api/status", headers=API_AUTH)).status_code == 200


async def test_api_and_mcp_tokens_are_not_a_way_in(stand):
    s = await stand()
    for token in (API_TOKEN, MCP_TOKEN):
        for headers in ({"Authorization": f"Bearer {token}"}, {"Cookie": f"shturman_setup={token}"},
                        {"X-Shturman-Session": token, "Authorization": f"Bearer {token}"}):
            got = await s.page.http.get(API + "/state", headers=headers)
            assert got.status_code == 401 and got.json()["code"] == "unauthenticated"
            changed = await s.page.http.post(API + "/bot/bind", json={}, headers={**s.page.headers(), **headers})
            assert changed.status_code == 401
        as_link = await s.page.http.post(API + "/login/link", json={"token": token}, headers=s.page.headers())
        assert as_link.status_code == 401
        as_code = await s.page.http.post(API + "/login/code", json={"code": token}, headers=s.page.headers())
        assert as_code.status_code == 401


async def test_page_session_does_not_open_the_internal_api(stand, conn):
    s = await stand()
    await s.page.login(conn)
    key = s.page.key
    assert key
    for path in ("/api/status", "/mcp"):
        got = await s.page.http.get(path, headers={"X-Shturman-Session": key, "Cookie": f"shturman_setup={key}",
                                                   "Authorization": f"Bearer {key}"})
        assert got.status_code == 401


async def test_only_known_host_names_are_served(stand):
    s = await stand(setup_origin="https://assistant.example.com")
    assert (await s.page.http.get(PREFIX + "/")).status_code == 200                                # локальное имя
    assert (await s.page.http.get(PREFIX + "/", headers={"Host": "assistant.example.com"})).status_code == 200
    for host in ("evil.example", "assistant.example.com.evil.example", "assistant.example.com:8443", "", "127.0.0.1"):
        got = await s.page.http.get(PREFIX + "/", headers={"Host": host})
        assert got.status_code == 421 and got.json() == {"error": "misdirected"}
    # заголовки прокси имя не подменяют
    got = await s.page.http.get(PREFIX + "/", headers={"Host": "evil.example", "X-Forwarded-Host": "assistant.example.com",
                                                       "Forwarded": "host=assistant.example.com"})
    assert got.status_code == 421


async def test_an_ip_address_instead_of_a_name_is_served_only_on_its_own_port(stand, conn):
    """Экземпляр без домена: дашборд — https://203.0.113.10, страница — тот же IP на порту 8443.
    Сверка Host и Origin идёт с портом, как и для имени: с адреса дашборда страница не открывается."""
    s = await stand(setup_origin="https://203.0.113.10:8443", dashboard_origin="https://203.0.113.10")
    assert s.config.setup_external == "https://203.0.113.10:8443"
    page = s.browser("https://203.0.113.10:8443")           # Host: 203.0.113.10:8443, Origin — тот же адрес
    assert (await page.http.get(PREFIX + "/")).status_code == 200
    await page.login(conn)
    assert (await page.get("/state")).status_code == 200
    for host in ("203.0.113.10", "203.0.113.10:443", "203.0.113.10:9443", "198.51.100.7:8443", "203.0.113.010:8443"):
        got = await page.http.get(PREFIX + "/", headers={"Host": host})
        assert got.status_code == 421, host
    # страница, открытая на адресе дашборда, действовать на странице настройки не может
    forged = await page.http.post(API + "/logout-all", json={},
                                  headers=page.headers(Origin="https://203.0.113.10"))
    assert forged.status_code == 403 and forged.json()["code"] == "bad_origin"
    assert (await page.get("/state")).status_code == 200


def test_origin_table_is_built_from_settings_only():
    from types import SimpleNamespace

    cfg = SimpleNamespace(allowed_hosts=("127.0.0.1:8765", "LocalHost:8765"), setup_external="https://assistant.example.com")
    assert shield.origins(cfg) == {"127.0.0.1:8765": "http://127.0.0.1:8765", "localhost:8765": "http://localhost:8765",
                                   "assistant.example.com": "https://assistant.example.com"}


@pytest.mark.parametrize("headers, code", [
    ({"Origin": "https://evil.example"}, "bad_origin"),                              # чужой сайт
    ({"Origin": None}, "bad_origin"),                                                # заголовка нет вовсе
    ({"Origin": "http://test.evil.example"}, "bad_origin"),
    ({"Origin": "null"}, "bad_origin"),
    ({"Sec-Fetch-Site": "cross-site"}, "bad_origin"),
    ({"Sec-Fetch-Site": "same-site"}, "bad_origin"),                                 # соседний поддомен — тоже чужой
    ({"X-Shturman-Setup": None}, "bad_origin"),                                      # простая форма с чужого сайта
])
async def test_forged_requests_change_nothing_even_with_the_session_key(stand, conn, headers, code):
    s = await stand()
    await s.page.login(conn)
    sent = {k: v for k, v in {**s.page.headers(), **headers}.items() if v is not None}
    for method, path in (("POST", "/logout-all"), ("PUT", "/tg/keys"), ("DELETE", "/bot/token"), ("POST", "/imports")):
        got = await s.page.http.request(method, API + path, json={}, headers=sent)
        assert got.status_code == 403 and got.json()["code"] == code, (method, path, got.text)
    assert (await s.page.get("/state")).status_code == 200                 # сессия цела: ничего не выполнилось
    token, _ = await auth.create_link(conn)
    login = await s.browser().http.post(API + "/login/link", json={"token": token}, headers=sent)
    assert login.status_code == 403
    assert await conn.fetchval("SELECT used_at IS NULL FROM setup_links")   # ссылка не израсходована


async def test_cross_site_reads_are_refused_too(stand, conn):
    s = await stand()
    await s.page.login(conn)
    for site in ("cross-site", "same-site"):
        got = await s.page.http.get(API + "/state", headers={"Sec-Fetch-Site": site})
        assert got.status_code == 403
    assert "access-control-allow-origin" not in (await s.page.get("/state")).headers


async def test_oversized_and_malformed_bodies_are_refused(stand, conn):
    s = await stand()
    await s.page.login(conn)
    big = await s.page.http.post(API + "/bot/token", content=b'{"token": "' + b"x" * 70_000 + b'"}',
                                 headers={**s.page.headers(), "Content-Type": "application/json"})
    assert big.status_code == 413
    for raw in (b"[1, 2]", b"not json", b'"text"'):
        got = await s.page.http.post(API + "/bot/token", content=raw,
                                     headers={**s.page.headers(), "Content-Type": "application/json"})
        assert got.status_code == 400


async def test_without_the_module_the_prefix_does_not_exist(make_client):
    client, _ = await make_client("shturman.api_core")
    for path in (PREFIX, PREFIX + "/", API + "/session", PREFIX + "/static/setup.js"):
        assert (await client.get(path)).status_code == 404
    status = (await client.get("/api/status")).json()
    assert status["setup"]["enabled"] is False


async def test_status_reports_only_flags_about_the_setup_page(stand, conn):
    from setup_fakes import LLM_KEY, TOKEN, bind_owner, save_bot

    s = await stand(setup_origin="https://assistant.example.com")
    empty = (await s.api.get("/api/status")).json()["setup"]
    assert empty == {"enabled": True, "origin_set": True, "origin": "https://assistant.example.com", "reason": None,
                     "tg_keys": False, "accounts": 0, "own_bot": False,
                     "owner_bound": False, "business_connected": False, "own_model": False}
    await s.page.login(conn)
    await save_bot(s)
    await bind_owner(s)
    await s.page.put("/tg/keys", {"api_id": "1234567", "api_hash": "0123456789abcdef0123456789abcdef"})
    await s.page.put("/llm", {"api_key": LLM_KEY, "model": "gpt-test"})
    raw = await s.api.get("/api/status")
    assert raw.json()["setup"] == {"enabled": True, "origin_set": True, "origin": "https://assistant.example.com",
                                   "reason": None, "tg_keys": True, "accounts": 0, "own_bot": True,
                                   "owner_bound": True, "business_connected": False, "own_model": True}
    # ни значений, ни имён через внутренний API не видно
    for secret in (TOKEN, LLM_KEY, "0123456789abcdef0123456789abcdef", "Евгений", "shturman_soglasovaniya_bot"):
        assert secret not in raw.text
    # только признаки и числа; адрес страницы — не секрет; ключа сессии страницы здесь нет
    assert all(isinstance(v, (bool, int)) for k, v in raw.json()["setup"].items() if k not in ("origin", "reason"))
    assert s.page.key not in raw.text
    assert ORIGIN
