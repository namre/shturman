"""Хранилище значений страницы: права файла, приоритет окружения, отсутствие утечек, отправка."""

import json
import logging
import os
import stat

import pytest

from setup_fakes import API_HASH, LLM_KEY, TOKEN, save_bot, stand  # noqa: F401, I001 — stand — фикстура
from conftest import API_AUTH, API_TOKEN, DSN, MCP_TOKEN
from exec_fakes import until

from shturman import bridge
from shturman.config import Config, ConfigError, with_page_values
from shturman.setup_page import secrets_store as ss

SECRETS = (TOKEN, LLM_KEY, API_HASH)


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_file_is_private_and_values_never_show_in_repr(tmp_path):
    store = ss.SecretStore(tmp_path)
    assert store.load() == {} and not store.path.exists()
    old = os.umask(0)                      # даже при самой свободной маске процесса
    try:
        store.update({ss.BOT_TOKEN: TOKEN, ss.LLM_API_KEY: LLM_KEY, ss.TG_API_ID: "1234567", ss.TG_API_HASH: API_HASH})
    finally:
        os.umask(old)
    assert mode(store.path) == 0o600 and mode(store.path.parent) == 0o700
    assert store.path == tmp_path / "setup" / "secrets.json"
    assert not [p for p in store.path.parent.iterdir() if p.name.endswith(".tmp")]
    for text in (repr(store), str(store), f"{store}", f"{store!r}"):
        assert not any(secret in text for secret in SECRETS)
        assert "bot_token" in text and "llm_api_key" in text
    assert store.get(ss.BOT_TOKEN) == TOKEN and store.has(ss.TG_API_HASH) and not store.has(ss.LLM_MODEL)

    store.update({ss.LLM_API_KEY: None, ss.TG_API_ID: ""})
    assert set(store.load()) == {ss.BOT_TOKEN, ss.TG_API_HASH}
    assert LLM_KEY not in store.path.read_text()
    with pytest.raises(KeyError):
        store.update({"sending": "on"})                       # посторонних имён в файле не бывает
    with pytest.raises(ValueError):
        store.update({ss.BOT_TOKEN: "x" * 5000})


def test_loosened_permissions_are_repaired_and_a_broken_file_is_ignored(tmp_path, caplog):
    store = ss.SecretStore(tmp_path)
    store.update({ss.BOT_TOKEN: TOKEN})
    os.chmod(store.path, 0o644)
    with caplog.at_level(logging.WARNING):
        assert store.load() == {ss.BOT_TOKEN: TOKEN}
    assert mode(store.path) == 0o600 and TOKEN not in caplog.text

    store.path.write_text("{ не json")
    with caplog.at_level(logging.ERROR):
        assert store.load() == {}
    store.path.write_text(json.dumps({"values": {"bot_token": 5, "unknown": "x", "llm_model": "m"}}))
    assert store.load() == {"llm_model": "m"}
    assert TOKEN not in caplog.text


def test_signing_key_is_created_once_and_is_private(tmp_path):
    key = ss.signing_key(tmp_path)
    assert len(key) == 32 and ss.signing_key(tmp_path) == key
    assert mode(tmp_path / "setup" / "key") == 0o600
    (tmp_path / "setup" / "key").write_bytes(b"short")
    assert len(ss.signing_key(tmp_path)) == 32


def base(tmp_path, **kw) -> Config:
    return Config(dsn=DSN, api_token=API_TOKEN, mcp_token=MCP_TOKEN, data_dir=tmp_path, **kw)


def test_environment_wins_over_the_file(tmp_path):
    ss.SecretStore(tmp_path).update({
        ss.BOT_TOKEN: TOKEN, ss.LLM_API_KEY: LLM_KEY, ss.LLM_MODEL: "from-page", ss.LLM_BASE_URL: "https://llm.example/v1/",
        ss.TG_API_ID: "1234567", ss.TG_API_HASH: API_HASH})
    # окружение молчит — всё берётся из файла
    plain = with_page_values(base(tmp_path), {})
    assert (plain.bot_token, plain.llm_api_key, plain.llm_model, plain.llm_base_url) == \
           (TOKEN, LLM_KEY, "from-page", "https://llm.example/v1")
    assert (plain.tg_api_id, plain.tg_api_hash) == (1234567, API_HASH) and plain.locked == frozenset()
    assert plain.own_bot and plain.own_llm

    # окружение задало — файл для этого значения не читается
    env = {"SHTURMAN_BOT_TOKEN": "7000000003:FROM-env-token-0000000000000000", "SHTURMAN_LLM_MODEL": "from-env",
           "TELEGRAM_API_ID": "42"}
    cfg = with_page_values(base(tmp_path, bot_token=env["SHTURMAN_BOT_TOKEN"], llm_model="from-env", tg_api_id=42), env)
    assert cfg.bot_token == env["SHTURMAN_BOT_TOKEN"] and cfg.llm_model == "from-env"
    assert cfg.llm_api_key == LLM_KEY                               # ключ окружение не задавало — он из файла
    # ключи приложения — только парой: задан один в окружении, второй из файла не подмешивается
    assert (cfg.tg_api_id, cfg.tg_api_hash) == (42, "")
    assert cfg.locked == {ss.BOT_TOKEN, ss.LLM_MODEL, ss.TG_API_ID, ss.TG_API_HASH}


