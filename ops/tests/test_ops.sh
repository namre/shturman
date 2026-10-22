#!/usr/bin/env bash
# Сценарные тесты ops/ без настоящего Docker (подставной docker — ops/tests/stub/docker):
#   замок от параллельного запуска (Д-9); разрешение владельца кнопкой в боте согласований — через сервис
#   и без него (Р-100), запасной путь — фраза человека только в терминале; копия, её опись,
#   ротация, внешний каталог и проверка; восстановление каталогов и .env; отказ восстанавливать копию
#   более новой версии; проверка свежей копии в up.sh; выбор версии в update.sh (выпуски, кандидаты,
#   перескок, неверный тег, откат назад), разрешение человека, решение «откатывать ли базу», успех,
#   откат только кода, остановка с откатом для человека и сам откат; --all — по выпуску за шаг до последнего,
#   итог каждого шага — сервису (Р-101); сброс истории репозитория — переход по ops/history-reset и отказ без него;
#   «Остановить всё» переживает восстановление базы, сервис, остановленный ради запроса напрямую, после отказа
#   запускается снова (Р-106).
# Настоящая база (pg_dump → порча → восстановление → сверка строк) — ops/tests/e2e_restore.sh.
# Запуск из корня репозитория: bash ops/tests/test_ops.sh
set -eu
root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
holder=""
cleanup() { [ -n "$holder" ] && kill "$holder" 2> /dev/null; rm -rf "$tmp"; }
trap cleanup EXIT
export PATH="$root/ops/tests/stub:$PATH" STUB_LOG="$tmp/docker.log" OPS_UPDATE_RETRY_PAUSE=0
unset STUB_PG STUB_APPLIED STUB_APPLIED_FILE STUB_HERMES_IMAGE STUB_IMAGES STUB_COMPOSE_OK SHTURMAN_BACKUP_DIR \
  STUB_SERVICE STUB_CONSENT STUB_IMAGE STUB_DIRECT STUB_CONSENT_STATUS STUB_RUNNING_COMMIT_FILE UP_FAIL STUB_RM_DUMPS \
  STUB_HALT_LIVE STUB_HALT_COPY STUB_HALT_FAIL 2> /dev/null || true
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

# --- 2б. Неполная копия (Д-57): опись есть, а файла из неё нет или он испорчен ---
d="$(mkinst partial)"; cd "$d"; echo v1 > data/shturman/file; mkdir -p data/postgres
all="$(cd service/src/shturman/migrations && ls -1 ./*.sql | sed 's|^\./||' | LC_ALL=C sort | paste -sd, -)"
but_last="$(printf '%s' "$all" | tr ',' '\n' | head -n -1 | paste -sd, -)"
export STUB_PG=running
run ./ops/backup.sh; want 0
s="$(./ops/backup.sh --list | awk 'NR == 1 {print $1}')"
[ "$(bash -c '. ops/lib.sh; . ops/lib-backup.sh; bk_fresh 60')" = "$s" ] || die "целая копия не считается свежей"
printf 'X' | dd of="local/backups/db-$s.dump" bs=1 seek=0 conv=notrunc 2> /dev/null   # тот же размер, другие байты
: > "$STUB_LOG"
STUB_APPLIED="$but_last" run ./ops/up.sh; want 1; has "не прошла проверку"; has "Свежей проверенной копии"
if grep -q 'compose build\|compose up -d --remove-orphans' "$STUB_LOG"; then die "up.sh пошёл к миграциям с испорченной копией"; fi
ok "up.sh: свежая копия испорчена (суммы не совпали) — на неё не полагается, остановка до миграций"
rm "local/backups/db-$s.dump"
run ./ops/backup.sh --verify "$s"; want 1 "--verify без выгрузки: код $code"; has "выгрузки НЕТ"; has "ИТОГ ПРОВЕРКИ КОПИИ: FAIL"
if bash -c '. ops/lib.sh; . ops/lib-backup.sh; bk_fresh 60' > /dev/null; then die "копия без выгрузки считается свежей"; fi
ok "--verify и bk_fresh: выгрузки базы, записанной в опись, нет — копия неполная (код 1)"
run ./ops/restore.sh --from "$s" --dry-run; want 1; has "выгрузки НЕТ"; has "только каталоги\|Только каталоги"; has "--only data"
run ./ops/restore.sh --from "$s" --only data --dry-run; want 0; has "каталоги ЗАМЕНЯЮТСЯ"; hasnt "база архива ЗАМЕНЯЕТСЯ"
ok "restore.sh --from: выгрузки нет — отказ (не «восстановлены только каталоги, OK»); только каталоги — осознанно, --only data"
rm "local/backups/data-$s.tar.gz"
if bash -c '. ops/lib.sh; . ops/lib-backup.sh; bk_verify local/backups "$1"' _ "$s" > /dev/null; then die "одна опись прошла проверку"; fi
: > "$STUB_LOG"
STUB_APPLIED="$but_last" run ./ops/up.sh; want 1; has "Свежей проверенной копии"; hasnt "Свежая копия есть"
if grep -q 'compose build' "$STUB_LOG"; then die "up.sh принял опись без файлов за копию"; fi
ok "одна опись без файлов — не копия: bk_verify и up.sh её не принимают"
mkdir -p "$tmp/ext-partial"; run ./ops/backup.sh --set-dir "$tmp/ext-partial"; want 0
run ./ops/backup.sh; want 0
s2="$(./ops/backup.sh --list | awk 'NR == 1 {print $1}')"
rm "local/backups/db-$s2.dump"
[ "$(bash -c '. ops/lib.sh; . ops/lib-backup.sh; bk_find "$1"' _ "$s2")" = "$tmp/ext-partial" ] || die "неполная копия здесь заслонила целую во внешнем каталоге"
run ./ops/restore.sh --from "$s2" --dry-run; want 0 "целая копия во внешнем каталоге: код $code"; has "Копия $s2 ($tmp/ext-partial)"; has "база архива ЗАМЕНЯЕТСЯ"
ok "неполная копия в local/backups не заслоняет целую во внешнем каталоге"
unset STUB_PG

