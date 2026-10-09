"""Страница настройки: своя модель сервиса по подписке ChatGPT — вход вставкой адреса.

OpenAI подставной (`tests/executor/openai_fakes.py`); в сеть тесты не ходят."""

import json
import logging
import stat
import time

import pytest

from setup_fakes import LLM_KEY, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from exec_fakes import until
from openai_fakes import CLIENT_ID, EMAIL, FakeOpenAI, api_error, completed, form_of

from shturman import bridge
from shturman.config import with_subscription
from shturman.executor import service as executor_service
from shturman.executor import siwc
from shturman.executor.subscription import ChatGptClient
from shturman.setup_page import secrets_store as ss


@pytest.fixture
def openai():
    fake = FakeOpenAI()
    executor_service.TEST_OVERRIDES["chatgpt_transport"] = fake.transport()
    return fake


async def opened(stand, conn, **changes):
    s = await stand(**changes)
    await s.page.login(conn)
    return s


async def audit_rows(conn):
    return [tuple(r) for r in await conn.fetch("SELECT action, outcome, detail FROM setup_audit ORDER BY id")]


async def connect(s, openai, **start):
    started = await s.page.post("/llm/chatgpt/start", start)
    assert started.status_code == 200, started.text
    address = openai.authorize(started.json()["url"])
    return await s.page.post("/llm/chatgpt/finish", {"address": address})


