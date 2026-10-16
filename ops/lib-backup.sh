#!/usr/bin/env bash
# Общие функции копий, восстановления и отката. Сама по себе не запускается: подключается строкой
# `. ops/lib-backup.sh` после `. ops/lib.sh` (backup.sh, restore.sh, update.sh, up.sh).
#
# Копия (./ops/backup.sh) — три файла с одной меткой времени в local/backups и, если задан,
# в каталоге SHTURMAN_BACKUP_DIR (другой диск, точка монтирования):
#   data-<метка>.tar.gz — каталог data/ без базы, моделей, журналов и временных файлов, и .env;
#   db-<метка>.dump     — выгрузка базы (pg_dump, формат custom);
#   meta-<метка>.txt    — опись без секретов: версия и коммит кода, режим, версия Hermes,
#                         применённые миграции, размеры и контрольные суммы файлов, число строк
#                         ключевых таблиц до и после выгрузки, итог проверки.
# Агент читает только опись и счётчики; архив и выгрузку не открывает: в них ключи и переписка.
#
# Восстановление базы идёт в отдельную базу shturman_restore; живая база заменяется переименованием
# только после того, как выгрузка легла без ошибок и число строк совпало с описью. Ошибки pg_restore
# пишутся в файл в local/restore/ (в них может оказаться строка данных) и на экран не выводятся.
#
# Несекретные строки .env, которые здесь читаются: SHTURMAN_BACKUP_DIR, SHTURMAN_BACKUP_KEEP,
# SHTURMAN_BACKUP_FRESH_MINUTES, HERMES_VERSION. Секрет читается один — POSTGRES_PASSWORD, и только
# при восстановлении с .env из копии поверх уже созданной базы (pg_align_password): чтобы пароль
# роли базы совпал с восстановленным .env. Значение не печатается и в параметры команд не попадает.

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$OPS_DIR/$(basename "$0")"
  case "${1:-}" in
    -h|--help|"") echo; echo "Это библиотека: сама по себе она ничего не делает."; exit 0 ;;
    *) echo "неизвестный параметр: $1 — справка: ./ops/$(basename "$0") --help" >&2; exit 2 ;;
  esac
fi

BK_LOCAL="local/backups"
PG=shturman-postgres
# Таблицы, число строк которых записывается в опись и сверяется после восстановления.
BK_COUNT_TABLES="chats messages commitments people pages jobs schema_migrations"
MIGRATIONS_DIR="service/src/shturman/migrations"

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Внешний каталог копий (несекретная строка SHTURMAN_BACKUP_DIR). Пусто — не задан.
bk_external() { env_get SHTURMAN_BACKUP_DIR; }

# Сколько последних копий хранить (в каждом каталоге). По умолчанию 5; меньше 2 нельзя: при откате
# нужна и копия до обновления, и копия, сделанная перед восстановлением.
bk_keep() {
  local n
  n="$(env_get SHTURMAN_BACKUP_KEEP)"
  case "$n" in
    ''|*[!0-9]*) echo 5 ;;
    *) if [ "$n" -lt 2 ]; then echo 2; elif [ "$n" -gt 100 ]; then echo 100; else echo "$n"; fi ;;
  esac
}

# Сколько минут копия считается свежей для ./ops/up.sh и ./ops/update.sh. По умолчанию 60.
bk_fresh_minutes() {
  local n
  n="$(env_get SHTURMAN_BACKUP_FRESH_MINUTES)"
  case "$n" in ''|*[!0-9]*|0) echo 60 ;; *) echo "$n" ;; esac
}

# Метки копий в каталоге, новые первыми.
bk_stamps() {
  [ -d "$1" ] || return 0
  find "$1" -maxdepth 1 -type f \( -name 'data-*.tar.gz' -o -name 'db-*.dump' -o -name 'meta-*.txt' \) -printf '%f\n' 2> /dev/null \
    | sed -nE 's/^(data|db|meta)-([0-9]{8}T[0-9]{6}Z)\..*$/\2/p' | sort -ru
}

# Значение строки описи. $1 — файл, $2 — ключ.
meta_get() {
  [ -f "$1" ] || return 0
  grep -E "^$2=" "$1" | tail -n 1 | cut -d= -f2- || true
}