# --- 3. Восстановление каталогов и .env, подтверждение человеком ---
d="$(mkinst rs)"; cd "$d"; echo v1 > data/shturman/file
run ./ops/backup.sh; want 0
s="$(./ops/backup.sh --list | awk 'NR == 1 {print $1}')"
echo v2 > data/shturman/file; echo new > data/shturman/new
sed -i 's/secret-token-one/secret-token-two/' .env
run ./ops/restore.sh --from "$s" --dry-run; want 0; has "ЗАМЕНЯЮТСЯ копией: data/shturman"; has "запрос в бот согласований"; hasnt "ssh -t"
[ "$(cat data/shturman/file)" = v2 ] || die "--dry-run что-то изменил"
ok "--dry-run: план, ничего не меняет"
run ./ops/restore.sh --from "$s"; want 4; has "ssh -t shturman-app"; [ "$(cat data/shturman/file)" = v2 ] || die "без терминала восстановил"
ok "бота нет и терминала нет — отказ (код 4) и команда для человека"
: > "$STUB_LOG"
STUB_SERVICE=running STUB_CONSENT=4 run ./ops/restore.sh --from "$s"; want 4; has "повторите: ./ops/restore.sh --from $s"; [ "$(cat data/shturman/file)" = v2 ] || die "без нажатия восстановил"
grep -q "ops-consent ask --action restore --target $s:all:keep" "$STUB_LOG" || die "запрос в бот без вида и цели"
grep -q -- "--detail parts=каталоги data/" "$STUB_LOG" || die "в карточке нет того, что заменится"
STUB_SERVICE=running STUB_CONSENT=5 run ./ops/restore.sh --from "$s"; want 4; [ "$(cat data/shturman/file)" = v2 ] || die "отклонённое восстановил"
hasnt "Наберите:"
ok "сервис работает: запрос в бот; владелец не ответил или отклонил — код 4, данные не тронуты, фразы не спрашивает"
STUB_SERVICE=running STUB_CONSENT=7 run ./ops/restore.sh --from "$s"; want 4; has "запасной путь\|Запасной путь"
grep -q "docker stop shturman-service" "$STUB_LOG" || die "зависший сервис не остановлен перед путём без сервиса"
grep -q "docker start shturman-service" "$STUB_LOG" || die "без разрешения остановленный ради запроса сервис не запущен снова"
has "запускаю контейнер сервиса снова"
: > "$STUB_LOG"
STUB_SERVICE=running STUB_CONSENT=7 STUB_IMAGE=1 STUB_DIRECT=5 run ./ops/restore.sh --from "$s"; want 4
[ "$(cat data/shturman/file)" = v2 ] || die "отклонённое напрямую восстановил"
grep -q "docker stop shturman-service" "$STUB_LOG" && grep -q "docker start shturman-service" "$STUB_LOG" \
  || die "после «Отклонить» на пути напрямую сервис оставлен остановленным"
ok "сервис остановлен ради запроса напрямую, владелец отклонил или бот недоступен — сервис запущен снова, данные не тронуты"
: > "$STUB_LOG"
STUB_IMAGE=1 STUB_DIRECT=5 run ./ops/restore.sh --from "$s"; want 4; [ "$(cat data/shturman/file)" = v2 ] || die "без сервиса восстановил отклонённое"
grep -q "compose run --rm --no-deps -T shturman ops-consent ask --action restore .*--direct" "$STUB_LOG" || die "путь без сервиса не через одноразовый контейнер"
ok "сервис не отвечает — запрос напрямую через бот (одноразовый контейнер); отклонено — ничего не тронуто"
run_tty none ./ops/restore.sh --from "$s"; want 4; [ "$(cat data/shturman/file)" = v2 ] || die "без фразы восстановил"
run_tty wrong ./ops/restore.sh --from "$s"; want 4; has "Не подтверждено"; [ "$(cat data/shturman/file)" = v2 ] || die "с неверной фразой восстановил"
ok "неверная фраза или пустой ввод — отказ, данные не тронуты"
printf '#!/bin/sh\necho "подставной up.sh"\nexit 0\n' > ops/up.sh
run_tty right ./ops/restore.sh --from "$s"; want 0 "восстановление: код $code"; has "ИТОГ ВОССТАНОВЛЕНИЯ: OK"; hasnt "secret-token"
[ "$(cat data/shturman/file)" = v1 ] && [ ! -e data/shturman/new ] || die "каталог не восстановлен"
grep -q 'secret-token-two' .env || die ".env заменён без --with-env"
[ ! -d "$(find local/restore -mindepth 1 -maxdepth 1 -type d | head -n 1)/replaced" ] || die "прежние каталоги не убраны"
./ops/backup.sh --list | grep -q 'причина: before-restore' || die "нет копии перед восстановлением"
ok "бота нет, фраза человека верна — каталоги восстановлены, .env прежний, копия «до» сделана"
echo v2 > data/shturman/file
STUB_SERVICE=running STUB_CONSENT=0 run ./ops/restore.sh --from "$s"; want 0 "восстановление по кнопке: код $code"; has "ИТОГ ВОССТАНОВЛЕНИЯ: OK"
[ "$(cat data/shturman/file)" = v1 ] || die "по кнопке владельца не восстановил"
echo v2 > data/shturman/file
STUB_IMAGE=1 STUB_DIRECT=0 run ./ops/restore.sh --from "$s"; want 0 "восстановление без сервиса: код $code"
[ "$(cat data/shturman/file)" = v1 ] || die "по кнопке без сервиса не восстановил"
ok "владелец нажал «Разрешить» (через сервис или напрямую) — восстановлено без терминала"
run_tty right ./ops/restore.sh --from "$s" --with-env; want 0; grep -q 'secret-token-one' .env || die "--with-env не вернул .env"; [ "$(stat -c %a .env)" = 600 ] || die "права .env"
ok "--with-env возвращает .env из копии (права 600)"
echo v3 > data/shturman/file
run_tty right ./ops/restore.sh --from "$s" --no-start; want 0; has "стек не запущен"; hasnt "подставной up.sh"; [ "$(cat data/shturman/file)" = v1 ] || die "--no-start не восстановил"
ok "--no-start: данные восстановлены, стек не запускается (следом — код другой версии)"
echo v1 > data/shturman/file; run ./ops/backup.sh; want 0; s="$(./ops/backup.sh --list | awk 'NR == 1 {print $1}')"
echo v2 > data/shturman/file; : > "$STUB_LOG"
STUB_SERVICE=running STUB_CONSENT=7 STUB_IMAGE=1 STUB_DIRECT=0 run ./ops/restore.sh --from "$s"; want 0 "напрямую после остановки зависшего: код $code"
[ "$(cat data/shturman/file)" = v1 ] || die "разрешённое напрямую не восстановил"
if grep -q "docker start shturman-service" "$STUB_LOG"; then die "с разрешением сервис запущен до восстановления"; fi
ok "зависший сервис остановлен, владелец разрешил напрямую — восстановление идёт без промежуточного запуска сервиса"
# Копия более новой версии: в описи миграция, которой нет в коде.
export STUB_PG=running STUB_APPLIED="0001_archive.sql,9999_future.sql"
run ./ops/backup.sh; want 0
s="$(./ops/backup.sh --list | awk 'NR == 1 {print $1}')"
run ./ops/restore.sh --from "$s" --dry-run; want 1; has "более новой версией"; has "9999_future.sql"
unset STUB_PG STUB_APPLIED
ok "копия более новой версии кода — отказ до подтверждения"

# --- 3б. «Остановить всё» переживает восстановление базы (Р-106, ОТ-51) ---
d="$(mkinst halt)"; cd "$d"; echo v1 > data/shturman/file; mkdir -p data/postgres
run ./ops/backup.sh --set-keep 30; want 0      # каждое восстановление снимает копию «до»: исходная не должна уйти в ротацию
STUB_APPLIED="$(cd service/src/shturman/migrations && ls -1 ./*.sql | sed 's|^\./||' | LC_ALL=C sort | paste -sd, -)"
export STUB_PG=running STUB_APPLIED
run ./ops/backup.sh; want 0
s="$(./ops/backup.sh --list | awk 'NR == 1 {print $1}')"
printf '#!/bin/sh\necho "подставной up.sh"\nexit 0\n' > ops/up.sh
export STUB_SERVICE=running STUB_CONSENT=0
STUB_HALT_LIVE=stopped run ./ops/restore.sh --from "$s" --dry-run; want 0; has "останется остановленным"
STUB_HALT_LIVE=running run ./ops/restore.sh --from "$s" --dry-run; want 0; hasnt "останется остановленным"
ok "план восстановления говорит, что остановка владельца сохранится"
echo v2 > data/shturman/file; : > "$STUB_LOG"
STUB_HALT_LIVE=stopped STUB_HALT_COPY=f run ./ops/restore.sh --from "$s"; want 0 "остановлен до, не в копии: код $code"
has "остановка сохранена"; has "Чтение переписки тоже остаётся на паузе"; has "ИТОГ ВОССТАНОВЛЕНИЯ: OK"
grep -q "shturman_restore .*WITH live(since, via, paused) AS (SELECT '2026-10-20 10:00:00+00'::timestamptz, 'bot'::text, '{7}'::bigint\[\])" "$STUB_LOG" \
  || die "отметка живой базы не перенесена в восстановленную"
