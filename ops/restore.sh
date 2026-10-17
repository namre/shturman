#!/usr/bin/env bash
# Восстановление экземпляра из копии ./ops/backup.sh. Это стоп-точка AGENTS.md («восстановление
# поверх данных»): скрипт сам просит разрешение владельца — карточкой в боте согласований (Р-100).
#   ./ops/restore.sh --list                 — какие копии есть: время, размер, версия кода, где лежат
#   ./ops/restore.sh --from МЕТКА --dry-run — что будет сделано и что будет перезаписано; ничего не меняет
#   ./ops/restore.sh --from МЕТКА           — восстановить. МЕТКА — из --list (20261016T081500Z) или latest.
#       --only db      — только базу архива;   --only data — только каталоги data/ (без базы);
#       --with-env     — заменить и .env файлом из копии (без ключа .env заменяется, только если его нет:
#                        новый сервер); пароль роли базы приводится к восстановленному .env;
#       --dir КАТАЛОГ  — искать копию ещё и в этом каталоге (диск с копиями на новом сервере);
#       --no-start     — не запускать стек после восстановления (база остаётся запущенной): когда
#                        следом нужен код другой версии — git checkout, затем ./ops/up.sh.
#   ./ops/restore.sh --rollback [--dry-run] — откатить последнее обновление (./ops/update.sh): код —
#       на прежний коммит; если применились миграции или сменился образ Hermes — ещё база и data/
#       из копии, снятой перед обновлением. Всё, что пришло после этой копии, из архива пропадёт.
#   -h, --help — эта справка; ничего не делается.
# Восстановление и откат может запустить и агент сопровождения: скрипт показывает, что будет перезаписано,
# и шлёт владельцу в бот согласований карточку с тем же описанием; продолжает только после нажатия
# «Разрешить» (ждёт OPS_CONSENT_WAIT секунд, по умолчанию 300; не дождался — код 4, запрос в силе: повтор
# той же команды продолжит). Сервис переписки не отвечает — карточку шлёт напрямую одноразовый контейнер
# от имени бота (владелец — из базы или из копии сервиса). Бот не настроен или недоступен вовсе
# (новый сервер без .env) — запасной путь: человек в своём терминале (ssh -t …) набирает фразу
# с одноразовым числом; без терминала — отказ с кодом 4 и готовой командой.
# Порядок: проверка копии (суммы по описи, архив, оглавление выгрузки) и совместимости с кодом →
# подтверждение → копия нынешнего состояния (./ops/backup.sh --reason before-restore; не вышла —
# отдельное подтверждение «БЕЗ-КОПИИ») → остановка всего, кроме базы → выгрузка в отдельную базу
# shturman_restore, сверка числа строк с описью, подмена живой базы → каталоги data/ → .env →
# ./ops/up.sh (поднимает стек и вызывает ./ops/doctor.sh).
# Копия сделана более новым кодом (в ней миграции, которых нет в коде) — отказ: сначала код той версии.
# Более старым — ./ops/up.sh после восстановления применит недостающие миграции.
# Секреты и переписку скрипт не печатает: о базе — только число строк ключевых таблиц, об архиве —
# имена каталогов верхнего уровня. Ошибки pg_restore — в local/restore/…/pg_restore.log (в нём может
# оказаться строка данных: агенту не открывать).
# Коды: 0 — восстановлено, ./ops/doctor.sh без FAIL; 1 — не удалось или doctor с FAIL (что именно —
# в выводе); 2 — неверные параметры; 4 — нет разрешения владельца (ждём нажатия, отклонено, срок вышел,
# бот недоступен — что именно, в выводе); 75 — идёт другая команда ops/.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

