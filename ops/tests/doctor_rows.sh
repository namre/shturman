#!/usr/bin/env bash
# Проверка новых строк ./ops/doctor.sh (Р-98) с подставными Docker и curl: tg-session, pulse, disk-usage
# в режиме без Hermes; сервис «отвечает» ответом из fixtures/status.json. Заодно ловит ошибки оболочки
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
echo "doctor rows: ok"