upd="$(grep -n "UPDATE assistant_halt h SET stopped = true" "$STUB_LOG" | head -n 1 | cut -d: -f1)"
ren="$(grep -n "RENAME TO shturman_replaced" "$STUB_LOG" | head -n 1 | cut -d: -f1)"
[ -n "$upd" ] && [ -n "$ren" ] && [ "$upd" -lt "$ren" ] || die "отметка перенесена не до подмены базы"
[ "$(cat data/shturman/file)" = v1 ] || die "данные не восстановлены"
ok "остановлен до восстановления, в копии — нет: отметка и пауза чтения перенесены в восстановленную базу до подмены"
: > "$STUB_LOG"
STUB_HALT_LIVE=running STUB_HALT_COPY=t run ./ops/restore.sh --from "$s"; want 0; has "В копии ассистент остановлен"
if grep -q "UPDATE assistant_halt" "$STUB_LOG"; then die "не остановленный сейчас — отметка всё равно переписана"; fi
ok "не остановлен сейчас, остановлен в копии — остаётся остановленным (как в копии), владельцу сказано"
: > "$STUB_LOG"
STUB_HALT_LIVE=down STUB_HALT_COPY=f run ./ops/restore.sh --from "$s"; want 0; has "прочитать не удалось"; has "на всякий случай после восстановления он остановлен"
upd="$(grep -n "shturman_restore .*UPDATE assistant_halt h SET stopped = true, since = now(), via = NULL" "$STUB_LOG" | head -n 1 | cut -d: -f1)"
ren="$(grep -n "RENAME TO shturman_replaced" "$STUB_LOG" | head -n 1 | cut -d: -f1)"
[ -n "$upd" ] && [ -n "$ren" ] && [ "$upd" -lt "$ren" ] || die "нынешняя отметка не прочитана — восстановленная база не остановлена до подмены"
ok "нынешняя база не читается — восстановление идёт, ассистент на всякий случай остановлен, об этом сказано"
STUB_HALT_LIVE=stopped run ./ops/restore.sh --from "$s"; want 0; has "копия сделана до появления «Остановить всё»"
ok "копия старше «Остановить всё» — восстановление идёт, владельцу сказать, что остановить снова"
for bad in odd fail; do
  echo v2 > data/shturman/file; : > "$STUB_LOG"
  if [ "$bad" = odd ]; then STUB_HALT_LIVE=odd STUB_HALT_COPY=f run ./ops/restore.sh --from "$s"; has "неожиданного вида"
  else STUB_HALT_LIVE=stopped STUB_HALT_COPY=f STUB_HALT_FAIL=1 run ./ops/restore.sh --from "$s"; fi
  want 1 "перенос отметки не удался ($bad): код $code"; has "Живая база не тронута"
  if grep -q "RENAME TO shturman_replaced\|UPDATE assistant_halt h" "$STUB_LOG" && [ "$bad" = odd ]; then die "значение неожиданного вида попало в запрос"; fi
  if grep -q "RENAME TO" "$STUB_LOG"; then die "база подменена, хотя остановку перенести не удалось ($bad)"; fi
  [ "$(cat data/shturman/file)" = v2 ] || die "данные заменены, хотя восстановление базы остановилось ($bad)"
done
ok "отметку остановки перенести нельзя (значение не того вида, сбой записи) — база не подменена, данные не тронуты"
unset STUB_PG STUB_APPLIED STUB_SERVICE STUB_CONSENT

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
ok "up.sh --skip-backup-check без бота и терминала — отказ, код 4"
: > "$STUB_LOG"
STUB_SERVICE=running STUB_CONSENT=5 STUB_APPLIED="$but_last" run ./ops/up.sh --skip-backup-check; want 4
if grep -q 'compose build' "$STUB_LOG"; then die "up.sh без разрешения дошёл до сборки"; fi
grep -q "ops-consent ask --action no_backup --target [0-9a-f]\{16\} .*--detail need=миграции базы: 1" "$STUB_LOG" || die "запрос в бот без того, что ждёт"
: > "$STUB_LOG"
STUB_SERVICE=running STUB_CONSENT=0 STUB_APPLIED="$but_last" run ./ops/up.sh --skip-backup-check
grep -q 'compose config --quiet' "$STUB_LOG" || die "up.sh с разрешением владельца не пошёл дальше"
ok "up.sh --skip-backup-check: отклонено в боте — стоп; разрешено — дальше, без терминала"
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
# Подставной запуск стека (тесты update.sh): «применяет миграции» — пишет их в $STUB_APPLIED_FILE;
# «пересоздаёт» сервис — пишет коммит кода в $STUB_RUNNING_COMMIT_FILE (его отдаёт подставной docker).
# UP_FAIL=<VERSION> — сборка этой версии «не проходит»: выход до перезапуска, прежние контейнеры работают.
# STUB_RM_DUMPS=1 — «пропали» выгрузки базы в local/backups (Д-57: откат из неполной копии).
# STUB_SLOW_START=N — новый сервис первые N запросов состояния не отвечает (применяет миграции).
# STUB_LATE_MIG=1 — новые миграции ещё не легли; лягут к остановке сервиса (local/late-mig, подставной docker).
# STUB_NONE=1 — новый сервис отвечает, но не называет, с каким кодом работает.
# STUB_FLAKY=N — N-й запрос состояния после запуска один раз не получает ответа (сбой, а не долгий запуск).
cd "$(dirname "$0")/.." || exit 1
case "${1:-}" in -h|--help) echo "подставной запуск"; exit 0 ;; esac
if [ -n "${STUB_RM_DUMPS:-}" ]; then rm -f local/backups/db-*.dump; fi
if [ -n "${UP_FAIL:-}" ] && [ "$UP_FAIL" = "$(cat VERSION)" ]; then echo "подставной up.sh: docker compose build не прошёл"; exit 1; fi
if [ -n "${STUB_LATE_MIG:-}" ]; then
  find service/src/shturman/migrations -name '*.sql' -printf '%f\n' > local/late-mig
elif [ -n "${STUB_APPLIED_FILE:-}" ]; then find service/src/shturman/migrations -name '*.sql' -printf '%f\n' | LC_ALL=C sort | paste -sd, - > "$STUB_APPLIED_FILE"; fi
if [ -n "${STUB_SLOW_START:-}" ]; then echo "$STUB_SLOW_START" > local/status-wait; fi
rm -f local/status-fail-at local/status-calls; if [ -n "${STUB_FLAKY:-}" ]; then echo "$STUB_FLAKY" > local/status-fail-at; fi
if [ -n "${STUB_RUNNING_COMMIT_FILE:-}" ]; then
  mkdir -p "$(dirname "$STUB_RUNNING_COMMIT_FILE")"
  if [ -n "${STUB_NONE:-}" ]; then echo none > "$STUB_RUNNING_COMMIT_FILE"; else git rev-parse HEAD > "$STUB_RUNNING_COMMIT_FILE"; fi