# Каталог, где лежит копия с этой меткой: local/backups, затем SHTURMAN_BACKUP_DIR, затем $2.
bk_find() {
  local d
  for d in "$BK_LOCAL" "$(bk_external)" "${2:-}"; do
    [ -n "$d" ] && [ -d "$d" ] || continue
    if [ -f "$d/data-$1.tar.gz" ] || [ -f "$d/db-$1.dump" ]; then printf '%s\n' "$d"; return 0; fi
  done
  return 1
}

# Время создания копии в секундах: из описи, а у копии без описи — по времени файла выгрузки базы.
bk_epoch() {
  local dir="$1" s="$2" e
  e="$(meta_get "$dir/meta-$s.txt" created_epoch)"
  if [ -z "$e" ]; then
    if [ -f "$dir/db-$s.dump" ]; then e="$(stat -c %Y "$dir/db-$s.dump")"
    elif [ -f "$dir/data-$s.tar.gz" ]; then e="$(stat -c %Y "$dir/data-$s.tar.gz")"
    fi
  fi
  printf '%s\n' "${e:-0}"
}

# Свежая проверенная копия: метка самой новой копии из local/backups моложе $1 минут, у которой
# есть выгрузка базы (если база есть) и опись с verified=yes. Копия без описи (сделанная до 0.0.8)
# считается, если её выгрузка базы моложе срока: прежний backup.sh тоже проверял оглавление.
bk_fresh() {
  local minutes="$1" s e now
  now="$(date +%s)"
  for s in $(bk_stamps "$BK_LOCAL"); do
    e="$(bk_epoch "$BK_LOCAL" "$s")"
    [ $((now - e)) -le $((minutes * 60)) ] || return 1
    if [ -f "$BK_LOCAL/meta-$s.txt" ]; then
      [ "$(meta_get "$BK_LOCAL/meta-$s.txt" verified)" = yes ] || continue
      [ "$(meta_get "$BK_LOCAL/meta-$s.txt" reason)" = before-restore ] && continue
    else
      [ -f "$BK_LOCAL/db-$s.dump" ] || [ ! -d data/postgres ] || continue
    fi
    printf '%s\n' "$s"; return 0
  done
  return 1
}

# Перечень копий: метка, время, размеры, версия кода, причина, где лежит, проверена ли.
bk_list() {
  local ext stamps s dir m where v c r ver sz any=no
  ext="$(bk_external)"
  stamps="$( { bk_stamps "$BK_LOCAL"; [ -n "$ext" ] && bk_stamps "$ext"; [ -n "${1:-}" ] && bk_stamps "$1"; } | sort -ru)"
  for s in $stamps; do
    any=yes; where=""
    for dir in "$BK_LOCAL" "$ext" "${1:-}"; do
      [ -n "$dir" ] || continue
      if [ -f "$dir/data-$s.tar.gz" ] || [ -f "$dir/db-$s.dump" ]; then
        case "$dir" in "$BK_LOCAL") where="${where:+$where+}здесь" ;; "$ext") where="${where:+$where+}внешний каталог" ;; *) where="${where:+$where+}$dir" ;; esac
      fi
    done
    dir="$(bk_find "$s" "${1:-}")" || continue
    m="$dir/meta-$s.txt"
    sz=""
    [ -f "$dir/data-$s.tar.gz" ] && sz="данные $(du -h "$dir/data-$s.tar.gz" | cut -f1)"
    [ -f "$dir/db-$s.dump" ] && sz="${sz:+$sz, }база $(du -h "$dir/db-$s.dump" | cut -f1)"
    if [ -f "$m" ]; then
      v="$(meta_get "$m" version)"; c="$(meta_get "$m" commit)"; r="$(meta_get "$m" reason)"
      ver="версия ${v:-?} (${c:0:7}, $(meta_get "$m" ref))"
      case "$(meta_get "$m" verified)" in yes) ver="$ver, проверена" ;; *) ver="$ver, НЕ проверена" ;; esac
      printf '%s  %s  %s  %s  причина: %s  где: %s\n' "$s" "$(date -u -d "@$(bk_epoch "$dir" "$s")" '+%Y-%m-%d %H:%M UTC')" "$sz" "$ver" "${r:-manual}" "$where"
    else
      printf '%s  %s  %s  версия неизвестна (копия сделана до 0.0.8, без описи)  где: %s\n' "$s" "$(date -u -d "@$(bk_epoch "$dir" "$s")" '+%Y-%m-%d %H:%M UTC')" "$sz" "$where"
    fi
  done
  [ "$any" = yes ] || echo "(копий нет)"
}