def test_repr_of_settings_has_no_secrets(tmp_path):
    ss.SecretStore(tmp_path).update({ss.BOT_TOKEN: TOKEN, ss.LLM_API_KEY: LLM_KEY, ss.LLM_MODEL: "m"})
    text = repr(with_page_values(base(tmp_path), {}))
    assert TOKEN not in text and LLM_KEY not in text and API_TOKEN not in text


def from_env(monkeypatch, tmp_path, **env) -> Config:
    for name in ("SHTURMAN_BOT_TOKEN", "SHTURMAN_SENDING", "SHTURMAN_LLM_API_KEY", "SHTURMAN_LLM_MODEL",
                 "SHTURMAN_LLM_BASE_URL", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "SHTURMAN_SETUP_ORIGIN",
                 "SHTURMAN_DASHBOARD_ORIGIN", "SHTURMAN_ALLOWED_HOSTS", "SHTURMAN_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SHTURMAN_DSN", DSN)
    monkeypatch.setenv("SHTURMAN_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("SHTURMAN_MCP_TOKEN", MCP_TOKEN)
    monkeypatch.setenv("SHTURMAN_DATA_DIR", str(tmp_path))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Config.from_env()


def test_a_token_entered_on_the_page_never_turns_sending_on(monkeypatch, tmp_path):
    ss.SecretStore(tmp_path).update({ss.BOT_TOKEN: TOKEN})
    cfg = from_env(monkeypatch, tmp_path, SHTURMAN_SENDING="on")
    assert cfg.own_bot is True and cfg.bot_token == TOKEN
    assert cfg.sending is False            # выключатель в окружении есть, а токена в окружении нет
    # отправка — только когда и выключатель, и токен бота заданы окружением
    assert from_env(monkeypatch, tmp_path, SHTURMAN_SENDING="on", SHTURMAN_BOT_TOKEN=TOKEN).sending is True
    assert from_env(monkeypatch, tmp_path, SHTURMAN_BOT_TOKEN=TOKEN).sending is False
    assert from_env(monkeypatch, tmp_path).sending is False


@pytest.mark.parametrize("raw, expected", [
    ("https://assistant.example.com", "https://assistant.example.com"),
    ("https://Assistant.Example.com/", "https://assistant.example.com"),
    ("https://assistant.example.com:443", "https://assistant.example.com"),
    ("https://assistant.example.com:8443", "https://assistant.example.com:8443"),
    ("http://shturman.test:8080", "http://shturman.test:8080"),
    ("https://assistant.example.com.:8443", "https://assistant.example.com:8443"),
    ("HTTPS://пример.example", "https://xn--e1afmkfd.example"),
    # без домена: внешний IPv4-адрес сервера (docs/deployment.md, «Без домена»)
    ("https://203.0.113.10:8443", "https://203.0.113.10:8443"),
    ("https://203.0.113.10:8443/", "https://203.0.113.10:8443"),
    ("https://203.0.113.10:443", "https://203.0.113.10"),
])
def test_setup_origin_is_normalised(monkeypatch, tmp_path, raw, expected):
    assert from_env(monkeypatch, tmp_path, SHTURMAN_SETUP_ORIGIN=raw).setup_origin == expected


@pytest.mark.parametrize("raw", ["assistant.example.com", "ftp://assistant.example.com", "https://assistant.example.com/setup",
                                 "https://user:pw@assistant.example.com", "https://assistant.example.com?x=1", "https://"])
def test_setup_origin_with_a_path_or_credentials_stops_the_service(monkeypatch, tmp_path, raw):
    with pytest.raises(ConfigError):
        from_env(monkeypatch, tmp_path, SHTURMAN_SETUP_ORIGIN=raw)


# --- на работающем сервисе ---

async def test_values_saved_on_the_page_never_come_back(stand, conn, caplog):
    caplog.set_level(logging.DEBUG)
    s = await stand()
    await s.page.login(conn)
    seen = [await save_bot(s)]
    seen.append(await s.page.put("/tg/keys", {"api_id": "1234567", "api_hash": API_HASH}))
    seen.append(await s.page.put("/llm", {"api_key": LLM_KEY, "model": "gpt-test", "base_url": "https://llm.example/v1"}))
    assert [r.status_code for r in seen] == [200, 200, 200]
    await until(lambda: s.state.extras["executor"].bot is not None and s.state.extras["executor"].bot.polling)
    seen += [await s.page.get("/state"), await s.page.get("/overview"), await s.page.get("/session"),
             await s.api.get("/api/status"), await s.api.get("/api/executor/status")]
    for response in seen:
        assert not any(secret in response.text for secret in SECRETS + (s.page.key,)), response.url
    state = (await s.page.get("/state")).json()
    assert state["bot"]["set"] is True and state["bot"]["source"] == "page" and state["bot"]["editable"] is True
    assert state["tg"]["keys"] == {"configured": True, "source": "page", "editable": True}
    assert state["llm"]["key"] == {"set": True, "source": "page", "editable": True}
    assert state["llm"]["base_url"]["value"] == "https://llm.example/v1" and state["llm"]["model"]["value"] == "gpt-test"
    # значения лежат в файле каталога данных, а не в базе
    stored = ss.SecretStore(s.config.data_dir)
    assert stored.get(ss.BOT_TOKEN) == TOKEN and mode(stored.path) == 0o600
    dump = await conn.fetchval(
        """SELECT string_agg(t, ' ') FROM (
             SELECT s::text AS t FROM settings s UNION ALL SELECT a::text FROM setup_audit a
             UNION ALL SELECT e::text FROM executor_state e UNION ALL SELECT j::text FROM jobs j
             UNION ALL SELECT x::text FROM setup_state x UNION ALL SELECT y::text FROM setup_sessions y) q""") or ""
    assert not any(secret in dump for secret in SECRETS)
    assert not any(secret in caplog.text for secret in SECRETS)
    assert not any(secret in repr(s.state.config) for secret in SECRETS)
    assert not any(secret in repr(s.state.extras["setup_page"]) for secret in SECRETS)


async def test_values_from_the_server_settings_cannot_be_changed_on_the_page(stand, conn):
    s = await stand(bot_token=TOKEN, llm_api_key=LLM_KEY, llm_model="gpt-env", tg_api_id=42, tg_api_hash=API_HASH,
                    locked=frozenset({ss.BOT_TOKEN, ss.LLM_API_KEY, ss.LLM_MODEL, ss.TG_API_ID, ss.TG_API_HASH}))
    await s.page.login(conn)
    state = (await s.page.get("/state")).json()
    assert state["bot"]["source"] == "server" and state["bot"]["editable"] is False
    assert state["tg"]["keys"] == {"configured": True, "source": "server", "editable": False}
    assert state["llm"]["key"] == {"set": True, "source": "server", "editable": False}
    other = "7000000009:OTHER-token-000000000000000000000"
    for response in (
        await s.page.post("/bot/token", {"token": other}), await s.page.post("/bot/token", {"token": other, "separate": True}),
        await s.page.delete("/bot/token"), await s.page.put("/tg/keys", {"api_id": "7", "api_hash": "f" * 32}),
        await s.page.delete("/tg/keys"), await s.page.put("/llm", {"api_key": "sk-other", "model": "m"}),
        await s.page.delete("/llm"),
    ):
        assert response.status_code == 422 and response.json()["code"] == "locked", response.text
        assert "настройках сервера" in response.json()["error"]
    assert s.state.config.bot_token == TOKEN and s.state.config.llm_model == "gpt-env" and s.state.config.tg_api_id == 42
    assert not ss.SecretStore(s.config.data_dir).path.exists()


async def test_saving_a_bot_token_on_the_page_does_not_enable_sending(stand, conn):
    """Отправка включается только окружением сервиса. Со страницы — ни прямо, ни через токен бота."""
    s = await stand(sending=False)
    await s.page.login(conn)
    assert (await save_bot(s)).status_code == 200
    await until(lambda: bridge.owns_bot())
    assert s.state.config.own_bot is True and s.state.config.sending is False
    assert (await s.api.get("/api/status")).json()["sending"] is False
    assert (await s.page.get("/state")).json()["sending"] is False
    manager = s.manager
    assert manager.config.sending is False and manager.can_send(1) is False
    # ни один маршрут страницы не принимает ничего похожего на выключатель отправки
    for body in ({"sending": True}, {"token": TOKEN, "separate": True, "sending": "on"}):
        await s.page.post("/bot/token", body)
    await s.page.put("/llm", {"api_key": LLM_KEY, "model": "m", "sending": True, "send_daily_hard_cap": 999})
    assert s.state.config.sending is False and s.state.config.send_daily_hard_cap == s.config.send_daily_hard_cap
    assert "sending" not in ss.SecretStore(s.config.data_dir).path.read_text()
    assert API_AUTH


def test_bot_bind_command_sees_a_token_entered_on_the_page(monkeypatch, tmp_path):
    from shturman.executor import commands

    monkeypatch.delenv("SHTURMAN_BOT_TOKEN", raising=False)
    monkeypatch.setenv("SHTURMAN_DATA_DIR", str(tmp_path))
    assert commands._bot_configured() is False
    ss.SecretStore(tmp_path).update({ss.BOT_TOKEN: TOKEN})
    assert commands._bot_configured() is True
    ss.SecretStore(tmp_path).update({ss.BOT_TOKEN: None})
    monkeypatch.setenv("SHTURMAN_BOT_TOKEN", TOKEN)
    assert commands._bot_configured() is True