async def test_sign_in_by_pasting_the_address_switches_the_service_model_to_the_subscription(stand, conn, openai, caplog):
    caplog.set_level(logging.DEBUG)
    s = await opened(stand, conn)
    before = (await s.page.get("/state")).json()["llm"]
    assert before["way"] is None and before["subscription"]["status"] == "none"
    assert before["subscription"]["available"] is True and before["subscription"]["usage_url"] == "https://chatgpt.com/settings/usage"

    started = await s.page.post("/llm/chatgpt/start", {})
    assert started.status_code == 200 and started.json()["first"] is True and started.json()["minutes"] == 10
    url = started.json()["url"]
    params = form_of(url)
    assert params["client_id"] == "dynamic_agent_client" and params["agent_name_hint"] == "Shturman"
    assert params["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
    pending = (await s.page.get("/state")).json()["llm"]["subscription"]
    assert pending["attempt"] is not None and pending["status"] == "none"

    address = openai.authorize(url)
    done = await s.page.post("/llm/chatgpt/finish", {"address": address})
    assert done.status_code == 200, done.text
    out = done.json()
    assert out["ok"] is True and out["email"] == EMAIL and out["model"] == "gpt-6.1-sol" and out["probe"] == "ok"
    assert [m["slug"] for m in out["models"]] == ["gpt-6.1-sol", "gpt-6.1-mini"]       # скрытые не показываются
    assert openai.responses[-1]["input"] == [{"role": "user", "content": "Ответь одним словом: да"}]

    config = s.state.config
    assert config.chatgpt is True and config.chatgpt_model == "gpt-6.1-sol" and config.own_llm is True
    assert config.llm_way == "subscription"
    assert isinstance(s.state.extras["executor"].llm, ChatGptClient)
    assert bridge.LLM_TEXT in bridge.builtin_kinds()
    state = (await s.page.get("/state")).json()["llm"]
    sub = state["subscription"]
    assert state["way"] == "subscription" and sub["status"] == "connected" and sub["email"] == EMAIL
    assert sub["status_text"] == "Используется подписка ChatGPT." and sub["attempt"] is None
    assert sub["model"] == "gpt-6.1-sol" and sub["registered"] is True

    # Файлы: 600 в каталоге 700; токены — только там.
    store = siwc.CredentialStore(s.config.data_dir)
    record = store.load()
    assert record["client_id"] == CLIENT_ID and record["refresh_token"].startswith("rt-SENTINEL")
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600 and stat.S_IMODE(store.host_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.dir.stat().st_mode) == 0o700

    # Сервис сам выполняет задания модели по подписке.
    job = await bridge.request_text(conn, handler="x", messages=[{"role": "user", "content": "привет"}])
    await until(lambda: conn.fetchval("SELECT status = 'done' FROM jobs WHERE id = $1", job))
    assert openai.responses[-1]["store"] is False

    # Ни токенов, ни кода, ни вставленного адреса — ни в ответах страницы, ни в журналах.
    everything = json.dumps((await s.page.get("/state")).json()) + json.dumps((await s.page.get("/overview")).json())
    everything += done.text + caplog.text + json.dumps(await audit_rows(conn), ensure_ascii=False)
    status = await s.api.get("/api/executor/status")
    everything += status.text
    assert "SENTINEL" not in everything and address not in everything and record["id_token"] not in everything
    assert EMAIL not in status.text                       # маршрут доступен ассистенту в Hermes — почты там нет
    assert status.json()["llm"]["way"] == "subscription" and status.json()["llm"]["subscription"] == "ok"
    rows = await audit_rows(conn)
    assert ("llm.chatgpt_start", "ok", "новая регистрация") in rows
    assert ("llm.chatgpt", "ok", "пробный вопрос: ответ получен") in rows


async def test_service_restart_picks_the_subscription_up_from_the_file(stand, conn, openai, config):
    s = await opened(stand, conn)
    assert (await connect(s, openai)).status_code == 200
    again = with_subscription(config)
    assert again.chatgpt is True and again.chatgpt_model == "gpt-6.1-sol"
    # Ключ модели в окружении главнее: подписка тогда не действует.
    import dataclasses
    locked = with_subscription(dataclasses.replace(config, llm_api_key="sk-env", llm_model="m",
                                                   locked=frozenset({ss.LLM_API_KEY})))
    assert locked.chatgpt is False and locked.llm_way == "api_key"


@pytest.mark.parametrize("pasted", ["full", "query", "bare"])
async def test_address_can_be_pasted_whole_or_as_its_query(stand, conn, openai, pasted):
    s = await opened(stand, conn)
    url = (await s.page.post("/llm/chatgpt/start", {})).json()["url"]
    address = openai.authorize(url)
    if pasted == "query":
        address = address[address.index("?"):]
    elif pasted == "bare":
        address = address[address.index("?") + 1:]
    assert (await s.page.post("/llm/chatgpt/finish", {"address": address})).status_code == 200


async def test_wrong_state_is_refused_and_the_attempt_stays(stand, conn, openai):
    s = await opened(stand, conn)
    first = (await s.page.post("/llm/chatgpt/start", {})).json()["url"]
    stale = openai.authorize(first)
    second = (await s.page.post("/llm/chatgpt/start", {})).json()["url"]      # новая попытка снимает прежнюю
    got = await s.page.post("/llm/chatgpt/finish", {"address": stale})
    assert got.status_code == 422 and got.json()["code"] == "wrong_state" and "не от последней" in got.json()["error"]
    assert not openai.token_forms                                           # до обмена кода не дошло
    assert (await s.page.get("/state")).json()["llm"]["subscription"]["attempt"] is not None
    ok = await s.page.post("/llm/chatgpt/finish", {"address": openai.authorize(second)})
    assert ok.status_code == 200
    rows = await audit_rows(conn)
    assert ("llm.chatgpt", "refused", "адрес не подошёл: wrong_state") in rows


async def test_too_many_wrong_addresses_drop_the_attempt(stand, conn, openai):
    s = await opened(stand, conn)
    url = (await s.page.post("/llm/chatgpt/start", {})).json()["url"]
    for _ in range(siwc.ATTEMPT_TRIES):
        await s.page.post("/llm/chatgpt/finish", {"address": "code=x&state=чужой&client_id=oaiapp_x"})
    got = await s.page.post("/llm/chatgpt/finish", {"address": openai.authorize(url)})
    assert got.json()["code"] == "no_attempt"


@pytest.mark.parametrize("case, code", [
    ("denied", "denied"), ("expired", "expired"), ("no_client_id", "no_client_id"), ("garbage", "not_callback"),
    ("empty", "empty"), ("none", "no_attempt"),
])
async def test_finish_refusals_are_explained(stand, conn, openai, case, code):
    s = await opened(stand, conn)
    if case != "none":
        url = (await s.page.post("/llm/chatgpt/start", {})).json()["url"]
    address = {
        "denied": lambda: openai.authorize(url, deny=True),
        "expired": lambda: openai.authorize(url),
        "no_client_id": lambda: openai.authorize(url, with_client_id=False),
        "garbage": lambda: "https://example.com/?code=1&state=2",
        "empty": lambda: "",
        "none": lambda: "code=1&state=2",
    }[case]()
    if case == "expired":
        s.state.extras["setup_page"].chatgpt_attempt.created -= siwc.ATTEMPT_TTL + 1
    got = await s.page.post("/llm/chatgpt/finish", {"address": address})
    assert got.status_code == 422 and got.json()["code"] == code and got.json()["error"]
    assert s.state.config.chatgpt is False and not openai.token_forms
    if case in ("denied", "expired", "no_client_id"):
        assert (await s.page.get("/state")).json()["llm"]["subscription"]["attempt"] is None


async def test_used_code_is_not_exchanged_twice(stand, conn, openai):
    s = await opened(stand, conn)
    url = (await s.page.post("/llm/chatgpt/start", {})).json()["url"]
    address = openai.authorize(url)
    assert (await s.page.post("/llm/chatgpt/finish", {"address": address})).status_code == 200
    again = await s.page.post("/llm/chatgpt/finish", {"address": address})
    assert again.json()["code"] == "no_attempt"


async def test_without_plan_permission_the_subscription_is_not_switched_on(stand, conn, openai):
    s = await opened(stand, conn)
    openai.scope = "email offline_access openid profile resource.invoke"
    got = await connect(s, openai)
    assert got.status_code == 422 and got.json()["code"] == "no_plan" and "разрешили" in got.json()["error"]
    assert s.state.config.chatgpt is False and openai.revoked
    store = siwc.CredentialStore(s.config.data_dir)
    assert store.status() == "signed_out" and "refresh_token" not in store.load()
    # Следующий вход — с выданным client_id и просьбой о согласии заново.
    params = form_of((await s.page.post("/llm/chatgpt/start", {})).json()["url"])
    assert params["client_id"] == CLIENT_ID and params["prompt"] == "consent" and "agent_name_hint" not in params


async def test_probe_refusal_keeps_the_model_unchanged(stand, conn, openai):
    s = await opened(stand, conn)
    openai.responses_script = [api_error(403, "subscription_sharing_user_not_eligible")]
    got = await connect(s, openai)
    assert got.status_code == 422 and got.json()["code"] == "probe_failed"
    assert "not_eligible" in got.json()["error"] and "не переключена" in got.json()["error"]
    assert s.state.config.chatgpt is False and openai.revoked
    assert siwc.CredentialStore(s.config.data_dir).status() == "signed_out"


async def test_usage_limit_during_the_probe_still_connects_and_says_so(stand, conn, openai):
    s = await opened(stand, conn)
    openai.responses_script = [api_error(429, "subscription_sharing_usage_limit_exceeded")]
    got = await connect(s, openai)
    assert got.status_code == 200 and got.json()["probe"] == "limit" and "Лимит" in got.json()["probe_text"]
    assert s.state.config.chatgpt is True


async def test_env_key_locks_the_subscription(stand, conn, openai):
    s = await opened(stand, conn, llm_api_key="sk-env-key", llm_model="gpt-env", locked=frozenset({ss.LLM_API_KEY}))
    sub = (await s.page.get("/state")).json()["llm"]["subscription"]
    assert sub["available"] is False and "SHTURMAN_LLM_API_KEY" in sub["locked_text"]
    got = await s.page.post("/llm/chatgpt/start", {})
    assert got.status_code == 422 and got.json()["code"] == "locked" and not openai.authorize_params


async def test_one_way_at_a_time_key_and_subscription_replace_each_other(stand, conn, openai):
    s = await opened(stand, conn)
    assert (await s.page.put("/llm", {"api_key": LLM_KEY, "model": "gpt-test"})).status_code == 200
    assert s.state.config.llm_way == "api_key"
    # Подписка заменит ключ — только с подтверждением.
    refused = await s.page.post("/llm/chatgpt/start", {})
    assert refused.status_code == 422 and refused.json()["code"] == "switch_needed"
    assert (await connect(s, openai, switch=True)).status_code == 200
    assert s.state.config.llm_way == "subscription" and s.state.config.llm_api_key == ""
    assert ss.SecretStore(s.config.data_dir).load() == {}                  # ключ удалён с сервера
    rows = await audit_rows(conn)
    assert ("llm.removed", "ok", "заменён подпиской ChatGPT") in rows

    # И обратно: ключ заменит подписку — тоже с подтверждением; сессия у OpenAI отзывается.
    back = await s.page.put("/llm", {"api_key": LLM_KEY, "model": "gpt-test"})
    assert back.status_code == 422 and back.json()["code"] == "switch_needed"
    refresh = siwc.CredentialStore(s.config.data_dir).load()["refresh_token"]
    assert (await s.page.put("/llm", {"api_key": LLM_KEY, "model": "gpt-test", "switch": True})).status_code == 200
    assert s.state.config.llm_way == "api_key" and s.state.config.chatgpt is False
    assert openai.revoked[-1] == refresh
    assert siwc.CredentialStore(s.config.data_dir).status() == "signed_out"
    assert not isinstance(s.state.extras["executor"].llm, ChatGptClient)


async def test_model_is_chosen_from_the_list_given_by_openai(stand, conn, openai):
    s = await opened(stand, conn)
    assert (await connect(s, openai)).status_code == 200
    bad = await s.page.put("/llm/chatgpt/model", {"model": "internal-hidden"})
    assert bad.status_code == 422 and bad.json()["code"] == "unknown_model"
    ok = await s.page.put("/llm/chatgpt/model", {"model": "gpt-6.1-mini"})
    assert ok.status_code == 200 and s.state.config.chatgpt_model == "gpt-6.1-mini"
    assert s.state.extras["executor"].llm.model == "gpt-6.1-mini"
    assert (await s.page.get("/state")).json()["llm"]["subscription"]["model"] == "gpt-6.1-mini"
    # Повторный вход той же учётной записью сохраняет выбранную модель.
    assert (await connect(s, openai)).json()["model"] == "gpt-6.1-mini"
    assert openai.authorize_params[-1]["client_id"] == CLIENT_ID


async def test_sign_out_revokes_and_keeps_the_registration_and_host(stand, conn, openai):
    s = await opened(stand, conn)
    assert (await connect(s, openai)).status_code == 200
    store = siwc.CredentialStore(s.config.data_dir)
    host, refresh = store.host_id(), store.load()["refresh_token"]
    out = await s.page.delete("/llm/chatgpt")
    assert out.status_code == 200 and out.json() == {"ok": True, "revoked": True}
    assert openai.revoked == [refresh] and store.status() == "signed_out" and store.host_id() == host
    assert s.state.config.own_llm is False and bridge.LLM_TEXT not in bridge.builtin_kinds()
    sub = (await s.page.get("/state")).json()["llm"]["subscription"]
    assert sub["status"] == "signed_out" and sub["registered"] is True
    params = form_of((await s.page.post("/llm/chatgpt/start", {})).json()["url"])
    assert params["client_id"] == CLIENT_ID and params["ext_agent_host_id"] == host
    assert "id_token_hint" not in params and params["login_hint"] == EMAIL
    rows = await audit_rows(conn)
    assert ("llm.chatgpt_removed", "ok", "") in rows


async def test_relogin_state_is_shown_and_jobs_wait(stand, conn, openai):
    s = await opened(stand, conn)
    assert (await connect(s, openai)).status_code == 200
    store = siwc.CredentialStore(s.config.data_dir)
    record = store.load()
    record["expires_at"] = time.time() - 10
    store.save(record)
    openai.token_script = [__import__("httpx").Response(400, json={"error": "refresh_token_expired"})]
    job = await bridge.request_text(conn, handler="x", messages=[{"role": "user", "content": "привет"}])
    await until(lambda: conn.fetchval("SELECT error IS NOT NULL FROM jobs WHERE id = $1", job))
    row = await conn.fetchrow("SELECT status, attempts FROM jobs WHERE id = $1", job)
    assert row["status"] == "queued" and row["attempts"] == 0
    sub = (await s.page.get("/state")).json()["llm"]["subscription"]
    assert sub["status"] == "relogin" and "войти заново" in sub["status_text"].lower()
    status = (await s.api.get("/api/executor/status")).json()["llm"]
    assert status["subscription"] == "relogin"
    # Повторный вход возвращает подписку в работу.
    assert (await connect(s, openai)).status_code == 200
    assert (await s.page.get("/state")).json()["llm"]["subscription"]["status"] == "connected"


async def test_subscription_routes_need_a_session(stand, conn, openai):
    s = await stand()
    guest = s.browser()
    for method, path in (("POST", "/llm/chatgpt/start"), ("POST", "/llm/chatgpt/finish"), ("POST", "/llm/chatgpt/cancel"),
                         ("PUT", "/llm/chatgpt/model"), ("DELETE", "/llm/chatgpt")):
        got = await guest.send(method, path)
        assert got.status_code == 401 and got.json()["code"] == "unauthenticated", (method, path)
    # С адреса дашборда (соседний порт того же имени) — отказ даже с ключом сессии.
    await s.page.login(conn)
    got = await s.page.http.post("/shturman-setup/api/llm/chatgpt/start", json={},
                                 headers=s.page.headers(**{"Sec-Fetch-Site": "same-site"}))
    assert got.status_code == 403 and got.json()["code"] == "bad_origin"
    # Внутренний API сервиса таких маршрутов не знает.
    for path in ("/api/llm/chatgpt/start", "/api/chatgpt", "/api/llm/chatgpt"):
        assert (await s.api.post(path, json={})).status_code in (404, 405)


async def test_cancel_drops_the_attempt(stand, conn, openai):
    s = await opened(stand, conn)
    url = (await s.page.post("/llm/chatgpt/start", {})).json()["url"]
    assert (await s.page.post("/llm/chatgpt/cancel", {})).status_code == 200
    got = await s.page.post("/llm/chatgpt/finish", {"address": openai.authorize(url)})
    assert got.json()["code"] == "no_attempt"


async def test_status_text_for_limit(stand, conn, openai):
    s = await opened(stand, conn)
    assert (await connect(s, openai)).status_code == 200
    openai.responses_script = [completed("x"), api_error(429, "subscription_sharing_usage_limit_exceeded")]
    llm = s.state.extras["executor"].llm
    await llm.chat([{"role": "user", "content": "a"}])
    with pytest.raises(Exception):
        await llm.chat([{"role": "user", "content": "b"}])
    sub = (await s.page.get("/state")).json()["llm"]["subscription"]
    assert sub["status"] == "limit" and "Лимит подписки исчерпан" in sub["status_text"]
    assert (await s.api.get("/api/executor/status")).json()["llm"]["subscription"] == "limit"


async def test_failed_relogin_keeps_the_working_subscription(stand, conn, openai):
    """Подписка работает; владелец входит ещё раз, и пробный вопрос получает отказ — прежний вход
    остаётся: токены на месте, своя модель по-прежнему подписка."""
    s = await opened(stand, conn)
    assert (await connect(s, openai)).status_code == 200
    store = siwc.CredentialStore(s.config.data_dir)
    refresh = store.load()["refresh_token"]
    openai.responses_script = [api_error(403, "subscription_sharing_user_not_eligible")]
    got = await connect(s, openai)
    assert got.status_code == 422 and got.json()["code"] == "probe_failed"
    assert store.status() == "active" and store.load()["refresh_token"] == refresh
    assert s.state.config.chatgpt is True and refresh not in openai.revoked
