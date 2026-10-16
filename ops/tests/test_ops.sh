#!/usr/bin/env bash
# Сценарные тесты ops/ без настоящего Docker (подставной docker — ops/tests/stub/docker):
#   замок от параллельного запуска (Д-9); подтверждение человеком только в терминале; копия, её опись,
#   ротация, внешний каталог и проверка; восстановление каталогов и .env; отказ восстанавливать копию
#   более новой версии; проверка свежей копии в up.sh; выбор версии в update.sh (выпуски, кандидаты,
#   перескок, неверный тег, откат назад), разрешение человека, решение «откатывать ли базу», успех,
#   откат только кода, остановка с откатом для человека и сам откат.
# Настоящая база (pg_dump → порча → восстановление → сверка строк) — ops/tests/e2e_restore.sh.
# Запуск из корня репозитория: bash ops/tests/test_ops.sh
set -eu
root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
holder=""
cleanup() { [ -n "$holder" ] && kill "$holder" 2> /dev/null; rm -rf "$tmp"; }
trap cleanup EXIT
export PATH="$root/ops/tests/stub:$PATH" STUB_LOG="$tmp/docker.log" OPS_UPDATE_RETRY_PAUSE=0
unset STUB_PG STUB_APPLIED STUB_APPLIED_FILE STUB_HERMES_IMAGE STUB_IMAGES STUB_COMPOSE_OK SHTURMAN_BACKUP_DIR 2> /dev/null || true
pty="$root/ops/tests/pty_confirm.py"
n=0
ok() { n=$((n + 1)); echo "ok $n — $1"; }
die() { echo "ПРОВАЛ — $1"; echo "--- вывод:"; sed 's/^/    /' "$tmp/out" 2> /dev/null || true; exit 1; }
# Запуск с перехватом: код — в $code, вывод — в $tmp/out. Без управляющего терминала (setsid).
run() { code=0; setsid -w "$@" > "$tmp/out" 2>&1 < /dev/null || code=$?; }
# Запуск в псевдотерминале: «человек» набирает фразу (right), не ту (wrong) или ничего (none).
run_tty() { local how="$1"; shift; code=0; python3 "$pty" "$how" -- "$@" > "$tmp/out" 2>&1 || code=$?; }
has() { grep -q -- "$1" "$tmp/out" || die "${2:-нет строки «$1»}"; }
hasnt() { if grep -q -- "$1" "$tmp/out"; then die "${2:-лишняя строка «$1»}"; fi; }
want() { [ "$code" -eq "$1" ] || die "${2:-код $code вместо $1}"; }

# Экземпляр-песочница: ops/, config/, миграции, VERSION; .env без настоящих секретов.
mkinst() {
  local d="$tmp/$1"
  mkdir -p "$d/service/src/shturman" "$d/data/shturman"
  cp -r "$root/ops" "$root/config" "$root/.env.example" "$root/VERSION" "$d/"
  cp -r "$root/service/src/shturman/migrations" "$d/service/src/shturman/"
  printf 'SHTURMAN_MODE=standalone\nSHTURMAN_BOT_TOKEN=secret-token-one\n' > "$d/.env"; chmod 600 "$d/.env"
  echo "$d"
}
stamps() { find "$1" -maxdepth 1 -name 'meta-*.txt' | wc -l; }

# --- 1. Замок ---
d="$(mkinst lock)"; cd "$d"
bash -c '. ops/lib.sh; ops_lock "тестовое долгое дело"; exec sleep 60' &
holder=$!
for _ in $(seq 1 50); do grep -q 'тестовое' local/ops.lock 2> /dev/null && break; sleep 0.1; done
run ./ops/backup.sh; want 75 "второй запуск backup.sh: код $code вместо 75"; has "Уже идёт: тестовое долгое дело"; has "ничего не изменено"
ok "backup.sh при занятом замке — код 75 и что именно идёт"
run ./ops/up.sh; want 75; has "Уже идёт: тестовое долгое дело"
ok "up.sh при занятом замке — код 75"
run ./ops/restore.sh --from 20990101T000000Z; want 1; hasnt "Уже идёт"
ok "restore.sh: проверка копии идёт до замка и без него"
run ./ops/backup.sh --list; want 0
ok "чтение (--list) замок не берёт"
code=0; OPS_LOCK_FD=9 setsid -w ./ops/backup.sh 9> /dev/null > "$tmp/out" 2>&1 || code=$?; want 75 "подставленная переменная без унаследованного замка прошла: код $code"
ok "одной переменной OPS_LOCK_FD мало — нужен унаследованный замок"
kill "$holder"; wait "$holder" 2> /dev/null || true; holder=""
run bash -c '. ops/lib.sh; ops_lock "родитель"; ./ops/backup.sh'; want 0 "дочерний скрипт под замком родителя: код $code"; has "Копия готова"
ok "дочерний скрипт ops/ работает под замком родителя (update.sh → backup.sh)"
run ./ops/backup.sh; want 0
ok "после снятия замка — обычный запуск"