fi
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
export STUB_PG=running STUB_APPLIED_FILE="$tmp/applied" STUB_RUNNING_COMMIT_FILE=local/running-commit OPS_UPDATE_POLL=0 OPS_UPDATE_WAIT=3; echo "0001_a.sql" > "$STUB_APPLIED_FILE"
# Контейнер сервиса «запущен» во всём разделе: по его GET /api/status update.sh узнаёт, какой код работает (Д-58).
# Бот согласований при этом «не настроен» (STUB_CONSENT по умолчанию 6) — как без сервиса.
export STUB_SERVICE=running

run ./ops/update.sh --check; want 0; has "Цель:     v0.0.2"; has "Миграции базы придут: 1 (0002_b.sql)"; has "Нужно разрешение владельца"; has "update.sh --consent"; has "бот согласований"
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
ok "миграции, бота нет и терминала нет — код 4, ничего не изменено"
: > "$STUB_LOG"
STUB_SERVICE=running STUB_CONSENT=4 run ./ops/update.sh; want 4; has "Нажмёт «Разрешить» — повторите: ./ops/update.sh"; has "no-auto-rollback"
[ "$(git rev-parse HEAD)" = "$c1" ] || die "без нажатия код сменился"
grep -q "ops-consent ask --action update --target $c2 .*--detail from=.*--detail to=v0.0.2 --detail scope=data --detail migrations=1 (0002_b.sql)" "$STUB_LOG" \
  || die "запрос в бот: не та цель или нет подробностей"
STUB_SERVICE=running STUB_CONSENT=5 run ./ops/update.sh; want 4; [ "$(git rev-parse HEAD)" = "$c1" ] || die "отклонённое обновление пошло"
hasnt "Наберите:"
ok "сервис работает: запрос в бот на этот коммит; не ответил или отклонил — код 4, ничего не изменено"
STUB_SERVICE=running STUB_CONSENT_STATUS="pending 14:00 (UTC)" run ./ops/update.sh --check; want 0; has "ждёт нажатия владельца (до 14:00 (UTC))"
STUB_SERVICE=running STUB_CONSENT_STATUS="granted 15:00 (UTC)" run ./ops/update.sh --check; want 0; has "Разрешение владельца в боте согласований есть"
ok "--check видит запрос в боте: ждёт нажатия или уже разрешено"
: > "$STUB_LOG"
STUB_SERVICE=running STUB_CONSENT=0 run ./ops/update.sh --consent; want 0; has "Разрешение получено"
grep -q "ops-consent ask --action update .*--keep" "$STUB_LOG" || die "--consent израсходовал разрешение сразу"
[ ! -f local/consent/update.env ] || die "разрешение из бота записано в файл: его держит сервис"
ok "--consent через бот: разрешение остаётся в сервисе до следующего запуска (--keep), файла нет"
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
: > "$STUB_LOG"
STUB_SERVICE=running STUB_CONSENT=5 run ./ops/restore.sh --rollback; want 4; [ "$(git rev-parse HEAD)" = "$c2" ] || die "отклонённый откат выполнен"
grep -q "ops-consent ask --action rollback --target $c1 .*--detail scope=data" "$STUB_LOG" || die "запрос отката без цели"
ok "откат: запрос в бот на прежний коммит; отклонено — ничего не тронуто"
echo v2 > data/shturman/file
run_tty right ./ops/restore.sh --rollback; want 0 "откат человеком: код $code"; has "ИТОГ ОТКАТА: OK"; has "База восстановлена"
[ "$(git rev-parse HEAD)" = "$c1" ] || die "откат не вернул код"
[ "$(cat data/shturman/file)" = v1 ] || die "откат не вернул каталоги из копии"
grep -q '^result=rolled-back$' local/update/last.env || die "итог отката не записан"
ok "откат человеком: код, база (подставная) и каталоги — из копии до обновления"

# Разрешение кнопкой в боте, обновление не прошло проверку — откат базы и данных сам, без терминала.
echo 0.0.2 > local/doctor-fail; echo "0001_a.sql" > "$STUB_APPLIED_FILE"; echo v1 > data/shturman/file
STUB_SERVICE=running STUB_CONSENT=0 run ./ops/update.sh; want 1 "откат по разрешению из бота: код $code"; has "ИТОГ ОБНОВЛЕНИЯ: ОТКАТ"; has "Откат: код, база и data/"
[ "$(git rev-parse HEAD)" = "$c1" ] || die "откат по разрешению из бота не вернул код"
grep -q '^consent=bot$' local/update/last.env || die "в записи обновления нет, что разрешил владелец в боте"
ok "владелец нажал «Разрешить» → обновление, провал → автоматический откат базы и data/ без терминала"

# Откат из неполной копии (Д-57): выгрузка базы, снятая перед обновлением, пропала — откат не начинается,
# код не возвращается на прежний поверх новых миграций, итог — «откат не удался».
echo 0.0.2 > local/doctor-fail; echo "0001_a.sql" > "$STUB_APPLIED_FILE"
: > "$STUB_LOG"
STUB_RM_DUMPS=1 STUB_SERVICE=running STUB_CONSENT=0 run ./ops/update.sh; want 3 "откат из неполной копии: код $code"
has "выгрузки НЕТ"; has "Откат не начат"; has "ИТОГ ОБНОВЛЕНИЯ: FAIL — откат не удался"; hasnt "Возвращаю код"; hasnt "ИТОГ ОБНОВЛЕНИЯ: ОТКАТ"
[ "$(git rev-parse HEAD)" = "$c2" ] || die "код вернулся на прежний, а база осталась с новыми миграциями"
grep -q '^result=rollback-failed$' local/update/last.env || die "итог «откат не удался» не записан"
grep -q -- "--result rollback-failed --from v0.0.1 --to v0.0.2" "$STUB_LOG" || die "сервису не передано «откат не удался»"
run ./ops/restore.sh --rollback; want 1 "--rollback из неполной копии: код $code"; has "Откат не начат"
if grep -q 'ops-consent ask --action rollback' "$STUB_LOG"; then die "с неполной копией владельца спросили об откате"; fi
[ "$(git rev-parse HEAD)" = "$c2" ] || die "restore.sh --rollback с неполной копией сменил код"
ok "выгрузка до обновления пропала — откат не начат (код 3, «откат не удался»), restore.sh --rollback отказывает до запроса"
git checkout -q --detach "$c1"; echo "$c1" > local/running-commit; echo "0001_a.sql" > "$STUB_APPLIED_FILE"
rm -f local/doctor-fail

