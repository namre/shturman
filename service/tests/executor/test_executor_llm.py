"""Своя модель сервиса: вид запроса, режим JSON, имя предела длины, повторы и журнал."""

import json
import logging

import httpx
import pytest

from shturman import bridge
from shturman.executor.llm import LlmClient, LlmError, parse_json, structured_messages, task_models
from shturman.executor.worker import Worker

from exec_fakes import FakeLlm, no_sleep

KEY = "sk-SENTINEL-llm-key-DoNotLeak"
SCHEMA = {"type": "object", "required": ["items"], "additionalProperties": False,
          "properties": {"items": {"type": "array", "items": {"type": "string"}}}}


def client(fake, *, base_url="https://llm.example/v1", **kwargs) -> LlmClient:
    kwargs.setdefault("models", {"shturman_watch": "cheap-model"})
    return LlmClient(base_url=base_url, api_key=KEY, model="main-model", transport=fake.transport(),
                     sleep=no_sleep, **kwargs)


def unsupported(param: str, other: str) -> httpx.Response:
    return httpx.Response(400, json={"error": {
        "message": f"Unsupported parameter: '{param}' is not supported with this model. Use '{other}' instead.",
        "type": "invalid_request_error", "param": param, "code": "unsupported_parameter"}})


@pytest.fixture
def results():
    """Собирает итоги заданий, как это делал бы модуль-владелец."""
    seen = {}

    @bridge.on_result("t.llm")
    async def done(conn, job, result):
        seen["result"] = result

    @bridge.on_failure("t.llm")
    async def failed(conn, job, error):
        seen["error"] = error

    return seen


@pytest.fixture
async def worker(make_client):
    _, state = await make_client("shturman.api_core")
    bridge.set_builtin({bridge.LLM_STRUCTURED, bridge.LLM_TEXT})
    made = []

    def build(fake, **kwargs) -> Worker:
        made.append(client(fake, **kwargs))
        return Worker(state, llm=made[-1], sleep=no_sleep)

    yield build
    bridge.set_builtin(())
    for item in made:
        await item.aclose()


async def structured(conn, **extra) -> int:
    return await bridge.request_structured(
        conn, handler="t.llm", instructions="Выпиши обещания из переписки.", input="Иван: пришлю смету в пятницу",
        json_schema=SCHEMA, schema_name="commitments", **extra)


# --- вид запроса ---

async def test_structured_request_is_one_json_mode_call_with_the_schema_in_the_text(conn, worker, results):
    fake = FakeLlm('{"items": ["прислать смету"]}')
    job = await structured(conn, max_tokens=700)
    assert await worker(fake).run_once("llm") == 1
    assert len(fake.requests) == 1
    body = fake.requests[0]
    assert body["model"] == "main-model" and body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 700 and "max_completion_tokens" not in body and "temperature" not in body
    system, user = body["messages"]
    assert system["role"] == "system" and "JSON" in system["content"]
    assert user["content"] == (
        "Выпиши обещания из переписки.\n\nJSON schema:\n" + json.dumps(SCHEMA, ensure_ascii=False, sort_keys=True)
        + "\n\nSchema name: commitments\n\nИван: пришлю смету в пятницу")
    assert fake.headers[0]["authorization"] == f"Bearer {KEY}"
    assert results["result"] == {"parsed": {"items": ["прислать смету"]}, "text": '{"items": ["прислать смету"]}',
                                 "model": "main-model", "schema_valid": True}
    assert await conn.fetchval("SELECT status FROM jobs WHERE id = $1", job) == "done"


@pytest.mark.parametrize("answer, parsed, valid", [
    ('{"items": "не список"}', {"items": "не список"}, False),              # JSON есть, схема нарушена
    ('{"other": 1}', {"other": 1}, False),
    ('```json\n{"items": []}\n```', {"items": []}, True),                   # модель обернула ответ
    ("Вот что я нашёл: ничего.", None, False),                              # не JSON
    ('{"items": ["оборвано', None, False),
    ("", None, False),
])
async def test_schema_violation_and_non_json_are_a_success_without_retry(conn, worker, results, answer, parsed, valid):
    fake = FakeLlm(answer)
    job = await structured(conn)
    await worker(fake).run_once("llm")
    assert results["result"]["parsed"] == parsed and results["result"]["schema_valid"] is valid
    assert results["result"]["text"] == answer and len(fake.requests) == 1
    assert await conn.fetchval("SELECT status FROM jobs WHERE id = $1", job) == "done"