# --- 2. Копия: опись, ротация, внешний каталог, проверка ---
d="$(mkinst bk)"; cd "$d"; echo v1 > data/shturman/file
run ./ops/backup.sh --set-dir relative/path; want 2
run ./ops/backup.sh --set-dir "$tmp/ext"; want 1; has "нельзя писать"
run ./ops/backup.sh --set-keep 1; want 2
mkdir "$tmp/ext"
run ./ops/backup.sh --set-keep 2; want 0
run ./ops/backup.sh --set-dir "$tmp/ext"; want 0; has "та же\|том же диске"
grep -qx "SHTURMAN_BACKUP_DIR=$tmp/ext" .env && grep -qx 'SHTURMAN_BACKUP_KEEP=2' .env && grep -q 'secret-token-one' .env || die "настройки копий записаны неверно"
[ "$(stat -c %a .env)" = 600 ] || die "права .env"
ok "--set-dir и --set-keep: проверка пути и числа, запись строк .env, прочее не тронуто"
for _ in 1 2 3; do run ./ops/backup.sh; want 0; done
has "Копия положена во внешний каталог"
hasnt "secret-token"
[ "$(stamps local/backups)" -eq 2 ] && [ "$(stamps "$tmp/ext")" -eq 2 ] || die "ротация: хранится не по 2 копии"
ok "ротация SHTURMAN_BACKUP_KEEP=2 — здесь и во внешнем каталоге"
m="$(find local/backups -name 'meta-*.txt' | sort | tail -n 1)"
grep -q '^verified=yes$' "$m" && grep -q '^data_sha256=[0-9a-f]\{64\}$' "$m" && grep -q "^version=$(cat VERSION)$" "$m" && grep -q '^reason=manual$' "$m" || die "в описи нет версии, суммы или итога проверки"
if grep -q 'secret-token' "$m"; then die "в описи секрет"; fi
[ "$(stat -c %a "$m")" = 600 ] || die "права описи не 600"
ok "опись: версия, сумма, проверена; без секретов; права 600"
run ./ops/backup.sh --list; want 0; [ "$(grep -c 'внешний каталог' "$tmp/out")" -eq 2 ] || die "--list: не видно внешнего каталога"; has "версия $(cat VERSION)"
ok "--list: версия кода и где лежит копия"
run ./ops/backup.sh --verify; want 0; has "ИТОГ ПРОВЕРКИ КОПИИ: OK"
s="$(basename "$m" .txt)"; s="${s#meta-}"
printf 'x' >> "$tmp/ext/data-$s.tar.gz"
run ./ops/backup.sh --verify "$s"; want 1; has "НЕ совпала"
ok "--verify ловит испорченную копию во внешнем каталоге"
rm -rf "$tmp/ext"
run ./ops/backup.sh; want 3; has "недоступен"; [ "$(stamps local/backups)" -eq 2 ] || die "локальная копия должна остаться"
ok "внешний каталог недоступен — код 3, локальная копия есть"

