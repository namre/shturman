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
                        ca_file=str(ca), frp_token=TOKEN, join_per_ip=3, join_window=900, join_fail_total=100, join_fail_window=60)
    conn = relayd.open_db(s.db)
    return s, conn


REAL_RESTART = relayd.restart_frps


@pytest.fixture(autouse=True)
def frps_restarts(monkeypatch):
    """relayctl revoke/release перезапускают frps (systemctl) и требуют root: здесь — подмена."""
    calls = []
    monkeypatch.setattr(relayd, "restart_frps", lambda: calls.append("restart") or True)
    monkeypatch.setattr(relayd, "_is_root", lambda: True)
    return calls


def new_key_hash(key=None):
    key = key or relayd.new_key()
    return key, relayd.key_hash(key)


def J(conn, s, code, name, key=None):
    """Подключение так, как его делает ./ops/relay.sh: ключ создаёт экземпляр, посреднику — только sha256."""
    key, kh = new_key_hash(key)
    return relayd.join(conn, s, code, name, kh), key


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
    out, key = J(conn, s, code, "ivan")
    assert out["name"] == "ivan" and "key" not in out          # ключ посредник не создаёт и не отдаёт
    assert out["dashboard_url"] == "https://ivan.sh.example.test"
    assert out["setup_url"] == "https://setup-ivan.sh.example.test"
    assert out["server"] == "relay.sh.example.test" and out["port"] == 7000
    assert out["frp_token"] == TOKEN and out["ca"].startswith("-----BEGIN CERTIFICATE-----")
    row = conn.execute("SELECT key_hash FROM instances WHERE name='ivan'").fetchone()
    assert row["key_hash"] == relayd.key_hash(key) and key not in json.dumps(out)
    with pytest.raises(relayd.JoinError) as e:
        J(conn, s, code, "petr")[0]
    assert e.value.code == "bad_code" and e.value.status == 403
    assert relayd.get_instance(conn, "petr") is None


def test_name_taken_keeps_code(env):
    s, conn = env
    J(conn, s, invite(conn), "ivan")[0]
    code = invite(conn)
    with pytest.raises(relayd.JoinError) as e:
        J(conn, s, code, "ivan")[0]
    assert e.value.code == "name_taken" and e.value.status == 409
    assert J(conn, s, code, "petr")[0]["name"] == "petr"


def test_bad_name_keeps_code(env):
    s, conn = env
    code = invite(conn)
    for name, why in (("setup-ivan", "bad_name"), ("relay", "name_reserved"), ("Ivan", "bad_name")):
        with pytest.raises(relayd.JoinError) as e:
            J(conn, s, code, name)[0]
        assert e.value.code == why and e.value.status == 400
    assert J(conn, s, code, "ivan")[0]["name"] == "ivan"


def test_expired_and_unknown_codes(env):
    s, conn = env
    old = invite(conn, days=1, now=int(time.time()) - 2 * 86400)
    for code in (old, relayd.new_code(), "garbage"):
        with pytest.raises(relayd.JoinError) as e:
            J(conn, s, code, "ivan")[0]
        assert e.value.code == "bad_code"


def test_retry_with_same_code_name_and_key_is_idempotent(env):
    """Ответ на join потерялся в сети: повтор тем же кодом, именем и ключом — тот же успех; любой
    другой повтор израсходованным кодом — отказ."""
    s, conn = env
    code = invite(conn)
    first, key = J(conn, s, code, "ivan")
    again = relayd.join(conn, s, code, "ivan", relayd.key_hash(key))
    assert again.pop("repeated") is True and again == first
    for name, k in (("ivan", relayd.new_key()), ("petr", key)):
        with pytest.raises(relayd.JoinError) as e:
            relayd.join(conn, s, code, name, relayd.key_hash(k))
        assert e.value.code == "bad_code"
    assert relayd.main(["--db", s.db, "revoke", "ivan", "--no-restart"]) == 0
    with pytest.raises(relayd.JoinError):
        relayd.join(conn, s, code, "ivan", relayd.key_hash(key))   # отключённому повтор не помогает