# Всё тело — в функции: откат меняет файлы репозитория, в том числе этот, пока скрипт работает;
# bash читает функцию целиком до запуска, и замена файла на диске ему не мешает.
NO_START=no
main() {
  local action="" from="" only=all with_env=no extra="" dry=no
  while [ $# -gt 0 ]; do
    case "$1" in
      --list) action=list ;;
      --rollback) action=rollback ;;
      --from) [ -n "${2:-}" ] || { echo "--from: нужна метка копии (./ops/restore.sh --list) или latest" >&2; exit 2; }; action=restore; from="$2"; shift ;;
      --only) case "${2:-}" in db|data|all) only="$2"; shift ;; *) echo "--only: db, data или all" >&2; exit 2 ;; esac ;;
      --with-env) with_env=yes ;;
      --dir) [ -d "${2:-}" ] || { echo "--dir: нет такого каталога: ${2:-}" >&2; exit 2; }; extra="$2"; shift ;;
      --dry-run) dry=yes ;;
      --no-start) NO_START=yes ;;
      *) ops_unknown "$1" ;;
    esac
    shift
  done
  . ops/lib-backup.sh
  case "$action" in
    list) bk_list "$extra"; exit 0 ;;
    restore) do_restore "$from" "$only" "$with_env" "$extra" "$dry" ;;
    rollback) do_rollback "$dry" ;;
    *) echo "нужно действие: --list, --from МЕТКА или --rollback — справка: ./ops/restore.sh --help" >&2; exit 2 ;;
  esac
}

# Копия нынешнего состояния перед перезаписью. Не вышла — только с отдельным разрешением владельца.
#   $1 — метка копии, из которой восстанавливаем; $2 — команда для повтора.
safety_copy() {
  if [ ! -d data ] && ! pg_running; then return 0; fi
  echo "Делаю копию нынешнего состояния (на случай, если восстановленное окажется хуже)…"
  if ./ops/backup.sh --reason before-restore; then return 0; fi
  echo "Копию нынешнего состояния сделать не удалось (причина — выше). Без неё нынешние данные после восстановления вернуть будет нечем."
  if OWNER_CONSENT_MAY_STOP=1 confirm_owner restore_nocopy "$1" "БЕЗ-КОПИИ" "$2" "reason=копия не сделана"; then return 0; fi
  echo "Ничего не изменено." >&2
  exit "$OPS_NEED_HUMAN"
}

