#!/usr/bin/env bash
# Проверка ./ops/diag.sh целиком (Р-98): подставной Docker отдаёт журналы с заведомо личными и секретными
# строками (fixtures/), подставной doctor — строки с адресами и токеном; в собранный архив не попадает
# ни одна строка из fixtures/secrets.txt. Запуск из корня репозитория: bash ops/tests/diag.sh.
set -eu
src="$(cd "$(dirname "$0")/../.." && pwd)"
fx="$src/ops/tests/fixtures"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/repo/local" "$tmp/stub"
cp -r "$src/ops" "$tmp/repo/"
printf '0.0.7\n' > "$tmp/repo/VERSION"
printf 'SHTURMAN_MODE=hermes\nTELEGRAM_BOT_TOKEN=123456789:AAH-secret-bot-token-ABCDEFGHIJKLMNOP\n' > "$tmp/repo/.env"
printf 'address=domain\npublic=https://assistant.example.com\n' > "$tmp/repo/local/state"
printf '2026-10-12T10:01:00Z отправил Иван Петров ссылку https://setup.example.com:8443/shturman-setup/#k=onetime\n' > "$tmp/repo/local/journal.log"
cp "$fx/doctor.txt" "$tmp/doctor.txt"
printf '#!/usr/bin/env bash\ncat "%s/doctor.txt"\n' "$tmp" > "$tmp/repo/ops/doctor.sh"
cat > "$tmp/stub/docker" <<EOF
#!/bin/sh
case "\$*" in
  "logs --tail 3000 shturman-service") cat "$fx/service.log" ;;
  "logs --tail 2000 shturman-postgres") echo "ERROR:  duplicate key value violates unique constraint: (Иван Петров, +7 916 123-45-67)" ;;
  inspect*shturman-hermes*|inspect*shturman-postgres*) echo running ;;
  inspect*) exit 1 ;;
  exec*shturman-hermes*) cat "$fx/hermes-errors.log" ;;
  exec*shturman-postgres*du*) echo "1200	/var/lib/postgresql/data" ;;
  "ps -a"*) printf 'shturman-hermes\tnousresearch/hermes-agent:v2026.9.24\tUp 2 hours\nshturman-postgres\tpgvector/pgvector:0.8.7-pg16\tUp 2 hours (healthy)\nother\timage\tUp\n' ;;
  "version --format"*) echo 27.3.1 ;;
  "compose version --short") echo 2.29.7 ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$tmp/stub/docker" "$tmp/repo/ops/doctor.sh"
export PATH="$tmp/stub:$PATH"
cd "$tmp/repo"
bad() { echo "ОШИБКА: $*" >&2; exit 1; }

./ops/diag.sh --print > "$tmp/out.txt" 2>&1 || bad "diag.sh завершился с ошибкой"
archive="$(find local/diag -name 'shturman-diag-*.tar.gz' | head -n 1)"
[ -n "$archive" ] || bad "архив не создан"
[ "$(stat -c '%a' "$archive")" = 600 ] || bad "архив не 600"
mkdir "$tmp/unpacked"; tar -xzf "$archive" -C "$tmp/unpacked"
inside="$(find "$tmp/unpacked" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
for f in versions.txt state.txt doctor.txt containers.txt disk.txt service-log.txt hermes-log.txt other-logs.txt; do
  [ -s "$inside/$f" ] || bad "в архиве нет $f"
done
grep -q "service-log.txt" "$tmp/out.txt" || bad "список файлов не показан"
while IFS= read -r secret; do
  [ -n "$secret" ] || continue
  if grep -rqF -- "$secret" "$tmp/unpacked" "$tmp/out.txt"; then bad "в пакет попало: $secret"; fi
done < "$fx/secrets.txt"
grep -rqF ivanpetrov "$tmp/unpacked" && bad "в пакет попало имя у посредника"
grep -q "WARNING shturman.tg \[AuthKeyUnregisteredError\]" "$inside/service-log.txt" || bad "вид ошибки сервиса потерян"
grep -q "FAIL  tg-session" "$inside/doctor.txt" || bad "строки doctor потеряны"
grep -q "shturman-hermes" "$inside/containers.txt" || bad "контейнеры потеряны"
grep -q "^other" "$inside/containers.txt" && bad "чужой контейнер попал в пакет"
[ -z "$(find local/diag -mindepth 1 -maxdepth 1 -type d)" ] || bad "рабочий каталог не убран"
echo "diag: ok"
