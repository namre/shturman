"""Исполнитель заданий с подставными сервисом, моделью и ботом."""

import asyncio
import logging
import types

import pytest

from shturman_core.bridge_stats import Stats
from shturman_core.executor import (
    BOT_KINDS, BUSINESS_SEND, LLM_KINDS, LLM_STRUCTURED, LLM_TEXT, NOTIFY_EDIT, NOTIFY_OWNER, Executor,
    NotSent, Outcome, parse_buttons,
)
from shturman_core.service_client import ServiceError, ServiceUnavailable

OWNER = {"user_id": 42, "chat_id": 42}
SECRET_TEXT = "Переведи деньги на карту 4276 — это личное"


class FakeService:
    def __init__(self, jobs=None) -> None:
        self.jobs = list(jobs or [])
        self.calls: list[tuple[str, str, dict]] = []
        self.down = False
        self.fail_reports = 0            # столько раз подряд отчёт не доходит

    async def __call__(self, method, path, json_body=None, *, timeout=None):
        self.calls.append((method, path, json_body))
        if self.down:
            raise ServiceUnavailable("нет связи")
        if path == "/api/jobs/claim":
            kinds, limit = json_body["kinds"], json_body["limit"]
            taken = [j for j in self.jobs if j["kind"] in kinds][:limit]
            self.jobs = [j for j in self.jobs if j not in taken]
            return {"jobs": taken}
        if self.fail_reports:
            self.fail_reports -= 1
            raise ServiceUnavailable("нет связи")
        return {"ok": True}

    def reports(self):
        return [(path, body) for _, path, body in self.calls if path != "/api/jobs/claim"]


class FakeBot:
    def __init__(self) -> None:
        self.is_ready = True
        self.sent: list[tuple] = []
        self.edits: list[tuple] = []
        self.business: list[tuple] = []
        self.error: BaseException | None = None
        self.delay = 0.0

    def ready(self) -> bool:
        return self.is_ready

    async def send_owner(self, chat_id, text, buttons, silent):
        self.sent.append((chat_id, text, buttons, silent))
        if self.error:
            raise self.error
        return 700 + len(self.sent)

    async def edit_owner(self, chat_id, message_id, text, buttons):
        self.edits.append((chat_id, message_id, text, buttons))
        if self.error:
            raise self.error

    async def send_business(self, connection_id, chat_id, text, reply_to):
        # ловушка: исполнитель плагина отправлять от имени владельца не должен (Д-45)
        self.business.append((connection_id, chat_id, text, reply_to))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return 9001


class FakeLlm:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.error: BaseException | None = None
        self.parsed = {"commitments": []}

    async def acomplete_structured(self, **kwargs):
        self.calls.append(("structured", (), kwargs))
        if self.error:
            raise self.error
        return types.SimpleNamespace(parsed=self.parsed, text='{"commitments": []}', model="cheap-1")

    async def acomplete(self, messages, **kwargs):
        self.calls.append(("text", (messages,), kwargs))
        if self.error:
            raise self.error
        return types.SimpleNamespace(text="Добрый день!", model="cheap-2")


async def no_sleep(seconds):
    return None


def make(jobs=None, *, owner=OWNER, llm="default", **kwargs):
    service, bot = FakeService(jobs), FakeBot()
    model = FakeLlm() if llm == "default" else llm
    stats = Stats()
    executor = Executor(service, llm=model, bot=bot, owner=lambda: owner, stats=stats, sleep=no_sleep, **kwargs)
    return executor, service, bot, model, stats


def job(kind, payload, job_id=1, attempt=1):
    return {"id": job_id, "kind": kind, "payload": payload, "attempt": attempt}


def run(coro):
    return asyncio.run(coro)


SEND = {"business_connection_id": "bc1", "chat_id": 555, "text": "Буду в 15:00", "reply_to_message_id": 77}


# --- модель ---

