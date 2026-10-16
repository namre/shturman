#!/usr/bin/env bash
# Проверка строк summary-last и bot-live в ./ops/doctor.sh (Р-98) без Hermes: подставной Docker исполняет
# пробы doctor настоящим python3 с подставными модулями Hermes 0.21.5 — теми именами и сигнатурами, что
# зовёт doctor (cron.jobs.list_jobs, cron.executions.latest_executions, hermes_cli.config,
# gateway.status.read_runtime_status / runtime_status_is_stale; сверено с кодом v2026.9.24). Сети нет:
# getMe проверяется в положениях «токен не того вида» и «нет связи».
# Запуск из корня репозитория: bash ops/tests/doctor_hermes.sh.
set -eu
src="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/repo" "$tmp/stub" "$tmp/fake/cron" "$tmp/fake/hermes_cli" "$tmp/fake/gateway"
cp -r "$src/ops" "$src/plugins" "$tmp/repo/"
touch "$tmp/fake/cron/__init__.py" "$tmp/fake/hermes_cli/__init__.py" "$tmp/fake/gateway/__init__.py"
cat > "$tmp/fake/cron/jobs.py" <<'PY'
import json, os
def list_jobs(include_disabled=False):
    return json.load(open(os.environ["FAKE_DIR"] + "/jobs.json"))
PY
cat > "$tmp/fake/cron/executions.py" <<'PY'
import json, os
def latest_executions(ids):
    rows = json.load(open(os.environ["FAKE_DIR"] + "/ledger.json"))
    return {k: v for k, v in rows.items() if k in ids}
PY
cat > "$tmp/fake/hermes_cli/config.py" <<'PY'
import os
def read_raw_config():
    return {"timezone": "Europe/Moscow"}
def load_config():
    return {"plugins": {"enabled": ["shturman"]}}
def get_env_value_prefer_dotenv(name):
    return os.environ.get("FAKE_TOKEN", "") if name == "TELEGRAM_BOT_TOKEN" else ""
PY
cat > "$tmp/fake/gateway/status.py" <<'PY'
import json, os
def read_runtime_status(path=None):
    return json.load(open(os.environ["FAKE_DIR"] + "/gateway_state.json"))
def runtime_status_is_stale(record, ttl_s=120):
    return bool(record.get("stale_for_test"))
PY
cat > "$tmp/stub/docker" <<EOF
#!/usr/bin/env bash
if [ "\$1" = exec ]; then
  shift
  while [ "\${1#-}" != "\$1" ]; do case "\$1" in -u) shift 2 ;; *) shift ;; esac; done
  box="\$1"; shift
  if [ "\$box" = shturman-hermes ] && [ "\$1" = /opt/hermes/.venv/bin/python ]; then
    shift; FAKE_DIR="$tmp" PYTHONPATH="$tmp/fake:$tmp/repo/plugins/shturman" exec python3 "\$@"
  fi
  exit 1
fi
case "\$*" in
  "inspect -f {{.State.Status}} shturman-hermes") echo running ;;
  "inspect -f {{.Config.Image}} shturman-hermes") echo nousresearch/hermes-agent:v2026.9.24 ;;
  "inspect -f {{.RestartCount}} shturman-hermes") echo 0 ;;
  *) exit 1 ;;
esac
EOF
printf '#!/bin/sh\nprintf 000\n' > "$tmp/stub/curl"
chmod +x "$tmp/stub/docker" "$tmp/stub/curl"
export PATH="$tmp/stub:$PATH"
cd "$tmp/repo"
printf 'SHTURMAN_MODE=hermes\n' > .env
bad() { echo "ОШИБКА: $*" >&2; cat "$tmp/out.txt" >&2; exit 1; }
iso() { python3 -c 'import sys; from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) + timedelta(hours=float(sys.argv[1]))).isoformat())' "$1"; }

