"""Reproducible, deliberately small security mutation check (not a pytest suite).

Run: python tests/security_mutations.py --output-dir /tmp/security-mutations
CI with an isolated test database may add --include-db. Only src/, tests/ and
pyproject.toml are copied; no instance files, real Telegram or providers are used.
Every mutation changes one exact anchor in a fresh temporary snapshot. This is
not an estimate of whole-project coverage. Exit 0 requires all selected mutants
killed by call-phase security assertions, with clean baseline/setup/collection.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Mutation:
    name: str
    file: str
    before: str
    after: str
    test: str
    database: bool = False


CONTROLS = "tests/replies/test_controls.py::"
AUTHORITY = "tests/test_authority.py::"
CODE_LOGIN = "tests/setup_page/test_code_login.py::"
MUTATIONS = (
    Mutation("background_owner_context", "src/shturman/authority.py",
             "    context.run(_owner.set, None)", "    # mutation: retain inherited owner context",
             AUTHORITY + "test_owner_authority_does_not_leak_to_background_tasks"),
    Mutation("claimed_owner_callback", "src/shturman/authority.py",
             "def requires_owner() -> AuthorityPrincipal:\n    principal = _owner.get()",
             "def requires_owner() -> AuthorityPrincipal:\n    principal = _owner.get() or AuthorityPrincipal('telegram:1000', 'telegram', 1000, 1000)",
             AUTHORITY + "test_legacy_claimed_owner_callback_cannot_touch_confirmation_database"),
    Mutation("independent_owner_bot", "src/shturman/replies/owner.py",
             "return bool(bridge.owns_bot() and principal and principal.source == 'telegram'",
             "return bool(principal and principal.source == 'telegram'",
             CONTROLS + "test_legacy_bot_does_not_grant"),
    Mutation("private_owner_chat", "src/shturman/replies/owner.py",
             "and principal.chat_id == int(owner['chat_id']))", "and True)",
             CONTROLS + "test_model_or_api_user_id_does_not_establish_owner"),
    Mutation("prepare_only_presend", "src/shturman/replies/workflow.py",
             "    if task['prepare_only']:", "    if False:",
             CONTROLS + "test_presend_rechecks_immutable_bindings[prepare_only]"),
    Mutation("immutable_source_receipts", "src/shturman/replies/workflow.py",
             "    if sources != task['source_refs']:", "    if False:",
             CONTROLS + "test_presend_rechecks_immutable_bindings[sources]"),
    Mutation("policy_revision_presend", "src/shturman/replies/workflow.py",
             "    if row.get('task_policy_revision') != task['policy_revision']:", "    if False:",
             CONTROLS + "test_presend_rechecks_immutable_bindings[revision]"),
    Mutation("read_is_not_disclosure", "src/shturman/replies/workflow.py",
             "        if task['requires_approval'] or not await broker.disclosure_allowed(conn, task, task['source_refs']):",
             "        if task['requires_approval']:",
             CONTROLS + "test_revoked_disclosure_grant_blocks_auto_even_if_read_is_valid"),
    Mutation("remote_origin_api_denial", "src/shturman/app.py",
             "        if self.remote is not None and any(self.remote.handles(path, host) for host in hosts):",
             "        if False:",
             AUTHORITY + "test_remote_host_cannot_use_internal_static_token_for_api"),
    Mutation("model_unoffered_source", "src/shturman/replies/model.py",
             "not isinstance(key, str) or key not in offered for key in keys",
             "not isinstance(key, str) for key in keys",
             "tests/replies/test_model.py::test_invalid_outcome_is_not_authority"),
    Mutation("group_reply_exact_topic", "src/shturman/outbox/scopes.py",
             "AND p.class='user' AND m.topic_tg_id IS NOT DISTINCT FROM $3::bigint",
             "AND p.class='user' AND ($3::bigint IS NULL OR true)",
             "tests/test_group_replies.py::test_database_session_reply_requires_same_chat_topic_and_visible_actual_sender",
             database=True),
    # Mini App бота согласований (docs/miniapp.md): подпись Telegram, одна сессия на одни данные
    # входа, только кнопка, которая сейчас в списке.
    Mutation("miniapp_signature", "src/shturman/setup_page/initdata.py",
             "    if not hmac.compare_digest(sign(fields, bot_token), got):", "    if False:",
             "tests/setup_page/test_miniapp.py::test_init_data_is_checked_exactly_as_telegram_describes"),
    Mutation("miniapp_replay", "src/shturman/setup_page/miniapp.py",
             "           ON CONFLICT (init_hash) DO NOTHING RETURNING expires_at\"\"\",\n        auth.token_hash(token), _init_hash(init),",
             "           ON CONFLICT (init_hash) DO NOTHING RETURNING expires_at\"\"\",\n        auth.token_hash(token), _init_hash(init) + token,",
             "tests/setup_page/test_miniapp.py::test_every_refusal_looks_the_same_and_tells_nothing",
             database=True),
    Mutation("miniapp_rebind_revokes", "src/shturman/bridge.py",
             "        await _end_app_sessions(conn)\n        previous = await get_owner(conn)",
             "        previous = await get_owner(conn)",
             "tests/setup_page/test_miniapp.py::test_rebinding_back_to_the_same_owner_does_not_revive_old_sessions",
             database=True),
    Mutation("miniapp_listed_button_only", "src/shturman/setup_page/miniapp.py",
             "        if found is None:\n            # Кнопки нет",
             "        if found is None:\n            found = ({'kind_title': 'x', 'id': 0, '_card_message_id': None}, {'label': 'x'})\n        if False:\n            # Кнопки нет",
             "tests/setup_page/test_miniapp.py::test_only_known_buttons_of_the_screen_are_accepted",
             database=True),
    # Экран «Чаты» приложения (Р-80): каждое изменение сообщается владельцу в бот; список диалогов —
    # только из базы, обновление из Telegram — только по кнопке «Обновить» (Р-81).
    Mutation("miniapp_chats_tell_owner", "src/shturman/setup_page/miniapp.py",
             "        await bridge.notify_owner(conn, text)", "        pass",
             "tests/setup_page/test_miniapp_chats.py::test_enable_and_disable_with_depth_are_told_to_the_owner_and_logged",
             database=True),
    Mutation("miniapp_chats_cached_list", "src/shturman/setup_page/miniapp.py",
             "_account_id(request), request,\n                                                 cached=True))",
             "_account_id(request), request,\n                                                 cached=False))",
             "tests/setup_page/test_miniapp_chats.py::test_list_comes_from_the_database_and_telegram_is_asked_only_by_refresh",
             database=True),
    # Бот согласований в одно нажатие (Р-77): только создатель становится владельцем; токен — именно
    # того бота, что создавался по ссылке; без живой сессии страницы токен не ждёт подтверждения;
    # без «Да, это мой аккаунт» токен не сохраняется.
    Mutation("managed_bot_creator_only", "src/shturman/executor/binding.py",
             "return creator if isinstance(creator, int) and not isinstance(creator, bool) else None",
             "return None",
             "tests/setup_page/test_setup_managed_bot.py::test_only_the_creator_can_become_the_owner",
             database=True),
    Mutation("managed_bot_same_bot", "src/shturman/setup_page/bot_setup.py",
             "    return expect is None or (bot[\"id\"] == expect.get(\"id\")",
             "    return True or (bot[\"id\"] == expect.get(\"id\")",
             "tests/setup_page/test_setup_managed_bot.py::test_token_of_another_bot_is_refused",
             database=True),
    Mutation("managed_bot_live_session", "src/shturman/setup_page/bot_setup.py",
             "    if not await _session_alive(state, pairing.session_id):",
             "    if False:",
             "tests/setup_page/test_setup_managed_bot.py::test_token_is_not_applied_after_the_session_ended",
             database=True),
    Mutation("managed_bot_owner_confirms", "src/shturman/setup_page/bot_setup.py",
             "    pairing.status = CONFIRM\n",
             "    pairing.status = CONFIRM\n    await confirm(state, getattr(state.extras.get('setup_page'), 'managed', None) and state.extras['setup_page'], pairing.session_id, accept=True)\n",
             "tests/setup_page/test_setup_managed_bot.py::test_bot_created_by_someone_else_is_never_accepted_silently",
             database=True),
    Mutation("managed_bot_claim_needs_code", "src/shturman/setup_page/bot_setup.py",
             "    claimed = data.get(\"claimed\") is True and bool(pairing.claim_link)\n",
             "    claimed = data.get(\"claimed\") is True\n",
             "tests/setup_page/test_setup_managed_bot.py::test_bot_with_other_address_needs_both_claim_code_and_claimed_mark",
             database=True),
    # Раскладки и сферы (Р-82): из приложения массово — только выключить; изменение сфер через внутренний
    # API ждёт нажатия владельца в боте согласований.
    Mutation("miniapp_no_bulk_enable", "src/shturman/setup_page/spheres.py",
             "    if enabled:\n        await page._log(request, \"app.bulk_on\"",
             "    if False:\n        await page._log(request, \"app.bulk_on\"",
             "tests/setup_page/test_spheres_page.py::test_in_the_app_enable_all_is_refused_and_disable_all_is_told_to_the_owner",
             database=True),
    Mutation("spheres_api_waits_for_owner", "src/shturman/spheres_service.py",
             "        answer, result = await settle(conn, SPHERE_CHATS, {\"sphere_id\": sphere_id, \"add\": add, \"remove\": remove},\n                                      summary=summary)",
             "        answer, result = await settle(conn, SPHERE_CHATS, {\"sphere_id\": sphere_id, \"add\": add, \"remove\": remove},\n                                      summary=None)",
             "tests/processing/test_spheres_tools.py::test_sphere_changes_through_the_api_wait_for_the_owner",
             database=True),
    # Уведомления владельцу (Р-84): расширение через внутренний API ждёт владельца; из приложения —
    # только сузить; скрытое защитой уходит без текста.
    Mutation("notify_api_widening_waits", "src/shturman/notify/service.py",
             "        summary = None\n        if wider:", "        summary = None\n        if False:",
             "tests/notify/test_notify_api.py::test_widening_waits_for_the_owner_in_the_bot",
             database=True),
    Mutation("notify_app_only_narrows", "src/shturman/setup_page/notify.py",
             "        if wider:\n            await page._log(request, \"app.notify\"",
             "        if False:\n            await page._log(request, \"app.notify\"",
             "tests/setup_page/test_notify_page.py::test_app_can_only_narrow_and_owner_is_told",
             database=True),
    Mutation("notify_hidden_without_text", "src/shturman/notify/core.py",
             "    if not row[\"agent_visible\"]:\n        return HIDDEN_TEXT", "    if False:\n        return HIDDEN_TEXT",
             "tests/notify/test_notify_core.py::test_hidden_by_guard_is_told_without_text",
             database=True),
    # Репетиция ответов на основном аккаунте (Р-85): основной аккаунт не отправляет ни при каком положении
    # выключателей; под карточкой — только оценки; только люди из списка; включение через внутренний API
    # ждёт владельца; скрытое защитой не уходит модели справкой; уведомление не приходит вторым сообщением.
    Mutation("owner_role_never_sends", "src/shturman/tg/client.py",
             "        return self.role == \"assistant\" and self.sending", "        return self.sending",
             "tests/tg/test_tg_policy.py::test_owner_role_never_sends_whatever_the_switches"),
    Mutation("rehearsal_card_only_ratings", "src/shturman/rehearsal/core.py",
             "RATINGS = ((\"good\", \"Годится\"),", "RATINGS = ((\"send\", \"Отправить\"), (\"good\", \"Годится\"),",
             "tests/rehearsal/test_rehearsal_core.py::test_listed_person_gets_one_card_with_ratings_and_nothing_is_sent",
             database=True),
    Mutation("rehearsal_listed_people_only", "src/shturman/rehearsal/core.py",
             "    if int(row[\"peer_tg_id\"]) not in set(s[\"people\"]):\n        return \"not_listed\"",
             "    if False:\n        return \"not_listed\"",
             "tests/rehearsal/test_rehearsal_core.py::test_what_does_not_start_a_rehearsal[not_listed]",
             database=True),
    Mutation("rehearsal_enable_waits_for_owner", "src/shturman/rehearsal/settings.py",
             "    if after[\"enabled\"] and not before[\"enabled\"]:\n        out.append(\"enabled\")",
             "    if False:\n        out.append(\"enabled\")",
             "tests/rehearsal/test_rehearsal_api.py::test_enabling_and_adding_people_wait_for_the_owner",
             database=True),
    Mutation("reply_context_hides_guarded", "src/shturman/outbox/autoreply.py",
             "WHERE chat_id = $1 AND id <> $2 AND kind = 'message' AND deleted_at IS NULL AND agent_visible\n",
             "WHERE chat_id = $1 AND id <> $2 AND kind = 'message' AND deleted_at IS NULL\n",
             "tests/rehearsal/test_rehearsal_core.py::test_hidden_by_guard_is_neither_a_trigger_nor_context",
             database=True),
    Mutation("rehearsal_holds_notification", "src/shturman/notify/core.py",
             "                if hold == \"hold\":\n                    due = max(due, now + rehearsal.NOTIFY_HOLD)",
             "                if False:\n                    due = max(due, now + rehearsal.NOTIFY_HOLD)",
             "tests/rehearsal/test_rehearsal_core.py::test_listed_person_gets_one_card_with_ratings_and_nothing_is_sent",
             database=True),
    # Вход на страницу по коду из бота (Р-88): код действует только в браузере, который его запросил,
    # пять минут и один раз; не больше трёх запросов за 15 минут и пяти попыток на код; с Hermes — только
    # через сокет обратного прокси; две cookie попытки — не вход.
    Mutation("code_login_browser_binding", "src/shturman/setup_page/auth.py",
             "row = await conn.fetchrow(\"SELECT * FROM setup_codes WHERE attempt_hash = $1 FOR UPDATE\", token_hash(attempt))",
             "row = await conn.fetchrow(\"SELECT * FROM setup_codes WHERE attempt_hash = $1 OR delivered ORDER BY id DESC LIMIT 1 FOR UPDATE\", token_hash(attempt))",
             CODE_LOGIN + "test_code_of_one_browser_does_not_work_in_another",
             database=True),
    Mutation("code_login_expiry", "src/shturman/setup_page/auth.py",
             "        if await conn.fetchval(\"SELECT $1::timestamptz <= now()\", row[\"expires_at\"]):",
             "        if False:",
             CODE_LOGIN + "test_expired_code_does_not_work",
             database=True),
    Mutation("code_login_one_time", "src/shturman/setup_page/auth.py",
             "        if row is None or row[\"used_at\"] is not None or row[\"cancelled_at\"] is not None:",
             "        if row is None or row[\"cancelled_at\"] is not None:",
             CODE_LOGIN + "test_code_from_the_bot_logs_in_once_in_the_browser_that_asked",
             database=True),
    Mutation("code_login_request_limit", "src/shturman/setup_page/auth.py",
             "        if len(recent) >= REQUEST_LIMIT:", "        if False:",
             CODE_LOGIN + "test_three_requests_per_15_minutes_for_everyone",
             database=True),
    Mutation("code_login_tries_limit", "src/shturman/setup_page/auth.py",
             "        if tries < CODE_MAX_ATTEMPTS:", "        if True:",
             CODE_LOGIN + "test_five_wrong_codes_cancel_the_code_and_pause_code_login",
             database=True),
    Mutation("code_login_proxy_socket_only", "src/shturman/setup_page/service.py",
             "    return request.scope.get(\"client\") is None or not state.config.dashboard_origin",
             "    return True",
             CODE_LOGIN + "test_dashboard_origin_and_service_port_cannot_ask_or_enter_a_code",
             database=True),
    Mutation("code_login_single_cookie", "src/shturman/setup_page/code_login.py",
             "    return found[0] if len(found) == 1 and auth.well_formed(found[0]) else None",
             "    return found[-1] if found and auth.well_formed(found[-1]) else None",
             CODE_LOGIN + "test_two_cookies_with_the_same_name_mean_no_attempt",
             database=True),
    # «Остановить всё» (Р-87): пока остановлено, подтверждения не заводятся и отправка не проходит;
    # «Продолжить» и включение входа по коду подтверждаются только в самом боте, не из приложения.
    # Код и база проверяют флаг оба: выключенная проверка в коде упирается в триггер (это не «убита»
    # и не «выжила» — ошибка базы), поэтому вторая линия проверяется отдельно.
    Mutation("halt_database_guard", "src/shturman/migrations/0035_stop_answer_code.sql",
             "    IF shturman_halted() THEN\n        RAISE EXCEPTION 'assistant_stopped'",
             "    IF false THEN\n        RAISE EXCEPTION 'assistant_stopped'",
             "tests/test_halt.py::test_database_is_the_second_line",
             database=True),
    Mutation("halt_refuses_sending", "src/shturman/outbox/policy.py",
             "    decision = await check_halt(conn)\n    if not decision.ok:\n        return decision\n    decision = check_switch(rules)",
             "    decision = check_switch(rules)",
             "tests/test_halt.py::test_every_point_where_the_assistant_acts_refuses_while_stopped[send_check]",
             database=True),
    Mutation("bot_only_not_from_app", "src/shturman/confirm.py",
             "    if row[\"kind\"] in BOT_ONLY and principal.action == \"telegram.miniapp\":",
             "    if False:",
             CODE_LOGIN + "test_managed_bot_has_code_login_off_until_the_owner_presses_in_the_bot",
             database=True),
)

# Capture actual pytest phases and exception types, rather than treating a
# nonzero exit (including broken imports, setup, connection errors) as a kill.
REPORT_PLUGIN = r'''
import json
import os
from pathlib import Path
import pytest

REPORT = {"collected": 0, "collection_errors": [], "reports": [], "deselected": 0}

def pytest_collection_finish(session):
    REPORT["collected"] = len(session.items)

def pytest_collectreport(report):
    if report.failed:
        REPORT["collection_errors"].append(str(report.longrepr))

def pytest_deselected(items):
    REPORT["deselected"] += len(items)

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    REPORT["reports"].append({
        "nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome,
        "exception": call.excinfo.typename if call.excinfo else None,
        "detail": str(report.longrepr) if report.failed else None,
    })

def pytest_sessionfinish(session, exitstatus):
    REPORT["exitstatus"] = int(exitstatus)
    Path(os.environ["SHTURMAN_MUTATION_PHASE_REPORT"]).write_text(
        json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
'''


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo), *args],
                                       text=True, timeout=30).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable:{type(exc).__name__}"


def copy_service(source: Path, target: Path) -> None:
    target.mkdir()
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    for name in ("src", "tests"):
        shutil.copytree(source / name, target / name, ignore=ignore)
    shutil.copy2(source / "pyproject.toml", target / "pyproject.toml")


def manifest(service: Path) -> dict[str, str]:
    return {str(path.relative_to(service)): sha(path.read_bytes())
            for path in sorted(service.rglob("*")) if path.is_file()}


def classify(returncode: int, phases: dict) -> str:
    records = phases.get("reports", [])
    if phases.get("collection_errors") or not phases.get("collected"):
        return "error"
    failures = [r for r in records if r["outcome"] == "failed"]
    def security_assertion(record: dict) -> bool:
        return record["phase"] == "call" and (
            record["exception"] == "AssertionError" or
            (record["exception"] == "Failed" and "DID NOT RAISE" in (record.get("detail") or "")))
    if any(not security_assertion(r) for r in failures):
        return "error"
    passed = [r for r in records if r["phase"] == "call" and r["outcome"] == "passed"]
    if failures and returncode == 1:
        return "assertion_failure"
    if returncode != 0:
        return "error"
    if not passed:
        return "untested"
    if any(r["outcome"] == "skipped" for r in records):
        return "untested"
    return "pass"


def run_test(service: Path, test: str, output: Path, label: str, timeout: int) -> dict:
    phases_path = output / f"{label}.phases.json"
    log_path = output / f"{label}.log"
    plugin_path = service / "_security_mutation_reports.py"
    plugin_path.write_text(REPORT_PLUGIN, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(service / "src"), str(service)))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["SHTURMAN_MUTATION_PHASE_REPORT"] = str(phases_path)
    command = [sys.executable, "-m", "pytest", "-q", "-p",
               "_security_mutation_reports", test]
    started = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.run(command, cwd=service, env=env, stdout=log,
                                  stderr=subprocess.STDOUT, timeout=timeout)
        phases = json.loads(phases_path.read_text(encoding="utf-8")) if phases_path.exists() else {}
        result = classify(proc.returncode, phases)
        return {"status": result, "exit_code": proc.returncode,
                "phases": phases, "command": command, "working_directory": str(service),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "log": str(log_path), "phase_report": str(phases_path)}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "command": command, "timeout_seconds": timeout,
                "elapsed_seconds": round(time.monotonic() - started, 3), "log": str(log_path)}
    except (OSError, ValueError) as exc:
        return {"status": "error", "command": command, "error": str(exc), "log": str(log_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=90, help="Seconds per focused test invocation")
    parser.add_argument("--include-db", action="store_true",
                        help="Run the exact-topic mutant against an explicitly named isolated test DB")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.include_db:
        database_name = urlsplit(os.environ.get("SHTURMAN_TEST_DSN", "")).path.strip("/")
        if "test" not in database_name.lower():
            parser.error("--include-db requires SHTURMAN_TEST_DSN with an explicitly test-named database")
    source = Path(__file__).resolve().parents[1]
    repo = source.parent
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    status = git(repo, "status", "--porcelain")
    report = {
        "schema_version": 1,
        "scope": f"{len(MUTATIONS)} targeted security mutations, not whole-project mutation coverage",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "provenance": {
            "git_head": git(repo, "rev-parse", "HEAD"),
            "dirty": bool(status), "dirty_files": status.splitlines(),
            "python": sys.version,
            "dependencies": {name: importlib.metadata.version(name)
                             for name in ("pytest", "pytest-asyncio", "asyncpg", "starlette")},
        },
        "baselines": {}, "mutations": [],
    }
    with tempfile.TemporaryDirectory(prefix="shturman-security-mutations-") as temp:
        root = Path(temp)
        frozen = root / "snapshot"
        copy_service(source, frozen)
        files = manifest(frozen)
        report["provenance"]["files_sha256"] = files
        report["provenance"]["tree_sha256"] = sha(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode())
        selected = [m for m in MUTATIONS if args.include_db or not m.database]
        # All required baselines finish before the first mutation runs.
        for number, test in enumerate(dict.fromkeys(m.test for m in selected)):
            base = root / f"baseline_{number}"
            copy_service(frozen, base)
            report["baselines"][test] = run_test(base, test, output, f"baseline_{number}", args.timeout)
        baseline_ok = all(r["status"] == "pass" for r in report["baselines"].values())
        for number, mutation in enumerate(MUTATIONS):
            item = asdict(mutation)
            if mutation.database and not args.include_db:
                item.update(status="untested", reason="isolated PostgreSQL test database not selected")
            elif not baseline_ok:
                item.update(status="blocked", reason="one or more required baselines did not pass")
            else:
                isolated = root / f"mutation_{number}"
                copy_service(frozen, isolated)
                path = isolated / mutation.file
                text = path.read_text(encoding="utf-8")
                count = text.count(mutation.before)
                if count != 1:
                    item.update(status="error", reason="exact anchor precondition failed", anchor_count=count)
                else:
                    path.write_text(text.replace(mutation.before, mutation.after, 1), encoding="utf-8")
                    item["mutated_file_sha256"] = sha(path.read_bytes())
                    result = run_test(isolated, mutation.test, output, mutation.name, args.timeout)
                    result["status"] = {"assertion_failure": "killed", "pass": "survived"}.get(
                        result["status"], result["status"])
                    item.update(result)
            report["mutations"].append(item)
            print(f"{item['name']}: {item['status']}", flush=True)
    counts = dict(Counter(item["status"] for item in report["mutations"]))
    tested = counts.get("killed", 0) + counts.get("survived", 0)
    report["summary"] = {
        "selected": len(selected), "total_planned": len(MUTATIONS), "counts": counts,
        "baseline_passed": baseline_ok, "tested_viable": tested,
        "killed_fraction_of_viable": counts.get("killed", 0) / tested if tested and baseline_ok else None,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    report_path = output / "security-mutations.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Provenance: " + json.dumps({key: report["provenance"][key] for key in
          ("git_head", "dirty", "dirty_files", "tree_sha256")}, ensure_ascii=False), flush=True)
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    print(f"Report: {report_path}", flush=True)
    if not baseline_ok or any(item["status"] in ("error", "timeout", "blocked") for item in report["mutations"]):
        return 2
    return 0 if all(item["status"] == "killed" for item in report["mutations"]
                    if not item.get("database") or args.include_db) else 1


if __name__ == "__main__":
    raise SystemExit(main())