# --- база ---
pg_running() { [ "$(container_state "$PG")" = running ]; }
# Запрос к базе: только числа и имена миграций, без строк таблиц. $1 — имя базы, $2 — SQL.
pg_q() { docker exec -i "$PG" psql -U shturman -d "$1" -XAtq -v ON_ERROR_STOP=1 -c "$2"; }
# Готовность — по TCP внутри контейнера: при первом запуске образ поднимает временный сервер только
# на сокете (initdb), и проверка по сокету проходила бы раньше, чем настоящий сервер запущен.
pg_wait() {
  local _
  for _ in $(seq 1 90); do
    if docker exec "$PG" pg_isready -h 127.0.0.1 -U shturman -d postgres > /dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}
# Запустить только контейнер базы (если ещё не запущен) и дождаться его.
pg_start() {
  local out
  # Каталог данных создаём сами, как ./ops/up.sh: иначе Docker создаст точку подключения базы от root,
  # и на новом сервере восстановить каталоги data/ будет нельзя.
  mkdir -p data && chmod 700 data || { echo "Не удалось создать каталог data (права?)." >&2; return 1; }
  if ! pg_running; then
    out="$(docker compose up -d postgres 2>&1)" || { printf '%s\n' "$out" | tail -n 5 >&2; return 1; }
  fi
  pg_wait
}

# Применённые миграции через запятую. Таблицы нет (новая база) — пусто. База недоступна — код 1.
#   $1 — имя базы (по умолчанию shturman).
db_migrations() {
  local db="${1:-shturman}" has
  has="$(pg_q "$db" "SELECT to_regclass('public.schema_migrations') IS NOT NULL")" || return 1
  [ "$has" = t ] || return 0
  pg_q "$db" "SELECT string_agg(name, ',' ORDER BY name) FROM schema_migrations"
}

# Миграции в коде (файлы каталога миграций сервиса) через запятую. $1 — коммит git (по умолчанию —
# рабочий каталог).
repo_migrations() {
  if [ -n "${1:-}" ]; then
    git ls-tree --name-only "$1" "$MIGRATIONS_DIR/" 2> /dev/null | sed 's|.*/||' | grep -E '\.sql$' | LC_ALL=C sort | paste -sd, -
  else
    find "$MIGRATIONS_DIR" -maxdepth 1 -name '*.sql' -printf '%f\n' 2> /dev/null | LC_ALL=C sort | paste -sd, -
  fi
}

# Миграции, которых нет во втором списке. $1, $2 — списки через запятую.
list_minus() {
  local a="$1" b=",$2," x out=""
  local IFS=,
  for x in $a; do
    case "$b" in *",$x,"*) ;; *) out="${out:+$out,}$x" ;; esac
  done
  printf '%s\n' "$out"
}
list_count() { if [ -z "$1" ]; then echo 0; else printf '%s\n' "$1" | tr ',' '\n' | wc -l; fi; }

# Число строк ключевых таблиц: «chats=12 messages=3456 …» (только существующие таблицы).
db_counts() {
  local db="${1:-shturman}" arr="" t
  for t in $BK_COUNT_TABLES; do arr="${arr:+$arr,}'$t'"; done
  pg_q "$db" "SELECT string_agg(t || '=' || (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM public.%I', t), false, true, '')))[1]::text, ' ' ORDER BY t) FROM unnest(ARRAY[$arr]) AS t WHERE to_regclass('public.' || t) IS NOT NULL"
}