async def test_text_request_passes_messages_through_and_reports_the_served_model(conn, worker, results):
    fake = FakeLlm("Добрый день! Смета будет в пятницу.")
    fake.served_model = "main-model-2026-09"
    messages = [{"role": "system", "content": "Ты пишешь коротко."}, {"role": "user", "content": "Ответь Ивану"}]
    await bridge.request_text(conn, handler="t.llm", messages=messages, max_tokens=99999)
    await worker(fake).run_once("llm")
    body = fake.requests[0]
    assert body["messages"] == messages and "response_format" not in body and body["max_tokens"] == 8000
    assert results["result"] == {"text": "Добрый день! Смета будет в пятницу.", "model": "main-model-2026-09"}


async def test_model_is_chosen_by_task_and_unknown_task_gets_the_default(conn, worker):
    fake = FakeLlm("ок")
    run = worker(fake)
    for task in ("shturman_watch", "shturman_reply", "чужая_задача"):
        await bridge.request_text(conn, handler="t.llm", messages=[{"role": "user", "content": "x"}], task=task)
        await run.run_once("llm")
    assert [r["model"] for r in fake.requests] == ["cheap-model", "main-model", "main-model"]
    env = {"SHTURMAN_LLM_MODEL_EXTRACT": " small ", "SHTURMAN_LLM_MODEL_REPLY": "", "SHTURMAN_LLM_MODEL_WATCH": "tiny"}
    assert task_models(env) == {"shturman_extract": "small", "shturman_watch": "tiny"}


async def test_broken_jobs_fail_for_good_without_calling_the_model(conn, worker, results):
    fake = FakeLlm("ок")
    run = worker(fake)
    await bridge.request_text(conn, handler="t.llm", messages=[{"role": "tool", "content": "x"}])
    await run.run_once("llm")
    assert fake.requests == [] and "messages" in results["error"]


# --- имя предела длины ---

async def test_openai_address_starts_with_the_current_parameter_name():
    fake = FakeLlm("ок")
    api = client(fake, base_url="https://api.openai.com/v1")
    await api.chat([{"role": "user", "content": "x"}], max_tokens=50)
    assert fake.requests[0]["max_completion_tokens"] == 50 and "max_tokens" not in fake.requests[0]
    await api.aclose()


async def test_rejected_parameter_name_is_swapped_once_and_remembered_per_model():
    fake = FakeLlm("ок")
    fake.script = [unsupported("max_tokens", "max_completion_tokens")]
    api = client(fake)
    ask = [{"role": "user", "content": "x"}]
    assert (await api.chat(ask, max_tokens=50))[0] == "ок"
    assert [sorted(k for k in r if k.startswith("max_")) for r in fake.requests] == [
        ["max_tokens"], ["max_completion_tokens"]]
    await api.chat(ask, max_tokens=50)                                    # та же модель — сразу верное имя
    await api.chat(ask, task="shturman_watch", max_tokens=50)             # другая модель — своё правило
    assert "max_completion_tokens" in fake.requests[2] and "max_tokens" in fake.requests[3]
    assert len(fake.requests) == 4
    await api.aclose()


async def test_both_names_rejected_is_a_final_failure_after_two_requests():
    fake = FakeLlm("ок")
    fake.script = [unsupported("max_tokens", "max_completion_tokens"),
                   unsupported("max_completion_tokens", "max_tokens")]
    api = client(fake)
    with pytest.raises(LlmError) as caught:
        await api.chat([{"role": "user", "content": "x"}], max_tokens=50)
    assert caught.value.final and caught.value.code == "http_400" and len(fake.requests) == 2
    assert api._param == {}                                              # неудачный выбор не запоминается
    await api.aclose()


async def test_fixed_parameter_name_is_never_swapped():
    fake = FakeLlm("ок")
    fake.script = [unsupported("max_tokens", "max_completion_tokens")]
    api = client(fake, tokens_param="max_tokens")
    with pytest.raises(LlmError):
        await api.chat([{"role": "user", "content": "x"}], max_tokens=50)
    assert len(fake.requests) == 1
    await api.aclose()


async def test_server_without_json_mode_gets_the_same_request_without_it():
    fake = FakeLlm('{"items": []}')
    fake.script = [httpx.Response(400, json={"error": {"message": "response_format is not supported"}})]
    api = client(fake)
    ask = structured_messages("Верни JSON", "текст", None, None)
    assert (await api.chat(ask, max_tokens=50, json_mode=True))[0] == '{"items": []}'
    await api.chat(ask, max_tokens=50, json_mode=True)
    assert ["response_format" in r for r in fake.requests] == [True, False, False]
    await api.aclose()