# --- 3. Восстановление каталогов и .env, подтверждение человеком ---
d="$(mkinst rs)"; cd "$d"; echo v1 > data/shturman/file
run ./ops/backup.sh; want 0
s="$(./ops/backup.sh --list | awk 'NR == 1 {print $1}')"
echo v2 > data/shturman/file; echo new > data/shturman/new
sed -i 's/secret-token-one/secret-token-two/' .env
run ./ops/restore.sh --from "$s" --dry-run; want 0; has "ЗАМЕНЯЮТСЯ копией: data/shturman"; has "ssh -t"
[ "$(cat data/shturman/file)" = v2 ] || die "--dry-run что-то изменил"
ok "--dry-run: план, ничего не меняет"
run ./ops/restore.sh --from "$s"; want 4; has "ssh -t shturman-app"; [ "$(cat data/shturman/file)" = v2 ] || die "без терминала восстановил"
ok "без терминала — отказ (код 4) и команда для человека"
run_tty none ./ops/restore.sh --from "$s"; want 4; [ "$(cat data/shturman/file)" = v2 ] || die "без фразы восстановил"
run_tty wrong ./ops/restore.sh --from "$s"; want 4; has "Не подтверждено"; [ "$(cat data/shturman/file)" = v2 ] || die "с неверной фразой восстановил"
ok "неверная фраза или пустой ввод — отказ, данные не тронуты"
printf '#!/bin/sh\necho "подставной up.sh"\nexit 0\n' > ops/up.sh
run_tty right ./ops/restore.sh --from "$s"; want 0 "восстановление: код $code"; has "ИТОГ ВОССТАНОВЛЕНИЯ: OK"; hasnt "secret-token"
[ "$(cat data/shturman/file)" = v1 ] && [ ! -e data/shturman/new ] || die "каталог не восстановлен"
grep -q 'secret-token-two' .env || die ".env заменён без --with-env"
[ ! -d "$(find local/restore -mindepth 1 -maxdepth 1 -type d | head -n 1)/replaced" ] || die "прежние каталоги не убраны"
./ops/backup.sh --list | grep -q 'причина: before-restore' || die "нет копии перед восстановлением"
ok "фраза верна — каталоги восстановлены, .env прежний, копия «до» сделана"
run_tty right ./ops/restore.sh --from "$s" --with-env; want 0; grep -q 'secret-token-one' .env || die "--with-env не вернул .env"; [ "$(stat -c %a .env)" = 600 ] || die "права .env"
ok "--with-env возвращает .env из копии (права 600)"
echo v3 > data/shturman/file
run_tty right ./ops/restore.sh --from "$s" --no-start; want 0; has "стек не запущен"; hasnt "подставной up.sh"; [ "$(cat data/shturman/file)" = v1 ] || die "--no-start не восстановил"
ok "--no-start: данные восстановлены, стек не запускается (следом — код другой версии)"
# Копия более новой версии: в описи миграция, которой нет в коде.
export STUB_PG=running STUB_APPLIED="0001_archive.sql,9999_future.sql"
run ./ops/backup.sh; want 0
s="$(./ops/backup.sh --list | awk 'NR == 1 {print $1}')"
run ./ops/restore.sh --from "$s" --dry-run; want 1; has "более новой версией"; has "9999_future.sql"
unset STUB_PG STUB_APPLIED
ok "копия более новой версии кода — отказ до подтверждения"

# --- 4. Решение «что откатывать» ---
(
  cd "$d"; . ops/lib.sh; . ops/lib-backup.sh
  [ "$(rollback_scope a,b a,b - -)" = code ] || exit 1
  [ "$(rollback_scope a a,b - -)" = data ] || exit 2
  [ "$(rollback_scope '?' a - -)" = data ] || exit 3
  [ "$(rollback_scope a a h:1 h:2)" = data ] || exit 4
  [ "$(rollback_scope a a h:1 h:1)" = code ] || exit 5
) || die "rollback_scope: неверное решение (случай $?)"
ok "откат базы — только если миграции применились, неизвестно или сменился Hermes"