def test_key_hash_is_required_and_ca_is_read_before_spending_code(env, tmp_path):
    s, conn = env
    code = invite(conn)
    for bad in (None, "", "x" * 64, "A" * 64, relayd.key_hash("k") + "\n", 5):
        with pytest.raises(relayd.JoinError) as e:
            relayd.join(conn, s, code, "ivan", bad)
        assert e.value.code == "bad_key_hash"
    broken = relayd.Settings(**{**s.__dict__, "ca_file": str(tmp_path / "nope.crt")})
    with pytest.raises(OSError):
        J(conn, broken, code, "ivan")
    assert J(conn, s, code, "ivan")[0]["name"] == "ivan"          # код не израсходован


# --- проверка имени до подключения ---

def test_check_needs_a_live_code_and_does_not_spend_it(env):
    s, conn = env
    code = invite(conn)
    out = relayd.check(conn, s, code, "ivan")
    assert out == {"name": "ivan", "domain": "sh.example.test", "available": True, "reason": None,
                   "message": "Имя свободно.", "suggestions": []}
    assert relayd.check(conn, s, code, "ivan")["available"] is True        # сколько угодно раз
    used = invite(conn)
    J(conn, s, used, "olga")
    old = invite(conn, days=1, now=int(time.time()) - 2 * 86400)
    for bad in (used, old, relayd.new_code(), "garbage", None, 5, ""):
        with pytest.raises(relayd.JoinError) as e:
            relayd.check(conn, s, bad, "ivan")
        assert (e.value.status, e.value.code, e.value.extra) == (403, "bad_code", {})
    assert J(conn, s, code, "ivan")[0]["name"] == "ivan"                   # код не израсходован проверкой
    with pytest.raises(relayd.JoinError):
        relayd.check(conn, s, code, "petr")                                # а join — израсходовал


def test_check_reasons_and_suggestions(env):
    s, conn = env
    J(conn, s, invite(conn), "ivan")
    J(conn, s, invite(conn), "ivan2")
    code = invite(conn)
    out = relayd.check(conn, s, code, "ivan")
    assert out["available"] is False and out["reason"] == "taken" and out["name"] == "ivan"
    assert 1 <= len(out["suggestions"]) <= 3
    for sug in out["suggestions"]:
        assert relayd.name_problem(sug) is None and relayd.get_instance(conn, sug) is None and sug.startswith("ivan")
    assert "ivan2" not in out["suggestions"] and out["suggestions"][0] == "ivan3"
    out = relayd.check(conn, s, code, "relay")
    assert out["reason"] == "reserved" and all(relayd.name_problem(x) is None for x in out["suggestions"])
    assert out["suggestions"] and not any(x.startswith("relay") and x[5:].isdigit() for x in out["suggestions"])
    out = relayd.check(conn, s, code, "Petr Ivanov")
    assert out["reason"] == "invalid" and out["suggestions"][0] == "petr-ivanov"
    assert relayd.check(conn, s, code, "setup-olga")["suggestions"][0] == "olga"
    for odd in (None, 42, "", "--", "я"):
        out = relayd.check(conn, s, code, odd)
        assert out["reason"] == "invalid" and out["suggestions"] == []
    assert relayd.check(conn, s, code, "setup") == {**out, "name": "setup", "reason": "reserved",
                                                    "message": relayd.MESSAGES["name_reserved"]}
    assert relayd.check(conn, s, code, "x" * 100)["name"] is None          # длинное имя назад не отдаётся
    long = relayd.check(conn, s, code, "a" * 40)["suggestions"]
    assert long and all(len(x) <= 30 and relayd.name_problem(x) is None for x in long)


def test_join_with_taken_name_offers_free_variants(env):
    s, conn = env
    J(conn, s, invite(conn), "ivan")
    code = invite(conn)
    with pytest.raises(relayd.JoinError) as e:
        J(conn, s, code, "ivan")
    assert e.value.status == 409 and e.value.extra["suggestions"][0] == "ivan2"
    assert J(conn, s, code, e.value.extra["suggestions"][0])[0]["name"] == "ivan2"
    # Служебное имя join отклоняет до проверки кода — без вариантов: иначе по ним без кода было бы
    # видно, какие имена заняты.
    with pytest.raises(relayd.JoinError) as e:
        J(conn, s, "garbage", "relay")
    assert e.value.code == "name_reserved" and e.value.extra == {}


