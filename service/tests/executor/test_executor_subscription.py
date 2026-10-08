"""Подписка ChatGPT как своя модель сервиса: вход вставкой адреса, токены, Responses API.

В сеть не ходит: OpenAI подставной (`openai_fakes.FakeOpenAI`)."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import stat
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from shturman import bridge, jobs
from shturman.executor import siwc, subscription
from shturman.executor.llm import Attachment, LlmClient, LlmError
from shturman.executor.subscription import ChatGptClient
from shturman.executor.worker import Worker

from exec_fakes import FakeLlm, no_sleep
from openai_fakes import CLIENT_ID, EMAIL, SUB, FakeOpenAI, api_error, completed, failed, form_of

SCHEMA = {"type": "object", "required": ["items"], "additionalProperties": False,
          "properties": {"items": {"type": "array", "items": {"type": "string"}}}}


@pytest.fixture(autouse=True)
def _forget_fallbacks():
    subscription._NO_FORMAT.clear()
    subscription._NO_SCHEMA.clear()
    yield
    subscription._NO_FORMAT.clear()
    subscription._NO_SCHEMA.clear()


@pytest.fixture
def store(tmp_path):
    return siwc.CredentialStore(tmp_path / "data")


@pytest.fixture
def fake():
    return FakeOpenAI()


def keeper_for(store, fake) -> siwc.TokenKeeper:
    return siwc.TokenKeeper(store, transport=fake.transport())


async def sign_in(store, fake, *, expires_in: int | None = None) -> dict:
    """Полный вход: попытка, «браузер», вставка, обмен, проверка — как делает страница."""
    attempt, url = siwc.new_attempt(store)
    address = fake.authorize(url)
    code, client_id = siwc.check_callback(attempt, siwc.parse_callback(address))
    async with siwc.http_client(transport=fake.transport()) as http:
        tokens = await siwc.exchange(http, attempt, code, client_id)
        claims = await siwc.verify_id_token(http, tokens["id_token"], client_id=client_id, nonce=attempt.nonce)
    record = siwc.record_from_tokens(tokens, claims, client_id=client_id, host_id=attempt.host_id, previous={})
    record["model"] = "gpt-6.1-sol"
    if expires_in is not None:
        record["expires_at"] = time.time() + expires_in
    store.save(record)
    return record


def client(keeper, **kw) -> ChatGptClient:
    kw.setdefault("model", "gpt-6.1-sol")
    return ChatGptClient(keeper, transport=keeper.transport, sleep=no_sleep, **kw)


# --- адрес входа ---

def test_first_authorize_url_registers_a_dynamic_client_with_a_persistent_host_id(store):
    attempt, url = siwc.new_attempt(store)
    params = form_of(url)
    assert url.startswith("https://auth.openai.com/api/accounts/authorize?")
    assert params["client_id"] == "dynamic_agent_client" and params["agent_name_hint"] == "Shturman"
    assert params["response_type"] == "code" and params["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
    assert params["scope"] == "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
    assert params["resource"] == "https://api.openai.com/v1" and params["code_challenge_method"] == "S256"
    assert params["state"] == attempt.state and params["nonce"] == attempt.nonce
    challenge = base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest()).rstrip(b"=").decode()
    assert params["code_challenge"] == challenge and attempt.verifier not in url
    assert params["ext_agent_host_id"].startswith("urn:uuid:") and attempt.client_id is None
    assert "id_token_hint" not in params and "login_hint" not in params and "prompt" not in params
    # Номер сервера постоянный: файл 600, при следующей попытке — тот же.
    assert stat.S_IMODE(store.host_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.host_path.parent.stat().st_mode) == 0o700
    again, _ = siwc.new_attempt(store)
    assert again.host_id == params["ext_agent_host_id"] and again.state != attempt.state
    assert "verifier" not in repr(attempt) and attempt.state not in repr(attempt)


async def test_repeat_authorize_uses_the_issued_client_id_and_hints(store, fake):
    await sign_in(store, fake)
    attempt, url = siwc.new_attempt(store)
    params = form_of(url)
    assert params["client_id"] == CLIENT_ID and attempt.client_id == CLIENT_ID and attempt.subject == SUB
    assert "agent_name_hint" not in params and params["login_hint"] == EMAIL and params["id_token_hint"]
    fresh, url = siwc.new_attempt(store, new_registration=True)
    assert form_of(url)["client_id"] == "dynamic_agent_client" and fresh.client_id is None


# --- вставленный адрес ---

@pytest.mark.parametrize("pasted", [
    "http://127.0.0.1:1455/auth/callback?code=C1&state=S1&client_id=oaiapp_x&scope=openid+email",
    "  http://127.0.0.1:1455/auth/callback/?code=C1&state=S1&client_id=oaiapp_x\n",
    "?code=C1&state=S1&client_id=oaiapp_x",
    "code=C1&state=S1&client_id=oaiapp_x",
    "127.0.0.1:1455/auth/callback?code=C1&state=S1&client_id=oaiapp_x",
])
def test_pasted_address_is_read_whole_or_as_its_query(pasted):
    params = siwc.parse_callback(pasted)
    assert params["code"] == "C1" and params["state"] == "S1" and params["client_id"] == "oaiapp_x"


@pytest.mark.parametrize("pasted, code", [
    ("", "empty"), (None, "empty"), ("   ", "empty"),
    ("https://chatgpt.com/settings?code=C1&state=S1", "not_callback"),
    ("http://127.0.0.1:1455/callback?code=C1&state=S1", "not_callback"),
    ("http://127.0.0.1:1455/auth/callback", "no_params"),
    ("code=C1&code=C2&state=S1", "bad_address"),
    ("code=C1\x00&state=S1", "bad_address"),
    ("x" * 9000, "bad_address"),
    ("просто текст", "no_params"),
])
def test_pasted_address_garbage_is_refused(pasted, code):
    with pytest.raises(siwc.SiwcError) as caught:
        siwc.parse_callback(pasted)
    assert caught.value.code == code


def test_callback_checks_state_error_and_client_id(store):
    attempt, _ = siwc.new_attempt(store)
    good = {"code": "C1", "state": attempt.state, "client_id": "oaiapp_new"}
    assert siwc.check_callback(attempt, good) == ("C1", "oaiapp_new")

    def code_of(params, att=attempt, **kw):
        with pytest.raises(siwc.SiwcError) as caught:
            siwc.check_callback(att, params, **kw)
        return caught.value.code

    assert code_of({**good, "state": "чужой"}) == "wrong_state" and attempt.tries == 1
    assert code_of(good, now=attempt.created + siwc.ATTEMPT_TTL + 1) == "expired"
    assert code_of({"error": "access_denied", "state": attempt.state}) == "denied"
    # Ошибка с чужим state — сначала state: чужой ответ не снимает попытку как «отказ».
    assert code_of({"error": "access_denied", "state": "x"}) == "wrong_state"
    assert code_of({"error": "server_error", "state": attempt.state}) == "oauth_error:server_error"
    assert code_of({"state": attempt.state, "client_id": "oaiapp_new"}) == "no_code"
    assert code_of({"code": "C1", "state": attempt.state}) == "no_client_id"            # первая регистрация
    assert code_of({**good, "client_id": "dynamic_agent_client"}) == "no_client_id"
    assert code_of({**good, "client_id": "bad id with spaces"}) == "bad_client_id"
    assert code_of(good, att=None) == "no_attempt"


def test_reauth_callback_may_omit_client_id_but_may_not_change_it(store):
    attempt = siwc.Attempt(state="S", nonce="N", verifier="V", host_id="urn:uuid:x", client_id="oaiapp_saved",
                           subject="sub")
    assert siwc.check_callback(attempt, {"code": "C", "state": "S"}) == ("C", "oaiapp_saved")
    assert siwc.check_callback(attempt, {"code": "C", "state": "S", "client_id": "oaiapp_saved"})[1] == "oaiapp_saved"
    with pytest.raises(siwc.SiwcError) as caught:
        siwc.check_callback(attempt, {"code": "C", "state": "S", "client_id": "oaiapp_other"})
    assert caught.value.code == "client_mismatch"


# --- обмен кода и ID token ---

async def test_exchange_and_id_token_validation_with_signature(store, fake):
    record = await sign_in(store, fake)
    assert record["client_id"] == CLIENT_ID and record["subject"] == SUB and record["email"] == EMAIL
    assert record["status"] == "active" and siwc.plan_allowed(record)
    form = fake.token_forms[0]
    assert form["grant_type"] == "authorization_code" and form["client_id"] == CLIENT_ID
    assert form["redirect_uri"] == "http://127.0.0.1:1455/auth/callback" and "client_secret" not in form
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.status() == "active" and repr(store) == "<CredentialStore active>"


async def test_id_token_with_wrong_nonce_audience_issuer_or_signature_is_refused(fake):
    async with siwc.http_client(transport=fake.transport()) as http:
        async def code_of(token, **kw):
            kw.setdefault("client_id", CLIENT_ID)
            kw.setdefault("nonce", "N")
            with pytest.raises(siwc.SiwcError) as caught:
                await siwc.verify_id_token(http, token, **kw)
            return caught.value.code

        good = fake.id_token(client_id=CLIENT_ID, nonce="N")
        assert (await siwc.verify_id_token(http, good, client_id=CLIENT_ID, nonce="N"))["_signature_checked"] is True
        assert await code_of(good, nonce="другой") == "id_token_nonce"
        assert await code_of(good, client_id="oaiapp_other") == "id_token_audience"
        assert await code_of(fake.id_token(client_id=CLIENT_ID, nonce="N", iss="https://evil.example")) == "id_token_issuer"
        assert await code_of(fake.id_token(client_id=CLIENT_ID, nonce="N", exp_in=-3600)) == "id_token_expired"
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        assert await code_of(fake.id_token(client_id=CLIENT_ID, nonce="N", key=other)) == "bad_id_token_signature"
        assert await code_of("не.токен") == "bad_id_token"
        fake.jwks_down = True       # ключи не получены: остаётся проверка по TLS (OIDC Core 3.1.3.7)
        assert (await siwc.verify_id_token(http, good, client_id=CLIENT_ID, nonce="N"))["_signature_checked"] is False


async def test_code_exchange_refusal_is_reported_without_values(store, fake, caplog):
    attempt, url = siwc.new_attempt(store)
    address = fake.authorize(url)
    code, client_id = siwc.check_callback(attempt, siwc.parse_callback(address))
    fake.codes.clear()                  # код уже использован
    caplog.set_level(logging.DEBUG)
    async with siwc.http_client(transport=fake.transport()) as http:
        with pytest.raises(siwc.SiwcError) as caught:
            await siwc.exchange(http, attempt, code, client_id)
    assert caught.value.code == "code_rejected" and code not in caplog.text and "SENTINEL" not in caplog.text


# --- обновление токенов ---

async def test_two_concurrent_requests_refresh_once_and_the_new_refresh_token_is_on_disk_first(store, fake):
    old = await sign_in(store, fake, expires_in=60)          # меньше пяти минут — пора обновлять
    keeper = keeper_for(store, fake)
    fake.refresh_delay = 0.05
    tokens = await asyncio.gather(keeper.access_token(), keeper.access_token(), keeper.access_token())
    assert len(set(tokens)) == 1 and tokens[0] != old["access_token"]
    assert [f["grant_type"] for f in fake.token_forms].count("refresh_token") == 1 and keeper.refreshes == 1
    saved = store.load()
    assert saved["refresh_token"] != old["refresh_token"] and saved["refresh_token"] in fake.refresh
    assert saved["access_token"] == tokens[0] and saved["expires_at"] > time.time() + 3000
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert await keeper.access_token() == tokens[0]           # свежий — не обновляется
    assert keeper.refreshes == 1


async def test_rejected_token_is_refreshed_only_if_nobody_did_it_already(store, fake):
    record = await sign_in(store, fake)
    keeper = keeper_for(store, fake)
    first = await keeper.access_token(failed=record["access_token"])
    assert first != record["access_token"] and keeper.refreshes == 1
    assert await keeper.access_token(failed=record["access_token"]) == first and keeper.refreshes == 1


async def test_earliest_refresh_at_is_respected_while_the_token_still_works(store, fake):
    record = await sign_in(store, fake, expires_in=120)
    record["earliest_refresh_at"] = time.time() + 60
    store.save(record)
    keeper = keeper_for(store, fake)
    assert await keeper.access_token() == record["access_token"] and keeper.refreshes == 0
    record["expires_at"] = time.time() - 1                    # истёк — обновляем, что бы ни просили
    store.save(record)
    assert await keeper.access_token() != record["access_token"] and keeper.refreshes == 1


@pytest.mark.parametrize("error", ["invalid_grant", "refresh_token_expired", "refresh_token_reused",
                                   "refresh_token_invalidated", "token_expired", "invalid_refresh_token"])
async def test_terminal_refresh_error_asks_to_sign_in_again_keeping_client_and_host(store, fake, error, caplog):
    await sign_in(store, fake, expires_in=10)
    host = store.host_id()
    fake.token_script = [httpx.Response(400, json={"error": error, "error_description": "nope"})]
    keeper = keeper_for(store, fake)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(siwc.NeedLogin):
        await keeper.access_token()
    saved = store.load()
    assert store.status() == "relogin" and saved["client_id"] == CLIENT_ID and saved["email"] == EMAIL
    assert "access_token" not in saved and "refresh_token" not in saved and saved.get("id_token")
    assert store.host_id() == host and "SENTINEL" not in caplog.text
    with pytest.raises(siwc.NeedLogin):
        await keeper.access_token()
    # Следующий вход — с выданным client_id, тем же номером сервера и подсказкой учётной записи.
    attempt, url = siwc.new_attempt(store)
    assert form_of(url)["client_id"] == CLIENT_ID and form_of(url)["ext_agent_host_id"] == host


@pytest.mark.parametrize("trouble", [httpx.Response(503), httpx.Response(500, json={"error": "server_error"}),
                                     httpx.ConnectError("нет связи"), httpx.ReadTimeout("долго")])
async def test_transient_refresh_failure_keeps_the_credentials(store, fake, trouble):
    old = await sign_in(store, fake, expires_in=10)
    fake.token_script = [trouble]
    keeper = keeper_for(store, fake)
    with pytest.raises(siwc.Transient):
        await keeper.access_token()
    assert store.load()["refresh_token"] == old["refresh_token"] and store.status() == "active"
    assert await keeper.access_token() != old["access_token"]        # следующая попытка удаётся


async def test_sign_out_revokes_and_keeps_registration(store, fake):
    old = await sign_in(store, fake)
    keeper = keeper_for(store, fake)
    assert await keeper.sign_out() is True
    assert fake.revoked == [old["refresh_token"]]
    saved = store.load()
    assert store.status() == "signed_out" and saved["client_id"] == CLIENT_ID
    assert not any(k in saved for k in ("access_token", "refresh_token", "id_token"))
    _, url = siwc.new_attempt(store)
    assert "id_token_hint" not in form_of(url) and form_of(url)["client_id"] == CLIENT_ID


# --- Responses API ---

async def test_request_shape_follows_the_preview_rules(store, fake):
    await sign_in(store, fake)
    api = client(keeper_for(store, fake), models={"shturman_watch": "gpt-6.1-mini"})
    text, model = await api.chat([{"role": "system", "content": "Будь краток."},
                                  {"role": "user", "content": "Привет"},
                                  {"role": "assistant", "content": "Здравствуйте"},
                                  {"role": "user", "content": "Как дела?"}], task="shturman_watch", max_tokens=700)
    assert (text, model) == ("да", "gpt-6.1-mini")
    body = fake.responses[-1]
    assert body["store"] is False and body["stream"] is True and body["model"] == "gpt-6.1-mini"
    assert body["instructions"] == "Будь краток."
    assert body["input"] == [{"role": "user", "content": "Привет"}, {"role": "assistant", "content": "Здравствуйте"},
                             {"role": "user", "content": "Как дела?"}]
    for field in ("max_output_tokens", "temperature", "top_p", "metadata", "user", "truncation", "previous_response_id"):
        assert field not in body
    assert all(item["role"] != "system" for item in body["input"])
    headers = fake.response_headers[-1]
    assert headers["authorization"].startswith("Bearer at-SENTINEL") and headers["accept"] == "text/event-stream"
    await api.aclose()


async def test_text_comes_from_completed_event_and_deltas_are_a_fallback(store, fake):
    await sign_in(store, fake)
    api = client(keeper_for(store, fake))
    fake.responses_script = [completed("целиком", deltas=False)]
    assert (await api.chat([{"role": "user", "content": "x"}]))[0] == "целиком"
    body = (b'data: {"type":"response.output_text.delta","delta":"\xd1\x87\xd0\xb0"}\n\n'
            b'data: {"type":"response.output_text.delta","delta":"\xd1\x81\xd1\x82\xd0\xb8"}\n\n'
            b'data: {"type":"response.completed","response":{"model":"gpt-6.1-sol","output":[]}}\n\n')
    fake.responses_script = [httpx.Response(200, content=body)]
    assert await api.chat([{"role": "user", "content": "x"}]) == ("части", "gpt-6.1-sol")
    await api.aclose()


async def test_stream_without_completed_or_incomplete_is_an_error(store, fake):
    await sign_in(store, fake)
    api = client(keeper_for(store, fake))
    cut = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"half"}\n\n'
    fake.responses_script = [httpx.Response(200, content=cut)]
    with pytest.raises(LlmError) as caught:
        await api.chat([{"role": "user", "content": "x"}])
    assert caught.value.code == "stream_cut" and not caught.value.final and api.ready()
    incomplete = (b'data: {"type":"response.incomplete","response":{"incomplete_details":'
                  b'{"reason":"max_output_tokens"}}}\n\n')
    fake.responses_script = [httpx.Response(200, content=incomplete)]
    with pytest.raises(LlmError) as caught:
        await api.chat([{"role": "user", "content": "x"}])
    assert caught.value.code == "incomplete:max_output_tokens"
    await api.aclose()


@pytest.mark.parametrize("answer", [failed("subscription_sharing_usage_limit_exceeded"),
                                    api_error(429, "subscription_sharing_usage_limit_exceeded")])
async def test_usage_limit_pauses_new_requests(store, fake, answer):
    await sign_in(store, fake)
    now = [1000.0]
    api = client(keeper_for(store, fake), clock=lambda: now[0])
    fake.responses_script = [answer]
    with pytest.raises(LlmError) as caught:
        await api.chat([{"role": "user", "content": "x"}])
    assert caught.value.code == "usage_limit" and caught.value.pause == subscription.LIMIT_PAUSE
    assert not api.ready() and api.status() == "limit"
    sent = len(fake.responses)
    with pytest.raises(LlmError) as caught:                  # на паузе — в OpenAI не ходим
        await api.chat([{"role": "user", "content": "x"}])
    assert caught.value.code == "usage_limit" and len(fake.responses) == sent
    now[0] += subscription.LIMIT_PAUSE + 1
    assert api.ready() and (await api.chat([{"role": "user", "content": "x"}]))[0] == "да"
    await api.aclose()


@pytest.mark.parametrize("answer, code", [(api_error(403, "subscription_sharing_user_not_eligible"),
                                           "denied:subscription_sharing_user_not_eligible"),
                                          (api_error(403), "denied:forbidden"),
                                          (failed("subscription_sharing_user_not_eligible"),
                                           "denied:subscription_sharing_user_not_eligible")])
async def test_not_eligible_or_region_refusal_is_final_and_not_repeated(store, fake, answer, code):
    await sign_in(store, fake)
    api = client(keeper_for(store, fake))
    fake.responses_script = [answer]
    with pytest.raises(LlmError) as caught:
        await api.chat([{"role": "user", "content": "x"}])
    assert caught.value.code == code and caught.value.final and api.status() == "denied" and not api.ready()
    assert len(fake.responses) == 1 and store.status() == "active"          # вход не трогаем
    await api.aclose()


async def test_401_refreshes_once_then_asks_to_sign_in_again(store, fake):
    record = await sign_in(store, fake)
    keeper = keeper_for(store, fake)
    api = client(keeper)
    fake.access.discard(record["access_token"])               # сервер больше не принимает этот токен
    assert (await api.chat([{"role": "user", "content": "x"}]))[0] == "да" and keeper.refreshes == 1
    fake.responses_script = [api_error(401, "subscription_sharing_invalid_user")] * 2
    with pytest.raises(LlmError) as caught:
        await api.chat([{"role": "user", "content": "x"}])
    assert caught.value.code == "relogin" and caught.value.pause and api.status() == "relogin"
    assert store.status() == "relogin" and store.load()["client_id"] == CLIENT_ID and not api.ready()


async def test_503_is_retried_with_backoff_and_credentials_kept(store, fake):
    await sign_in(store, fake)
    api = client(keeper_for(store, fake))
    fake.responses_script = [api_error(503, "subscription_sharing_usage_unavailable"), api_error(503)]
    assert (await api.chat([{"role": "user", "content": "x"}]))[0] == "да" and len(fake.responses) == 3
    fake.responses_script = [api_error(503)] * 3
    with pytest.raises(LlmError) as caught:
        await api.chat([{"role": "user", "content": "x"}])
    assert caught.value.code.startswith("http_503") and not caught.value.final and caught.value.pause is None
    assert store.status() == "active"
    await api.aclose()


async def test_structured_output_uses_json_schema_and_falls_back_to_instructions(store, fake):
    await sign_in(store, fake)
    api = client(keeper_for(store, fake))
    ask = [{"role": "system", "content": "JSON only"}, {"role": "user", "content": "схема в тексте"}]
    fake.responses_script = [completed('{"items": []}')]
    await api.chat(ask, json_mode=True, schema=SCHEMA, schema_name="commitments v2")
    assert fake.responses[-1]["text"] == {"format": {"type": "json_schema", "name": "commitments_v2",
                                                     "schema": SCHEMA, "strict": True}}
    fake.responses_script = [api_error(400, "subscription_sharing_unsupported_capability", "text.format")]
    assert (await api.chat(ask, json_mode=True, schema=SCHEMA, schema_name="c"))[0] == "да"
    assert "text" in fake.responses[-2] and "text" not in fake.responses[-1]
    assert fake.responses[-1]["instructions"] == "JSON only"
    count = len(fake.responses)
    await api.chat(ask, json_mode=True, schema=SCHEMA, schema_name="c")         # отказ запомнен
    assert len(fake.responses) == count + 1 and "text" not in fake.responses[-1]
    other = client(keeper_for(store, fake))                                    # и для других клиентов
    await other.chat(ask, json_mode=True, schema=SCHEMA, schema_name="c")
    assert "text" not in fake.responses[-1]
    await api.aclose()
    await other.aclose()


async def test_schema_refused_by_strict_mode_falls_back_for_that_schema_only(store, fake):
    await sign_in(store, fake)
    api = client(keeper_for(store, fake))
    ask = [{"role": "user", "content": "x"}]
    fake.responses_script = [api_error(400, "invalid_json_schema", "text.format.schema")]
    await api.chat(ask, json_mode=True, schema={"type": "object"}, schema_name="loose")
    assert "text" in fake.responses[-2] and "text" not in fake.responses[-1]
    await api.chat(ask, json_mode=True, schema=SCHEMA, schema_name="strict")
    assert fake.responses[-1]["text"]["format"]["name"] == "strict"
    fake.responses_script = [api_error(400, "invalid_request_error", "input")]
    with pytest.raises(LlmError) as caught:
        await api.chat(ask, json_mode=True, schema=SCHEMA, schema_name="strict")
    assert caught.value.final and caught.value.code == "http_400:invalid_request_error"
    await api.aclose()


async def test_no_tokens_or_texts_in_logs(store, fake, caplog):
    caplog.set_level(logging.DEBUG)
    record = await sign_in(store, fake, expires_in=10)
    api = client(keeper_for(store, fake))
    fake.responses_script = [api_error(503), failed("subscription_sharing_usage_limit_exceeded")]
    with pytest.raises(LlmError):
        await api.chat([{"role": "user", "content": "секретная переписка"}])
    text = caplog.text
    assert "SENTINEL" not in text and "секретная" not in text and record["id_token"] not in text
    assert "req_failed" in text and "usage_limit_exceeded" in text
    await api.aclose()


# --- вложения ---

IMAGE = Attachment(kind="image", mime="image/png", name="фото.png", data=b"\x89PNG-bytes")
PDF = Attachment(kind="file", mime="application/pdf", name="смета.pdf", data=b"%PDF-1.7 bytes")


async def test_attachments_go_with_the_last_user_message_for_the_subscription(store, fake):
    await sign_in(store, fake)
    api = client(keeper_for(store, fake))
    await api.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "раньше"},
                    {"role": "user", "content": "что на фото?"}], attachments=[IMAGE, PDF])
    items = fake.responses[-1]["input"]
    assert items[0] == {"role": "user", "content": "раньше"}
    assert items[1] == {"role": "user", "content": [
        {"type": "input_text", "text": "что на фото?"},
        {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(IMAGE.data).decode(),
         "detail": "auto"},
        {"type": "input_file", "filename": "смета.pdf",
         "file_data": "data:application/pdf;base64," + base64.b64encode(PDF.data).decode()}]}
    assert "PNG-bytes" not in repr(IMAGE) and "фото" not in repr(IMAGE)
    with pytest.raises(LlmError) as caught:
        await api.chat([{"role": "user", "content": "x"}], attachments=[Attachment("audio", "audio/ogg", "a", b"")])
    assert caught.value.code == "bad_attachment" and caught.value.final
    await api.aclose()


async def test_attachments_for_chat_completions():
    fake_llm = FakeLlm("ок")
    api = LlmClient(base_url="https://llm.example/v1", api_key="k", model="m", transport=fake_llm.transport(),
                    sleep=no_sleep)
    await api.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "что тут?"}],
                   max_tokens=10, attachments=[IMAGE, PDF])
    messages = fake_llm.requests[-1]["messages"]
    assert messages[0] == {"role": "system", "content": "s"}
    assert messages[1]["content"] == [
        {"type": "text", "text": "что тут?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(IMAGE.data).decode()}},
        {"type": "file", "file": {"filename": "смета.pdf",
                                  "file_data": "data:application/pdf;base64," + base64.b64encode(PDF.data).decode()}}]
    await api.chat([{"role": "user", "content": "без вложений"}], max_tokens=10)
    assert fake_llm.requests[-1]["messages"] == [{"role": "user", "content": "без вложений"}]
    assert api.ready() is True
    await api.aclose()


# --- исполнитель заданий ---

@pytest.fixture
async def rig(make_client, store, fake):
    _, state = await make_client("shturman.api_core")
    bridge.set_builtin({bridge.LLM_STRUCTURED, bridge.LLM_TEXT})
    await sign_in(store, fake)
    api = client(keeper_for(store, fake))
    yield state, api, Worker(state, llm=api, sleep=no_sleep)
    bridge.set_builtin(())
    await api.aclose()


async def test_worker_runs_structured_jobs_through_the_subscription(conn, rig, fake):
    state, api, worker = rig
    fake.answer = '{"items": ["смета"]}'
    job = await bridge.request_structured(conn, handler="t", instructions="Выпиши обещания.", input="Иван: пришлю смету",
                                          json_schema=SCHEMA, schema_name="commitments", max_tokens=700)
    assert await worker.run_once("llm") == 1
    row = await conn.fetchrow("SELECT status, result FROM jobs WHERE id = $1", job)
    result = json.loads(row["result"])
    assert row["status"] == "done" and result["parsed"] == {"items": ["смета"]} and result["schema_valid"] is True
    body = fake.responses[-1]
    assert body["text"]["format"]["name"] == "commitments" and "max_output_tokens" not in body
    assert "Выпиши обещания" in body["input"][0]["content"] and "Respond with a single JSON" in body["instructions"]


async def test_usage_limit_puts_the_job_back_without_spending_attempts(conn, rig, fake):
    state, api, worker = rig
    fake.responses_script = [api_error(429, "subscription_sharing_usage_limit_exceeded")]
    job = await bridge.request_text(conn, handler="t", messages=[{"role": "user", "content": "привет"}])
    assert await worker.run_once("llm") == 1
    row = await conn.fetchrow("SELECT status, attempts, run_after > now() + interval '20 minutes' AS later, error "
                              "FROM jobs WHERE id = $1", job)
    assert row["status"] == "queued" and row["attempts"] == 0 and row["later"] and "usage_limit" in row["error"]
    assert worker.kinds("llm") == () and await worker.run_once("llm") == 0        # на паузе — не забирает
    status = api.status()
    assert status == "limit"


async def test_relogin_keeps_jobs_waiting(conn, rig, fake, store):
    state, api, worker = rig
    fake.responses_script = [api_error(401, "subscription_sharing_invalid_user")] * 2
    job = await bridge.request_text(conn, handler="t", messages=[{"role": "user", "content": "привет"}])
    assert await worker.run_once("llm") == 1
    row = await conn.fetchrow("SELECT status, attempts FROM jobs WHERE id = $1", job)
    assert row["status"] == "queued" and row["attempts"] == 0
    assert store.status() == "relogin" and worker.kinds("llm") == ()


async def test_denied_job_fails_without_retry(conn, rig, fake):
    state, api, worker = rig
    fake.responses_script = [api_error(403, "subscription_sharing_user_not_eligible")]
    job = await bridge.request_text(conn, handler="t", messages=[{"role": "user", "content": "привет"}])
    assert await worker.run_once("llm") == 1
    row = await conn.fetchrow("SELECT status, error FROM jobs WHERE id = $1", job)
    assert row["status"] == "failed" and "not_eligible" in row["error"]


async def test_postpone_returns_the_attempt(conn):
    job = await jobs.enqueue(conn, "llm.text", {"messages": []}, executor="builtin")
    claimed = await jobs.claim(conn, ["llm.text"], worker="w", executor="builtin")
    assert claimed and claimed[0]["attempt"] == 1
    assert await jobs.postpone(conn, job, "лимит", delay=600) == "queued"
    row = await conn.fetchrow("SELECT status, attempts, run_after > now() AS later FROM jobs WHERE id = $1", job)
    assert (row["status"], row["attempts"], row["later"]) == ("queued", 0, True)
    assert await jobs.postpone(conn, job, "ещё", delay=600) == "unknown"        # не забрано — не трогаем


def test_store_ignores_garbage_and_fixes_permissions(store):
    store.dir.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{не json")
    assert store.load() == {} and store.status() == "none"
    store.path.write_text(json.dumps({"client_id": "oaiapp_x", "status": "active", "refresh_token": 5,
                                      "models": [{"slug": "ok-model"}, {"slug": "плохой slug"}, "x"]}))
    os.chmod(store.path, 0o644)
    record = store.load()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert "refresh_token" not in record and store.status() == "relogin"
    assert record["models"] == [{"slug": "ok-model", "display_name": "ok-model"}]