# Совпадает ли число строк восстановленной базы с описью: для каждой таблицы — в пределах между
# счётом до выгрузки и после неё (между ними могли прийти сообщения). Печатает расхождения.
#   $1 — счёт восстановленной базы; $2, $3 — счёт до и после выгрузки из описи.
counts_match() {
  local got="$1" before="$2" after="$3" pair t n lo hi b a bad=""
  for pair in $before; do
    t="${pair%%=*}"; b="${pair#*=}"
    a="$(printf '%s\n' $after | sed -n "s/^$t=//p")"; a="${a:-$b}"
    n="$(printf '%s\n' $got | sed -n "s/^$t=//p")"
    if [ "$b" -le "$a" ]; then lo="$b"; hi="$a"; else lo="$a"; hi="$b"; fi
    if [ -z "$n" ] || [ "$n" -lt "$lo" ] || [ "$n" -gt "$hi" ]; then bad="${bad:+$bad; }$t: ${n:-нет таблицы} вместо $lo…$hi"; fi
  done
  [ -z "$bad" ] && return 0
  printf '%s\n' "$bad"; return 1
}

# Оглавление выгрузки читается и содержит таблицу сообщений. Через контейнер базы, а если он не
# запущен — программой pg_restore сервера. Код 2 — проверить нечем.
dump_readable() {
  if pg_running; then
    docker exec -i "$PG" pg_restore --list < "$1" 2> /dev/null | grep -q 'TABLE public messages'
  elif command -v pg_restore > /dev/null 2>&1; then
    pg_restore --list "$1" 2> /dev/null | grep -q 'TABLE public messages'
  else
    return 2
  fi
}

# Проверка копии: контрольные суммы по описи, архив читается, оглавление выгрузки читается.
# Печатает по строке на файл. $1 — каталог, $2 — метка. Код 0 — всё прочитано и совпало.
bk_verify() {
  local dir="$1" s="$2" m bad=0 want got rc
  m="$dir/meta-$s.txt"
  if [ -f "$dir/data-$s.tar.gz" ]; then
    want="$(meta_get "$m" data_sha256)"
    if [ -n "$want" ]; then
      got="$(sha256sum "$dir/data-$s.tar.gz" | cut -d' ' -f1)"
      if [ "$got" = "$want" ]; then echo "  данные: контрольная сумма совпала"; else echo "  данные: контрольная сумма НЕ совпала — файл повреждён"; bad=1; fi
    else
      echo "  данные: описи нет — контрольную сумму сверить не с чем"
    fi
    if tar -tzf "$dir/data-$s.tar.gz" > /dev/null 2>&1; then echo "  данные: архив читается"; else echo "  данные: архив НЕ читается"; bad=1; fi
  fi
  if [ -f "$dir/db-$s.dump" ]; then
    want="$(meta_get "$m" db_sha256)"
    if [ -n "$want" ]; then
      got="$(sha256sum "$dir/db-$s.dump" | cut -d' ' -f1)"
      if [ "$got" = "$want" ]; then echo "  база: контрольная сумма совпала"; else echo "  база: контрольная сумма НЕ совпала — файл повреждён"; bad=1; fi
    fi
    rc=0; dump_readable "$dir/db-$s.dump" || rc=$?
    case "$rc" in
      0) echo "  база: оглавление выгрузки читается (pg_restore --list)" ;;
      2) echo "  база: проверить оглавление нечем — нет ни запущенной базы, ни pg_restore на сервере" ;;
      *) echo "  база: оглавление выгрузки НЕ читается"; bad=1 ;;
    esac
  fi
  return "$bad"
}

# Ротация: в каталоге $1 остаются $2 последних копий; остальные метки удаляются целиком (все файлы
# с этой меткой). Метку $3 (копия, из которой сейчас восстанавливают) не трогаем.
bk_rotate() {
  local dir="$1" keep="$2" protect="${3:-}" s i=0
  for s in $(bk_stamps "$dir"); do
    i=$((i + 1))
    [ "$i" -le "$keep" ] && continue
    [ "$s" = "$protect" ] && continue
    rm -f -- "$dir/data-$s.tar.gz" "$dir/db-$s.dump" "$dir/meta-$s.txt"
  done
}

