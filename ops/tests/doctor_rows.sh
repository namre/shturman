#!/usr/bin/env bash
# Проверка новых строк ./ops/doctor.sh (Р-98) с подставными Docker и curl: tg-session, pulse, disk-usage,
# jobs (Д-25) в режиме без Hermes; сервис «отвечает» ответом из fixtures/status.json. Заодно ловит ошибки оболочки
# в doctor (как Д-2: строки ниже сломанной молча становились «не проверено»).
# Запуск из корня репозитория: bash ops/tests/doctor_rows.sh.
set -eu
src="$(cd "$(dirname "$0")/../.." && pwd)"
fx="$src/ops/tests/fixtures"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/repo" "$tmp/stub"
cp -r "$src/ops" "$tmp/repo/"
cat > "$tmp/stub/docker" <<EOF
#!/usr/bin/env bash
# exec [-i] [-u …] КОНТЕЙНЕР команда…
if [ "\$1" = exec ]; then
  shift
  while [ "\${1#-}" != "\$1" ]; do case "\$1" in -u) shift 2 ;; *) shift ;; esac; done
  box="\$1"; shift
  case "\$box:\$*" in
    "shturman-service:shturman call GET /api/status") cat "$fx/status.json" ;;
    "shturman-service:shturman call GET /api/executor/status") echo '{"bot": {}, "llm": {}}' ;;
    shturman-service:python*) shift; exec python3 "\$@" ;;
    shturman-postgres:du*) printf '1200\t/var/lib/postgresql/data\n' ;;
    *) exit 1 ;;
  esac
  exit 0
fi
case "\$*" in
  "inspect shturman-service") echo '[]' ;;
  "inspect -f {{.State.Status}} shturman-service"|"inspect -f {{.State.Status}} shturman-postgres") echo running ;;
  "inspect -f {{.State.Health.Status}} shturman-postgres") echo healthy ;;
  "system df --format"*) echo 'Images|4.1GB|1.2GB (29%)' ;;
  *) exit 1 ;;
esac
EOF
printf '#!/bin/sh\ncase "$*" in *8765/health*) printf 200 ;; *8765/mcp*) printf 401 ;; *) printf 000 ;; esac\n' > "$tmp/stub/curl"
chmod +x "$tmp/stub/docker" "$tmp/stub/curl"
export PATH="$tmp/stub:$PATH"
cd "$tmp/repo"
printf 'SHTURMAN_MODE=standalone\n' > .env
mkdir -p local/backups data/shturman
bad() { echo "ОШИБКА: $*" >&2; cat "$tmp/out.txt" >&2; exit 1; }

code=0; ./ops/doctor.sh > "$tmp/out.txt" 2> "$tmp/err.txt" || code=$?
[ "$code" -eq 1 ] || bad "doctor: код $code вместо 1 (есть FAIL tg-session)"
grep -q "line [0-9]*:" "$tmp/err.txt" && { cat "$tmp/err.txt" >&2; bad "ошибки оболочки в doctor"; }
grep -q '^FAIL  tg-session: ваш аккаунт: нужно войти заново' "$tmp/out.txt" || bad "нет FAIL tg-session для основного аккаунта"
grep -q '^PASS  tg-session: аккаунт помощника: на связи' "$tmp/out.txt" || bad "нет PASS tg-session для помощника"
grep -q '^PASS  pulse: внешний сторож (hc-ping.com) .*«неполадка»: telegram:relogin' "$tmp/out.txt" || bad "нет строки pulse"
grep -Eq '^(PASS|WARN)  disk-usage: база архива 1.2 ГБ' "$tmp/out.txt" || bad "нет строки disk-usage"
grep -q 'образы Docker 4.1GB (не используются: 1.2GB)' "$tmp/out.txt" || bad "нет образов в disk-usage"
grep -q '^ИТОГ: FAIL' "$tmp/out.txt" || bad "нет итога"