def test_caddy_of_the_relay_passes_check_to_relayd():
    caddyfile = (Path(__file__).resolve().parents[1] / "Caddyfile").read_text(encoding="utf-8")
    assert "@api path /api/join /api/check /api/health /api/ca" in caddyfile


def test_name_with_trailing_newline_is_refused():
    for name in ("ivan\n", "ivan\r", "iv\nan", " ivan", "ivan "):
        assert relayd.name_problem(name) == "bad_name"
    assert relayd.name_problem("relay\n") == "bad_name"


def test_revoke_and_release_restart_frps(env, frps_restarts, monkeypatch):
    s, conn = env
    J(conn, s, invite(conn), "ivan")
    assert relayd.main(["--db", s.db, "revoke", "ivan", "--no-restart"]) == 0 and frps_restarts == []
    assert relayd.main(["--db", s.db, "revoke", "ivan"]) == 0 and frps_restarts == ["restart"]
    assert relayd.main(["--db", s.db, "release", "ivan"]) == 0 and frps_restarts == ["restart"] * 2
    monkeypatch.setattr(relayd, "_is_root", lambda: False)
    J(conn, s, invite(conn), "petr")
    assert relayd.main(["--db", s.db, "revoke", "petr"]) == 2        # перезапуск без root невозможен
    assert relayd.get_instance(conn, "petr").revoked is False
    assert relayd.main(["--db", s.db, "revoke", "petr", "--no-restart"]) == 0
    assert relayd.main(["--db", s.db, "release", "petr"]) == 2       # release без перезапуска не бывает
    assert relayd.get_instance(conn, "petr") is not None


def test_failed_frps_restart_keeps_name_taken(env, monkeypatch):
    s, conn = env
    J(conn, s, invite(conn), "ivan")
    monkeypatch.setattr(relayd.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("нет systemctl")))
    monkeypatch.setattr(relayd, "restart_frps", REAL_RESTART)
    assert relayd.main(["--db", s.db, "revoke", "ivan"]) == 1             # отмечен, но сказано, что frps не перезапущен
    assert relayd.get_instance(conn, "ivan").revoked is True
    assert relayd.main(["--db", s.db, "release", "ivan"]) == 1
    assert relayd.get_instance(conn, "ivan") is not None                  # имя не освобождено


def test_revoked_name_stays_taken_until_release(env, tmp_path):
    s, conn = env
    J(conn, s, invite(conn), "ivan")[0]
    assert relayd.main(["--db", s.db, "revoke", "ivan"]) == 0
    with pytest.raises(relayd.JoinError) as e:
        J(conn, s, invite(conn), "ivan")[0]
    assert e.value.code == "name_taken"
    assert relayd.main(["--db", s.db, "release", "ivan"]) == 0
    assert J(conn, s, invite(conn), "ivan")[0]["name"] == "ivan"


def test_release_requires_revoke_and_operator_is_not_revoked(env, tmp_path):
    s, conn = env
    J(conn, s, invite(conn), "ivan")[0]
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
    assert J(conn, s, code, "ivan")[0]["name"] == "ivan"
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
    _, key = J(conn, s, invite(conn), "ivan")
    _, petr_key = J(conn, s, invite(conn), "petr")
    return conn, key, petr_key


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


