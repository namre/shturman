#!/usr/bin/env bash
# Копия данных экземпляра перед обновлением или рискованным действием.
# Складывает в local/backups (вне git, права 600) архив каталога data/ и выгрузку базы архива.
# Вместе с data/ в архив кладётся файл .env. Работает одинаково в обоих режимах установки:
# без Hermes каталога data/hermes просто нет. Значения, которые владелец ввёл на странице
# настройки переписки (ключи приложения Telegram, а если заводил — токен бота согласований и ключ
# модели), лежат в data/shturman/setup и попадают в архив вместе с каталогом; введённые
# в терминале — в .env.
# В копии ключи, настройки и переписка владельца: агент её не открывает и никуда не передаёт.
# Файлы базы и модели (эмбеддинги, защита, распознавание речи) в архив каталога не входят: база
# сохраняется выгрузкой (pg_dump), модели скачиваются заново командой «on» своего скрипта.
# Не входят и временные файлы сервиса: загруженные выгрузки (data/shturman/uploads) и файлы
# вложений из архива выгрузки, ждущие разбора (data/shturman/media-files) — их может быть много,
# и после разбора они удаляются сами. Ссылки на них в восстановленной базе сервис снимет сам.
# Рядом с архивом и выгрузкой — опись meta-<метка>.txt без секретов: версия и коммит кода, режим,
# версия Hermes, применённые миграции, размеры и контрольные суммы файлов, число строк ключевых
# таблиц до и после выгрузки, итог проверки. Её читают ./ops/restore.sh, ./ops/up.sh и ./ops/update.sh.
# Версия, коммит и тег в описи (version, commit, ref) — работающего кода (GET /api/status сервиса переписки;
# code_source=service, а если сервис не ответил — checkout), checkout_* — кода в каталоге; обычно совпадают. Копию, у которой нет записанного в опись файла, ни проверка
# (--verify), ни ./ops/up.sh, ни ./ops/update.sh, ни ./ops/restore.sh за целую не принимают.
#   ./ops/backup.sh                — сделать копию, проверить её и, если задан внешний каталог,
#                                    положить копию и туда (с проверкой контрольных сумм)
#   ./ops/backup.sh --list         — показать имеющиеся копии: время, размер, версия кода, где лежат
#   ./ops/backup.sh --verify [МЕТКА] — проверить копию (по умолчанию — самую новую) во всех местах,
#                                    где она лежит: контрольные суммы по описи, архив читается,
#                                    оглавление выгрузки читается (pg_restore --list). Ничего не меняет
#   ./ops/backup.sh --reason before-update|before-restore|before-hermes — причина в описи; ставят
#                                    ./ops/update.sh и ./ops/restore.sh, руками не нужно
#   ./ops/backup.sh --set-dir ПУТЬ — записать внешний каталог (SHTURMAN_BACKUP_DIR): полный путь,
#                                    каталог есть и доступен на запись. Копию не делает
#   ./ops/backup.sh --unset-dir    — убрать внешний каталог (копии в нём остаются)
#   ./ops/backup.sh --set-keep N   — сколько копий хранить (SHTURMAN_BACKUP_KEEP, от 2 до 100)
#   ./ops/backup.sh --help         — эта справка; ничего не создаётся
# Несекретные строки .env:
#   SHTURMAN_BACKUP_KEEP=5        — сколько последних копий хранить в каждом каталоге (не меньше 2);
#   SHTURMAN_BACKUP_DIR=/mnt/…    — внешний каталог (другой диск, точка монтирования): копия кладётся
#                                    и туда. Тот же диск, что local/backups, — предупреждение: от потери
#                                    диска это не спасает. Каталог недоступен — локальная копия остаётся,
#                                    скрипт завершается с кодом 3. Варианты вне сервера — docs/deployment.md.
# Копия идёт под общим замком ops/ (второй запуск — код 75). Восстановление — ./ops/restore.sh.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"
action="make"; reason="manual"; vstamp=""
while [ $# -gt 0 ]; do
  case "$1" in
    --list) action=list ;;
    --verify)
      action=verify
      case "${2:-}" in [0-9]*) vstamp="$2"; shift ;; esac ;;
    --reason)
      case "${2:-}" in
        manual|before-update|before-restore|before-hermes) reason="$2"; shift ;;
        *) echo "--reason: одно из manual, before-update, before-restore, before-hermes" >&2; exit 2 ;;
      esac ;;
    --set-dir) [ -n "${2:-}" ] || { echo "--set-dir: нужен полный путь каталога" >&2; exit 2; }; action="set-dir"; vstamp="$2"; shift ;;
    --unset-dir) action="unset-dir" ;;
    --set-keep)
      case "${2:-}" in ''|*[!0-9]*) echo "--set-keep: число от 2 до 100" >&2; exit 2 ;; esac
      [ "$2" -ge 2 ] && [ "$2" -le 100 ] || { echo "--set-keep: число от 2 до 100" >&2; exit 2; }
      action="set-keep"; vstamp="$2"; shift ;;
    *) ops_unknown "$1" ;;
  esac
  shift