# Сторожа нет — WARN, а не PASS.
python3 - "$fx/status.json" > "$tmp/status-nopulse.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["pulse"] = {"enabled": False, "configured": False}
d["tg_health"]["accounts"] = [{"role": "owner", "state": "ok", "for_s": 100, "update_age_s": 60}]
print(json.dumps(d))
PY

sed -i "s|$fx/status.json|$tmp/status-nopulse.json|" "$tmp/stub/docker"
./ops/doctor.sh > "$tmp/out.txt" 2>&1 || true

grep -q '^WARN  pulse: внешнего сторожа нет' "$tmp/out.txt" || bad "нет WARN pulse без сторожа"
grep -q '^PASS  tg-session: ваш аккаунт: на связи, сбор переписки идёт; последнее обновление от Telegram 1 мин назад' "$tmp/out.txt" || bad "нет PASS tg-session"
grep -q '^PASS  jobs: очередь заданий: 0, неудачных за всё время: 0$' "$tmp/out.txt" || bad "нет PASS jobs у сервиса прежней версии"

# Строка jobs (Д-25): длинная очередь, которая разбирается, — PASS; стоящая — WARN при любой длине;
# неудачные — «за всё время». $1 — jobs_waiting, $2 — объект jobs_queue (JSON) или пусто.
jobs_status() {
  python3 - "$fx/status.json" "$1" "$2" > "$tmp/status-jobs.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["jobs_waiting"], d["jobs_failed"] = int(sys.argv[2]), 3021
if sys.argv[3]:
    d["jobs_queue"] = json.loads(sys.argv[3])
print(json.dumps(d))
PY
  sed -i "s|$tmp/status-[a-z]*.json|$tmp/status-jobs.json|" "$tmp/stub/docker"
  ./ops/doctor.sh > "$tmp/out.txt" 2>&1 || true
  grep -q "line [0-9]*:" "$tmp/out.txt" && bad "ошибки оболочки в doctor (jobs)"
  true
}
jobs_status 86 '{"llm": {"waiting": 86, "ready_age_s": 1500, "done_hour": 40}, "bot": {"waiting": 0, "ready_age_s": 0, "done_hour": 5}}'
grep -q '^PASS  jobs: в очереди 86 заданий — разбираются: за час выполнено 45 (модели — 40, бота — 5); неудачных за всё время: 3021$' "$tmp/out.txt" \
  || bad "движущаяся очередь — не PASS «разбираются»"
jobs_status 86 '{"llm": {"waiting": 86, "ready_age_s": 1500, "done_hour": 0}, "bot": {"waiting": 0, "ready_age_s": 0, "done_hour": 0}}'
grep -q '^WARN  jobs: в очереди 86 заданий, за час не выполнено ни одного: модели — ждут 86, самое старое 25 мин (строки model и jobs-failing); неудачных за всё время: 3021$' "$tmp/out.txt" \
  || bad "стоящая очередь модели — не WARN"
jobs_status 4 '{"llm": {"waiting": 0, "ready_age_s": 0, "done_hour": 9}, "bot": {"waiting": 4, "ready_age_s": 3600, "done_hour": 0}}'
grep -q '^WARN  jobs: в очереди 4 заданий, за час не выполнено ни одного: бота — ждут 4, самое старое 60 мин (строка approvals-bot); неудачных за всё время: 3021$' "$tmp/out.txt" \
  || bad "короткая стоящая очередь бота — не WARN"
# Только что поставленные задания после тихого часа — не «стоит».
jobs_status 3 '{"llm": {"waiting": 3, "ready_age_s": 60, "done_hour": 0}, "bot": {"waiting": 0, "ready_age_s": 0, "done_hour": 0}}'
grep -q '^PASS  jobs: очередь заданий: 3, за час выполнено: 0; неудачных за всё время: 3021$' "$tmp/out.txt" || bad "свежая очередь — не PASS"
# Сервис прежней версии (без jobs_queue) — прежнее правило: больше 50 — WARN.
jobs_status 86 ''
grep -q '^WARN  jobs: в очереди 86 заданий — их некому выполнять: смотрите строки approvals-bot и model; неудачных за всё время: 3021$' "$tmp/out.txt" \
  || bad "прежний сервис, длинная очередь — не WARN"