def test_structured_request_goes_to_the_model_as_text_block_and_result_is_reported():
    payload = {"instructions": "Найди обязательства", "input": SECRET_TEXT, "json_schema": {"type": "object"},
               "schema_name": "commitments", "task": "shturman_extract", "max_tokens": 1200}
    executor, service, _, llm, stats = make([job(LLM_STRUCTURED, payload)])
    assert run(executor.poll("llm")) == 1
    _, _, kwargs = llm.calls[0]
    assert kwargs["input"] == [{"type": "text", "text": SECRET_TEXT}]      # список блоков, не строка
    # Схема уходит модели текстом в указаниях; параметра json_schema нет — иначе Hermes сам
    # сверял бы ответ и при расхождении не отдал бы его вовсе.
    assert "json_schema" not in kwargs and kwargs["json_mode"] is True
    assert kwargs["instructions"] == 'Найди обязательства\n\nJSON schema:\n{"type": "object"}'
    assert kwargs["schema_name"] == "commitments" and kwargs["task"] == "shturman_extract"
    assert kwargs["max_tokens"] == 1200 and kwargs["timeout"] == executor.llm_timeout
    assert service.reports() == [("/api/jobs/1/complete", {"result": {
        "parsed": {"commitments": []}, "text": '{"commitments": []}', "model": "cheap-1", "schema_valid": True}})]
    assert stats.counters["jobs_done"] == 1 and stats.last_job_at is not None



def test_structured_request_carries_images_as_hermes_image_blocks():
    import base64
    jpeg = b"\xff\xd8\xff\xe0 jpeg"
    payload = {"instructions": "Опиши", "input": "фото", "schema_name": "media",
               "images": [{"mime": "image/jpeg", "data": base64.b64encode(jpeg).decode()}]}
    executor, service, _, llm, _ = make([job(LLM_STRUCTURED, payload)])
    run(executor.poll("llm"))
    _, _, kwargs = llm.calls[0]
    assert kwargs["input"] == [{"type": "text", "text": "фото"},
                               {"type": "image", "data": jpeg, "mime_type": "image/jpeg", "file_name": "image-1.jpg"}]
    assert service.reports()[0][0] == "/api/jobs/1/complete"


def test_malformed_images_fail_the_job_without_calling_the_model():
    payload = {"instructions": "Опиши", "input": "фото", "images": [{"mime": "image/gif", "data": "AAAA"}]}
    executor, service, _, llm, _ = make([job(LLM_STRUCTURED, payload)])
    run(executor.poll("llm"))
    assert llm.calls == [] and service.reports()[0][0] == "/api/jobs/1/fail"


def test_text_request_and_task_fallback():
    payload = {"messages": [{"role": "system", "content": "Ты помощник"}, {"role": "user", "content": "Привет"}],
               "task": "vision", "max_tokens": 999_999}
    executor, service, _, llm, _ = make([job(LLM_TEXT, payload)])
    run(executor.poll("llm"))
    _, (messages,), kwargs = llm.calls[0]
    assert messages == payload["messages"]
    assert kwargs["task"] == "shturman_reply"          # чужую или встроенную задачу Hermes сервис назвать не может
    assert kwargs["max_tokens"] == 8000
    assert service.reports()[0][1] == {"result": {"text": "Добрый день!", "model": "cheap-2"}}


def test_invalid_model_output_is_reported_as_parsed_null():
    executor, service, _, llm, _ = make([job(LLM_STRUCTURED, {"instructions": "и", "input": "т"})])
    llm.parsed = None
    run(executor.poll("llm"))
    path, body = service.reports()[0]
    assert path == "/api/jobs/1/complete"               # неразборчивый ответ — не сбой: сервис решит сам
    assert body["result"]["parsed"] is None and body["result"]["schema_valid"] is False
    assert body["result"]["text"] == '{"commitments": []}'
    assert llm.calls[0][2]["json_mode"] is True and llm.calls[0][2]["instructions"] == "и"


def test_model_failure_is_reported_with_growing_retry(caplog):
    executor, service, _, llm, stats = make()
    llm.error = ValueError("Plugin LLM structured output did not match schema: " + SECRET_TEXT)
    with caplog.at_level(logging.DEBUG):
        first = run(executor.execute(LLM_STRUCTURED, {"instructions": "и", "input": SECRET_TEXT}, 1))
        third = run(executor.execute(LLM_STRUCTURED, {"instructions": "и", "input": SECRET_TEXT}, 3))
        run(executor.handle(job(LLM_STRUCTURED, {"instructions": "и", "input": SECRET_TEXT})))
    assert not first.ok and first.retry_in == 30 and third.retry_in == 120
    assert service.reports()[0][0] == "/api/jobs/1/fail" and service.reports()[0][1]["retry_in"] == 30
    # В строку ошибки (она хранится в очереди заданий сервиса) попадает только вид ошибки.
    assert first.error == "ValueError" and SECRET_TEXT not in service.reports()[0][1]["error"]
    assert stats.counters["jobs_failed"] == 1
    assert SECRET_TEXT not in caplog.text                # в журнал — только вид задания


