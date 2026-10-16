#!/usr/bin/env bash
# Сквозная проверка копии и восстановления на настоящей базе (Postgres 16 с pgvector из compose.yaml):
#   1. база с миграциями сервиса и синтетическим архивом (чаты, сообщения, люди — без настоящих данных);
#   2. ./ops/backup.sh — копия с описью; ./ops/backup.sh --verify;
#   3. порча: часть сообщений удалена, чаты очищены, файл в data/ испорчен;
#   4. ./ops/restore.sh --from — «человек» набирает фразу в псевдотерминале; сверка числа строк
#      ключевых таблиц с состоянием до порчи и файла в data/;
#   5. переезд: базы, data/, .env и local/ нет совсем, копия — на «внешнем диске» (--dir);
#      восстановление на пустое место и та же сверка.
# Нужен Docker с Compose; запускает контейнер shturman-postgres (проект shturman) — на сервере
# с экземпляром НЕ запускать. Запуск из корня репозитория: bash ops/tests/e2e_restore.sh
#   E2E_FULL=1 — вместо подставного ./ops/up.sh (только база) настоящий: собирает образ сервиса,
#                тот сам применяет миграции, в конце — ./ops/doctor.sh. Дольше (сборка образа).
#   E2E_MESSAGES=N — сколько синтетических сообщений (по умолчанию 20000).
set -eu
root="$(cd "$(dirname "$0")/../.." && pwd)"
if docker inspect shturman-postgres > /dev/null 2>&1; then
  echo "Контейнер shturman-postgres уже есть — это экземпляр или прошлый прогон. Проверка не запускается." >&2
  exit 2
fi
tmp="$(mktemp -d)"
d="$tmp/inst"
cleanup() {
  ( cd "$d" 2> /dev/null && docker compose --profile hermes down -v --remove-orphans > /dev/null 2>&1 ) || true
  docker rm -f shturman-postgres shturman-service > /dev/null 2>&1 || true
  docker run --rm -v "$tmp:/t" pgvector/pgvector:0.8.7-pg16-bookworm rm -rf /t/inst/data /t/disk > /dev/null 2>&1 || true
  rm -rf "$tmp"
}
trap cleanup EXIT
pty="$root/ops/tests/pty_confirm.py"
msgs="${E2E_MESSAGES:-20000}"
n=0
ok() { n=$((n + 1)); echo "ok $n — $1"; }
die() { echo "ПРОВАЛ — $1"; [ -f "$tmp/out" ] && sed 's/^/    /' "$tmp/out" | tail -n 40; exit 1; }

mkdir -p "$d/service/src/shturman"
cp -r "$root/ops" "$root/config" "$root/compose.yaml" "$root/.env.example" "$root/VERSION" "$d/"
cp -r "$root/service/src/shturman/migrations" "$d/service/src/shturman/"
if [ "${E2E_FULL:-}" = 1 ]; then cp -r "$root/service" "$d/"; fi
cd "$d"
./ops/init-env.sh --auto --mode standalone > /dev/null
if [ "${E2E_FULL:-}" != 1 ]; then
  # Подставной запуск стека: только база. Миграции и запуск сервиса проверяет E2E_FULL=1.
  cat > ops/up.sh <<'EOF'
#!/usr/bin/env bash
# Подставной запуск стека для e2e_restore.sh: только база.
cd "$(dirname "$0")/.." || exit 1
case "${1:-}" in -h|--help) echo "подставной запуск: только база"; exit 0 ;; esac
docker compose up -d postgres > /dev/null
for _ in $(seq 1 60); do docker exec shturman-postgres pg_isready -h 127.0.0.1 -U shturman -d shturman > /dev/null 2>&1 && break; sleep 1; done
echo "ИТОГ: OK"
EOF
fi
mkdir -p data/shturman; echo "страница памяти v1" > data/shturman/marker

q() { docker exec -i shturman-postgres psql -U shturman -d shturman -XAtq -v ON_ERROR_STOP=1 "$@"; }
counts() { q -c "SELECT (SELECT count(*) FROM chats) || ' ' || (SELECT count(*) FROM messages) || ' ' || (SELECT count(*) FROM people) || ' ' || (SELECT count(*) FROM schema_migrations) || ' ' || (SELECT coalesce(sum(length(text)), 0) FROM messages)"; }

start=$(date +%s)
if [ "${E2E_FULL:-}" = 1 ]; then
  ./ops/up.sh > "$tmp/out" 2>&1 || true
  grep -q '^ИТОГ:' "$tmp/out" || die "настоящий up.sh не дошёл до doctor"
  grep -E '^(FAIL|ИТОГ)' "$tmp/out" | sed 's/^/    /'