def test_rate_limit_window_and_ipv6_buckets():
    w = relayd.Window(limit=2, window=10)
    w.add("a", 0); w.add("a", 1)
    assert w.blocked("a", 2) and not w.blocked("b", 2)
    assert not w.blocked("a", 11.5)                         # окно сдвинулось
    assert relayd.ip_bucket("2001:db8:1:2:aaaa::1") == relayd.ip_bucket("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert relayd.ip_bucket("2001:db8:1:3::1") != relayd.ip_bucket("2001:db8:1:2::1")
    assert relayd.ip_bucket("203.0.113.5") == "203.0.113.5" != relayd.ip_bucket("203.0.113.6")
    assert relayd.ip_bucket("::ffff:203.0.113.5") == "203.0.113.5"


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
    key, kh = new_key_hash()
    body_ok = {"code": code, "name": "ivan", "key_hash": kh}
    status, body, hdr = call(api, "POST", "/api/join", body_ok)
    assert status == 200 and hdr["Cache-Control"] == "no-store"
    out = json.loads(body)
    assert "key" not in out and key.encode() not in body
    status, body, _ = call(api, "POST", "/api/join", body_ok)            # повтор — тот же успех
    assert status == 200 and json.loads(body)["repeated"] is True
    status, body, _ = call(api, "POST", "/api/join", {"code": code, "name": "ivan2", "key_hash": kh},
                           {"X-Forwarded-For": "203.0.113.9"})
    assert status == 403 and json.loads(body)["error"] == "bad_code"
    # Ответ frps — в формате doc/server_plugin.md.
    req = {"version": "0.1.0", "op": "Login", "content": login("ivan", key)}
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
    bad = {"code": "AAAA-AAAA-AAAA-AAAA-AAAA", "name": "ivan", "key_hash": "0" * 64}
    for _ in range(3):
        assert call(api, "POST", "/api/join", bad, {"X-Forwarded-For": "203.0.113.1"})[0] == 403
    status, body, hdr = call(api, "POST", "/api/join", bad, {"X-Forwarded-For": "203.0.113.1"})
    assert status == 429 and hdr.get("Retry-After") == "900"
    # Другой клиент — свой счёт. Подставленный клиентом заголовок Caddy заменяет; здесь проверяется,
    # что берётся последний адрес цепочки.
    assert call(api, "POST", "/api/join", bad, {"X-Forwarded-For": "203.0.113.1, 203.0.113.2"})[0] == 403


def test_http_rate_limit_groups_ipv6_by_64(servers):
    _, _, api = servers
    bad = {"code": "AAAA-AAAA-AAAA-AAAA-AAAA", "name": "ivan", "key_hash": "0" * 64}
    for i in range(3):
        assert call(api, "POST", "/api/join", bad, {"X-Forwarded-For": f"2001:db8:5:6::{i + 1}"})[0] == 403
    assert call(api, "POST", "/api/join", bad, {"X-Forwarded-For": "2001:db8:5:6:ffff::77"})[0] == 429
    assert call(api, "POST", "/api/join", bad, {"X-Forwarded-For": "2001:db8:5:7::1"})[0] == 403


def test_global_cap_counts_failures_and_clears_quickly(env, tmp_path):
    s, conn = env
    s = relayd.Settings(**{**s.__dict__, "join_per_ip": 100, "join_fail_total": 3, "join_fail_window": 1})
    plugin, api = relayd.make_servers(s, ("127.0.0.1", 0), ("127.0.0.1", 0))
    threading.Thread(target=api.serve_forever, daemon=True).start()
    try:
        bad = {"code": "AAAA-AAAA-AAAA-AAAA-AAAA", "name": "ivan", "key_hash": "0" * 64}
        for i in range(3):
            assert call(api, "POST", "/api/join", bad, {"X-Forwarded-For": f"198.51.100.{i + 1}"})[0] == 403
        good = {"code": invite(conn), "name": "ivan", "key_hash": new_key_hash()[1]}
        status, _, hdr = call(api, "POST", "/api/join", good, {"X-Forwarded-For": "198.51.100.99"})
        assert status == 429 and hdr.get("Retry-After") == "1"        # перебор с многих адресов
        time.sleep(1.2)
        assert call(api, "POST", "/api/join", good, {"X-Forwarded-For": "198.51.100.99"})[0] == 200
        # Удачные подключения общий предел не заполняют.
        for name in ("olga", "petr", "anna", "boris"):
            ok = {"code": invite(conn), "name": name, "key_hash": new_key_hash()[1]}
            assert call(api, "POST", "/api/join", ok, {"X-Forwarded-For": "198.51.100.98"})[0] == 200
    finally:
        api.shutdown()
        api.server_close()
        plugin.server_close()


def test_http_check(servers):
    s, _, api = servers
    conn = relayd.open_db(s.db)
    J(conn, s, invite(conn), "ivan")
    code = invite(conn)
    status, body, hdr = call(api, "POST", "/api/check", {"code": code, "name": "petr"})
    assert status == 200 and hdr["Cache-Control"] == "no-store"
    assert json.loads(body) == {"name": "petr", "domain": "sh.example.test", "available": True, "reason": None,
                                "message": "Имя свободно.", "suggestions": []}
    out = json.loads(call(api, "POST", "/api/check", {"code": code, "name": "ivan"})[1])
    assert out["available"] is False and out["reason"] == "taken" and out["suggestions"][0] == "ivan2"
    # Неверный код: тот же отказ, что у join, и ни слова об имени — занятое оно или свободное.
    replies = set()
    for name in ("ivan", "petr", "relay"):
        status, body, _ = call(api, "POST", "/api/check", {"code": "AAAA-AAAA-AAAA-AAAA-AAAA", "name": name},
                               {"X-Forwarded-For": "198.51.100.40"})
        assert status == 403
        replies.add(body)
    join_403 = call(api, "POST", "/api/join", {"code": "AAAA-AAAA-AAAA-AAAA-AAAA", "name": "petr", "key_hash": "0" * 64},
                    {"X-Forwarded-For": "198.51.100.41"})[1]
    assert replies == {join_403} and json.loads(join_403)["error"] == "bad_code"
    # Код проверкой не израсходован; занятое имя в join — 409 с вариантами, код опять цел.
    status, body, _ = call(api, "POST", "/api/join", {"code": code, "name": "ivan", "key_hash": "1" * 64})
    assert status == 409 and json.loads(body)["suggestions"][0] == "ivan2"
    assert call(api, "POST", "/api/join", {"code": code, "name": "petr", "key_hash": "1" * 64})[0] == 200
    assert call(api, "POST", "/api/check", {"code": code, "name": "olga"})[0] == 403    # теперь израсходован
    assert call(api, "POST", "/api/check", b"name=x", {"Content-Type": "text/plain"})[0] == 400


def test_http_check_has_its_own_budget_but_shares_the_failure_cap(env):
    s, conn = env
    s = relayd.Settings(**{**s.__dict__, "join_per_ip": 2, "check_per_ip": 3, "join_fail_total": 4, "join_fail_window": 1})
    plugin, api = relayd.make_servers(s, ("127.0.0.1", 0), ("127.0.0.1", 0))
    threading.Thread(target=api.serve_forever, daemon=True).start()
    try:
        code = invite(conn)
        ip = {"X-Forwarded-For": "203.0.113.70"}
        for name in ("anna", "boris", "vera"):
            assert call(api, "POST", "/api/check", {"code": code, "name": name}, ip)[0] == 200
        status, _, hdr = call(api, "POST", "/api/check", {"code": code, "name": "gleb"}, ip)
        assert status == 429 and hdr.get("Retry-After") == "900"
        # Проверки не съели попытки подключения с этого адреса.
        assert call(api, "POST", "/api/join", {"code": code, "name": "anna", "key_hash": "2" * 64}, ip)[0] == 200
        # Неверные коды в проверке — в общем пределе неудачных: после четырёх join с других адресов ждёт.
        for i in range(4):
            assert call(api, "POST", "/api/check", {"code": "AAAA-AAAA-AAAA-AAAA-AAAA", "name": "x1x"},
                        {"X-Forwarded-For": f"198.51.100.{60 + i}"})[0] == 403
        good = {"code": invite(conn), "name": "dima", "key_hash": "3" * 64}
        assert call(api, "POST", "/api/join", good, {"X-Forwarded-For": "198.51.100.99"})[0] == 429
        time.sleep(1.2)
        assert call(api, "POST", "/api/join", good, {"X-Forwarded-For": "198.51.100.99"})[0] == 200
    finally:
        api.shutdown()
        api.server_close()
        plugin.server_close()


def test_keepalive_connection_survives_refusals(servers):
    """Caddy держит соединение с relayd открытым: тело отклонённого запроса не должно остаться
    в соединении и стать «следующим запросом» (так было: 429 без чтения тела, затем 501)."""
    import http.client
    _, _, api = servers
    conn = http.client.HTTPConnection(*api.server_address[:2], timeout=5)
    bad = json.dumps({"code": "AAAA-AAAA-AAAA-AAAA-AAAA", "name": "ivan", "key_hash": "0" * 64})
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