# --- 5. up.sh: свежая копия перед миграциями и сменой Hermes ---
d="$(mkinst up)"; cd "$d"; mkdir -p data/postgres
all="$(cd service/src/shturman/migrations && ls -1 ./*.sql | sed 's|^\./||' | LC_ALL=C sort | paste -sd, -)"
but_last="$(printf '%s' "$all" | tr ',' '\n' | head -n -1 | paste -sd, -)"
export STUB_PG=running
: > "$STUB_LOG"
STUB_APPLIED="$but_last" run ./ops/up.sh; want 1; has "Ждут: миграции базы: 1"; has "Свежей проверенной копии"
if grep -q 'compose build\|compose up -d --remove-orphans' "$STUB_LOG"; then die "up.sh дошёл до сборки без копии"; fi
ok "up.sh: ждут миграции, копии нет — остановка до перезапуска"
STUB_APPLIED="$but_last" run ./ops/up.sh --skip-backup-check; want 4; has "ssh -t"
ok "up.sh --skip-backup-check без терминала — отказ, код 4"
STUB_APPLIED="$but_last" run ./ops/backup.sh; want 0
: > "$STUB_LOG"
STUB_APPLIED="$but_last" run ./ops/up.sh; has "Свежая копия есть"; grep -q 'compose config --quiet' "$STUB_LOG" || die "после свежей копии up.sh не пошёл дальше"
ok "up.sh: свежая копия есть — идёт дальше"
rm -rf local/backups
: > "$STUB_LOG"
STUB_APPLIED="$all" run ./ops/up.sh; hasnt "Ждут:"; grep -q 'compose config --quiet' "$STUB_LOG" || die "без миграций up.sh должен идти дальше"
ok "up.sh: миграций нет — копия не требуется"
printf 'SHTURMAN_MODE=hermes\n' > .env
STUB_APPLIED="$all" STUB_HERMES_IMAGE="nousresearch/hermes-agent:v2026.9.24" STUB_IMAGES="nousresearch/hermes-agent:v2099.1.1" run ./ops/up.sh
want 1; has "смена образа Hermes: nousresearch/hermes-agent:v2026.9.24 → nousresearch/hermes-agent:v2099.1.1"; has "нет в списке проверенных"
ok "up.sh: смена образа Hermes без копии — остановка и предупреждение о непроверенной версии"
unset STUB_PG

# --- 6. update.sh: выпуски, разрешение, успех, откат ---
o="$tmp/origin"; mkdir -p "$o"; cd "$o"
git init -q -b main
g() { git -c user.name=test -c user.email=test@example.com "$@"; }
cp -r "$root/ops" "$root/config" "$root/.env.example" .
rm -rf ops/tests
printf 'local/\ndata/\n.env\n' > .gitignore
cat > ops/doctor.sh <<'EOF'
#!/usr/bin/env bash
# Подставная проверка здоровья (тесты update.sh): FAIL, если версия кода названа в local/doctor-fail.
cd "$(dirname "$0")/.." || exit 1
case "${1:-}" in -h|--help) echo "подставная проверка"; exit 0 ;; "") ;; *) echo "неизвестный параметр: $1" >&2; exit 2 ;; esac
echo "PASS  mode: тест"
if [ -f local/doctor-fail ] && [ "$(cat local/doctor-fail)" = "$(cat VERSION)" ]; then echo "FAIL  service: подставной сбой"; echo; echo "ИТОГ: FAIL (1)"; exit 1; fi
echo; echo "ИТОГ: OK"
EOF
cat > ops/up.sh <<'EOF'
#!/usr/bin/env bash
# Подставной запуск стека (тесты update.sh): «применяет миграции» — пишет их в $STUB_APPLIED_FILE.
cd "$(dirname "$0")/.." || exit 1
case "${1:-}" in -h|--help) echo "подставной запуск"; exit 0 ;; esac
if [ -n "${STUB_APPLIED_FILE:-}" ]; then find service/src/shturman/migrations -name '*.sql' -printf '%f\n' | LC_ALL=C sort | paste -sd, - > "$STUB_APPLIED_FILE"; fi
echo "подставной up.sh: $(cat VERSION)"
./ops/doctor.sh
EOF
printf 'services:\n  hermes:\n    image: nousresearch/hermes-agent:${HERMES_VERSION:-v2026.9.24}\n' > compose.yaml
mkdir -p service/src/shturman/migrations
rel() {  # $1 — VERSION, $2 — новая миграция или пусто, $3… — теги; UPGRADING.md пишется до вызова
  local v="$1" mig="$2"; shift 2
  echo "$v" > VERSION
  [ -z "$mig" ] || echo "SELECT 1;" > "service/src/shturman/migrations/$mig"
  g add -A .; g commit -q -m "$v"
  for t in "$@"; do g tag "$t"; done
}
printf '# Обновление\n' > UPGRADING.md
rel 0.0.1 0001_a.sql v0.0.1
printf '# Обновление\n\n## 0.0.1 → 0.0.2\n\nМиграция 0002_b.sql.\n' > UPGRADING.md
rel 0.0.2 0002_b.sql v0.0.2
printf '# Обновление\n\n## 0.0.2 → 0.0.3 (кандидат)\n\nБез действий.\n\n## 0.0.1 → 0.0.2\n\nМиграция.\n' > UPGRADING.md
rel 0.0.3 "" v0.0.3-rc.1 v0.0.9
printf '# Обновление\n\n## 0.0.2 → 0.0.3\n\n**Стоп-точка (sudo, прокси).** Шаг вне скрипта.\n\n## 0.0.1 → 0.0.2\n\nМиграция.\n' > UPGRADING.md
rel 0.0.3 "" v0.0.3
rel 0.0.4 ""
git clone -q "$o" "$tmp/inst"; cd "$tmp/inst"; git checkout -q --detach v0.0.1
printf 'SHTURMAN_MODE=standalone\n' > .env; chmod 600 .env; mkdir -p data/shturman; echo v1 > data/shturman/file
c1="$(git rev-parse v0.0.1)"; c2="$(git rev-parse v0.0.2)"; c3="$(git rev-parse v0.0.3)"
export STUB_PG=running STUB_APPLIED_FILE="$tmp/applied"; echo "0001_a.sql" > "$STUB_APPLIED_FILE"