do_restore() {
  local from="$1" only="$2" with_env="$3" extra="$4" dry="$5" dir meta s parts="" have_db=no have_data=no
  local mig code_mig newer older env_act now_counts tops run rc=0 src
  if [ "$from" = latest ]; then
    from="$( { bk_stamps "$BK_LOCAL"; [ -n "$(bk_external)" ] && bk_stamps "$(bk_external)"; [ -n "$extra" ] && bk_stamps "$extra"; } | sort -ru | head -n 1)"
    [ -n "$from" ] || { echo "Копий нет. Сделать: ./ops/backup.sh" >&2; exit 1; }
  fi
  printf '%s' "$from" | grep -Eq '^[0-9]{8}T[0-9]{6}Z$' || { echo "--from: метка вида 20261016T081500Z (./ops/restore.sh --list)" >&2; exit 2; }
  dir="$(bk_find "$from" "$extra")" || { echo "Копии $from нет ни в $BK_LOCAL, ни во внешнем каталоге${extra:+, ни в $extra}. Какие есть — ./ops/restore.sh --list" >&2; exit 1; }
  meta="$dir/meta-$from.txt"
  if [ "$only" != data ] && [ -f "$dir/db-$from.dump" ]; then have_db=yes; parts="база"; fi
  if [ "$only" != db ] && [ -f "$dir/data-$from.tar.gz" ]; then have_data=yes; parts="${parts:+$parts, }каталоги data/"; fi
  if [ "$only" = db ] && [ "$have_db" = no ]; then echo "В копии $from нет выгрузки базы." >&2; exit 1; fi
  if [ "$only" = data ] && [ "$have_data" = no ]; then echo "В копии $from нет архива данных." >&2; exit 1; fi
  [ -n "$parts" ] || { echo "В копии $from нечего восстанавливать." >&2; exit 1; }
  env_act="остаётся нынешний"
  if [ "$with_env" = yes ]; then env_act="ЗАМЕНЯЕТСЯ файлом из копии"; elif [ ! -f .env ]; then env_act="берётся из копии (сейчас его нет)"; fi
  if [ "$with_env" = yes ] || [ ! -f .env ]; then
    [ -f "$dir/data-$from.tar.gz" ] || { echo "Файл .env нужен из копии, а архива данных в ней нет." >&2; exit 1; }
  fi

  echo "Копия $from ($dir):"
  if [ -f "$meta" ]; then
    echo "  сделана $(meta_get "$meta" created), причина: $(meta_get "$meta" reason), версия $(meta_get "$meta" version) ($(meta_get "$meta" commit | cut -c1-12), $(meta_get "$meta" ref)), режим $(meta_get "$meta" mode)"
  else
    echo "  описи нет (копия сделана до 0.0.8): версия кода и число строк неизвестны"
  fi
  if ! bk_verify "$dir" "$from"; then
    echo "Копия не прошла проверку — восстанавливать из неё нельзя. Возьмите другую (./ops/restore.sh --list)." >&2
    exit 1
  fi

  # Совместимость с кодом: миграции копии против миграций в коде.
  mig="$(meta_get "$meta" migrations)"
  code_mig="$(repo_migrations)"
  if [ "$have_db" = yes ] && [ -n "$mig" ]; then
    newer="$(list_minus "$mig" "$code_mig")"
    if [ -n "$newer" ]; then
      echo "Копия сделана более новой версией кода: в ней миграции, которых нет в нынешнем коде ($newer)." >&2
      echo "Сначала код той версии: коммит $(meta_get "$meta" commit) (git checkout --detach …), или откат обновления: ./ops/restore.sh --rollback. Ничего не изменено." >&2
      exit 1
    fi
    older="$(list_minus "$code_mig" "$mig")"
  else
    older=""
  fi
  if [ -f "$meta" ] && git rev-parse HEAD > /dev/null 2>&1 && [ "$(meta_get "$meta" commit)" != "нет-git" ] \
     && [ "$(meta_get "$meta" commit)" != "$(git rev-parse HEAD)" ]; then
    echo "  код сейчас другой: $(cat VERSION 2> /dev/null) ($(git rev-parse --short=12 HEAD 2> /dev/null)) — код не меняется, восстанавливаются только данные"
  fi

  echo
  echo "Что будет сделано:"
  echo "  1. копия нынешнего состояния (./ops/backup.sh --reason before-restore);"
  echo "  2. остановка ассистента и сервиса (база остаётся запущенной);"
  if [ "$have_db" = yes ]; then
    now_counts=""; if pg_running; then now_counts="$(db_counts shturman 2> /dev/null || true)"; fi
    echo "  3. база архива ЗАМЕНЯЕТСЯ копией: сейчас — ${now_counts:-не запущена или пуста}; в копии — $(meta_get "$meta" counts_after)"
    echo "     всё, что пришло в архив после $(meta_get "$meta" created), из базы пропадёт"
  fi
  if [ "$have_data" = yes ]; then
    tops="$(tar -tzf "$dir/data-$from.tar.gz" 2> /dev/null | awk -F/ '$1 == "data" && $2 != "" {print "data/" $2}' | sort -u | paste -sd' ' -)"
    echo "  4. каталоги ЗАМЕНЯЮТСЯ копией: ${tops:-нет}; база, модели и журналы не затрагиваются"
  fi
  echo "  5. файл .env: $env_act"
  if [ "$NO_START" = yes ]; then echo "  6. стек не запускается (--no-start): дальше — код нужной версии и ./ops/up.sh"
  elif [ -n "$older" ]; then echo "  6. ./ops/up.sh поднимет стек и применит миграции, которых в копии ещё не было: $(list_count "$older")"; else echo "  6. ./ops/up.sh поднимет стек и вызовет ./ops/doctor.sh"; fi
  local again envflag=keep
  again="./ops/restore.sh --from $from"
  [ "$only" = all ] || again="$again --only $only"
  [ "$with_env" = no ] || again="$again --with-env"
  [ -z "$extra" ] || again="$again --dir $extra"
  [ "$NO_START" = no ] || again="$again --no-start"
  if [ "$dry" = yes ]; then
    echo; echo "Это проверка (--dry-run): ничего не изменено. Запуск без --dry-run ($again) пришлёт владельцу запрос в бот согласований и продолжит после нажатия «Разрешить»."
    exit 0
  fi

  echo
  echo "Восстановление перезапишет то, что перечислено выше. Спрашиваю разрешение владельца."
  { [ "$with_env" = yes ] || [ ! -f .env ]; } && envflag="env"
  if ! OWNER_CONSENT_MAY_STOP=1 confirm_owner restore "$from:$only:$envflag" "ВОССТАНОВИТЬ" "$again" \
       "created=$(meta_get "$meta" created)" "parts=$parts" "env=$env_act" "version=$(meta_get "$meta" version)"; then
    exit "$OPS_NEED_HUMAN"
  fi
  ./ops/state.sh log "восстановление из копии $from: владелец разрешил (${OWNER_CONSENT_VIA:-?})" > /dev/null 2>&1 || true

  ops_lock "восстановление из копии $from (./ops/restore.sh)"
  ./ops/state.sh log "восстановление из копии $from: начато (только: $only)" > /dev/null 2>&1 || true
  run="local/restore/$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$run/src"; chmod 700 local/restore "$run"
  # Источник — в рабочий каталог (жёсткой ссылкой, если тот же диск): ротация копии перед
  # восстановлением не должна удалить ту, из которой восстанавливаем.
  for s in "data-$from.tar.gz" "db-$from.dump" "meta-$from.txt"; do
    [ -f "$dir/$s" ] || continue
    ln "$dir/$s" "$run/src/$s" 2> /dev/null || cp "$dir/$s" "$run/src/$s"
  done
  src="$run/src"
  safety_copy "$from" "$again"

  if [ "$with_env" = yes ] || [ ! -f .env ]; then
    local had_cluster=no
    [ -d data/postgres ] && had_cluster=yes
    restore_env "$src/data-$from.tar.gz" "$run" || exit 1
    if [ "$had_cluster" = yes ] && pg_running; then
      pg_align_password && echo "Пароль роли базы приведён к восстановленному .env."
    fi
  fi
  load_mode
  echo "Останавливаю ассистента и сервис (база остаётся запущенной)…"
  stack_stop_but_db
  if [ "$have_db" = yes ]; then restore_db "$src/db-$from.dump" "$src/meta-$from.txt" "$run" || exit 1; fi
  if [ "$have_data" = yes ]; then restore_data "$src/data-$from.tar.gz" "$run" || exit 1; fi
  mark_restored "$from"
  rm -rf "$run/src" "$run/stage" "$run/replaced" "$run/env"
  if [ "$NO_START" = yes ]; then
    ./ops/state.sh log "восстановление из копии $from: данные на месте, стек не запущен (--no-start)" > /dev/null 2>&1 || true
    echo; echo "ИТОГ ВОССТАНОВЛЕНИЯ: OK — данные из копии $from на месте; стек не запущен (--no-start). Дальше: код нужной версии (git checkout --detach …) и ./ops/up.sh в ближайшие $(bk_fresh_minutes) минут — свежая копия ему тогда не понадобится."
    exit 0
  fi
  ./ops/state.sh log "восстановление из копии $from: данные на месте, запускаю ./ops/up.sh" > /dev/null 2>&1 || true
  echo "Поднимаю стек: ./ops/up.sh"
  ./ops/up.sh || rc=$?
  if [ "$rc" -eq 0 ]; then
    ./ops/state.sh log "восстановление из копии $from: ИТОГ OK" > /dev/null 2>&1 || true
    echo; echo "ИТОГ ВОССТАНОВЛЕНИЯ: OK — копия $from, ./ops/doctor.sh без FAIL. Копия состояния до восстановления — в ./ops/backup.sh --list (причина before-restore)."
  else
    ./ops/state.sh log "восстановление из копии $from: данные восстановлены, ./ops/up.sh с ошибкой ($rc)" > /dev/null 2>&1 || true
    echo; echo "ИТОГ ВОССТАНОВЛЕНИЯ: FAIL — данные из копии $from на месте, но ./ops/up.sh завершился с кодом $rc (строки FAIL выше). Разбор — по строкам doctor (docs/runbooks/restore.md, «Если что-то не так»)."
    exit 1
  fi
}