def test_model_timeout_is_a_retry_not_a_hang():
    class Slow(FakeLlm):
        async def acomplete(self, messages, **kwargs):
            await asyncio.sleep(5)

    executor, *_ = make(llm=Slow(), llm_timeout=-14.95)   # внешний предел = llm_timeout + 15 секунд
    outcome = run(executor.execute(LLM_TEXT, {"messages": [{"role": "user", "content": "?"}]}, 1))
    assert not outcome.ok and "TimeoutError" in outcome.error and outcome.retry_in == 30


@pytest.mark.parametrize("payload", [
    {"messages": []}, {"messages": "текст"}, {"messages": [{"role": "tool", "content": "x"}]},
    {"messages": [{"role": "user", "content": 5}]},
])
def test_malformed_text_request_fails_for_good(payload):
    executor, _, _, llm, _ = make()
    outcome = run(executor.execute(LLM_TEXT, payload, 1))
    assert not outcome.ok and outcome.retry_in is None and llm.calls == []


def test_without_model_access_llm_jobs_stay_in_the_queue():
    executor, service, *_ = make([job(LLM_TEXT, {"messages": [{"role": "user", "content": "?"}]})], llm=None)
    assert executor.kinds("llm") == ()
    assert run(executor.poll("llm")) == 0 and service.calls == []


def test_llm_lane_takes_two_jobs_at_once_and_bot_lane_one():
    jobs = [job(LLM_TEXT, {"messages": [{"role": "user", "content": str(i)}]}, job_id=i) for i in (1, 2, 3)]
    jobs += [job(NOTIFY_OWNER, {"text": f"т{i}"}, job_id=i) for i in (4, 5)]
    executor, service, bot, llm, _ = make(jobs)
    assert run(executor.poll("llm")) == 2 and len(llm.calls) == 2
    claim = service.calls[0][2]
    assert claim["kinds"] == list(LLM_KINDS) and claim["limit"] == 2 and claim["worker"]
    assert run(executor.poll("bot")) == 1 and len(bot.sent) == 1
    claim = [c for c in service.calls if c[1] == "/api/jobs/claim"][-1][2]
    assert claim["kinds"] == list(BOT_KINDS) and claim["limit"] == 1


# --- сообщения владельцу ---

def test_notify_owner_sends_plain_text_with_service_buttons():
    buttons = [[{"text": "Отправить", "data": "sh:d:12:ok"}, {"text": "Отклонить", "data": "sh:d:12:no"}]]
    executor, service, bot, _, _ = make([job(NOTIFY_OWNER, {"text": "x" * 5000, "buttons": buttons, "silent": True})])
    run(executor.poll("bot"))
    chat_id, text, sent_buttons, silent = bot.sent[0]
    assert chat_id == 42 and len(text) == 4096 and silent is True
    assert sent_buttons == [[("Отправить", "sh:d:12:ok"), ("Отклонить", "sh:d:12:no")]]   # данные — как есть
    assert service.reports() == [("/api/jobs/1/complete", {"result": {"message_id": 701}})]


def test_notify_owner_without_owner_waits_for_binding():
    executor, service, bot, _, _ = make([job(NOTIFY_OWNER, {"text": "привет"})], owner={})
    run(executor.poll("bot"))
    assert bot.sent == []
    path, body = service.reports()[0]
    assert path == "/api/jobs/1/fail" and body["retry_in"] == 300 and "владелец" in body["error"]


@pytest.mark.parametrize("buttons", [
    [[{"text": "Одобрить", "data": "ea:once:1"}]],          # кнопка ядра Hermes: одобрение команды
    [[{"text": "Черновик", "data": "bd:send:1"}]],          # кнопка плагина бизнес-режима
    [[{"text": "Длинно", "data": "sh:" + "x" * 70}]],
    [[{"text": "", "data": "sh:a"}]],
    [[]], "sh:a", [{"text": "a", "data": "sh:a"}],
])
def test_foreign_or_malformed_buttons_are_never_shown(buttons):
    executor, _, bot, _, _ = make()
    outcome = run(executor.execute(NOTIFY_OWNER, {"text": "карточка", "buttons": buttons}, 1))
    assert not outcome.ok and outcome.retry_in is None and bot.sent == []
    with pytest.raises(ValueError):
        parse_buttons(buttons)