# 1. Утренняя сводка вчера промолчала ([SILENT]), следующая — через час; обзор недели на паузе.
#    Бот: токен не того вида; шлюз на связи.
cat > "$tmp/jobs.json" <<EOF
[{"id": "m1", "name": "shturman:morning-brief", "enabled": true, "state": "scheduled",
  "schedule": {"kind": "cron", "expr": "27 8 * * *"}, "next_run_at": "$(iso 1)",
  "last_run_at": "$(iso -23)", "last_status": "ok"},
 {"id": "w1", "name": "shturman:weekly-review", "enabled": false, "state": "paused",
  "schedule": {"kind": "cron", "expr": "47 17 * * 5"}, "next_run_at": null}]
EOF
echo '{"m1": {"status": "completed", "delivery_outcome": "suppressed", "finished_at": "'"$(iso -23)"'", "error": null}}' > "$tmp/ledger.json"
echo '{"platforms": {"telegram": {"state": "connected"}}}' > "$tmp/gateway_state.json"
FAKE_TOKEN="не-токен" ./ops/doctor.sh > "$tmp/out.txt" 2>&1 || true
grep -q '^PASS  summary-last: утренняя сводка: последний запуск (.*) отработал, но сказать было нечего (\[SILENT\])' "$tmp/out.txt" || bad "нет «промолчала»"
grep -q '^WARN  summary-last: задача «обзор недели» выключена' "$tmp/out.txt" || bad "выключенная задача не WARN"
grep -q '^FAIL  bot-live: токен бота ассистента записан в Hermes не в том виде' "$tmp/out.txt" || bad "нет FAIL для токена не того вида"
grep -q "не-токен" "$tmp/out.txt" && bad "doctor напечатал токен"

# 2. Сводка упала (журнал запусков), запуск обзора просрочен; нет связи с Telegram (прокси в никуда).
cat > "$tmp/jobs.json" <<EOF
[{"id": "m1", "name": "shturman:morning-brief", "enabled": true, "state": "scheduled",
  "schedule": {"kind": "cron", "expr": "27 8 * * *"}, "next_run_at": "$(iso 20)",
  "last_run_at": "$(iso -4)", "last_status": "error", "last_error": "секретный текст ошибки"},
 {"id": "w1", "name": "shturman:weekly-review", "enabled": true, "state": "scheduled",
  "schedule": {"kind": "cron", "expr": "47 17 * * 5"}, "next_run_at": "$(iso -2)"}]
EOF
echo '{"m1": {"status": "failed", "delivery_outcome": "suppressed_acked", "finished_at": "'"$(iso -4)"'"}}' > "$tmp/ledger.json"
FAKE_TOKEN="123456789:AAH-secret-bot-token-ABCDEFGHIJKLMNOP" HTTPS_PROXY=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 \
  ./ops/doctor.sh > "$tmp/out.txt" 2>&1 || true
grep -q '^FAIL  summary-last: утренняя сводка: последний запуск (.*) упал' "$tmp/out.txt" || bad "упавшая сводка не FAIL"
grep -q '^FAIL  summary-last: обзор недели: следующий запуск (.*) прошёл больше 10 минут назад' "$tmp/out.txt" || bad "просроченный запуск не FAIL"
grep -q '^WARN  bot-live: из контейнера Hermes нет связи с api.telegram.org' "$tmp/out.txt" || bad "нет WARN без связи"
grep -q "секретный текст" "$tmp/out.txt" && bad "doctor напечатал текст ошибки задачи"
grep -q "AAH-secret" "$tmp/out.txt" && bad "doctor напечатал токен"

# 3. Задачи сводки нет вовсе — WARN.
echo '[]' > "$tmp/jobs.json"; echo '{}' > "$tmp/ledger.json"
./ops/doctor.sh > "$tmp/out.txt" 2>&1 || true
grep -q '^WARN  summary-last: задачи утренней сводки в Hermes нет' "$tmp/out.txt" || bad "пропавшая задача не WARN"
echo "doctor hermes rows: ok"