# Новый сервис не отвечает (долгая миграция), а миграция ложится уже после того, как up.sh сдался: update.sh
# ждёт, затем останавливает сервис и только потом смотрит, какие миграции применены — откат с базой, а не «только код».
: > "$STUB_LOG"
STUB_LATE_MIG=1 STUB_SLOW_START=100000 OPS_UPDATE_WAIT=1 run ./ops/update.sh --no-auto-rollback; want 3 "поздняя миграция: код $code"
has "Жду новую версию: сервис переписки не отвечает"; has "останавливаю его"; has "так и не ответил"; has "нужен человек"
has "Сервис переписки снова запущен"; hasnt "данные не тронуты"
grep -q 'docker start shturman-service' "$STUB_LOG" || die "после решения «нужен человек» сервис оставлен остановленным"
grep -q 'update-result --run [0-9TZ]* --result failed-needs-human' "$STUB_LOG" || die "итог «нужен человек» не записан сервису"
grep -q '^scope=data$' local/update/last.env || die "миграция легла после проверки, а откат решён без базы"
grep -q 'docker stop shturman-service' "$STUB_LOG" || die "сервис не остановлен до решения об откате"
ok "сервис не ответил, миграция легла позже — остановка сервиса, затем решение: откат с базой (нужен человек)"
git checkout -q --detach "$c1"; echo "$c1" > local/running-commit; echo "0001_a.sql" > "$STUB_APPLIED_FILE"; rm -f local/status-wait local/late-mig

# Разрешение заранее, затем обновление агентом без терминала — успех.
rm -f local/doctor-fail; echo "0001_a.sql" > "$STUB_APPLIED_FILE"
run_tty right ./ops/update.sh --consent; want 0; [ -f local/consent/update.env ] || die "разрешение не записано"
ok "--consent с верной фразой — разрешение на 2 часа"
# Свежая копия того же кода и той же базы, но испорчена (тот же размер) — update.sh её не берёт, снимает свою.
run ./ops/backup.sh; want 0
fs="$(./ops/backup.sh --list | awk 'NR == 1 {print $1}')"
printf 'X' | dd of="local/backups/db-$fs.dump" bs=1 seek=0 conv=notrunc 2> /dev/null
run ./ops/update.sh; want 0 "обновление: код $code"; has "ИТОГ ОБНОВЛЕНИЯ: OK"
has "Свежая копия $fs не прошла проверку"; has "Копия: ./ops/backup.sh --reason before-update"; hasnt "беру свежую"
[ "$(git rev-parse HEAD)" = "$c2" ] || die "код не на v0.0.2"
grep -q '^reason=before-update$' "local/backups/meta-$(sed -n 's/^backup=//p' local/update/last.env).txt" || die "откатываться предстояло бы из испорченной копии"
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
: > "$STUB_LOG"
STUB_FLAKY=3 run ./ops/update.sh --upgrading-read; want 1 "откат кода: код $code"; has "ИТОГ ОБНОВЛЕНИЯ: ОТКАТ"; has "Откат: только код"
if grep -q 'docker stop shturman-service' "$STUB_LOG"; then die "сервис ответил коммитом цели, а его остановили из-за FAIL doctor"; fi
[ "$(git rev-parse HEAD)" = "$c2" ] || die "код не вернулся на v0.0.2"
[ "$(cat data/shturman/file)" = v2 ] || die "при откате только кода данные тронуты"
ok "провал без миграций — откат только кода, данные не трогаются; ответивший сервис не останавливается и при сбое повторного запроса"
rm -f local/doctor-fail

# Новый сервис так и не ответил (без миграций): ожидание ограничено, затем «не установлено», откат только кода —
# но не «новая версия не запускалась»: она запускалась.
STUB_SLOW_START=100000 OPS_UPDATE_WAIT=1 run ./ops/update.sh --upgrading-read; want 1 "сервис не ответил: код $code"
has "так и не ответил"; has "останавливаю его"; has "Откат: только код — миграции не применились"; hasnt "новая версия не запускалась"
has "ИТОГ ОБНОВЛЕНИЯ: НЕ УСТАНОВЛЕНО — v0.0.3 не заработал"
grep -q '^scope=code$' local/update/last.env || die "без миграций откат — с базой"
[ "$(git rev-parse HEAD)" = "$c2" ] || die "сервис не ответил, а код остался на v0.0.3"
rm -f local/status-wait
ok "новый сервис не ответил за отведённое время — «не установлено», сервис остановлен до решения, откат только кода"
# Сервис в цикле перезапусков (restarting) не отвечает — его тоже останавливают до решения.
: > "$STUB_LOG"
STUB_SERVICE=restarting OPS_UPDATE_WAIT=1 run ./ops/update.sh --upgrading-read; want 1 "сервис перезапускается: код $code"
has "так и не ответил"; has "останавливаю его"; grep -q 'docker stop shturman-service' "$STUB_LOG" || die "перезапускающийся сервис не остановлен"
[ "$(git rev-parse HEAD)" = "$c2" ] || die "перезапускающийся сервис: код не вернулся"
ok "сервис в цикле перезапусков — тоже останавливается до решения об откате"

# Сборка новой версии не прошла (Д-58): прежние контейнеры работают, doctor без FAIL — это не успех.
: > "$STUB_LOG"
UP_FAIL=0.0.3 STUB_SERVICE=running run ./ops/update.sh --upgrading-read; want 1 "сборка не прошла: код $code"
has "Новая версия v0.0.3 не запустилась: сервис переписки работает с прежним кодом ${c2:0:7}"
has "ИТОГ ОБНОВЛЕНИЯ: НЕ УСТАНОВЛЕНО"; has "работает прежняя версия v0.0.2, данные не тронуты"; hasnt "ИТОГ ОБНОВЛЕНИЯ: OK"
[ "$(git rev-parse HEAD)" = "$c2" ] || die "после неустановленного обновления код не вернулся на v0.0.2"
grep -q '^result=not-installed$' local/update/last.env && grep -q '^scope=code$' local/update/last.env || die "запись: не «не установлено» или не только код"
if grep -q -- '--result ok' "$STUB_LOG"; then die "сервису передано «обновлено»"; fi
grep -q -- "--result rolled-back --from v0.0.2 --to v0.0.3 --new-fails up-sh" "$STUB_LOG" || die "сервису не передано «не установлено» (rolled-back, up-sh)"
if grep -q 'ops-consent ask' "$STUB_LOG"; then die "откат только кода спрашивал владельца"; fi
ok "сборка не прошла, doctor без FAIL — «не установлено» (код 1), код на прежнем, сервису — rolled-back с меткой up-sh"
# Работает не тот код, что в каталоге: опись копии пишет работающий, план предупреждает.
echo "$c1" > local/running-commit
run ./ops/backup.sh; want 0
m="$(find local/backups -name 'meta-*.txt' | sort | tail -n 1)"
grep -qx "commit=$c1" "$m" && grep -qx "checkout_commit=$c2" "$m" || die "в описи коммит каталога вместо работающего"
run ./ops/update.sh --check; want 0; has "сервис переписки работает с кодом ${c1:0:7}, а в каталоге — ${c2:0:7}"
echo "$c2" > local/running-commit
ok "опись копии — коммит работающего кода (и каталога отдельно); план предупреждает о расхождении"
STUB_SLOW_START=3 run ./ops/update.sh --upgrading-read; want 0 "медленный запуск: код $code"; [ "$(git rev-parse HEAD)" = "$c3" ] || die "не обновился до v0.0.3"
has "Жду новую версию: сервис переписки не отвечает"; has "ИТОГ ОБНОВЛЕНИЯ: OK"; hasnt "НЕ УСТАНОВЛЕНО"
ok "медленный запуск (миграции): сервис ответил не сразу — дождались, обновлено, без отката"
run ./ops/update.sh; want 0; has "Обновлений нет"
run ./ops/update.sh --check --channel main; want 0; has "Цель:     origin/main"
ok "последний выпуск — «обновлений нет»; main — только явно"