def test_no_buttons_is_fine():
    assert parse_buttons(None) is None and parse_buttons([]) is None


def test_notify_owner_refused_by_telegram_is_retried_later():
    executor, _, bot, _, _ = make()
    bot.error = NotSent("Forbidden: bot was blocked by the user")
    blocked = run(executor.execute(NOTIFY_OWNER, {"text": "привет"}, 1))
    bot.error = NotSent("BadRequest: Message is too long")
    too_long = run(executor.execute(NOTIFY_OWNER, {"text": "привет"}, 1))
    bot.error = NotSent("flood", retry_after=17)
    flood = run(executor.execute(NOTIFY_OWNER, {"text": "привет"}, 1))
    bot.error = NotSent("нет связи с Telegram (ConnectError)", replied=False)
    never_left = run(executor.execute(NOTIFY_OWNER, {"text": "привет"}, 1))
    bot.error = ConnectionError("обрыв")
    network = run(executor.execute(NOTIFY_OWNER, {"text": "привет"}, 2))
    # Точный отказ Telegram окончателен: повтор дал бы тот же ответ. Повторяем только по просьбе
    # подождать и когда запрос до Telegram не дошёл либо исход неясен.
    assert (blocked.retry_in, too_long.retry_in) == (None, None)
    assert (flood.retry_in, never_left.retry_in, network.retry_in) == (18, 30, 60)


def test_notify_owner_waits_while_bot_is_not_connected():
    executor, service, bot, _, _ = make([job(NOTIFY_OWNER, {"text": "привет"})])
    bot.is_ready = False
    assert executor.kinds("bot") == () and run(executor.poll("bot")) == 0 and service.calls == []
    assert run(executor.execute(NOTIFY_OWNER, {"text": "привет"}, 1)).retry_in == 30


def test_notify_edit_removes_buttons_by_default_and_can_keep_known_ones():
    buttons = [[{"text": "Отправить", "data": "sh:d:1:ok"}]]
    executor, _, bot, _, _ = make()
    sent = run(executor.execute(NOTIFY_OWNER, {"text": "карточка", "buttons": buttons}, 1))
    message_id = sent.result["message_id"]
    kept = run(executor.execute(NOTIFY_EDIT, {"message_id": message_id, "text": "новое", "remove_buttons": False}, 1))
    assert kept.result == {} and bot.edits[-1] == (42, message_id, "новое", [[("Отправить", "sh:d:1:ok")]])
    removed = run(executor.execute(NOTIFY_EDIT, {"message_id": message_id, "text": "итог"}, 1))
    assert removed.result == {} and bot.edits[-1] == (42, message_id, "итог", None)
    # кнопки сняты — «оставить» больше нечего; молча снимать вместо «оставить» нельзя
    again = run(executor.execute(NOTIFY_EDIT, {"message_id": message_id, "text": "ещё", "remove_buttons": False}, 1))
    assert not again.ok and again.retry_in is None and "keep_buttons_unavailable" in again.error
    assert len(bot.edits) == 2


def test_notify_edit_refusal_is_final_but_flood_is_retried():
    executor, _, bot, _, _ = make()
    bot.error = NotSent("BadRequest: message can't be edited")
    final = run(executor.execute(NOTIFY_EDIT, {"message_id": 5, "text": "т"}, 1))
    bot.error = NotSent("flood", retry_after=4)
    flood = run(executor.execute(NOTIFY_EDIT, {"message_id": 5, "text": "т"}, 1))
    assert (final.retry_in, flood.retry_in) == (None, 5)
    assert run(executor.execute(NOTIFY_EDIT, {"text": "т"}, 1)).retry_in is None


# --- отправка от имени владельца: исполнитель плагина её не выполняет (Д-45) ---

