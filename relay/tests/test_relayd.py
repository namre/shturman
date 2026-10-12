"""Тесты службы посредника (relay/relayd.py). Только стандартная библиотека и pytest:
    pytest -q relay/tests
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import relayd  # noqa: E402

TOKEN = "frps-shared-token"


@pytest.fixture()
def env(tmp_path):
    ca = tmp_path / "frps.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    s = relayd.Settings(db=str(tmp_path / "relay.db"), domain="sh.example.test", port=7000,
                        ca_file=str(ca), frp_token=TOKEN, join_per_ip=3, join_total=100, join_window=900)
    conn = relayd.open_db(s.db)
    return s, conn


def invite(conn, days=7, now=None):
    now = int(time.time()) if now is None else now
    code = relayd.new_code()
    conn.execute("INSERT INTO invites(id, code_hash, created_at, expires_at) VALUES(?, ?, ?, ?)",
                 (relayd.secrets.token_hex(4), relayd.code_hash(relayd.normalize_code(code)), now, now + days * 86400))
    return code


# --- имена и коды ---

@pytest.mark.parametrize("name", ["ivan", "ivan-petrov", "a1b", "abc" * 10])
def test_good_names(name):
    assert relayd.name_problem(name) is None


@pytest.mark.parametrize("name,why", [
    ("ab", "bad_name"), ("a" * 31, "bad_name"), ("Ivan", "bad_name"), ("-ivan", "bad_name"), ("ivan-", "bad_name"),
    ("iv_an", "bad_name"), ("iv.an", "bad_name"), ("иван", "bad_name"), ("setup-ivan", "bad_name"),
    ("xn--80a", "bad_name"), ("", "bad_name"), (None, "bad_name"), (42, "bad_name"),
    ("relay", "name_reserved"), ("www", "name_reserved"), ("api", "name_reserved"), ("admin", "name_reserved"),
    ("mail", "name_reserved"), ("test", "name_reserved"), ("setup", "name_reserved"), ("sh", "bad_name"),
    ("ns1", "name_reserved"), ("ns", "bad_name"), ("ns12", "name_reserved"), ("status", "name_reserved"),
    ("www2", "name_reserved"), ("relay1", "name_reserved"),
])
def test_bad_names(name, why):
    assert relayd.name_problem(name) == why


def test_code_has_100_bits_and_is_forgiving():
    code = relayd.new_code()
    assert len(code) == 24 and code.count("-") == 4
    norm = relayd.normalize_code(code)
    assert len(norm) == 20 and set(norm) <= set(relayd.CODE_ALPHABET)
    assert relayd.normalize_code(code.lower().replace("-", " ")) == norm
    assert relayd.normalize_code("O0IL-" + "1" * 16) == "0011" + "1" * 16
    for bad in ("", "short", "U" * 20, None, 5, "A" * 100):
        assert relayd.normalize_code(bad) is None
    assert len({relayd.new_code() for _ in range(200)}) == 200


# --- выдача имён ---

def test_join_consumes_code_once(env):
    s, conn = env
    code = invite(conn)
    out = relayd.join(conn, s, code, "ivan")
    assert out["name"] == "ivan"
    assert out["dashboard_url"] == "https://ivan.sh.example.test"
    assert out["setup_url"] == "https://setup-ivan.sh.example.test"
    assert out["server"] == "relay.sh.example.test" and out["port"] == 7000
    assert out["frp_token"] == TOKEN and out["ca"].startswith("-----BEGIN CERTIFICATE-----")
    assert len(out["key"]) >= 43
    row = conn.execute("SELECT key_hash FROM instances WHERE name='ivan'").fetchone()
    assert row["key_hash"] == relayd.key_hash(out["key"]) and out["key"] not in row["key_hash"]
    with pytest.raises(relayd.JoinError) as e:
        relayd.join(conn, s, code, "petr")
    assert e.value.code == "bad_code" and e.value.status == 403
    assert relayd.get_instance(conn, "petr") is None


def test_name_taken_keeps_code(env):
    s, conn = env
    relayd.join(conn, s, invite(conn), "ivan")
    code = invite(conn)
    with pytest.raises(relayd.JoinError) as e:
        relayd.join(conn, s, code, "ivan")
    assert e.value.code == "name_taken" and e.value.status == 409
    assert relayd.join(conn, s, code, "petr")["name"] == "petr"


def test_bad_name_keeps_code(env):
    s, conn = env
    code = invite(conn)
    for name, why in (("setup-ivan", "bad_name"), ("relay", "name_reserved"), ("Ivan", "bad_name")):
        with pytest.raises(relayd.JoinError) as e:
            relayd.join(conn, s, code, name)
        assert e.value.code == why and e.value.status == 400
    assert relayd.join(conn, s, code, "ivan")["name"] == "ivan"


def test_expired_and_unknown_codes(env):
    s, conn = env
    old = invite(conn, days=1, now=int(time.time()) - 2 * 86400)
    for code in (old, relayd.new_code(), "garbage"):
        with pytest.raises(relayd.JoinError) as e:
            relayd.join(conn, s, code, "ivan")
        assert e.value.code == "bad_code"


def test_revoked_name_stays_taken_until_release(env, tmp_path):
    s, conn = env
    relayd.join(conn, s, invite(conn), "ivan")
    assert relayd.main(["--db", s.db, "revoke", "ivan"]) == 0
    with pytest.raises(relayd.JoinError) as e:
        relayd.join(conn, s, invite(conn), "ivan")
    assert e.value.code == "name_taken"
    assert relayd.main(["--db", s.db, "release", "ivan"]) == 0
    assert relayd.join(conn, s, invite(conn), "ivan")["name"] == "ivan"


def test_release_requires_revoke_and_operator_is_not_revoked(env, tmp_path):
    s, conn = env
    relayd.join(conn, s, invite(conn), "ivan")
    assert relayd.main(["--db", s.db, "release", "ivan"]) == 2
    keyfile = tmp_path / "operator.env"
    assert relayd.main(["--db", s.db, "operator", "--key-file", str(keyfile)]) == 0
    assert oct(keyfile.stat().st_mode & 0o777) == "0o600"
    first = keyfile.read_text()
    assert relayd.main(["--db", s.db, "operator", "--key-file", str(keyfile)]) == 0
    assert keyfile.read_text() == first                     # повторный запуск ключ не меняет
    assert relayd.main(["--db", s.db, "revoke", "relay"]) == 2


def test_invite_command_prints_code_and_stores_only_hash(env, capsys):
    s, conn = env
    assert relayd.main(["--db", s.db, "invite", "--days", "3", "--note", "Иван"]) == 0
    code = capsys.readouterr().out.strip()
    assert relayd.normalize_code(code)
    dump = "\n".join(conn.iterdump())
    assert code not in dump and relayd.normalize_code(code) not in dump
    assert relayd.join(conn, s, code, "ivan")["name"] == "ivan"
    assert relayd.main(["--db", s.db, "list"]) == 0
    listing = capsys.readouterr().out
    assert "ivan" in listing and "Иван" in listing and code not in listing
    assert relayd.main(["--db", s.db, "invite", "--days", "0"]) == 2


# --- плагин frps ---

def login(user, key, run_id="", addr="198.51.100.7:5555"):
    return {"version": "0.71.0", "user": user, "metas": {"key": key} if key is not None else {},
            "run_id": run_id, "client_address": addr, "privilege_key": "x", "timestamp": 1}


def new_proxy(user, key, ptype="https", sub=None, run_id="r1", **extra):
    c = {"user": {"user": user, "metas": {"key": key}, "run_id": run_id},
         "proxy_name": f"{user}.p", "proxy_type": ptype}
    if sub is not None:
        c["subdomain"] = sub
    c.update(extra)
    return c


@pytest.fixture()
def ivan(env):
    s, conn = env
    out = relayd.join(conn, s, invite(conn), "ivan")
    petr = relayd.join(conn, s, invite(conn), "petr")
    return conn, out["key"], petr["key"]


def decide(conn, op, content):
    return relayd.plugin_decide(conn, op, content)[0]


def test_login_needs_own_key(ivan):
    conn, key, petr_key = ivan
    assert decide(conn, "Login", login("ivan", key)) == relayd.ALLOW
    for c in (login("ivan", petr_key), login("ivan", key + "x"), login("ivan", ""), login("ivan", None),
              login("nobody", key), login("", key), login(None, key), login("IVAN", key)):
        r = decide(conn, "Login", c)
        assert r["reject"] is True and r["reject_reason"] == relayd.REASON_AUTH
    assert decide(conn, "Login", "garbage")["reject"] is True


def test_login_log_line_has_no_key(ivan):
    conn, key, _ = ivan
    for c in (login("ivan", key), login("ivan", "wrong-" + key)):
        note = relayd.plugin_decide(conn, "Login", c)[1]
        assert key not in note and "ivan" in note


def test_revoked_instance_is_cut_off(ivan, env):
    conn, key, _ = ivan
    s, _ = env
    work = {"user": {"user": "ivan", "metas": {"key": key}, "run_id": "r1"}, "run_id": "r1"}
    assert decide(conn, "NewWorkConn", work) == relayd.ALLOW
    assert relayd.main(["--db", s.db, "revoke", "ivan"]) == 0
    assert decide(conn, "Login", login("ivan", key))["reject_reason"] == relayd.REASON_REVOKED
    assert decide(conn, "NewWorkConn", work)["reject_reason"] == relayd.REASON_REVOKED
    assert decide(conn, "NewProxy", new_proxy("ivan", key, sub="ivan"))["reject_reason"] == relayd.REASON_REVOKED


def test_new_proxy_only_own_names_and_web_types(ivan):
    conn, key, _ = ivan
    for ptype in ("https", "http"):
        for sub in ("ivan", "setup-ivan"):
            assert decide(conn, "NewProxy", new_proxy("ivan", key, ptype, sub)) == relayd.ALLOW
    cases = [
        (new_proxy("ivan", key, "https", "petr"), relayd.REASON_NAME),
        (new_proxy("ivan", key, "https", "setup-petr"), relayd.REASON_NAME),
        (new_proxy("ivan", key, "https", "relay"), relayd.REASON_NAME),
        (new_proxy("ivan", key, "https", "ivan.sh.example.test"), relayd.REASON_NAME),
        (new_proxy("ivan", key, "https", ""), relayd.REASON_NAME),
        (new_proxy("ivan", key, "https"), relayd.REASON_NAME),
        (new_proxy("ivan", key, "https", None, custom_domains=["ivan.sh.example.test"]), relayd.REASON_DOMAINS),
        (new_proxy("ivan", key, "https", "ivan", custom_domains=["evil.example"]), relayd.REASON_DOMAINS),
        (new_proxy("ivan", key, "http", "ivan", group="g", group_key="k"), relayd.REASON_GROUP),
    ]
    for ptype in ("tcp", "udp", "stcp", "sudp", "xtcp", "tcpmux", "", None):
        cases.append((new_proxy("ivan", key, ptype, "ivan", remote_port=2222), relayd.REASON_TYPE))
    for content, why in cases:
        r = decide(conn, "NewProxy", content)
        assert r == {"reject": True, "reject_reason": why}, content
    # Чужой ключ — даже для своих имён.
    _, _, petr_key = ivan
    assert decide(conn, "NewProxy", new_proxy("ivan", petr_key, "https", "ivan"))["reject_reason"] == relayd.REASON_AUTH


def test_operator_may_only_hold_relay_name(env, tmp_path):
    s, conn = env
    keyfile = tmp_path / "op.env"
    relayd.main(["--db", s.db, "operator", "--key-file", str(keyfile)])
    key = keyfile.read_text().split("=", 1)[1].strip()
    assert decide(conn, "Login", login("relay", key)) == relayd.ALLOW
    assert decide(conn, "NewProxy", new_proxy("relay", key, "https", "relay")) == relayd.ALLOW
    for sub in ("setup-relay", "ivan", "www"):
        assert decide(conn, "NewProxy", new_proxy("relay", key, "https", sub))["reject_reason"] == relayd.REASON_NAME


def test_run_id_of_another_instance_is_refused(ivan):
    conn, key, petr_key = ivan
    assert decide(conn, "NewProxy", new_proxy("ivan", key, "https", "ivan", run_id="aaaa1111")) == relayd.ALLOW
    assert decide(conn, "Login", login("petr", petr_key, run_id="aaaa1111"))["reject_reason"] == relayd.REASON_RUN
    assert decide(conn, "Login", login("ivan", key, run_id="aaaa1111")) == relayd.ALLOW


def test_unknown_ops_are_refused(ivan):
    conn, key, _ = ivan
    for op in ("Ping", "CloseProxy", "NewUserConn", "", "Whatever"):
        assert decide(conn, op, {"user": {"user": "ivan", "metas": {"key": key}}})["reject"] is True


def test_rate_limit_window():
    rl = relayd.RateLimit(per_key=2, total=3, window=10)
    assert rl.allow("a", 0) and rl.allow("a", 1) and not rl.allow("a", 2)
    assert rl.allow("b", 2) and not rl.allow("c", 3)       # общий предел
    assert rl.allow("a", 11.5)                              # окно сдвинулось


# --- HTTP ---

@pytest.fixture()
def servers(env):
    s, conn = env
    plugin, api = relayd.make_servers(s, ("127.0.0.1", 0), ("127.0.0.1", 0))
    for srv in (plugin, api):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield s, plugin, api
    for srv in (plugin, api):
        srv.shutdown()
        srv.server_close()


def call(srv, method, path, body=None, headers=None):
    host, port = srv.server_address[:2]
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    req = urllib.request.Request(f"http://{host}:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_http_join_and_plugin_round_trip(servers):
    s, plugin, api = servers
    assert call(api, "GET", "/api/health")[0] == 200
    status, body, _ = call(api, "GET", "/api/ca")
    assert status == 200 and body.startswith(b"-----BEGIN CERTIFICATE-----")
    conn = relayd.open_db(s.db)
    code = invite(conn)
    status, body, hdr = call(api, "POST", "/api/join", {"code": code, "name": "ivan"})
    assert status == 200 and hdr["Cache-Control"] == "no-store"
    out = json.loads(body)
    status, body, _ = call(api, "POST", "/api/join", {"code": code, "name": "ivan2"}, {"X-Forwarded-For": "203.0.113.9"})
    assert status == 403 and json.loads(body)["error"] == "bad_code"
    # Ответ frps — в формате doc/server_plugin.md.
    req = {"version": "0.1.0", "op": "Login", "content": login("ivan", out["key"])}
    status, body, _ = call(plugin, "POST", "/frps/handler?version=0.1.0&op=Login", req)
    assert status == 200 and json.loads(body) == {"reject": False, "unchange": True}
    req["content"] = login("ivan", "wrong")
    assert json.loads(call(plugin, "POST", "/frps/handler?op=Login", req)[1])["reject"] is True
    assert call(plugin, "POST", "/other", req)[0] == 404
    assert call(api, "POST", "/frps/handler?op=Login", req)[0] == 404      # плагин — не на адресе API


def test_http_join_rejects_non_json_and_big_bodies(servers):
    s, _, api = servers
    assert call(api, "POST", "/api/join", b"code=1&name=x", {"Content-Type": "application/x-www-form-urlencoded",
                                                             "X-Forwarded-For": "10.0.0.1"})[0] == 400
    assert call(api, "POST", "/api/join", b"x" * 5000, {"X-Forwarded-For": "10.0.0.2"})[0] == 400


def test_http_rate_limit_is_per_client_ip_from_local_proxy(servers):
    _, _, api = servers
    bad = {"code": "AAAA-AAAA-AAAA-AAAA-AAAA", "name": "ivan"}
    for _ in range(3):
        assert call(api, "POST", "/api/join", bad, {"X-Forwarded-For": "203.0.113.1"})[0] == 403
    status, body, hdr = call(api, "POST", "/api/join", bad, {"X-Forwarded-For": "203.0.113.1"})
    assert status == 429 and hdr.get("Retry-After") == "900"
    # Другой клиент — свой счёт. Подставленный клиентом заголовок Caddy заменяет; здесь проверяется,
    # что берётся последний адрес цепочки.
    assert call(api, "POST", "/api/join", bad, {"X-Forwarded-For": "203.0.113.1, 203.0.113.2"})[0] == 403


def test_keepalive_connection_survives_refusals(servers):
    """Caddy держит соединение с relayd открытым: тело отклонённого запроса не должно остаться
    в соединении и стать «следующим запросом» (так было: 429 без чтения тела, затем 501)."""
    import http.client
    _, _, api = servers
    conn = http.client.HTTPConnection(*api.server_address[:2], timeout=5)
    bad = json.dumps({"code": "AAAA-AAAA-AAAA-AAAA-AAAA", "name": "ivan"})
    hdr = {"Content-Type": "application/json", "X-Forwarded-For": "203.0.113.50"}
    codes = []
    for path in ("/api/join", "/api/join", "/api/join", "/api/join", "/nope", "/api/join"):
        conn.request("POST", path, bad, hdr)
        r = conn.getresponse()
        r.read()
        codes.append(r.status)
    conn.request("GET", "/api/health")
    r = conn.getresponse()
    assert json.loads(r.read())["ok"] is True
    assert codes == [403, 403, 403, 429, 404, 429]


def test_serve_refuses_without_token_or_ca(env, monkeypatch, capsys):
    s, _ = env
    monkeypatch.delenv("FRP_AUTH_TOKEN", raising=False)
    assert relayd.main(["--db", s.db, "serve", "--ca", s.ca_file]) == 2
    monkeypatch.setenv("FRP_AUTH_TOKEN", "t")
    assert relayd.main(["--db", s.db, "serve", "--ca", "/nonexistent"]) == 2
    with pytest.raises(SystemExit):
        relayd.main(["--db", s.db, "serve", "--plugin", "0.0.0.0:7400"])