# --- 6б. update.sh --all: по выпуску за шаг до последнего; итог каждого шага — сервису (Р-101) ---
o2="$tmp/origin2"; mkdir -p "$o2"; cd "$o2"
git init -q -b main
cp -r "$root/ops" "$root/config" "$root/.env.example" .
rm -rf ops/tests
cp "$o/ops/doctor.sh" "$o/ops/up.sh" ops/
printf 'local/\ndata/\n.env\n' > .gitignore
printf 'services:\n  hermes:\n    image: nousresearch/hermes-agent:${HERMES_VERSION:-v2026.9.24}\n' > compose.yaml
mkdir -p service/src/shturman/migrations
printf '# Обновление\n' > UPGRADING.md
rel 0.0.1 0001_a.sql v0.0.1
rel 0.0.2 "" v0.0.2
rel 0.0.3 "" v0.0.3-rc.1
echo "исправление кандидата" > notes.txt; rel 0.0.3 "" v0.0.3
rel 0.0.4 "" v0.0.4-rc.1
chain_inst() {
  rm -rf "${tmp:?}/$1"; git clone -q "$o2" "$tmp/$1"; cd "$tmp/$1"; git checkout -q --detach v0.0.1
  printf 'SHTURMAN_MODE=standalone\n' > .env; chmod 600 .env; mkdir -p data/shturman local
  echo "0001_a.sql" > "$STUB_APPLIED_FILE"; git rev-parse HEAD > local/running-commit   # сервис работает с v0.0.1
}
chain_inst chain1
: > "$STUB_LOG"
STUB_SERVICE=running run ./ops/update.sh --all; want 0 "цепочка: код $code"
has "=== Шаг 1"; has "=== Шаг 2"; has "ИТОГ ЦЕПОЧКИ: OK — последний выпуск канала установлен (v0.0.3), шагов: 2"
[ "$(git rev-parse HEAD)" = "$(git rev-parse v0.0.3)" ] || die "цепочка не дошла до v0.0.3"
[ "$(grep -c 'shturman update-result --run [0-9TZ]* --result ok' "$STUB_LOG")" = 2 ] || die "итог шагов не передан сервису"
grep -q -- "--result ok --from v0.0.1 --to v0.0.2 --new-fails" "$STUB_LOG" || die "итог: не те версии"
[ "$(./ops/backup.sh --list | grep -c 'before-update')" -ge 2 ] || die "не на каждом шаге копия"
ok "--all: выпуск за выпуском (кандидаты в канале release — мимо), каждый с копией и проверкой; итог — сервису"
run ./ops/update.sh --all; want 0; has "обновлений больше нет, шагов с обновлением: 0"
run ./ops/update.sh --all --channel pilot; want 0; [ "$(git rev-parse HEAD)" = "$(git rev-parse v0.0.4-rc.1)" ] || die "pilot: не до кандидата"
ok "--all в канале pilot доходит и до кандидата"
chain_inst chain2
echo 0.0.3 > local/doctor-fail
: > "$STUB_LOG"
STUB_SERVICE=running run ./ops/update.sh --all; want 1 "провал на шаге 2: код $code"
has "ИТОГ ЦЕПОЧКИ: остановка на шаге 2 (код 1)"; has "экземпляр на v0.0.2"
[ "$(git rev-parse HEAD)" = "$(git rev-parse v0.0.2)" ] || die "после отката не на v0.0.2"
grep -q -- "--result rolled-back --from v0.0.2 --to v0.0.3 --new-fails service" "$STUB_LOG" || die "откат не передан сервису"
ok "--all: провал шага — откат этого шага, цепочка остановлена; сервису — «откат» с именами строк doctor"
for bad in "--all --to v0.0.3" "--all --upgrading-read" "--all --despite-fail" "--all --check" "--all --consent" "--all --channel main"; do
  # shellcheck disable=SC2086
  run ./ops/update.sh $bad; want 2 "$bad: код $code"
done
ok "--all не сочетается с --to, --upgrading-read, --despite-fail, --check, --consent, --channel main"
chain_inst chain3
UP_FAIL=0.0.2 run ./ops/update.sh --all; want 1 "--all, сборка не прошла: код $code"
has "ИТОГ ЦЕПОЧКИ: остановка на шаге 1 (код 1)"; has "ИТОГ ОБНОВЛЕНИЯ: НЕ УСТАНОВЛЕНО"; hasnt "=== Шаг 2"
[ "$(git rev-parse HEAD)" = "$(git rev-parse v0.0.1)" ] || die "--all: после неустановленного шага не на v0.0.1"
ok "--all: сборка шага не прошла — «не установлено», цепочка остановлена на шаге 1"
# Режим с Hermes: новый образ задуман, но сборка упала до перезапуска — Hermes прежний, откат только кода,
# без разрешения на перезапись данных.
chain_inst hermes1; printf 'SHTURMAN_MODE=hermes\n' > .env
: > "$STUB_LOG"
STUB_HERMES_IMAGE="nousresearch/hermes-agent:v2026.9.10" STUB_IMAGES="nousresearch/hermes-agent:v2026.9.24" UP_FAIL=0.0.2 STUB_SERVICE=running \
  run ./ops/update.sh --no-auto-rollback; want 1 "Hermes, сборка не прошла: код $code"
has "образ сменится: nousresearch/hermes-agent:v2026.9.10 → nousresearch/hermes-agent:v2026.9.24"
has "сервис переписки работает с прежним кодом"; has "Откат: только код — новая версия не запускалась"; hasnt "нужен человек"
grep -q '^scope=code$' local/update/last.env || die "Hermes не перезапускался, а откат — с базой"
if grep -q 'ops-consent ask' "$STUB_LOG"; then die "откат только кода спрашивал владельца"; fi
[ "$(git rev-parse HEAD)" = "$(git rev-parse v0.0.1)" ] || die "Hermes: код не вернулся"
hasnt "версия закреплена строкой HERMES_VERSION"
ok "Hermes: образ не сменился (сборка упала) — откат только кода, без запроса владельцу"
chain_inst hermes2; printf 'SHTURMAN_MODE=hermes\nHERMES_VERSION=v2026.9.10\n' > .env
STUB_HERMES_IMAGE="nousresearch/hermes-agent:v2026.9.10" STUB_SERVICE=running run ./ops/update.sh --check; want 0 "закреплённый Hermes, план: код $code"
has "Hermes:   тот же образ nousresearch/hermes-agent:v2026.9.10"; has "версия закреплена строкой HERMES_VERSION в .env"
hasnt "HERMES_VERSION=v2026"
ok "план называет закреплённую строкой .env версию Hermes (агент .env не читает), саму строку не печатает"
chain_inst chain4
rm -f local/doctor-fail; : > "$STUB_LOG"
STUB_SERVICE="" run ./ops/update.sh; want 1 "сервиса нет: код $code"; has "контейнер сервиса переписки не работает"; has "ИТОГ ОБНОВЛЕНИЯ: НЕ УСТАНОВЛЕНО"
hasnt "Жду новую версию"
grep -q 'compose run --rm --no-deps -T shturman update-result --run [0-9TZ]* --result rolled-back' "$STUB_LOG" \
  || die "контейнера сервиса нет — итог не записан одноразовым контейнером"