def test_business_send_is_refused_without_touching_telegram():
    executor, service, bot, _, stats = make([job(BUSINESS_SEND, SEND, job_id=31)])
    run(executor.poll("bot"))
    path, body = service.reports()[0]
    assert path == "/api/jobs/31/fail" and body["error"].startswith("not_sent:")
    assert "бот согласований" in body["error"]
    assert "retry_in" in body and body["retry_in"] is None       # поле есть всегда: без него сервис назначил бы повтор
    assert bot.business == [] and stats.counters["sends_unknown"] == 0


@pytest.mark.parametrize("change", [
    {}, {"business_connection_id": ""}, {"chat_id": "555"}, {"text": ""}, {"text": "x" * 4097},
    {"reply_to_message_id": -1},
])
def test_business_send_is_refused_whatever_the_payload(change):
    executor, _, bot, _, _ = make()
    outcome = run(executor.execute(BUSINESS_SEND, {**SEND, **change}, 1))
    assert outcome.error.startswith("not_sent:") and outcome.retry_in is None and bot.business == []


def test_business_send_is_refused_even_with_a_connected_bot_and_no_errors():
    executor, _, bot, _, _ = make()
    bot.is_ready, bot.error, bot.delay = True, None, 0
    outcome = run(executor.execute(BUSINESS_SEND, SEND, 1))
    assert not outcome.ok and outcome.error.startswith("not_sent:") and bot.business == []


def test_lost_report_of_a_refusal_never_turns_into_a_send():
    executor, service, bot, _, stats = make([job(BUSINESS_SEND, SEND, job_id=34)])
    service.fail_reports = 3
    run(executor.poll("bot"))
    fails = [r for r in service.reports() if r[0] == "/api/jobs/34/fail"]
    assert len(fails) == 4 and bot.business == [] and stats.counters["reports_lost"] == 0


def test_report_refused_by_service_is_not_repeated():
    executor, service, bot, _, stats = make()

    async def refuse(method, path, json_body=None, *, timeout=None):
        service.calls.append((method, path, json_body))
        raise ServiceError("задание уже закрыто", status=409)

    executor.call = refuse
    assert run(executor.report(7, Outcome(result={"message_id": 1}))) is False
    assert len(service.calls) == 1 and stats.counters["reports_lost"] == 1


# --- прочее ---

def test_unknown_kind_and_broken_job_do_not_break_the_lane():
    executor, service, *_ = make()
    assert run(executor.execute("tg.delete_everything", {}, 1)).retry_in is None
    run(executor.handle({"id": "x", "kind": NOTIFY_OWNER}))
    run(executor.handle({"id": 5, "kind": NOTIFY_OWNER, "payload": "не объект"}))
    assert service.reports()[-1][0] == "/api/jobs/5/fail"


def test_lane_backs_off_while_service_is_down_and_recovers():
    executor, service, bot, _, stats = make([job(NOTIFY_OWNER, {"text": "привет"})], idle=0.01)
    service.down = True

    async def scenario():
        task = asyncio.create_task(executor.run_lane("bot"))
        await asyncio.sleep(0.1)
        attempts_while_down = len(service.calls)
        assert stats.reachable is False and bot.sent == []
        service.down = False
        executor.wake("bot")
        for _ in range(100):
            if bot.sent:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return attempts_while_down

    attempts = run(scenario())
    assert 1 <= attempts <= 4                # паузы растут: 0,02 → 0,04 → 0,08 секунды
    assert len(bot.sent) == 1 and stats.reachable is True


def test_wake_makes_the_lane_poll_now():
    executor, service, bot, _, _ = make(idle=30)

    async def scenario():
        task = asyncio.create_task(executor.run_lane("bot"))
        await asyncio.sleep(0.05)
        service.jobs.append(job(NOTIFY_OWNER, {"text": "готово"}))
        executor.wake("bot")
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert len(bot.sent) == 1


# --- расхождение со схемой — не сбой задания ---

SCHEMA = {"type": "object", "required": ["commitments"], "additionalProperties": False,
          "properties": {"commitments": {"type": "array", "items": {
              "type": "object", "required": ["message", "what"],
              "properties": {"message": {"type": "integer"}, "what": {"type": "string"}}}}}}