# --- остановка и восстановление ---
# Остановить всё, кроме базы: контейнеры shturman-* (Hermes, сервис, посредник, эмбеддинги, защита,
# распознавание речи). Данные не трогаются; поднимет их снова ./ops/up.sh.
stack_stop_but_db() {
  local names
  names="$(docker ps --filter name='^shturman-' --format '{{.Names}}' | grep -vx "$PG" || true)"
  [ -n "$names" ] || return 0
  # shellcheck disable=SC2086
  docker stop $names > /dev/null
}

# Восстановить базу из выгрузки. Живая база не трогается, пока выгрузка не легла в отдельную базу
# и число строк не совпало с описью. $1 — выгрузка, $2 — опись (может отсутствовать), $3 — рабочий
# каталог восстановления. Печатает ход; ошибки pg_restore — в файл $3/pg_restore.log.
restore_db() {
  local dump="$1" meta="$2" run="$3" got bad free need
  free="$(df -Pk . | awk 'NR == 2 {print $4}')"
  need=$(( $(stat -c %s "$dump") / 1024 * 3 + 1048576 ))
  if [ "${free:-0}" -lt "$need" ]; then
    echo "Мало места на диске: свободно $((free / 1024)) МБ, для восстановления базы нужно около $((need / 1024)) МБ (втрое больше выгрузки и 1 ГБ запаса). Ничего не изменено." >&2
    return 1
  fi
  pg_start || { echo "База не запустилась (docker logs --tail 50 $PG). Ничего не изменено." >&2; return 1; }
  docker exec "$PG" dropdb -U shturman --if-exists shturman_restore > /dev/null 2>&1 || true
  docker exec "$PG" createdb -U shturman -O shturman shturman_restore || { echo "Не удалось создать базу shturman_restore. Ничего не изменено." >&2; return 1; }
  echo "Восстанавливаю выгрузку в отдельную базу shturman_restore…"
  if ! docker exec -i "$PG" pg_restore -U shturman -d shturman_restore --exit-on-error --single-transaction < "$dump" > "$run/pg_restore.log" 2>&1; then
    docker exec "$PG" dropdb -U shturman --if-exists shturman_restore > /dev/null 2>&1 || true
    echo "Выгрузка не легла в базу (pg_restore завершился с ошибкой). Живая база не тронута. Подробности — в $run/pg_restore.log: там может оказаться строка данных, агенту его не открывать; владельцу — переслать разработчику только по своему решению." >&2
    return 1
  fi
  got="$(db_counts shturman_restore)" || got=""
  if [ -f "$meta" ] && [ -n "$(meta_get "$meta" counts_before)" ]; then
    if ! bad="$(counts_match "$got" "$(meta_get "$meta" counts_before)" "$(meta_get "$meta" counts_after)")"; then
      docker exec "$PG" dropdb -U shturman --if-exists shturman_restore > /dev/null 2>&1 || true
      echo "Число строк восстановленной базы не совпало с описью копии: $bad. Живая база не тронута." >&2
      return 1
    fi
    echo "Число строк совпало с описью: $got"
  else
    echo "Описи нет — сверить число строк не с чем; в восстановленной базе: $got"
  fi
  # Подмена: живая база уходит под другим именем, восстановленная получает её имя; прежняя удаляется.
  pg_q postgres "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE datname IN ('shturman', 'shturman_restore') AND pid <> pg_backend_pid()" > /dev/null || true
  if [ "$(pg_q postgres "SELECT count(*) FROM pg_database WHERE datname = 'shturman'")" = 1 ]; then
    pg_q postgres "ALTER DATABASE shturman RENAME TO shturman_replaced" > /dev/null || { echo "Не удалось отодвинуть живую базу (к ней кто-то подключён?). Восстановленная лежит в shturman_restore; ничего не потеряно." >&2; return 1; }
  fi
  if ! pg_q postgres "ALTER DATABASE shturman_restore RENAME TO shturman" > /dev/null; then
    pg_q postgres "ALTER DATABASE shturman_replaced RENAME TO shturman" > /dev/null || true
    echo "Не удалось переименовать восстановленную базу; живая база возвращена на место." >&2
    return 1
  fi
  docker exec "$PG" dropdb -U shturman --if-exists shturman_replaced > /dev/null 2>&1 || true
  echo "База восстановлена."
}