done
. ops/lib-backup.sh

dir="$BK_LOCAL"

# Настройки копий — несекретные строки .env; остальное в .env не читается и не печатается.
case "$action" in
  set-dir|unset-dir|set-keep)
    [ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }
    ops_lock "настройка копий (./ops/backup.sh --$action)"
    umask 077
    case "$action" in
      set-dir)
        case "$vstamp" in /*) ;; *) echo "--set-dir: нужен полный путь (/mnt/…)" >&2; exit 2 ;; esac
        [ -d "$vstamp" ] && [ -w "$vstamp" ] || { echo "Каталога $vstamp нет или в него нельзя писать (диск не подключён? подключение — стоп-точка: sudo). Ничего не записано." >&2; exit 1; }
        mkdir -p "$dir"
        if [ "$(stat -c %d "$vstamp")" = "$(stat -c %d "$dir")" ]; then
          echo "Внимание: $vstamp на том же диске, что local/backups, — от потери диска или сервера это не спасает." >&2
        fi
        env_put SHTURMAN_BACKUP_DIR "$vstamp"
        echo "Внешний каталог копий записан: $vstamp. Следующая ./ops/backup.sh положит копию и туда." ;;
      unset-dir) env_del SHTURMAN_BACKUP_DIR; echo "Внешний каталог копий убран; копии, что в нём уже есть, не тронуты." ;;
      set-keep) env_put SHTURMAN_BACKUP_KEEP "$vstamp"; echo "Хранить последних копий: $vstamp (лишние удалятся при следующей копии)." ;;
    esac
    exit 0 ;;
esac

if [ "$action" = list ]; then
  bk_list
  exit 0
fi

if [ "$action" = verify ]; then
  ext="$(bk_external)"
  if [ -z "$vstamp" ]; then
    vstamp="$( { bk_stamps "$dir"; [ -n "$ext" ] && bk_stamps "$ext"; } | sort -ru | head -n 1)"
  fi
  [ -n "$vstamp" ] || { echo "Копий нет — проверять нечего. Сделать: ./ops/backup.sh" >&2; exit 1; }
  bad=0; seen=no
  for d in "$dir" "$ext"; do
    [ -n "$d" ] || continue
    if [ -f "$d/data-$vstamp.tar.gz" ] || [ -f "$d/db-$vstamp.dump" ]; then
      seen=yes
      echo "Копия $vstamp — $d:"
      bk_verify "$d" "$vstamp" || bad=1
    fi
  done
  [ "$seen" = yes ] || { echo "Копии $vstamp нет. Какие есть — ./ops/backup.sh --list" >&2; exit 1; }
  if [ "$bad" -eq 0 ]; then echo "ИТОГ ПРОВЕРКИ КОПИИ: OK"; else echo "ИТОГ ПРОВЕРКИ КОПИИ: FAIL — копия повреждена; сделайте новую: ./ops/backup.sh"; exit 1; fi
  exit 0
fi

ops_lock "копия данных (./ops/backup.sh)"
mkdir -p "$dir"; chmod 700 local "$dir"
[ -d data ] || { echo "нет каталога data — копировать нечего" >&2; exit 1; }
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
while [ -e "$dir/data-$stamp.tar.gz" ] || [ -e "$dir/meta-$stamp.txt" ]; do
  sleep 1; stamp="$(date -u +%Y%m%dT%H%M%SZ)"
done
# Провал на середине не оставляет полкопии: файлы этой метки удаляются.
done_ok=no
cleanup() { if [ "$done_ok" != yes ]; then rm -f -- "$dir/data-$stamp.tar.gz" "$dir/db-$stamp.dump" "$dir/db-$stamp.dump.part" "$dir/meta-$stamp.txt"; fi; }
trap cleanup EXIT

name="$dir/data-$stamp.tar.gz"
# Файлы могут меняться на ходу (журналы Hermes): для tar это предупреждение, а не провал.
# .env кладётся в архив целиком, не читая: в нём режим установки и значения, введённые владельцем.
members="data"
[ -f .env ] && members="data .env"
# shellcheck disable=SC2086
tar -czf "$name" --warning=no-file-changed \
  --exclude='data/hermes/logs' --exclude='data/postgres' --exclude='data/embeddings' --exclude='data/guard' --exclude='data/asr' \
  --exclude='data/shturman/uploads' --exclude='data/shturman/media-files' $members || [ $? -eq 1 ]
count="$(tar -tzf "$name" | wc -l)"
[ "$count" -gt 0 ] || { echo "копия пустая: $name" >&2; exit 1; }
echo "Копия готова: $name ($(du -h "$name" | cut -f1), файлов: $count)"

# База архива: выгрузка средствами Postgres — согласованная, в отличие от копии файлов на ходу.
# Число строк ключевых таблиц — до и после выгрузки: восстановленная база должна попасть между ними.
dump=""; counts_before=""; counts_after=""; migrations=""
if pg_running; then
  dump="$dir/db-$stamp.dump"
  counts_before="$(db_counts shturman 2> /dev/null || true)"
  migrations="$(db_migrations shturman 2> /dev/null || true)"
  if docker exec "$PG" pg_dump -U shturman -d shturman -Fc > "$dump.part" && [ -s "$dump.part" ]; then
    mv "$dump.part" "$dump"
    counts_after="$(db_counts shturman 2> /dev/null || true)"
    # Проверка, что выгрузка читается: оглавление должно содержать таблицу сообщений.
    if docker exec -i "$PG" pg_restore --list < "$dump" | grep -q 'TABLE public messages'; then
      echo "Копия базы готова: $dump ($(du -h "$dump" | cut -f1))"
    else
      echo "выгрузка базы не читается: $dump" >&2; exit 1
    fi
  else
    echo "не удалось выгрузить базу архива" >&2; exit 1
  fi
elif [ -d data/postgres ]; then
  echo "контейнер базы не запущен — копия базы НЕ сделана (./ops/up.sh и повторите)" >&2; exit 1
fi

# Опись: без секретов и без содержимого — версии, имена файлов, суммы, числа.
# Версия, коммит и тег — того кода, что работает (сервис переписки, его GET /api/status), а не того, что лежит
# в каталоге: после неудачной сборки они расходятся, и база в копии — от работающего кода (Д-58). Код каталога —
# отдельно (checkout_*). Сервис не отвечает или не называет коммита — всё из каталога; code_source — откуда.
checkout_commit="$(git rev-parse HEAD 2> /dev/null || echo нет-git)"
checkout_version="$(cat VERSION 2> /dev/null || echo ?)"
checkout_ref="$(git describe --tags --exact-match 2> /dev/null || git symbolic-ref --short -q HEAD 2> /dev/null || echo "коммит вне выпуска")"
IFS="$(printf '\t')" read -r commit run_version run_tag <<EOF_RUN || true
$(service_running)
EOF_RUN
case "$commit" in
  ""|none) commit="$checkout_commit"; version="$checkout_version"; ref="$checkout_ref"; code_source=checkout ;;
  *) code_source=service
     version="$run_version"; [ "$version" != - ] || version="?"
     ref="$run_tag"; [ "$ref" != - ] || ref="коммит вне выпуска"
     # Работает тот же код, что в каталоге, — версия и ссылка каталога точнее (тег мог быть и не передан).
     if [ "$commit" = "$checkout_commit" ]; then version="$checkout_version"; ref="$checkout_ref"; fi ;;
esac
mode="$(shturman_mode 2> /dev/null || echo ?)"
hv="$(env_get HERMES_VERSION)"
{
  echo "stamp=$stamp"
  echo "created=$(now_utc)"
  echo "created_epoch=$(date +%s)"
  echo "reason=$reason"
  echo "version=$version"
  echo "commit=$commit"
  echo "ref=$ref"
  echo "code_source=$code_source"
  echo "checkout_version=$checkout_version"
  echo "checkout_commit=$checkout_commit"
  echo "checkout_ref=$checkout_ref"
  echo "mode=$mode"
  echo "hermes_version=${hv:-по умолчанию compose.yaml}"
  echo "hermes_image=$(hermes_image_running)"
  echo "migrations=$migrations"
  echo "migrations_count=$(list_count "$migrations")"
  echo "data_file=$(basename "$name")"
  echo "data_bytes=$(stat -c %s "$name")"
  echo "data_sha256=$(sha256sum "$name" | cut -d' ' -f1)"
  echo "env_included=$([ -f .env ] && echo yes || echo no)"
  if [ -n "$dump" ]; then
    echo "db_file=$(basename "$dump")"
    echo "db_bytes=$(stat -c %s "$dump")"
    echo "db_sha256=$(sha256sum "$dump" | cut -d' ' -f1)"
    echo "counts_before=$counts_before"
    echo "counts_after=$counts_after"
  fi
  echo "verified=yes"
} > "$dir/meta-$stamp.txt"
echo "Опись копии: $dir/meta-$stamp.txt (работающий код: версия $version, коммит ${commit:0:7}; миграций: $(list_count "$migrations"))"

keep="$(bk_keep)"
bk_rotate "$dir" "$keep"

# Внешний каталог: копия кладётся и туда, затем суммы сверяются уже по месту.
ext="$(bk_external)"
if [ -n "$ext" ]; then
  done_ok=yes   # локальная копия готова и проверена — её не удаляем, что бы ни случилось дальше
  case "$ext" in /*) ;; *) echo "SHTURMAN_BACKUP_DIR должен быть полным путём (/mnt/…), а записан «$ext». Локальная копия готова: $stamp." >&2; exit 3 ;; esac
  if [ ! -d "$ext" ] || [ ! -w "$ext" ]; then
    echo "Внешний каталог копий $ext недоступен (не смонтирован или нет прав на запись). Локальная копия готова: $stamp; во внешний каталог она НЕ положена." >&2
    echo "Проверьте подключение диска (стоп-точка: монтирование — sudo) и повторите ./ops/backup.sh." >&2
    exit 3
  fi
  if [ "$(stat -c %d "$ext")" = "$(stat -c %d "$dir")" ]; then
    echo "Внимание: внешний каталог $ext на том же диске, что local/backups, — от потери диска или сервера это не спасает." >&2
  fi
  chmod 700 "$ext" 2> /dev/null || true
  for f in "data-$stamp.tar.gz" "db-$stamp.dump" "meta-$stamp.txt"; do
    [ -f "$dir/$f" ] || continue
    cp "$dir/$f" "$ext/$f.part" && mv "$ext/$f.part" "$ext/$f" || { rm -f "$ext/$f.part"; echo "Не удалось записать $f во внешний каталог $ext (место?). Локальная копия готова: $stamp." >&2; exit 3; }
  done
  ok=yes
  if [ "$(sha256sum "$ext/data-$stamp.tar.gz" | cut -d' ' -f1)" != "$(meta_get "$dir/meta-$stamp.txt" data_sha256)" ]; then ok=no; fi
  if [ -n "$dump" ] && [ "$(sha256sum "$ext/db-$stamp.dump" | cut -d' ' -f1)" != "$(meta_get "$dir/meta-$stamp.txt" db_sha256)" ]; then ok=no; fi
  if [ "$ok" != yes ]; then
    echo "Во внешнем каталоге $ext копия $stamp НЕ совпала по контрольной сумме — диск неисправен? Локальная копия готова." >&2
    exit 3
  fi
  bk_rotate "$ext" "$keep"
  echo "Копия положена во внешний каталог: $ext (контрольные суммы совпали)"
fi
done_ok=yes
echo "Хранятся последние $keep копий (SHTURMAN_BACKUP_KEEP). Список — ./ops/backup.sh --list; восстановление — ./ops/restore.sh."