@pytest.mark.parametrize("parsed", [
    {"commitments": [{"message": "1", "what": "прислать смету"}]},      # номер строкой
    {"commitments": [{"what": "прислать смету"}], "note": "лишнее"},     # нет поля, есть лишнее
    [{"message": 1, "what": "x"}],                                       # список вместо объекта
])
def test_schema_violation_is_a_successful_result_for_the_service_to_judge(parsed):
    payload = {"instructions": "Найди", "input": "текст", "json_schema": SCHEMA, "schema_name": "commitments"}
    executor, service, _, llm, stats = make([job(LLM_STRUCTURED, payload, job_id=8)])
    llm.parsed = parsed
    for _ in range(3):
        run(executor.poll("llm"))
    assert len(llm.calls) == 1                           # один вызов модели, без повторов
    path, body = service.reports()[0]
    assert path == "/api/jobs/8/complete" and len(service.reports()) == 1
    assert body["result"]["parsed"] == parsed and body["result"]["schema_valid"] is False
    assert body["result"]["text"] and stats.counters["jobs_failed"] == 0


def test_valid_answer_is_marked_valid():
    payload = {"instructions": "Найди", "input": "текст", "json_schema": SCHEMA}
    executor, service, _, llm, _ = make([job(LLM_STRUCTURED, payload)])
    llm.parsed = {"commitments": [{"message": 3, "what": "прислать смету"}]}
    run(executor.poll("llm"))
    assert service.reports()[0][1]["result"]["schema_valid"] is True


def test_watch_task_is_registered_and_routed():
    from shturman_core.executor import AUX_TASKS

    assert set(AUX_TASKS) == {"shturman_extract", "shturman_reply", "shturman_watch", "shturman_media"}
    executor, _, _, llm, _ = make([job(LLM_STRUCTURED, {"instructions": "и", "input": "т", "task": "shturman_watch"})])
    run(executor.poll("llm"))
    assert llm.calls[0][2]["task"] == "shturman_watch"


# --- длина по счёту Telegram ---

def test_owner_messages_are_cut_by_utf16_units():
    from shturman_core.textlimits import utf16_len

    executor, _, bot, _, _ = make()
    run(executor.execute(NOTIFY_OWNER, {"text": "😀" * 3000}, 1))         # 3000 знаков, 6000 единиц
    run(executor.execute(NOTIFY_EDIT, {"message_id": 5, "text": "я" * 4000 + "😀" * 200}, 1))
    assert utf16_len(bot.sent[0][1]) <= 4096 and utf16_len(bot.edits[0][2]) <= 4096
    assert bot.sent[0][1].endswith("…")


# --- область секретов профиля Hermes вокруг вызова модели (Р-83) ---

import contextlib  # noqa: E402
import contextvars  # noqa: E402

from shturman_core.executor import FailureLog, error_label  # noqa: E402

SCOPE = contextvars.ContextVar("scope", default=None)


class UnscopedSecretError(RuntimeError):
    """Как agent.secret_scope.UnscopedSecretError: ключ спрошен вне области при мультиплексе."""


class ScopedHermesLlm(FakeLlm):
    """Подставной Hermes с включённым мультиплексом: модель доступна только внутри области."""

    def __init__(self) -> None:
        super().__init__()
        self.scopes: list = []

    def _key(self):
        self.scopes.append(SCOPE.get())
        if SCOPE.get() is None:
            raise UnscopedSecretError("Hermes could not read this profile's OPENAI_API_KEY " + SECRET_TEXT)

    async def acomplete_structured(self, **kwargs):
        self._key()
        return await super().acomplete_structured(**kwargs)

    async def acomplete(self, messages, **kwargs):
        self._key()
        return await super().acomplete(messages, **kwargs)


def launch_scope():
    entered = []

    @contextlib.asynccontextmanager
    async def scope():
        token = SCOPE.set("launch")
        entered.append(True)
        try:
            yield
        finally:
            SCOPE.reset(token)
    return scope, entered


STRUCT = {"instructions": "Найди обязательства", "input": SECRET_TEXT}
TEXT = {"messages": [{"role": "user", "content": SECRET_TEXT}]}


def test_without_profile_scope_hermes_refuses_and_only_the_class_name_leaves():
    executor, _, _, _, _ = make(llm=ScopedHermesLlm())
    out = run(executor.execute(LLM_STRUCTURED, STRUCT, 1))
    assert not out.ok and out.error == "UnscopedSecretError" and out.label == "UnscopedSecretError"
    assert SECRET_TEXT not in out.error