run ./ops/update.sh --check; want 0; has "Цель:     v0.0.2"; has "Миграции базы придут: 1 (0002_b.sql)"; has "Нужно разрешение человека"; has "update.sh --consent"
[ "$(git rev-parse HEAD)" = "$c1" ] || die "--check сменил код"
ok "--check: следующий выпуск, миграции, нужно разрешение; код не меняется"
run ./ops/update.sh --check --channel pilot; want 0; has "Цель:     v0.0.2"
run ./ops/update.sh --set-channel main; want 2
run ./ops/update.sh --set-channel pilot; want 0; grep -qx 'SHTURMAN_UPDATE_CHANNEL=pilot' .env || die "канал не записан"
run ./ops/update.sh --set-channel release; want 0
ok "канал обновлений: release или pilot в .env; main — только ключом"
run ./ops/update.sh --to v0.0.9; want 2; has "собран неверно"
ok "тег, у которого VERSION не совпадает, — отказ"
run ./ops/update.sh --to v0.0.3-rc.1; want 2; has "по одному выпуску"; has "v0.0.2"
ok "перескок через выпуск — отказ"
run ./ops/update.sh --channel nightly; want 2
run ./ops/update.sh; want 4; has "update.sh --consent"; has "no-auto-rollback"; [ "$(git rev-parse HEAD)" = "$c1" ] || die "без разрешения код сменился"
ok "миграции без разрешения человека и без терминала — код 4, ничего не изменено"
run ./ops/update.sh --consent; want 4
run_tty wrong ./ops/update.sh --consent; want 4; [ ! -f local/consent/update.env ] || die "неверная фраза записала разрешение"
ok "--consent: без терминала и с неверной фразой разрешение не записывается"

# Провал после миграций без разрешения: остановка, откат — человеку.
echo 0.0.2 > local/doctor-fail
run ./ops/update.sh --no-auto-rollback; want 3 "провал без разрешения: код $code"; has "ИТОГ ОБНОВЛЕНИЯ: FAIL — нужен человек"; has "restore.sh --rollback"
[ "$(git rev-parse HEAD)" = "$c2" ] || die "без разрешения код откатился сам"
grep -q '^result=failed-needs-human$' local/update/last.env && grep -q '^scope=data$' local/update/last.env || die "запись обновления неверна"
ok "миграции применились, проверка не прошла, разрешения нет — остановка (код 3), откат — команда человеку"
run ./ops/restore.sh --rollback --dry-run; want 0; has "база и каталоги data/ — из копии"
run ./ops/restore.sh --rollback; want 4
echo v2 > data/shturman/file
run_tty right ./ops/restore.sh --rollback; want 0 "откат человеком: код $code"; has "ИТОГ ОТКАТА: OK"; has "База восстановлена"
[ "$(git rev-parse HEAD)" = "$c1" ] || die "откат не вернул код"
[ "$(cat data/shturman/file)" = v1 ] || die "откат не вернул каталоги из копии"
grep -q '^result=rolled-back$' local/update/last.env || die "итог отката не записан"
ok "откат человеком: код, база (подставная) и каталоги — из копии до обновления"

