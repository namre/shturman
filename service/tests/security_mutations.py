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
        "scope": "11 targeted security mutations, not whole-project mutation coverage",
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
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    print(f"Report: {report_path}", flush=True)
    if not baseline_ok or any(item["status"] in ("error", "timeout", "blocked") for item in report["mutations"]):
        return 2
    return 0 if all(item["status"] == "killed" for item in report["mutations"]
                    if not item.get("database") or args.include_db) else 1


if __name__ == "__main__":
    raise SystemExit(main())