do_rollback() {
  local dry="$1" from stamp dir meta scope cur_mig bk_mig hb ha result rc=0 age
  [ -f "$UPDATE_LAST" ] || { echo "Записи об обновлении ($UPDATE_LAST) нет: ./ops/update.sh здесь не запускался. Вернуть данные из копии — ./ops/restore.sh --from МЕТКА." >&2; exit 1; }
  from="$(meta_get "$UPDATE_LAST" from_commit)"; stamp="$(meta_get "$UPDATE_LAST" backup)"; result="$(meta_get "$UPDATE_LAST" result)"
  if [ "$(git rev-parse HEAD)" = "$from" ]; then
    echo "Код уже на прежнем коммите ${from:0:12} (результат последнего обновления: $result) — откатывать нечего." >&2
    exit 0
  fi
  git diff --quiet HEAD -- 2> /dev/null || { echo "Отслеживаемые файлы на сервере изменены (git status) — откат кода невозможен без потери правок. Стоп: скажите владельцу." >&2; exit 1; }
  dir="$(bk_find "$stamp")" || dir=""
  meta="${dir:+$dir/meta-$stamp.txt}"
  scope="$(meta_get "$UPDATE_LAST" scope)"
  if [ -z "$scope" ]; then
    load_mode
    cur_mig="?"; if pg_running; then cur_mig="$(db_migrations shturman 2> /dev/null || echo "?")"; fi
    bk_mig="$(meta_get "$meta" migrations)"; [ -n "$bk_mig" ] || bk_mig="?"
    hb="$(meta_get "$UPDATE_LAST" hermes_before)"; ha="$(hermes_image_running)"
    scope="$(rollback_scope "$bk_mig" "$cur_mig" "${hb:--}" "$ha")"
  fi
  echo "Последнее обновление: $(meta_get "$UPDATE_LAST" from_name) → $(meta_get "$UPDATE_LAST" to_name), начато $(meta_get "$UPDATE_LAST" started), результат: ${result:-неизвестен}."
  echo "Откат:"
  echo "  код — на прежний коммит ${from:0:12} ($(meta_get "$UPDATE_LAST" from_name));"
  if [ "$scope" = data ]; then
    [ -n "$dir" ] || { echo "Копии $stamp, снятой перед обновлением, нет — откат базы невозможен. Стоп: скажите владельцу." >&2; exit 1; }
    age=$(( ($(date +%s) - $(bk_epoch "$dir" "$stamp")) / 60 ))
    echo "  база и каталоги data/ — из копии $stamp (сделана $age мин назад): применились миграции или сменился Hermes, прежний код с новыми данными не проверен;"
    echo "  всё, что пришло в архив за эти $age мин, из базы пропадёт; перед откатом — копия нынешнего состояния."
  else
    echo "  база и данные не меняются: миграции не применялись, Hermes тот же."
  fi
  echo "  затем ./ops/up.sh прежней версии и ./ops/doctor.sh."
  if [ "$dry" = yes ]; then echo; echo "Это проверка (--dry-run): ничего не изменено. Запуск без --dry-run (./ops/restore.sh --rollback) пришлёт владельцу запрос в бот согласований и продолжит после нажатия «Разрешить»."; exit 0; fi
  if ! OWNER_CONSENT_MAY_STOP=1 confirm_owner rollback "$from" "ОТКАТИТЬ" "./ops/restore.sh --rollback" \
       "from=$(meta_get "$UPDATE_LAST" from_name)" "to=$(meta_get "$UPDATE_LAST" to_name)" "stamp=$stamp" \
       "minutes=${age:-}" "scope=$scope"; then
    exit "$OPS_NEED_HUMAN"
  fi
  ./ops/state.sh log "откат обновления: владелец разрешил (${OWNER_CONSENT_VIA:-?})" > /dev/null 2>&1 || true
  ops_lock "откат обновления (./ops/restore.sh --rollback)"
  load_mode
  if [ "$scope" = data ]; then safety_copy "${stamp:-}" "./ops/restore.sh --rollback"; fi
  ./ops/state.sh log "откат обновления на ${from:0:12} (scope $scope): начат" > /dev/null 2>&1 || true
  rollback_run "$scope" || rc=$?
  last_set result "$( [ "$rc" -eq 0 ] && echo rolled-back || echo rollback-failed)"
  if [ "$rc" -eq 0 ]; then
    ./ops/state.sh log "откат обновления: ИТОГ OK" > /dev/null 2>&1 || true
    echo; echo "ИТОГ ОТКАТА: OK — код на ${from:0:12}, ./ops/doctor.sh без FAIL."
  else
    ./ops/state.sh log "откат обновления: ошибка ($rc)" > /dev/null 2>&1 || true
    echo; echo "ИТОГ ОТКАТА: FAIL (код $rc) — строки выше. Стоп: строка дефекта (docs/pilot/defects.md) и владельцу."
    exit 1
  fi
}

main "$@"; exit $?