ok "контейнера сервиса после обновления нет — сразу «не установлено» (без ожидания), итог — одноразовым контейнером"
chain_inst chain5
STUB_NONE=1 run ./ops/update.sh; want 0 "сервис без коммита: код $code"; has "не назвал, с каким кодом работает"; has "ИТОГ ОБНОВЛЕНИЯ: OK"
ok "сервис отвечает, но коммита не называет — сверка коммита пропущена, решает doctor"

# --- 6в. Режим с Hermes: образ и код плагина (Д-58) ---
o3="$tmp/origin3"; mkdir -p "$o3"; cd "$o3"
git init -q -b main
cp -r "$root/ops" "$root/config" "$root/.env.example" .
rm -rf ops/tests
cp "$o/ops/doctor.sh" "$o/ops/up.sh" ops/
printf 'local/\ndata/\n.env\n' > .gitignore
printf 'services:\n  hermes:\n    image: nousresearch/hermes-agent:${HERMES_VERSION:-v2026.9.24}\n' > compose.yaml
mkdir -p service/src/shturman/migrations plugins/shturman
printf '# Обновление\n' > UPGRADING.md
echo "a = 1" > plugins/shturman/core.py; rel 0.0.1 0001_a.sql v0.0.1
echo "a = 2" > plugins/shturman/core.py; rel 0.0.2 "" v0.0.2
mkdir -p plugins/shturman/tests; echo "def test_x(): pass" > plugins/shturman/tests/test_x.py; rel 0.0.3 "" v0.0.3
hermes_inst() {
  rm -rf "${tmp:?}/$1"; git clone -q "$o3" "$tmp/$1"; cd "$tmp/$1"; git checkout -q --detach v0.0.1
  printf 'SHTURMAN_MODE=hermes\n' > .env; chmod 600 .env; mkdir -p data/shturman local
  echo "0001_a.sql" > "$STUB_APPLIED_FILE"; git rev-parse HEAD > local/running-commit
}
export STUB_HERMES_IMAGE="nousresearch/hermes-agent:v2026.9.24" STUB_IMAGES="nousresearch/hermes-agent:v2026.9.24"
hermes_inst h1
STUB_HERMES_STARTED="2020-01-01T00:00:00Z" run ./ops/update.sh; want 1 "плагин не перезагружен: код $code"
has "запущена не вся: Hermes не перезапускался после смены кода"; has "Откат: только код — миграции не применились"
[ "$(git rev-parse HEAD)" = "$(git rev-parse v0.0.1)" ] || die "Hermes с прежним плагином — код не вернулся"
hermes_inst h2
STUB_HERMES_STARTED="$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)" run ./ops/update.sh; want 0 "плагин перезагружен: код $code"; has "ИТОГ ОБНОВЛЕНИЯ: OK"
# Выпуск меняет только тесты плагина — Hermes перезапускать не нужно (up.sh и не перезапустит: plugin_print их не считает).
echo "$(git rev-parse HEAD)" > local/running-commit
STUB_HERMES_STARTED="2020-01-01T00:00:00Z" run ./ops/update.sh; want 0 "изменились только тесты плагина: код $code"; has "ИТОГ ОБНОВЛЕНИЯ: OK"
[ "$(git rev-parse HEAD)" = "$(git rev-parse v0.0.3)" ] || die "только тесты плагина — не обновился"
ok "Hermes: код плагина изменился — «установлено», только если Hermes запущен после смены кода (не по отпечатку прежней версии)"
hermes_inst h3; : > "$STUB_LOG"
STUB_HERMES_IMAGE="nousresearch/hermes-agent:v2026.9.10" STUB_HERMES_STARTED="$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)" \
  run ./ops/update.sh --no-auto-rollback; want 1 "Hermes с прежним образом: код $code"
has "запущена не вся: Hermes работает с образом nousresearch/hermes-agent:v2026.9.10, а задуман nousresearch/hermes-agent:v2026.9.24"
grep -q '^scope=code$' local/update/last.env || die "Hermes прежний, а откат — с базой"
if grep -q 'ops-consent ask' "$STUB_LOG"; then die "откат только кода спрашивал владельца"; fi
ok "Hermes: работает прежний образ — «не установлено», откат только кода, без запроса владельцу"
unset STUB_HERMES_IMAGE STUB_IMAGES