else
  docker compose up -d postgres > /dev/null
  for _ in $(seq 1 60); do docker exec shturman-postgres pg_isready -h 127.0.0.1 -U shturman -d shturman > /dev/null 2>&1 && break; sleep 1; done
  q -c "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
  for f in service/src/shturman/migrations/*.sql; do
    q -1 < "$f" > /dev/null || die "миграция $(basename "$f") не легла"
    q -c "INSERT INTO schema_migrations (name) VALUES ('$(basename "$f")')"
  done
fi
mig="$(q -c "SELECT count(*) FROM schema_migrations")"
[ "$mig" -gt 30 ] || die "миграций в базе: $mig"
ok "база: Postgres 16 + pgvector, миграций сервиса: $mig ($(( $(date +%s) - start )) с)"

q > /dev/null <<SQL
INSERT INTO accounts (tg_user_id, label, role) VALUES (1000, 'тестовый владелец', 'owner') ON CONFLICT DO NOTHING;
INSERT INTO peers (class, tg_id, name) SELECT 'user', 5000 + g, 'Собеседник ' || g FROM generate_series(1, 200) g;
INSERT INTO chats (account_id, peer_id, type, title)
  SELECT (SELECT id FROM accounts WHERE tg_user_id = 1000), p.id, 'personal_chat', p.name FROM peers p WHERE p.tg_id > 5000;
INSERT INTO messages (chat_id, tg_message_id, sent_at, sender_name, is_outgoing, text, sources)
  SELECT c.id, g, now() - (g || ' minutes')::interval, 'Синтетика', g % 2 = 0,
         'Синтетическое сообщение ' || g || ' — обещаю прислать отчёт до пятницы', ARRAY['import']
  FROM chats c CROSS JOIN generate_series(1, $msgs / 200) g;
INSERT INTO people (display_name) SELECT 'Человек ' || g FROM generate_series(1, 150) g;
SQL
before="$(counts)"
ok "синтетический архив: чаты, сообщения, люди, миграции, сумма длин текстов — $before"

start=$(date +%s)
./ops/backup.sh > "$tmp/out" 2>&1 || die "backup.sh"
grep -q 'Копия базы готова' "$tmp/out" || die "нет копии базы"
s="$(./ops/backup.sh --list | awk 'NR == 1 {print $1}')"
grep -q "^counts_after=.*messages=$(echo "$before" | cut -d' ' -f2)" "local/backups/meta-$s.txt" || die "в описи нет числа сообщений"
./ops/backup.sh --verify > "$tmp/out" 2>&1 || die "--verify"
grep -q 'ИТОГ ПРОВЕРКИ КОПИИ: OK' "$tmp/out" || die "--verify без OK"
ok "копия $s ($(du -h "local/backups/db-$s.dump" | cut -f1) база, $(( $(date +%s) - start )) с), опись с числом строк, --verify OK"

q -c "DELETE FROM messages WHERE id % 3 = 0" > /dev/null
q -c "SET client_min_messages = warning; TRUNCATE chats CASCADE" > /dev/null
echo "испорчено" > data/shturman/marker
broken="$(counts)"
[ "$broken" != "$before" ] || die "порча не удалась"
ok "порча: $broken"

code=0; setsid -w ./ops/restore.sh --from "$s" > "$tmp/out" 2>&1 < /dev/null || code=$?
[ "$code" -eq 4 ] || die "без терминала: код $code вместо 4"
[ "$(counts)" = "$broken" ] || die "без подтверждения база изменилась"
ok "без человека в терминале — отказ, база не тронута"

start=$(date +%s)
code=0; python3 "$pty" right -- ./ops/restore.sh --from "$s" > "$tmp/out" 2>&1 || code=$?
[ "$code" -eq 0 ] || die "восстановление: код $code"
grep -q 'Число строк совпало с описью' "$tmp/out" || die "нет сверки с описью"
after="$(counts)"
[ "$after" = "$before" ] || die "после восстановления $after, а было $before"
[ "$(cat data/shturman/marker)" = "страница памяти v1" ] || die "файл в data/ не восстановлен"
[ "$(q -c "SELECT count(*) FROM pg_database WHERE datname IN ('shturman_restore', 'shturman_replaced')")" = 0 ] || die "остались временные базы"
./ops/backup.sh --list | grep -q 'причина: before-restore' || die "нет копии перед восстановлением"
if grep -q 'Синтетическое сообщение' "$tmp/out"; then die "в выводе восстановления текст сообщений"; fi
ok "восстановление ($(( $(date +%s) - start )) с): число строк и сумма длин текстов как до порчи ($after), data/ на месте, временных баз нет, копия «до» сделана, текста в выводе нет"

# Переезд: на «новом сервере» ничего нет, копия — на внешнем диске.
mkdir -p "$tmp/disk"; cp "local/backups/data-$s.tar.gz" "local/backups/db-$s.dump" "local/backups/meta-$s.txt" "$tmp/disk/"
docker compose --profile hermes down -v --remove-orphans > /dev/null 2>&1 || true
docker run --rm -v "$d:/i" pgvector/pgvector:0.8.7-pg16-bookworm rm -rf /i/data > /dev/null
rm -rf local .env
start=$(date +%s)
code=0; python3 "$pty" right -- ./ops/restore.sh --from "$s" --dir "$tmp/disk" > "$tmp/out" 2>&1 || code=$?
[ "$code" -eq 0 ] || die "восстановление на пустое место: код $code"
[ -f .env ] && [ "$(stat -c %a .env)" = 600 ] || die ".env не восстановлен"
after="$(counts)"
[ "$after" = "$before" ] || die "после переезда $after, а было $before"
[ "$(cat data/shturman/marker)" = "страница памяти v1" ] || die "data/ не восстановлен"
ok "переезд на пустое место с внешнего диска ($(( $(date +%s) - start )) с): .env, база ($after) и data/ на месте"

echo "Все проверки пройдены: $n."
[ -z "${E2E_SHOW:-}" ] || cat "$tmp/out"