# Восстановить каталоги data/ из архива копии. Каталоги верхнего уровня, которые есть в архиве
# (hermes, shturman, relay…), подменяются целиком; прежние уходят в $2/replaced (удаляет их
# вызывающий после успеха). База (data/postgres) и модели архивом не затрагиваются.
#   $1 — архив data-*.tar.gz, $2 — рабочий каталог восстановления.
restore_data() {
  local archive="$1" run="$2" name
  mkdir -p "$run/stage" "$run/replaced"
  if ! tar -xzf "$archive" -C "$run/stage" data 2> "$run/tar.log"; then
    echo "Архив данных не распаковался (подробности — $run/tar.log). Данные не тронуты." >&2
    return 1
  fi
  if ! { mkdir -p data && chmod 700 data && [ -w data ]; }; then
    echo "Каталог data недоступен на запись этому пользователю (создан от root?) — каталоги не восстановлены; база, если восстанавливалась, уже на месте. Стоп: права — стоп-точка (sudo), скажите владельцу." >&2
    return 1
  fi
  local done_list=""
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    if [ -e "data/$name" ] && ! mv "data/$name" "$run/replaced/$name"; then
      echo "Не удалось отодвинуть data/$name — каталоги восстановлены не все (готово: ${done_list:-ничего}). Прежнее — в $run/replaced." >&2
      return 1
    fi
    if ! mv "$run/stage/data/$name" "data/$name"; then
      echo "Не удалось положить data/$name из копии — прежний каталог в $run/replaced/$name, копия — в $run/stage/data/$name (агенту не открывать). Стоп: скажите владельцу." >&2
      return 1
    fi
    done_list="${done_list:+$done_list, }data/$name"
  done <<EOF_DATA
$(find "$run/stage/data" -mindepth 1 -maxdepth 1 -printf '%f\n')
EOF_DATA
  # Журналы Hermes в копию не входят: прежние возвращаем на место, чтобы каталог не пропал.
  if [ -d "$run/replaced/hermes/logs" ] && [ ! -e data/hermes/logs ] && [ -d data/hermes ]; then
    mv "$run/replaced/hermes/logs" data/hermes/logs
  fi
  echo "Каталоги данных восстановлены: ${done_list:-(в архиве их нет)}"
}

# Отметка «база и данные только что восстановлены из копии <метка>»: ./ops/up.sh принимает её
# вместо свежей копии — восстановленное состояние и есть копия. Пишется через ./ops/state.sh.
mark_restored() {
  ./ops/state.sh set restore.last "$1@$(date +%s)" > /dev/null 2>&1 || true
}
# Метка копии, из которой восстановлено не раньше $1 минут назад; пусто — нет.
recently_restored() {
  local v s e
  v="$(./ops/state.sh get restore.last 2> /dev/null || true)"
  s="${v%@*}"; e="${v##*@}"
  case "$e" in ''|*[!0-9]*) return 1 ;; esac
  [ $(( $(date +%s) - e )) -le $(( $1 * 60 )) ] || return 1
  printf '%s\n' "$s"
}

# Восстановить .env из архива копии. Прежний .env — в $2/replaced/.env.
restore_env() {
  local archive="$1" run="$2"
  mkdir -p "$run/env" "$run/replaced"
  if ! tar -xzf "$archive" -C "$run/env" .env 2> /dev/null; then
    echo "В архиве копии нет файла .env." >&2
    return 1
  fi
  if [ -f .env ] && ! mv .env "$run/replaced/.env"; then echo "Не удалось отодвинуть нынешний .env." >&2; return 1; fi
  chmod 600 "$run/env/.env" && mv "$run/env/.env" .env || { echo "Не удалось положить .env из копии; прежний — в $run/replaced/.env." >&2; return 1; }
  echo "Файл .env восстановлен из копии."
}