# Строка updates (Р-101): сервис прежней версии — строки нет; дальше — по объекту updates в /api/status.
grep -q '  updates:' "$tmp/out.txt" && bad "у сервиса без объекта updates строки быть не должно"
updates_status() {
  python3 - "$fx/status.json" "$1" > "$tmp/status-upd.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
base = {"enabled": True, "channel": "pilot", "running": {"version": "0.0.10", "tag": "v0.0.10-rc.1",
        "commit": "0123456789ab", "hermes": None}, "checked_age_s": 7200, "problem": None, "fails": 0,
        "expires": "2026-12-01", "available": None, "available_kind": None, "behind": 0, "hermes_new": None}
base.update(json.loads(sys.argv[2]))
d["updates"] = base
print(json.dumps(d))
PY
  sed -i "s|$tmp/status-[a-z]*.json|$tmp/status-upd.json|" "$tmp/stub/docker"
  ./ops/doctor.sh > "$tmp/out.txt" 2>&1 || true
  grep -q "line [0-9]*:" "$tmp/out.txt" && bad "ошибки оболочки в doctor (updates)"
  true
}
updates_status '{}'
grep -q '^PASS  updates: работает v0.0.10-rc.1 (коммит 0123456); новых версий нет (проверено 2 ч назад, канал pilot, список годен до 2026-12-01)$' "$tmp/out.txt" \
  || bad "нет PASS updates «новых версий нет»"
updates_status '{"available": "v0.0.11", "available_kind": "regular", "behind": 2, "hermes_new": "v2026.11.2"}'
grep -q '^PASS  updates: .*доступна новая версия v0.0.11 (выпусков впереди: 2, канал pilot) — ./ops/update.sh --check; проверена новая версия Hermes v2026.11.2$' "$tmp/out.txt" \
  || bad "нет PASS updates «доступна»"
updates_status '{"available": "v0.0.11", "available_kind": "security", "behind": 1}'
grep -q '^WARN  updates: .*исправление безопасности v0.0.11' "$tmp/out.txt" || bad "исправление безопасности — не WARN"
updates_status '{"problem": "expired", "expires": null}'
grep -q '^WARN  updates: .*просрочен' "$tmp/out.txt" || bad "просроченный список — не WARN"
updates_status '{"problem": "unreachable", "fails": 1}'
grep -q '^PASS  updates: .*не удалась (unreachable), повторит через сутки$' "$tmp/out.txt" || bad "одна неудача — не PASS"
updates_status '{"problem": "unreachable", "fails": 3}'
grep -q '^WARN  updates: .*GitHub не отвечает сервису 3 проверок подряд' "$tmp/out.txt" || bad "три неудачи — не WARN"
updates_status '{"enabled": false, "checked_age_s": null}'
grep -q '^PASS  updates: .*выключена (SHTURMAN_UPDATES=off)' "$tmp/out.txt" || bad "выключено — не PASS"
updates_status '{"checked_age_s": null}'
grep -q '^PASS  updates: .*ещё не проверялся' "$tmp/out.txt" || bad "ещё не проверялся"
# Сервис запущен с другим коммитом, чем в каталоге, — WARN и ./ops/up.sh.
git init -q . && git -c user.name=t -c user.email=t@example.com commit -q --allow-empty -m x
updates_status '{}'
grep -q '^WARN  updates: работает v0.0.10-rc.1 (коммит 0123456); сервис запущен с другим кодом, чем в каталоге' "$tmp/out.txt" \
  || bad "другой код — не WARN"