def test_model_is_called_inside_the_profile_scope_for_both_llm_kinds():
    scope, entered = launch_scope()
    llm = ScopedHermesLlm()
    executor, service, _, _, _ = make([job(LLM_STRUCTURED, STRUCT, 1), job(LLM_TEXT, TEXT, 2)],
                                      llm=llm, llm_scope=scope)
    assert run(executor.poll("llm")) == 2
    assert llm.scopes == ["launch", "launch"] and len(entered) == 2
    assert sorted(p for p, _ in service.reports()) == ["/api/jobs/1/complete", "/api/jobs/2/complete"]
    assert SCOPE.get() is None                       # область снята после вызова


def test_bot_jobs_do_not_enter_the_model_scope():
    scope, entered = launch_scope()
    executor, _, bot, _, _ = make(llm_scope=scope)
    assert run(executor.execute(NOTIFY_OWNER, {"text": "Привет"}, 1)).ok
    assert entered == [] and bot.sent


def test_scope_that_fails_to_open_is_a_retry_with_its_class_name():
    @contextlib.asynccontextmanager
    async def broken():
        raise UnscopedSecretError("не удалось " + SECRET_TEXT)
        yield
    executor, _, _, llm, _ = make(llm_scope=broken)
    out = run(executor.execute(LLM_TEXT, TEXT, 1))
    assert out.error == "UnscopedSecretError" and out.retry_in == 30 and llm.calls == []


def test_failure_log_names_the_error_kind_and_folds_repeats(caplog, clock):
    executor, service, _, _, _ = make(llm=ScopedHermesLlm(), clock=clock)
    with caplog.at_level(logging.WARNING, logger="shturman.executor"):
        for n in range(5):
            run(executor.handle(job(LLM_STRUCTURED, STRUCT, job_id=n + 1)))
            clock.tick(5)
        lines = [r.getMessage() for r in caplog.records]
        assert lines == ["shturman: задание вида llm.structured не выполнено: UnscopedSecretError"]
        clock.tick(60)
        run(executor.handle(job(LLM_STRUCTURED, STRUCT, job_id=9)))
        run(executor.handle(job(LLM_TEXT, TEXT, job_id=10)))       # другой вид задания — своя строка
    lines = [r.getMessage() for r in caplog.records]
    assert lines[1].startswith(
        "shturman: задание вида llm.structured не выполнено: UnscopedSecretError (ещё 4 таких за ")
    assert lines[2] == "shturman: задание вида llm.text не выполнено: UnscopedSecretError"
    assert len(lines) == 3 and SECRET_TEXT not in caplog.text
    assert len(service.reports()) == 7               # сервису сообщается о каждом задании


def test_failure_log_keeps_a_bounded_number_of_keys(clock):
    log = FailureLog(logging.getLogger("shturman.test"), clock=clock)
    for n in range(FailureLog.MAX_KEYS + 50):
        log.failed("llm.text", f"E{n}")
    assert len(log._seen) == FailureLog.MAX_KEYS


@pytest.mark.parametrize("error, label", [
    ("ValueError", "ValueError"),
    ("UnscopedSecretError", "UnscopedSecretError"),
    ("у плагина нет доступа к модели Hermes", "no_model_access"),
    ("в задании нет поля input", "bad_payload"),
    ("поле images задано неверно", "bad_payload"),
    ("владелец не привязан к боту", "owner_unbound"),
    ("бот не подключён", "bot_not_ready"),
    ("Telegram отказал: BadRequest: " + SECRET_TEXT, "Telegram:BadRequest"),
    ("Telegram отказал: Telegram просит подождать (слишком много запросов)", "Telegram:RetryAfter"),
    ("Telegram отказал: нет связи с Telegram (ConnectError)", "Telegram:no_connection"),
    ("not_sent: Forbidden: " + SECRET_TEXT, "not_sent"),
    ("исход неизвестен: TimedOut", "unknown_outcome:TimedOut"),
    ("исход неизвестен: " + SECRET_TEXT, "unknown_outcome"),
    (SECRET_TEXT, "other"),
    ("", "other"),
])
def test_error_label_never_carries_free_text(error, label):
    assert error_label(error) == label
    assert SECRET_TEXT not in error_label(error)