# Пароль роли базы — как в восстановленном .env (база уже была создана с другим .env). Значение
# читается из файла и передаётся psql через стандартный ввод; на экран и в параметры не попадает.
pg_align_password() {
  local pw
  pw="$(grep -E '^POSTGRES_PASSWORD=' .env | tail -n 1 | cut -d= -f2-)"
  if ! printf '%s' "$pw" | grep -Eq '^[A-Za-z0-9._~+/=-]{8,}$'; then
    echo "Пароль базы в восстановленном .env не похож на сгенерированный ./ops/init-env.sh — роль базы не изменена; если сервис не подключится к базе, это и есть причина (стоп, скажите владельцу)." >&2
    return 1
  fi
  printf 'ALTER ROLE shturman PASSWORD $pw$%s$pw$;\n' "$pw" | docker exec -i "$PG" psql -U shturman -d postgres -Xq -v ON_ERROR_STOP=1 > /dev/null
}

# --- откат обновления ---
UPDATE_DIR="local/update"
UPDATE_LAST="$UPDATE_DIR/last.env"

# Записать поле в local/update/last.env (несекретное: коммиты, метка копии, итог).
last_set() {
  local tmp
  mkdir -p "$UPDATE_DIR"; chmod 700 "$UPDATE_DIR"
  touch "$UPDATE_LAST"
  tmp="$(mktemp "$UPDATE_DIR/last.XXXXXX")"
  grep -Ev "^$1=" "$UPDATE_LAST" > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  mv "$tmp" "$UPDATE_LAST"
}

# Что возвращать при откате обновления:
#   code — только код (миграции не применялись, Hermes тот же): база и данные совместимы с прежним кодом;
#   data — код, база и каталоги data/ из копии, снятой перед обновлением.
#   $1, $2 — применённые миграции до и после через запятую («?» — узнать не удалось);
#   $3, $4 — образ Hermes до и после («-» — Hermes нет).
rollback_scope() {
  if [ "$1" = "?" ] || [ "$2" = "?" ] || [ "$1" != "$2" ]; then echo data; return 0; fi
  if [ "$3" != "$4" ]; then echo data; return 0; fi
  echo code
}

# Образ Hermes, с которым экземпляр запустится по нынешним compose.yaml и .env; «-» — режим без Hermes.
hermes_image_planned() {
  [ "${MODE:-hermes}" = hermes ] || { echo -; return 0; }
  docker compose --profile hermes config --images 2> /dev/null | grep -m1 'hermes-agent' || echo "?"
}
# Образ работающего контейнера Hermes; «-» — контейнера нет.
hermes_image_running() {
  local i
  i="$(docker inspect -f '{{.Config.Image}}' shturman-hermes 2> /dev/null)" || i=""
  printf '%s\n' "${i:--}"
}

# Откат последнего обновления по записи local/update/last.env: каталоги и база — из копии, снятой
# перед обновлением (если scope=data), код — на прежний коммит, затем ./ops/up.sh прежней версии.
# Подтверждение человека проверяет вызывающий. $1 — scope (code|data).
rollback_run() {
  local scope="$1" from stamp dir run rc=0
  from="$(meta_get "$UPDATE_LAST" from_commit)"; stamp="$(meta_get "$UPDATE_LAST" backup)"
  [ -n "$from" ] || { echo "В $UPDATE_LAST нет прежнего коммита — откатывать не к чему." >&2; return 1; }
  if [ "$scope" = data ]; then
    dir="$(bk_find "$stamp")" || { echo "Копии $stamp, снятой перед обновлением, нет ни в $BK_LOCAL, ни во внешнем каталоге — откат базы невозможен." >&2; return 1; }
    run="local/restore/rollback-$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$run"; chmod 700 local/restore "$run"
    echo "Останавливаю ассистента и сервис (база остаётся запущенной)…"
    stack_stop_but_db || true
    if [ -f "$dir/db-$stamp.dump" ]; then
      restore_db "$dir/db-$stamp.dump" "$dir/meta-$stamp.txt" "$run" || return 1
    fi
    if [ -f "$dir/data-$stamp.tar.gz" ]; then
      restore_data "$dir/data-$stamp.tar.gz" "$run" || return 1
    fi
    rm -rf "$run/replaced" "$run/stage"
  fi
  if [ "$scope" = data ]; then mark_restored "$stamp"; fi
  echo "Возвращаю код на прежний коммит ${from:0:12}…"
  git checkout -q --detach "$from" || { echo "git checkout $from не прошёл — отслеживаемые файлы изменены? (git status)" >&2; return 1; }
  ./ops/up.sh || rc=$?
  return "$rc"
}