# Строка archive-guard (Р-104): сервис прежней версии — строки нет; дальше — по объекту archive_guard в /api/status.
grep -q '  archive-guard:' "$tmp/out.txt" && bad "у сервиса без archive_guard строки быть не должно"
guard_status() {
  python3 - "$fx/status.json" "$1" > "$tmp/status-guard.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
base = {"state": "open", "reason": None, "since": None, "quiet": False, "canary": True, "canaries": 1,
        "limits": {"hour_messages": 3000, "hour_chars": 600000, "day_messages": 12000, "day_chars": 2500000},
        "used": {"mcp": {"hour_messages": 140, "hour_chars": 9000, "day_messages": 900, "day_chars": 80000}},
        "alarms_7d": 0, "canary_hits_7d": 0, "refused_24h": 0}
base.update(json.loads(sys.argv[2]))
d["archive_guard"] = base
print(json.dumps(d))
PY
  sed -i "s|$tmp/status-[a-z]*.json|$tmp/status-guard.json|" "$tmp/stub/docker"
  ./ops/doctor.sh > "$tmp/out.txt" 2>&1 || true
  grep -q "line [0-9]*:" "$tmp/out.txt" && bad "ошибки оболочки в doctor (archive-guard)"
  true
}
guard_status '{}'
grep -q '^PASS  archive-guard: выдача архива ассистенту в пределах: за час 140 из 3000 сообщений, за сутки 900 из 12000; ловушек: 1' "$tmp/out.txt" \
  || bad "нет PASS archive-guard"
guard_status '{"state": "slowed", "reason": "walk", "alarms_7d": 1}'
grep -q '^WARN  archive-guard: выдача архива приторможена до решения владельца (перебор архива)' "$tmp/out.txt" || bad "приторможено — не WARN"
guard_status '{"state": "closed", "reason": "canary", "alarms_7d": 1, "canary_hits_7d": 1}'
grep -q '^FAIL  archive-guard: ассистент получил ловушку' "$tmp/out.txt" || bad "ловушка — не FAIL"
guard_status '{"state": "closed", "reason": "owner"}'
grep -q '^WARN  archive-guard: доступ ассистента к архиву закрыт (закрыто владельцем)' "$tmp/out.txt" || bad "закрыто владельцем — не WARN"

# Д-54: с Hermes и без своей модели сервиса — строка model о том, что обработку переписки делает модель ассистента,
# а закрытая выдача архива говорит, что задания модели она не останавливает. Со своей моделью — без приписки.
printf 'SHTURMAN_MODE=hermes\n' > .env
sed -i 's|^  "inspect shturman-service") echo .\[\]. ;;|&\n  "inspect -f {{.State.Status}} shturman-hermes") echo running ;;\n  "inspect -f {{.Config.Image}} shturman-hermes") echo nousresearch/hermes-agent:v2026.9.24 ;;\n  "inspect -f {{.RestartCount}} shturman-hermes") echo 0 ;;|' "$tmp/stub/docker"
sed -i "s|echo '{\"bot\": {}, \"llm\": {}}'|echo '{\"bot\": {}, \"llm\": {\"configured\": false}}'|" "$tmp/stub/docker"
python3 - "$tmp/status-guard.json" > "$tmp/status-nollm.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["own_llm"] = False
print(json.dumps(d))
PY
sed -i "s|$tmp/status-guard.json|$tmp/status-nollm.json|" "$tmp/stub/docker"
./ops/doctor.sh > "$tmp/out.txt" 2>&1 || true
grep -q "line [0-9]*:" "$tmp/out.txt" && bad "ошибки оболочки в doctor (Hermes без своей модели)"
grep -q '^PASS  model: своей модели у сервиса нет: обработка переписки идёт через Hermes' "$tmp/out.txt" \
  || bad "нет строки model без своей модели"
grep -q '^WARN  archive-guard: доступ ассистента к архиву закрыт (закрыто владельцем).*Обработка переписки при этом идёт через Hermes' \
  "$tmp/out.txt" || bad "закрытая выдача без приписки о Hermes"
sed -i 's|"own_llm": false|"own_llm": true|' "$tmp/status-nollm.json"
./ops/doctor.sh > "$tmp/out.txt" 2>&1 || true
grep -q '^WARN  archive-guard: доступ ассистента к архиву закрыт' "$tmp/out.txt" || bad "нет WARN archive-guard"
grep -q 'Обработка переписки при этом идёт через Hermes' "$tmp/out.txt" && bad "приписка о Hermes при своей модели"
echo "doctor rows: ok"