# --- повторы и сбои ---

async def test_rate_limit_and_server_errors_are_retried_inside_one_call():
    fake = FakeLlm("ок")
    fake.script = [httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "1"}),
                   httpx.Response(503, text="overloaded"), None]
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    api = LlmClient(base_url="https://llm.example/v1", api_key=KEY, model="m", transport=fake.transport(), sleep=sleep)
    assert (await api.chat([{"role": "user", "content": "x"}], max_tokens=5))[0] == "ок"
    assert len(fake.requests) == 3 and waits == [2.0, 5.0] and api.last_ok is True
    await api.aclose()


async def test_connect_errors_are_retried_then_reported_as_retryable():
    fake = FakeLlm("ок")
    fake.script = [httpx.ConnectError("нет сети")] * 3
    api = client(fake)
    with pytest.raises(LlmError) as caught:
        await api.chat([{"role": "user", "content": "x"}], max_tokens=5)
    assert len(fake.requests) == 3 and not caught.value.final and caught.value.code.startswith("connect")
    assert api.failures == 1 and api.last_ok is False
    await api.aclose()


@pytest.mark.parametrize("failure, requests, final", [
    (httpx.ReadTimeout("долго"), 1, False),                 # истёкшее время клиент не повторяет
    (httpx.ReadError("обрыв"), 1, False),
    (httpx.Response(401, json={"error": {"message": "bad key"}}), 1, True),
    (httpx.Response(404, json={"error": {"message": "no such model"}}), 1, True),
    (httpx.Response(200, text="<html>"), 1, False),
])
async def test_failed_call_becomes_a_job_failure_with_or_without_retry(conn, worker, results, failure, requests, final):
    fake = FakeLlm("ок")
    fake.script = [failure]
    job = await structured(conn)
    await worker(fake).run_once("llm")
    row = await conn.fetchrow("SELECT status, error, run_after > now() AS later FROM jobs WHERE id = $1", job)
    assert len(fake.requests) == requests
    assert row["status"] == ("failed" if final else "queued") and bool(row["later"]) is not final
    assert row["error"].startswith("модель: ")
    assert ("error" in results) is final


async def test_hanging_model_is_a_timeout_failure_with_retry(conn, worker):
    import asyncio

    async def hang(body):
        await asyncio.sleep(5)

    fake = FakeLlm("ок")
    fake.script = [hang]
    run = worker(fake)
    run.llm_timeout = 0.05
    job = await structured(conn)
    await run.run_once("llm")
    row = await conn.fetchrow("SELECT status, error, run_after > now() AS later FROM jobs WHERE id = $1", job)
    assert row["status"] == "queued" and row["later"] and row["error"] == "модель: timeout"
    assert len(fake.requests) == 1


async def test_two_calls_at_most_run_at_the_same_time(conn, worker):
    import asyncio

    running = peak = 0

    async def slow(request):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return FakeLlm.completion("ок")

    api = LlmClient(base_url="https://llm.example/v1", api_key=KEY, model="m",
                    transport=httpx.MockTransport(slow), slots=7)
    await asyncio.gather(*[api.chat([{"role": "user", "content": "x"}], max_tokens=5) for _ in range(6)])
    assert peak == 2 and api.calls == 6
    await api.aclose()


# --- журнал ---

async def test_prompts_answers_and_key_never_reach_the_log_or_the_job_error(conn, worker, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeLlm("Секретный ответ модели")
    fake.script = [httpx.Response(500, text="echo: Иван: пришлю смету в пятницу"), None,
                   httpx.Response(400, json={"error": {"message": "bad request near 'пришлю смету'"}})]
    run = worker(fake)
    first = await structured(conn)
    await run.run_once("llm")
    second = await structured(conn, dedup_key="второй")
    await run.run_once("llm")
    errors = [r["error"] or "" for r in await conn.fetch("SELECT error FROM jobs WHERE id = ANY($1::bigint[])", [first, second])]
    for place in (caplog.text, *errors):
        assert "смету" not in place and "Секретный" not in place and "SENTINEL" not in place
        assert "Выпиши" not in place
    assert any("http_400" in e for e in errors)


def test_json_parsing_is_tolerant_but_never_guesses():
    assert parse_json('  {"a": 1}  ') == {"a": 1}
    assert parse_json('Вот:\n```\n[1, 2]\n```\nготово') == [1, 2]
    assert parse_json("{a: 1}") is None and parse_json("") is None