# --- 6г. Сброс истории репозитория: переход по ops/history-reset ---
# Origin, чья история заменена новой с первого коммита (прежние коммиты и теги на GitHub удалены); экземпляр
# стоит на последнем прежнем выпуске. С отметкой — переход записывается локально (git replace --graft), дальше
# обычное обновление; без отметки или с чужим коммитом в ней — стоп с объяснением, ничего не меняется.
reset_origin() {  # $1 — каталог origin; $2 — что писать в ops/history-reset новой истории: old (прежний выпуск), none, другой SHA
  local d="$1" mark="$2" old
  rm -rf "$d"; mkdir -p "$d"; cd "$d"; git init -q -b main
  cp -r "$root/ops" "$root/config" "$root/.env.example" .
  rm -rf ops/tests ops/history-reset
  cp "$o/ops/doctor.sh" "$o/ops/up.sh" ops/
  printf 'local/\ndata/\n.env\n' > .gitignore
  printf 'services:\n  hermes:\n    image: nousresearch/hermes-agent:${HERMES_VERSION:-v2026.9.24}\n' > compose.yaml
  mkdir -p service/src/shturman/migrations
  printf '# Обновление\n' > UPGRADING.md
  rel 0.0.1 0001_a.sql v0.0.1
  rel 0.0.2 "" v0.0.2-rc.1
  old="$(git rev-parse HEAD)"
  git clone -q "$d" "$d.inst"            # экземпляр развёрнут до сброса: прежние теги у него есть
  git checkout -q --orphan fresh
  case "$mark" in
    old) printf '# сброс истории\n%s v0.0.2-rc.1\n' "$old" > ops/history-reset ;;
    none) ;;
    *) printf '%s v0.0.2-rc.1\n' "$mark" > ops/history-reset ;;
  esac
  printf '# Обновление\n\n## 0.0.2 → 0.0.3\n\nСброс истории.\n' > UPGRADING.md
  rel 0.0.3 "" v0.0.3-rc.1
  echo "дальше" > notes.txt; rel 0.0.4 "" v0.0.4-rc.1
  git branch -q -D main; git branch -q -m main
  git tag -d v0.0.1 v0.0.2-rc.1 > /dev/null
  git gc -q --prune=now
}
reset_inst() {  # $1 — каталог origin; $2 — тег, на котором стоит экземпляр
  rm -rf "${tmp:?}/ri"; mv "$1.inst" "$tmp/ri"; cd "$tmp/ri"; git checkout -q --detach "$2"
  printf 'SHTURMAN_MODE=standalone\nSHTURMAN_UPDATE_CHANNEL=pilot\n' > .env; chmod 600 .env; mkdir -p data/shturman local
  echo "0001_a.sql" > "$STUB_APPLIED_FILE"; git rev-parse HEAD > local/running-commit
}
o4="$tmp/origin4"
reset_origin "$o4" old; reset_inst "$o4" v0.0.2-rc.1
old="$(git rev-parse HEAD)"
run ./ops/update.sh --check; want 0 "план через сброс истории: код $code"
has "Сброс истории: первый коммит новой истории"; has "продолжает прежний ${old:0:12} (v0.0.2-rc.1)"; has "Цель:     v0.0.3-rc.1"
has "UPGRADING.md: «0.0.2 → 0.0.3»"
[ "$(git rev-parse HEAD)" = "$old" ] || die "--check сменил код"
[ "$(git replace -l | wc -l)" -eq 1 ] || die "переход не записан"
run ./ops/update.sh; want 0 "обновление через сброс истории: код $code"; has "ИТОГ ОБНОВЛЕНИЯ: OK"; hasnt "Сброс истории"
[ "$(git rev-parse HEAD)" = "$(git rev-parse v0.0.3-rc.1)" ] || die "не перешёл на первый выпуск новой истории"
git diff --quiet HEAD -- || die "отслеживаемые файлы изменены"
run ./ops/update.sh; want 0; has "ИТОГ ОБНОВЛЕНИЯ: OK"
[ "$(git rev-parse HEAD)" = "$(git rev-parse v0.0.4-rc.1)" ] || die "после перехода не обновился дальше"
run ./ops/update.sh --to v0.0.2-rc.1; want 2; has "не впереди"
ok "сброс истории: отметка называет прежний выпуск — переход записан локально, дальше обычные обновления; назад — нет"
# Провал после перехода — откат на прежний коммит прежней истории.
reset_origin "$o4" old; reset_inst "$o4" v0.0.2-rc.1
old="$(git rev-parse HEAD)"; echo 0.0.3 > local/doctor-fail
run ./ops/update.sh; want 1 "откат через сброс истории: код $code"; has "ИТОГ ОБНОВЛЕНИЯ: ОТКАТ"
[ "$(git rev-parse HEAD)" = "$old" ] || die "откат не вернул прежний коммит"
rm -f local/doctor-fail
run ./ops/update.sh --check; want 0; has "Цель:     v0.0.3-rc.1"
ok "сброс истории: провал после перехода — откат на прежний коммит, переход остаётся для следующей попытки"
# Экземпляр раньше последнего прежнего выпуска: сначала до него, потом через сброс — по одному выпуску.
reset_origin "$o4" old; reset_inst "$o4" v0.0.1
run ./ops/update.sh --all; want 0 "цепочка через сброс истории: код $code"
has "=== Шаг 3"; has "ИТОГ ЦЕПОЧКИ: OK — последний выпуск канала установлен (v0.0.4-rc.1), шагов: 3"
grep -q -- "--result ok --from v0.0.1 --to v0.0.2-rc.1" "$STUB_LOG" || die "первый шаг — не до прежнего выпуска"
ok "сброс истории: экземпляр старше прежнего выпуска — сначала до него, затем по выпуску через сброс"
# Без отметки — стоп с объяснением, ничего не меняется.
reset_origin "$o4" none; reset_inst "$o4" v0.0.2-rc.1
old="$(git rev-parse HEAD)"
run ./ops/update.sh; want 3 "без отметки: код $code"
has "ветка другой истории репозитория, а перейти на неё нельзя"; has "нет файла ops/history-reset"; has "скажите разработчику"
hasnt "Обновлений нет"
[ "$(git rev-parse HEAD)" = "$old" ] && [ -z "$(git replace -l)" ] || die "без отметки что-то изменилось"
run ./ops/update.sh --check --channel release; want 3; has "перейти на неё нельзя"
run ./ops/update.sh --to v0.0.3-rc.1; want 3; has "перейти на неё нельзя"
ok "сброс истории без ops/history-reset — стоп (код 3) с объяснением, код и ссылки не тронуты"
# Отметка называет коммит, которого на экземпляре нет.
reset_origin "$o4" 0123456789abcdef0123456789abcdef01234567; reset_inst "$o4" v0.0.2-rc.1
run ./ops/update.sh; want 3 "чужой коммит в отметке: код $code"; has "которого на этом экземпляре нет"
[ -z "$(git replace -l)" ] || die "переход записан по чужой отметке"
ok "отметка называет чужой коммит — переход не записывается"
# Экземпляр, чей ./ops/update.sh сброса ещё не знает (0.0.13): скрипт перехода из новой истории — из local/.
reset_origin "$o4" old; reset_inst "$o4" v0.0.2-rc.1
git fetch -q --tags origin
git show origin/main:ops/history-reset.sh > local/history-reset.sh
run bash local/history-reset.sh --dry-run; want 0; has "записал бы переход"; [ -z "$(git replace -l)" ] || die "--dry-run записал"
run bash local/history-reset.sh; want 0; has "записано локально (git replace)"
git merge-base --is-ancestor HEAD v0.0.3-rc.1 || die "после перехода новый выпуск не впереди"
git diff --quiet HEAD -- || die "отслеживаемые файлы изменены"
run bash local/history-reset.sh; want 0; hasnt "записано"
ok "переход вручную (UPGRADING 0.0.13 → 0.0.14): скрипт из новой истории, запущенный из local/, записывает его один раз"

unset STUB_SERVICE

# --- Порог защиты: только человек в терминале (Р-99) ---
d="$(mkinst guard)"; cd "$d"
printf '#!/bin/sh\necho "подставной up.sh"\nexit 0\n' > ops/up.sh
run ./ops/guard.sh threshold 0.97; want 4; has "ssh -t shturman-app"; hasnt "подставной up.sh"
grep -q SHTURMAN_GUARD_THRESHOLD .env && die "без бота и терминала порог записан"
STUB_SERVICE=running STUB_CONSENT=5 run ./ops/guard.sh threshold 0.97; want 4; hasnt "подставной up.sh"
grep -q SHTURMAN_GUARD_THRESHOLD .env && die "отклонённый в боте порог записан"
run_tty wrong ./ops/guard.sh threshold 0.97; want 4; has "Не подтверждено"
grep -q SHTURMAN_GUARD_THRESHOLD .env && die "с неверной фразой порог записан"
run ./ops/guard.sh threshold 0.2; want 2; has "от 0.5 до 1"
run_tty right ./ops/guard.sh threshold 0,97; want 0 "порог: код $code"; has "подставной up.sh"
grep -q '^SHTURMAN_GUARD_THRESHOLD=0.97$' .env || die "порог не записан"
grep -q 'secret-token-one' .env || die "остальные строки .env тронуты"
sed -i '/^SHTURMAN_GUARD_THRESHOLD=/d' .env
: > "$STUB_LOG"
STUB_SERVICE=running STUB_CONSENT=0 run ./ops/guard.sh threshold 0.98; want 0 "порог по кнопке: код $code"; has "подставной up.sh"
grep -q '^SHTURMAN_GUARD_THRESHOLD=0.98$' .env || die "порог по кнопке не записан"
grep -q "ops-consent ask --action guard_threshold --target 0.98 .*--detail old=0.9" "$STUB_LOG" || die "запрос порога без цели"
ok "порог защиты меняет только владелец: кнопкой в боте или (бота нет) фразой человека в терминале; иначе — код 4"

echo "Все проверки пройдены: $n."