# Разрешение заранее, затем обновление агентом без терминала — успех.
rm -f local/doctor-fail; echo "0001_a.sql" > "$STUB_APPLIED_FILE"
run_tty right ./ops/update.sh --consent; want 0; [ -f local/consent/update.env ] || die "разрешение не записано"
ok "--consent с верной фразой — разрешение на 2 часа"
run ./ops/update.sh; want 0 "обновление: код $code"; has "ИТОГ ОБНОВЛЕНИЯ: OK"
[ "$(git rev-parse HEAD)" = "$c2" ] || die "код не на v0.0.2"
[ ! -f local/consent/update.env ] || die "разрешение не израсходовано"
grep -q '^result=ok$' local/update/last.env || die "итог не записан"
./ops/backup.sh --list | grep -q 'причина: before-update' || die "нет копии перед обновлением"
ok "обновление с разрешением: копия, новый код, проверка — OK; разрешение одноразовое"
run ./ops/update.sh --to v0.0.1; want 2; has "не впереди"
ok "назад update.sh не ходит"

# Следующий выпуск без миграций, со стоп-точкой в UPGRADING.md; проверка не прошла — откат только кода.
run ./ops/update.sh; want 2; has "Стоп-точка"; [ "$(git rev-parse HEAD)" = "$c2" ] || die "стоп-точка не остановила"
ok "стоп-точка в разделе UPGRADING.md — нужен --upgrading-read"
echo 0.0.3 > local/doctor-fail; echo v2 > data/shturman/file
run ./ops/update.sh --upgrading-read; want 1 "откат кода: код $code"; has "ИТОГ ОБНОВЛЕНИЯ: ОТКАТ"; has "Откат: только код"
[ "$(git rev-parse HEAD)" = "$c2" ] || die "код не вернулся на v0.0.2"
[ "$(cat data/shturman/file)" = v2 ] || die "при откате только кода данные тронуты"
ok "провал без миграций — откат только кода, данные не трогаются (код 1)"
rm -f local/doctor-fail
run ./ops/update.sh --upgrading-read; want 0; [ "$(git rev-parse HEAD)" = "$c3" ] || die "не обновился до v0.0.3"
run ./ops/update.sh; want 0; has "Обновлений нет"
run ./ops/update.sh --check --channel main; want 0; has "Цель:     origin/main"
ok "последний выпуск — «обновлений нет»; main — только явно"

# --- Порог защиты: только человек в терминале (Р-99) ---
d="$(mkinst guard)"; cd "$d"
printf '#!/bin/sh\necho "подставной up.sh"\nexit 0\n' > ops/up.sh
run ./ops/guard.sh threshold 0.97; want 4; has "ssh -t shturman-app"; hasnt "подставной up.sh"
grep -q SHTURMAN_GUARD_THRESHOLD .env && die "без терминала порог записан"
run_tty wrong ./ops/guard.sh threshold 0.97; want 4; has "Не подтверждено"
grep -q SHTURMAN_GUARD_THRESHOLD .env && die "с неверной фразой порог записан"
run ./ops/guard.sh threshold 0.2; want 2; has "от 0.5 до 1"
run_tty right ./ops/guard.sh threshold 0,97; want 0 "порог: код $code"; has "подставной up.sh"
grep -q '^SHTURMAN_GUARD_THRESHOLD=0.97$' .env || die "порог не записан"
grep -q 'secret-token-one' .env || die "остальные строки .env тронуты"
ok "порог защиты меняет только человек фразой в терминале; без неё — код 4 и команда для человека"

echo "Все проверки пройдены: $n."
