#!/usr/bin/env bash
# Обновление «Штурмана» одной командой: проверка → копия → новая версия → ./ops/up.sh → проверка →
# при провале откат. Обновляется только код этого репозитория (и образы, которые он называет);
# HERMES_VERSION в .env скрипт не меняет никогда.
#   ./ops/update.sh --check          — план без изменений: что сейчас, до какой версии, какие миграции
#                                      базы придут, версия Hermes, свежесть копии, нужно ли разрешение
#                                      человека. Меняет только ссылки git (git fetch; после сброса истории
#                                      репозитория — ещё запись перехода, ops/history-reset.sh)
#   ./ops/update.sh                  — обновить до следующего выпуска (тег vX.Y.Z) на пути к последнему:
#                                      по одному выпуску за запуск, без перескоков (UPGRADING.md)
#   ./ops/update.sh --all            — по очереди до последнего выпуска канала: шаг за шагом, каждый — как
#                                      обычный запуск (проверка до, копия, обновление, проверка после, откат при
#                                      провале, разрешение владельца в боте, если шаг его просит); первая неудача
#                                      или стоп-точка UPGRADING.md останавливает цепочку. Только если владелец
#                                      разрешил переход до последнего выпуска (не вместе с --to, --upgrading-read,
#                                      --despite-fail)
#   ./ops/update.sh --to vX.Y.Z      — до названного выпуска (vX.Y.Z или кандидат vX.Y.Z-rc.N); только
#                                      вперёд от нынешнего кода и без пропуска выпусков между ними
#   ./ops/update.sh --channel pilot  — до следующего выпуска или кандидата (rc) — для экземпляра, который
#                                      берёт кандидаты;
#                                      то же — строкой SHTURMAN_UPDATE_CHANNEL=pilot в .env
#   ./ops/update.sh --channel main   — до нынешнего main, мимо выпусков. Только явно, для разработки
#   ./ops/update.sh --set-channel release|pilot — записать канал экземпляра в .env (ничего не обновляет)
#   ./ops/update.sh --consent [--to …|--channel …] — заранее попросить у владельца разрешение на
#                                      автоматический откат базы и data/ из копии (и на непроверенную
#                                      версию Hermes, если она придёт): владельцу в бот согласований
#                                      приходит карточка, он нажимает «Разрешить» (Р-100). Разрешение — на
#                                      одну целевую версию, один раз, 2 часа после нажатия; его израсходует
#                                      следующий ./ops/update.sh. Без --consent обычный запуск спрашивает
#                                      сам и ждёт нажатия (OPS_CONSENT_WAIT секунд, по умолчанию 300)
#   ./ops/update.sh --no-auto-rollback — обновить без такого разрешения (с согласия владельца в разговоре):
#                                      если после миграций проверка не пройдёт, скрипт остановится (код 3)
#                                      и назовёт команду отката (./ops/restore.sh --rollback — тоже через бот)
#   ./ops/update.sh --despite-fail   — обновлять, хотя ./ops/doctor.sh уже до обновления с FAIL;
#                                      успех — если новых FAIL не появилось (только с согласия владельца)
#   ./ops/update.sh --upgrading-read — раздел UPGRADING.md целевой версии прочитан и пересказан владельцу;
#                                      нужен, если в разделе есть «Стоп-точка» (шаги вне этого скрипта)
#   -h, --help                       — эта справка; ничего не делается
# Шаги: замок ops/ → отслеживаемые файлы не изменены → git fetch --tags → сброс истории репозитория, если он
# был: переход по ops/history-reset (./ops/history-reset.sh; иначе стоп, код 3) → выбор цели и план →
# разрешение владельца в боте согласований, если оно нужно (бот недоступен — запасной путь: фраза
# человека в терминале) → ./ops/doctor.sh до (сохраняется) → копия (своя, если нет
# свежей с тем же кодом и той же базой: ./ops/backup.sh --reason before-update) → запись
# local/update/last.env → git checkout --detach цели → ./ops/up.sh (миграции, образы, перезапуск) →
# ./ops/doctor.sh после (до трёх раз с паузой 30 с) → успех: новая версия действительно запущена
# (сервис переписки работает с кодом цели, в режиме с Hermes — Hermes с задуманным образом и новым кодом
# плагина: Hermes запущен после смены кода) и новых FAIL нет. Сервис не отвечает (первый запуск с миграциями
# идёт долго) — скрипт ждёт до OPS_UPDATE_WAIT секунд (по умолчанию 600). Новая версия не запустилась (сборка
# или запуск не прошли, работает прежний код) или так и не ответила — это не успех, а «не установлено»:
# код возвращается на прежний коммит, итог — код 1. Не ответивший сервис перед решением об откате
# останавливается: какие миграции успели лечь, видно только после этого.
# Провал → откат:
#   код — всегда, на прежний коммит;
#   база и data/ из копии — только если применились миграции или сменился образ работающего Hermes (иначе
#   прежний код работает с теми же данными); это перезапись данных — только с разрешением владельца.
# Свежую копию скрипт берёт, только если она целая и прошла проверку сумм (bk_verify), иначе снимает свою;
# откат из неполной копии (нет выгрузки базы) не начинается вовсе.
# Экземпляр после обновления стоит на теге (detached HEAD): git pull больше не путь обновления.
# Вывод и итог дублируются в local/update/<время>.log (без секретов: вывод up.sh и doctor).
# Итог передаётся сервису переписки (`shturman update-result` в его контейнере, Р-101): он пишет владельцу в
# бот согласований «обновлено, проверки прошли» или что не прошло. Сервис не отвечает — обновлению не мешает.
# Коды: 0 — обновлено (или уже последняя версия); 1 — не обновлено (не установилось или откат выполнен),
# doctor без FAIL;
# 2 — неверные параметры или цель; 3 — нужен человек: откат не выполнен или не удался, либо копия
# не сделана; 4 — нет разрешения владельца: ждём нажатия в боте, он отклонил, срок вышел или бот
# недоступен (что именно и что дальше — в выводе); 75 — идёт другая команда ops/.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

REL_RE='^v[0-9]+\.[0-9]+\.[0-9]+$'
RC_RE='^v[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9]+$'
CONSENT_FILE="local/consent/update.env"

# Выпуски (и кандидаты для pilot), которые впереди нынешнего коммита: «ключ<TAB>тег», по возрастанию.
tags_ahead() {
  local channel="$1" t c v head
  head="$(git rev-parse HEAD)"
  git tag -l 'v*' | while IFS= read -r t; do
    if printf '%s' "$t" | grep -Eq "$REL_RE"; then :
    elif [ "$channel" = pilot ] && printf '%s' "$t" | grep -Eq "$RC_RE"; then :
    else continue; fi
    c="$(git rev-parse -q --verify "$t^{commit}")" || continue
    [ "$c" != "$head" ] || continue
    git merge-base --is-ancestor "$head" "$c" 2> /dev/null || continue
    # Тег, у которого VERSION не совпадает с номером, — не выпуск (собран неверно): мимо.
    v="${t#v}"; v="${v%%-rc.*}"
    [ "$(git show "$c:VERSION" 2> /dev/null | tr -d '[:space:]')" = "$v" ] || continue
    printf '%s\t%s\n' "$(tag_key "$t")" "$t"
  done | sort -V
}
# Выпуски строго между нынешним коммитом и целью (их нельзя перескочить).
releases_between() {
  local target_c="$1" t c
  tags_ahead release | while IFS="$(printf '\t')" read -r _ t; do
    c="$(git rev-parse "$t^{commit}")"
    [ "$c" != "$target_c" ] || continue
    git merge-base --is-ancestor "$c" "$target_c" 2> /dev/null && printf '%s\n' "$t"
  done
}
# Раздел UPGRADING.md цели: от заголовка «… → X.Y.Z» до следующего «## ». Для main — первый раздел.
upgrading_section() {
  local rev="$1" ver="$2"
  git show "$rev:UPGRADING.md" 2> /dev/null | awk -v v="$ver" '
    /^## / { if (on) exit; if (v == "" || index($0, "→ " v) > 0) { on = 1 } }
    on { print }'
}

# Итог — сервису переписки (Р-101): `shturman update-result` пишет его прямо в базу, а владельцу его отправит
# сервис, как только работает. Контейнер сервиса не запущен — запись делает одноразовый контейнер из того же
# образа (нужна только база). Не вышло и так — обновлению не мешает. Переменные run, head_name, target — из main.
tell_service() {
  local result="$1" fails="${2:-}"
  local -a args=(update-result --run "$run" --result "$result" --from "$head_name" --to "$target"
                 --new-fails "$(printf '%s' "$fails" | tr ',' ' ')")
  if [ "$(container_state shturman-service)" = running ]; then
    timeout 30 docker exec shturman-service shturman "${args[@]}" > /dev/null 2>&1 || true
  elif pg_running; then
    timeout 90 docker compose run --rm --no-deps -T shturman "${args[@]}" > /dev/null 2>&1 || true
  fi
}

# --all: шаг за шагом до последнего выпуска канала. Каждый шаг — отдельный запуск этого же скрипта под тем же
# замком: со всеми проверками, копией, разрешением и откатом. Первая неудача останавливает цепочку.
chain() {
  local step=0 rc out
  . ops/lib-backup.sh
  ops_lock "обновление по очереди до последнего выпуска (./ops/update.sh --all)"
  mkdir -p "$UPDATE_DIR"; chmod 700 "$UPDATE_DIR"
  out="$UPDATE_DIR/chain-step.txt"
  while [ "$step" -lt 20 ]; do
    step=$((step + 1))
    echo "=== Шаг $step: ./ops/update.sh${*:+ $*} ==="
    rc=0
    ./ops/update.sh "$@" 2>&1 | tee "$out"; rc="${PIPESTATUS[0]}"
    if [ "$rc" -ne 0 ]; then
      echo
      if [ "$step" -gt 1 ]; then echo "Выпуски до этого шага установлены и проверены; экземпляр на $(code_tag || true)."; fi
      echo "ИТОГ ЦЕПОЧКИ: остановка на шаге $step (код $rc) — что дальше, сказано выше (docs/runbooks/update.md, таблица итогов)."
      exit "$rc"
    fi
    if grep -q '^ИТОГ ОБНОВЛЕНИЯ: OK' "$out"; then
      if grep -q '^Есть ещё выпуск' "$out"; then continue; fi
      echo; echo "ИТОГ ЦЕПОЧКИ: OK — последний выпуск канала установлен ($(code_tag || true)), шагов: $step."
      exit 0
    fi
    # Обновлений нет (или уже на цели) — цепочка кончилась.
    echo; echo "ИТОГ ЦЕПОЧКИ: OK — обновлений больше нет, шагов с обновлением: $((step - 1))."
    exit 0
  done
  echo "ИТОГ ЦЕПОЧКИ: остановка — больше 20 шагов подряд; так быть не должно, скажите разработчику." >&2
  exit 3
}

main() {
  local action=update to="" channel="" no_rb=no despite=no read_up=no all=no
  local -a pass=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --all) all=yes ;;
      --check) action=check ;;
      --consent) action=consent ;;
      --to) [ -n "${2:-}" ] || { echo "--to: нужен тег выпуска (vX.Y.Z)" >&2; exit 2; }; to="$2"; shift ;;
      --channel) case "${2:-}" in release|pilot|main) channel="$2"; pass+=(--channel "$2"); shift ;; *) echo "--channel: release, pilot или main" >&2; exit 2 ;; esac ;;
      --no-auto-rollback) no_rb=yes; pass+=(--no-auto-rollback) ;;
      --despite-fail) despite=yes ;;
      --upgrading-read) read_up=yes ;;
      --set-channel)
        case "${2:-}" in release|pilot) ;; *) echo "--set-channel: release или pilot (main — только ключом --channel main)" >&2; exit 2 ;; esac
        [ -f .env ] || { echo "нет .env — экземпляр не развёрнут" >&2; exit 2; }
        ops_lock "канал обновлений (./ops/update.sh --set-channel)"
        umask 077; env_put SHTURMAN_UPDATE_CHANNEL "$2"
        echo "Канал обновлений записан: $2. Ничего не обновлено; план — ./ops/update.sh --check."
        exit 0 ;;
      *) ops_unknown "$1" ;;
    esac
    shift
  done
  if [ -n "$to" ] && [ -n "$channel" ]; then echo "--to и --channel вместе не задаются" >&2; exit 2; fi
  if [ "$all" = yes ]; then
    if [ -n "$to" ] || [ "$action" != update ] || [ "$read_up" = yes ] || [ "$despite" = yes ] || [ "$channel" = main ]; then
      echo "--all — только обновление по очереди до последнего выпуска: без --to, --check, --consent, --channel main," >&2
      echo "--upgrading-read и --despite-fail (стоп-точки и обновление поверх FAIL решаются на каждом выпуске отдельно)." >&2
      exit 2
    fi
    [ -f .env ] || { echo "нет .env — экземпляр не развёрнут; обновлять нечего (docs/runbooks/deploy.md)" >&2; exit 2; }
    chain "${pass[@]}"
  fi
  if [ -n "$to" ] && ! printf '%s' "$to" | grep -Eq "$REL_RE|$RC_RE"; then echo "--to: тег вида v0.0.8 или v0.0.8-rc.1" >&2; exit 2; fi
  [ -f .env ] || { echo "нет .env — экземпляр не развёрнут; обновлять нечего (docs/runbooks/deploy.md)" >&2; exit 2; }
  git rev-parse --git-dir > /dev/null 2>&1 || { echo "каталог не репозиторий git — обновление этим скриптом невозможно" >&2; exit 2; }
  if [ -z "$channel" ] && [ -z "$to" ]; then
    channel="$(env_get SHTURMAN_UPDATE_CHANNEL)"
    case "$channel" in ""|release) channel=release ;; pilot) ;; *) echo "SHTURMAN_UPDATE_CHANNEL в .env: release или pilot (main — только ключом --channel main)" >&2; exit 2 ;; esac
  fi
  . ops/lib-backup.sh
  load_mode

  ops_lock "обновление (./ops/update.sh$( [ "$action" != update ] && printf ' --%s' "$action"))"
  mkdir -p "$UPDATE_DIR"; chmod 700 "$UPDATE_DIR"

  if ! git diff --quiet HEAD -- 2> /dev/null; then
    echo "Отслеживаемые файлы на сервере изменены (git status) — обновление их перезапишет. Так быть не должно (AGENTS.md: локальные настройки — вне git). Стоп: скажите владельцу, ничего не сбрасывайте." >&2
    exit 3
  fi

  echo "Получаю выпуски с GitHub (git fetch --tags)…"
  if ! git fetch --quiet --tags origin 2> "$UPDATE_DIR/fetch.err"; then
    echo "git fetch не прошёл: $(head -n 2 "$UPDATE_DIR/fetch.err" | tr '\n' ' ')" >&2
    echo "Нет сети, или тег на GitHub подменён (would clobber existing tag) — второе не должно случаться: стоп, скажите разработчику." >&2
    exit 3
  fi
  # Историю репозитория начали заново (docs/releases.md, «Сброс истории и закрытый репозиторий»): новые выпуски не связаны с нынешним
  # коммитом. Переход записывает ops/history-reset.sh, если новая история называет его прежний выпуск; дальше —
  # обычные правила «только вперёд, по одному выпуску». Перейти нельзя — он объяснил почему, стоп.
  ./ops/history-reset.sh || exit 3

  # --- цель ---
  local head head_name target target_c target_ver next latest between ahead
  head="$(git rev-parse HEAD)"
  head_name="$(code_tag)"
  [ -n "$head_name" ] || head_name="$(cat VERSION 2> /dev/null), коммит ${head:0:7} вне выпуска"
  if [ "$channel" = main ]; then
    target="origin/main"
    target_c="$(git rev-parse -q --verify "origin/main^{commit}")" || { echo "нет origin/main" >&2; exit 2; }
    target_ver=""
  elif [ -n "$to" ]; then
    target="$to"
    target_c="$(git rev-parse -q --verify "$to^{commit}")" || { echo "Выпуска $to нет на GitHub. Какие есть — git tag -l 'v*'." >&2; exit 2; }
    target_ver="${to#v}"; target_ver="${target_ver%%-rc.*}"
  else
    ahead="$(tags_ahead "$channel")"
    if [ -z "$ahead" ]; then
      if [ -z "$(git tag -l 'v*')" ]; then
        echo "Выпусков (тегов vX.Y.Z) в репозитории ещё нет — обновляться не до чего. Разработка — только явно: ./ops/update.sh --channel main (docs/releases.md)."
      else
        echo "Обновлений нет: нынешний код ($head_name) — последний выпуск канала ${channel}."
      fi
      exit 0
    fi
    next="$(printf '%s\n' "$ahead" | head -n 1 | cut -f2)"
    latest="$(printf '%s\n' "$ahead" | tail -n 1 | cut -f2)"
    target="$next"; target_c="$(git rev-parse "$next^{commit}")"
    target_ver="${next#v}"; target_ver="${target_ver%%-rc.*}"
  fi
  if [ "$target_c" = "$head" ]; then echo "Уже на $target ($head_name) — обновлять нечего."; exit 0; fi
  if ! git merge-base "$head" "$target_c" > /dev/null 2>&1; then
    echo "$target — из другой истории репозитория, и перехода на неё нет (ops/history-reset не называет нынешний коммит или прежний выпуск). Стоп: скажите разработчику (docs/releases.md, «Сброс истории и закрытый репозиторий»)." >&2
    exit 3
  fi
  if ! git merge-base --is-ancestor "$head" "$target_c"; then
    echo "$target не впереди нынешнего кода ($head_name): это откат или другая ветка. Обновление идёт только вперёд; вернуться назад — ./ops/restore.sh --rollback (по записи последнего обновления) или восстановлением копии." >&2
    exit 2
  fi
  if [ -n "$target_ver" ]; then
    local tv
    tv="$(git show "$target_c:VERSION" 2> /dev/null | tr -d '[:space:]')"
    if [ "$tv" != "$target_ver" ]; then
      echo "Тег $target указывает на код с VERSION=$tv, а должен — $target_ver. Выпуск собран неверно: стоп, скажите разработчику (docs/releases.md)." >&2
      exit 2
    fi
  fi
  between="$(releases_between "$target_c" | paste -sd' ' -)"
  if [ -n "$between" ]; then
    echo "Между нынешним кодом и $target есть выпуски: $between. Обновление идёт по одному выпуску за раз — сначала ./ops/update.sh без --to (или --to с первым из них)." >&2
    exit 2
  fi

  # --- план ---
  local mig_new hb ha hv def vfile hermes_ok=yes section stops=no fresh="" scope_possible=code
  mig_new="$(list_minus "$(repo_migrations "$target_c")" "$(repo_migrations "$head")")"
  hb="-"; ha="-"
  if [ "$MODE" = hermes ]; then
    hb="$(hermes_image_running)"
    hv="$(env_get HERMES_VERSION)"
    def="$(git show "$target_c:compose.yaml" 2> /dev/null | sed -n 's/.*hermes-agent:\${HERMES_VERSION:-\([^}]*\)}.*/\1/p' | head -n 1)"
    ha="nousresearch/hermes-agent:${hv:-$def}"
    vfile="$UPDATE_DIR/hermes-versions.txt"
    git show "$target_c:$HERMES_VERSIONS_FILE" > "$vfile" 2> /dev/null || cp "$HERMES_VERSIONS_FILE" "$vfile" 2> /dev/null || : > "$vfile"
    hermes_verified "$ha" "$vfile" || hermes_ok=no
  fi
  section="$(upgrading_section "$target_c" "$target_ver")"
  if printf '%s\n' "$section" | grep -q 'Стоп-точка'; then stops=yes; fi
  if [ -n "$mig_new" ] || { [ "$hb" != "$ha" ] && [ "$hb" != "-" ]; }; then scope_possible=data; fi

  echo
  echo "Сейчас:   $head_name"
  echo "Цель:     $target (${target_c:0:12})${latest:+$( [ "$latest" != "$target" ] && printf ' — следующий выпуск; последний — %s, до него ещё запуски' "$latest")}"
  if [ -n "$mig_new" ]; then echo "Миграции базы придут: $(list_count "$mig_new") ($mig_new) — применит ./ops/up.sh"; else echo "Миграций базы нет."; fi
  if [ "$MODE" = hermes ]; then
    if [ "$hb" = "$ha" ]; then echo "Hermes:   тот же образ $ha"; else echo "Hermes:   образ сменится: $hb → $ha"; fi
    if [ "$hermes_ok" = yes ]; then echo "          $ha — в списке проверенных ($HERMES_VERSIONS_FILE)"; else echo "          $ha НЕТ в списке проверенных: совместимость с плагином не проверена; откат Hermes — только восстановлением data/hermes из копии"; fi
  fi
  local run_before
  run_before="$(service_commit_running)"
  if [ -n "$run_before" ] && [ "$run_before" != none ] && ! same_commit "$run_before" "$head"; then
    echo "Внимание: сервис переписки работает с кодом ${run_before:0:7}, а в каталоге — ${head:0:7}: прошлое обновление, похоже, не установилось (./ops/doctor.sh, строка updates). Откат при провале вернёт код каталога ($head_name)."
  fi
  if [ -n "$section" ]; then
    echo "UPGRADING.md: «$(printf '%s\n' "$section" | head -n 1 | sed 's/^## //')» — прочитать и пересказать владельцу до обновления (git show $target:UPGRADING.md)"
    [ "$stops" = yes ] && echo "          в разделе есть «Стоп-точка» — шаги вне этого скрипта; запуск — с --upgrading-read после пересказа владельцу"
  else
    echo "UPGRADING.md: раздела для этой версии нет — значит, действий сверх ./ops/up.sh не нужно (так быть должно только у выпусков без изменений для владельца)"
  fi
  if fresh="$(bk_fresh "$(bk_fresh_minutes)")"; then echo "Копия:    свежая есть — $fresh; перед обновлением всё равно будет снята своя, если код или база с тех пор менялись"; else echo "Копия:    будет снята перед обновлением (./ops/backup.sh --reason before-update)"; fi
  if [ "$scope_possible" = data ]; then
    echo "Откат при провале: код, база и data/ из этой копии (применятся миграции или сменится Hermes) — перезапись данных, нужно разрешение владельца в боте согласований"
  else
    echo "Откат при провале: только код (миграций нет, Hermes тот же) — данные не перезаписываются, разрешение не нужно"
  fi

  # --- разрешение владельца (Р-100): кнопка в боте согласований ---
  local consent=no need_consent=no need_hermes=no c_target c_exp c_hermes again rc_c=0
  local -a details
  [ "$scope_possible" = data ] && need_consent=yes
  # Непроверенная версия Hermes — разрешение владельца, даже если она уже работает: новый код
  # плагина с ней тоже не проверен.
  if [ "$MODE" = hermes ] && [ "$hermes_ok" = no ]; then need_hermes=yes; fi
  # Разрешение, записанное запасным путём (фраза в терминале) или полученное без сервиса заранее.
  if [ -f "$CONSENT_FILE" ]; then
    c_target="$(meta_get "$CONSENT_FILE" target_commit)"; c_exp="$(meta_get "$CONSENT_FILE" expires)"; c_hermes="$(meta_get "$CONSENT_FILE" hermes)"
    if [ "$c_target" = "$target_c" ] && [ "${c_exp:-0}" -ge "$(date +%s)" ] && { [ "$need_hermes" = no ] || [ "$c_hermes" = "$ha" ]; }; then
      consent="file"
    fi
  fi
  again="./ops/update.sh${to:+ --to $to}"
  case "$channel" in pilot|main) again="$again --channel $channel" ;; esac
  details=("from=$head_name" "to=$target" "scope=$scope_possible")
  [ -n "$mig_new" ] && details+=("migrations=$(list_count "$mig_new") ($mig_new)")
  [ "$need_hermes" = yes ] && details+=("hermes=$ha")

  if [ "$action" = check ]; then
    echo
    if [ "$need_consent" = yes ] || [ "$need_hermes" = yes ]; then
      if [ "$consent" = file ]; then echo "Разрешение на этот выпуск уже записано (до $(date -d "@$c_exp" '+%H:%M')) — ./ops/update.sh можно запускать."
      else
        local bot_state="unknown"
        if [ "$(container_state shturman-service)" = running ]; then
          bot_state="$(docker exec shturman-service shturman ops-consent status --action update --target "$target_c" 2> /dev/null || echo unknown)"
        fi
        case "$bot_state" in
          granted*) echo "Разрешение владельца в боте согласований есть (годно до ${bot_state#granted }) — ./ops/update.sh можно запускать." ;;
          pending*) echo "Запрос разрешения уже в боте согласований и ждёт нажатия владельца (до ${bot_state#pending }). Нажал — запускайте $again." ;;
          *)
            echo "Нужно разрешение владельца$( [ "$need_hermes" = yes ] && printf ' (в том числе на непроверенную версию Hermes)'): $again сам пришлёт ему"
            echo "  запрос в бот согласований и подождёт нажатия «Разрешить»; заранее — ${again/update.sh/update.sh --consent}."
            [ "$need_hermes" = no ] && echo "  или обновление без автоматического отката базы: ./ops/update.sh --no-auto-rollback (при провале — остановка и откат: ./ops/restore.sh --rollback)" ;;
        esac
      fi
    fi
    echo "Это план (--check): ничего не изменено."
    exit 0
  fi

  if [ "$action" = consent ] || { { [ "$need_consent" = yes ] || [ "$need_hermes" = yes ]; } && [ "$consent" = no ] && [ "$no_rb" = no ]; } \
     || { [ "$need_hermes" = yes ] && [ "$consent" = no ]; }; then
    if [ "$action" = consent ] && [ "$need_consent" = no ] && [ "$need_hermes" = no ]; then
      echo "Разрешение не нужно: откат при провале — только код, данные не перезаписываются. Запускайте $again."
      exit 0
    fi
    echo
    rc_c=0
    if [ "$action" = consent ]; then OWNER_CONSENT_KEEP=1 owner_consent update "$target_c" "${details[@]}" || rc_c=$?
    else owner_consent update "$target_c" "${details[@]}" || rc_c=$?; fi
    if [ "$rc_c" -eq 1 ]; then
      echo "Разрешения владельца нет — ничего не изменено. Нажмёт «Разрешить» — повторите: $again" >&2
      [ "$need_hermes" = no ] && echo "Другой путь — с согласия владельца в разговоре: ./ops/update.sh --no-auto-rollback (при провале — остановка и откат: ./ops/restore.sh --rollback)." >&2
      exit "$OPS_NEED_HUMAN"
    fi
    if [ "$rc_c" -eq 2 ]; then
      echo "Подтвердить в боте согласований сейчас нельзя (причина — выше). Запасной путь — человек в своём терминале; он слабее кнопки в боте." >&2
      if has_tty; then
        {
          echo "Что вы разрешаете:"
          [ "$need_consent" = yes ] && echo "  * если после обновления проверка не пройдёт — вернуть прежнюю версию ВМЕСТЕ С БАЗОЙ и data/ из копии, которую скрипт снимет прямо перед обновлением; всё, что придёт в архив за время обновления (обычно минуты), может пропасть;"
          [ "$need_hermes" = yes ] && echo "  * запуск Hermes $ha, которого нет в списке проверенных версий: плагин с ним не проверен; откат — только восстановлением копии data/hermes;"
        } > /dev/tty
        if ! human_confirm "РАЗРЕШАЮ"; then echo "Не подтверждено — ничего не изменено." >&2; exit "$OPS_NEED_HUMAN"; fi
        OWNER_CONSENT_VIA="tty"
      else
        need_human "${again/update.sh/update.sh --consent}"
        [ "$need_hermes" = no ] && echo "Другой путь — с согласия владельца в разговоре: ./ops/update.sh --no-auto-rollback." >&2
        exit "$OPS_NEED_HUMAN"
      fi
    fi
    if [ "$action" = consent ]; then
      if [ "$OWNER_CONSENT_VIA" != bot ]; then
        # Без сервиса (или фразой в терминале) разрешение хранится здесь: сервису его не передать.
        mkdir -p local/consent; chmod 700 local/consent
        { echo "target_commit=$target_c"; echo "target_name=$target"; echo "hermes=$ha"; echo "via=$OWNER_CONSENT_VIA"; echo "created=$(date +%s)"; echo "expires=$(( $(date +%s) + 7200 ))"; } > "$CONSENT_FILE"
      fi
      ./ops/state.sh log "обновление до $target: владелец разрешил автоматический откат ($OWNER_CONSENT_VIA, на 2 часа)" > /dev/null 2>&1 || true
      echo "Разрешение получено: обновление до $target, один раз, 2 часа. Теперь агент запускает $again."
      exit 0
    fi
    consent="$OWNER_CONSENT_VIA"
    ./ops/state.sh log "обновление до $target: владелец разрешил автоматический откат ($consent)" > /dev/null 2>&1 || true
  fi
  if [ "$stops" = yes ] && [ "$read_up" = no ]; then
    echo "В разделе UPGRADING.md этой версии есть «Стоп-точка». Прочитайте раздел (git show $target:UPGRADING.md), перескажите владельцу, получите его «да» и запустите с --upgrading-read." >&2
    exit 2
  fi
  [ "$consent" = file ] && rm -f "$CONSENT_FILE"

  # --- проверка до ---
  local run log fails_before fails_after new_fails rc_up=0 try stamp mig_before
  run="$(date -u +%Y%m%dT%H%M%SZ)"; log="$UPDATE_DIR/$run.log"
  : > "$log"; chmod 600 "$log"
  say() { printf '%s\n' "$*" | tee -a "$log"; }
  say "Обновление $head_name → $target: начато $(now_utc)"
  ./ops/state.sh log "обновление до $target: начато" > /dev/null 2>&1 || true
  say "Проверка до обновления: ./ops/doctor.sh"
  ./ops/doctor.sh > "$UPDATE_DIR/$run.doctor-before.txt" 2>&1 || true
  fails_before="$(awk '$1 == "FAIL" { sub(/:$/, "", $2); print $2 }' "$UPDATE_DIR/$run.doctor-before.txt" | sort -u | paste -sd' ' -)"
  if [ -n "$fails_before" ]; then
    say "До обновления уже FAIL: $fails_before"
    if [ "$despite" = no ]; then
      say "Обновление поверх неисправного — только с согласия владельца: ./ops/update.sh --despite-fail (успех — если новых FAIL не будет). Ничего не изменено."
      exit 3
    fi
  else
    say "До обновления FAIL нет."
  fi

  # --- копия ---
  mig_before="?"; if pg_running; then mig_before="$(db_migrations shturman 2> /dev/null || echo "?")"; fi
  stamp=""
  if fresh="$(bk_fresh "$(bk_fresh_minutes)")" && [ "$(meta_get "$BK_LOCAL/meta-$fresh.txt" commit)" = "$head" ] \
     && [ "$(meta_get "$BK_LOCAL/meta-$fresh.txt" migrations)" = "$mig_before" ] && [ "$mig_before" != "?" ] \
     && bk_verify "$BK_LOCAL" "$fresh" > "$UPDATE_DIR/$run.verify.txt" 2>&1; then
    stamp="$fresh"
    say "Копия: беру свежую $stamp — тот же код и та же база, файлы на месте и совпали по контрольным суммам."
  else
    if [ -n "$fresh" ] && [ -s "$UPDATE_DIR/$run.verify.txt" ] && grep -Eq 'НЕ|НЕТ|нет ни|не доделана' "$UPDATE_DIR/$run.verify.txt"; then
      say "Свежая копия $fresh не прошла проверку ($(grep -E 'НЕ|НЕТ|нет ни|не доделана' "$UPDATE_DIR/$run.verify.txt" | sed 's/^ *//' | paste -sd';' -)) — снимаю новую."
    fi
    say "Копия: ./ops/backup.sh --reason before-update"
    ./ops/backup.sh --reason before-update 2>&1 | tee -a "$log"
    if [ "${PIPESTATUS[0]}" -ne 0 ]; then
      say "Копия не сделана или не положена во внешний каталог — обновление не начато. Разбор — по сообщению выше."
      exit 3
    fi
    stamp="$(bk_stamps "$BK_LOCAL" | head -n 1)"
  fi

  # --- обновление ---
  rm -f "$UPDATE_LAST"
  last_set run "$run"; last_set started "$(now_utc)"
  last_set from_commit "$head"; last_set from_name "$head_name"
  last_set to_commit "$target_c"; last_set to_name "$target"
  last_set backup "$stamp"; last_set migrations_before "$mig_before"
  last_set hermes_before "$hb"; last_set consent "$consent"; last_set result running
  # Для проверки «новая версия запущена»: изменился ли код плагина и когда переключён код (Hermes, запущенный
  # позже, читает уже новые файлы плагина — каталог подключён в контейнер как есть).
  local plugin_new=no switched_at
  if plugin_changed "$head" "$target_c"; then plugin_new=yes; fi
  switched_at="$(date +%s)"
  say "Переключаю код на $target…"
  git checkout -q --detach "$target_c"
  say "./ops/up.sh (сборка, миграции, перезапуск) — до 15 минут"
  ./ops/up.sh 2>&1 | tee -a "$log"
  rc_up="${PIPESTATUS[0]}"
  # Установилась ли новая версия на самом деле (Д-58): код ./ops/up.sh сам по себе не ответ (он выходит
  # с кодом doctor, а с --despite-fail FAIL бывали и до обновления, и сдаётся через 90 с ожидания, пока сервис
  # ещё применяет миграции), а doctor о работающем прежнем коде только предупреждает. Ответ — install_state:
  #   yes — сервис переписки называет коммит цели (GET /api/status, как строка updates в doctor), в режиме
  #         с Hermes его образ — задуманный, а если код плагина изменился — Hermes запущен после переключения кода;
  #   no<TAB>почему<TAB>old|other — точно не установлена: old — сервис отвечает прежним коммитом (сборка или
  #         запуск не прошли до перезапуска, прежние контейнеры работают), other — Hermes прежний;
  #   wait<TAB>почему — ещё не ясно: сервис не отвечает (миграции при первом запуске могут идти минуты).
  # Ждём ясности до OPS_UPDATE_WAIT секунд (по умолчанию 600), спрашивая раз в OPS_UPDATE_POLL (15).
  install_state() {
    local c hp hr hs cs
    c="$(service_commit_running)"
    if [ -z "$c" ]; then
      cs="$(container_state shturman-service)"
      case "$cs" in
        running|restarting|created) printf 'wait\tсервис переписки не отвечает\n' ;;
        *) printf 'no\tконтейнер сервиса переписки не работает (%s)\tother\n' "$cs" ;;
      esac
      return 0
    fi
    if [ "$c" != none ] && ! same_commit "$c" "$target_c"; then
      printf 'no\tсервис переписки работает с прежним кодом %s, а не с кодом цели %s\told\n' "${c:0:7}" "${target_c:0:7}"; return 0
    fi
    if [ "$MODE" = hermes ]; then
      hp="$(hermes_image_planned)"; hr="$(hermes_image_running)"
      if [ "$hp" != "?" ] && [ "$hr" != "$hp" ]; then
        printf 'no\tHermes работает с образом %s, а задуман %s\tother\n' "$hr" "$hp"; return 0
      fi
      if [ "$plugin_new" = yes ]; then
        hs="$(date -d "$(docker inspect -f '{{.State.StartedAt}}' shturman-hermes 2> /dev/null)" +%s 2> /dev/null || true)"
        if [ -n "$hs" ] && [ "$hs" -lt "$switched_at" ]; then
          printf 'no\tHermes не перезапускался после смены кода и исполняет прежний код плагина\tother\n'; return 0
        fi
      fi
    fi
    if [ "$c" = none ]; then printf 'yes\tnone\n'; else echo yes; fi
  }
  local installed=yes why="" st not_started=no no_answer=no t0 wait_max="${OPS_UPDATE_WAIT:-600}"
  t0="$(date +%s)"
  while :; do
    st="$(install_state)"
    case "$st" in yes*|no*) break ;; esac
    [ $(( $(date +%s) - t0 )) -lt "$wait_max" ] || break
    say "Жду новую версию: $(printf '%s' "$st" | cut -f2) — прошло $(( $(date +%s) - t0 )) с из $wait_max (первый запуск с миграциями базы может идти минуты)"
    sleep "${OPS_UPDATE_POLL:-15}"
  done
  case "$st" in
    yes*) installed=yes
      [ "$st" = yes ] || say "Сервис переписки не назвал, с каким кодом работает (updates в /api/status), — сверка коммита пропущена, решает doctor." ;;
    no*) installed=no; why="$(printf '%s' "$st" | cut -f2)"; [ "$(printf '%s' "$st" | cut -f3)" = old ] && not_started=yes ;;
    *) installed=no; no_answer=yes; why="$(printf '%s' "$st" | cut -f2) и за $(( (wait_max + 59) / 60 )) мин так и не ответил" ;;
  esac
  last_set running_commit_after "$(service_commit_running | grep . || echo -)"
  for try in 1 2 3; do
    ./ops/doctor.sh > "$UPDATE_DIR/$run.doctor-after.txt" 2>&1 || true
    fails_after="$(awk '$1 == "FAIL" { sub(/:$/, "", $2); print $2 }' "$UPDATE_DIR/$run.doctor-after.txt" | sort -u | paste -sd' ' -)"
    new_fails="$(list_minus "$(printf '%s' "$fails_after" | tr ' ' ',')" "$(printf '%s' "$fails_before" | tr ' ' ',')")"
    # Новая версия не запущена — ждать нечего: повтор doctor её не запустит.
    [ "$installed" = yes ] || break
    if [ -z "$new_fails" ] && grep -q '^ИТОГ:' "$UPDATE_DIR/$run.doctor-after.txt"; then break; fi
    [ "$try" -lt 3 ] && { say "Новые FAIL после обновления: ${new_fails:-нет итога doctor} — проверю ещё раз через ${OPS_UPDATE_RETRY_PAUSE:-30} секунд (попытка $try из 3)"; sleep "${OPS_UPDATE_RETRY_PAUSE:-30}"; }
  done
  grep -E '^FAIL' "$UPDATE_DIR/$run.doctor-after.txt" | tee -a "$log" || true
  if [ "$installed" = yes ] && [ -z "$new_fails" ] && grep -q '^ИТОГ:' "$UPDATE_DIR/$run.doctor-after.txt"; then
    last_set result ok; last_set finished "$(now_utc)"
    ./ops/state.sh "done" "update-$target" > /dev/null 2>&1 || true
    ./ops/state.sh log "обновление до $target: ИТОГ OK" > /dev/null 2>&1 || true
    [ "$rc_up" -eq 0 ] || say "Внимание: ./ops/up.sh завершился с кодом $rc_up, но новая версия запущена и новых FAIL в doctor нет — посмотрите строки выше."
    say ""
    say "ИТОГ ОБНОВЛЕНИЯ: OK — $head_name → $target. Копия до обновления: $stamp. Журнал: $log"
    tell_service ok
    if [ -n "${latest:-}" ] && [ "$latest" != "$target" ]; then say "Есть ещё выпуск(и) до $latest — после проверки владельцем запустите ./ops/update.sh снова."; fi
    exit 0
  fi

  # --- провал: откат ---
  local mig_after ha_after scope rc_rb=0 fails_tell svc_stopped=no
  # Когда останавливать сервис перед решением об откате. Миграции он применяет при запуске, до того как начнёт
  # отвечать, — каждую своей транзакцией. Ответил в этом запуске (коммитом цели или прежним) — миграции этого
  # запуска закончены, схема окончательная: не трогаем, даже если doctor нашёл новые FAIL. Так и не ответил за
  # всё ожидание — он, может быть, ещё мигрирует (или перезапускается и мигрирует снова): тогда, если он и сейчас
  # молчит (три попытки), останавливаем его — и только потом смотрим, какие миграции легли. Иначе откат «только
  # кода» мог бы оставить прежний код на схеме, которую поздняя миграция догонит уже после решения. Остановка
  # прерывает только незаконченную миграцию. Перезапускающийся контейнер (restarting) — тоже.
  if [ "$no_answer" = yes ]; then
    local again=0 answered=no
    while [ "$again" -lt 3 ]; do
      if [ -n "$(service_commit_running)" ]; then answered=yes; break; fi
      again=$((again + 1)); [ "$again" -lt 3 ] && sleep "$(( ${OPS_UPDATE_POLL:-15} < 5 ? ${OPS_UPDATE_POLL:-15} : 5 ))"
    done
    case "$answered:$(container_state shturman-service)" in
      no:running|no:restarting)
        say "Сервис переписки не отвечает — останавливаю его, чтобы незаконченная миграция не легла после решения об откате."
        docker stop shturman-service > /dev/null 2>&1 || true
        svc_stopped=yes ;;
      yes:*) say "Сервис переписки всё же ответил — миграции этого запуска закончены, сервис не останавливаю." ;;
    esac
  fi
  mig_after="?"; if pg_running; then mig_after="$(db_migrations shturman 2> /dev/null || echo "?")"; fi
  # Образ Hermes, который на самом деле работает: сборка упала до перезапуска — Hermes прежний, и данные
  # data/hermes новой версией не тронуты (откат только кода, без разрешения на перезапись данных).
  ha_after="$( [ "$MODE" = hermes ] && hermes_image_running || echo -)"
  scope="$(rollback_scope "$mig_before" "$mig_after" "$hb" "$ha_after")"
  last_set scope "$scope"; last_set migrations_after "$mig_after"
  say ""
  fails_tell="$new_fails"
  if [ "$installed" = no ]; then
    if [ "$not_started" = yes ]; then
      say "Новая версия $target не запустилась: $why — сборка или запуск не прошли до перезапуска (./ops/up.sh: код $rc_up; причина — строки ./ops/up.sh выше)."
    elif [ "$no_answer" = yes ]; then
      say "Новая версия $target не заработала: $why (./ops/up.sh: код $rc_up; причина — строки ./ops/up.sh выше и docker logs --tail 50 shturman-service)."
    else
      say "Новая версия $target запущена не вся: $why (./ops/up.sh: код $rc_up)."
    fi
    # Сервису прежней версии новый вид итога неизвестен: «rolled-back» с меткой up-sh (сервис с Д-58 пишет по
    # ней «не установлено, данные не тронуты»), а если данные возвращались из копии — меткой running-code.
    if [ "$scope" = code ]; then fails_tell="up-sh"; else fails_tell="${new_fails:+$new_fails }running-code"; fi
  else
    say "Обновление не прошло проверку: новые FAIL — ${new_fails:-doctor не дал итога} (./ops/up.sh: код $rc_up)."
  fi
  if [ "$scope" = data ] && [ "$consent" = no ]; then
    last_set result failed-needs-human
    ./ops/state.sh log "обновление до $target: провал, откат ждёт человека" > /dev/null 2>&1 || true
    say "Миграции применились (или сменился образ работающего Hermes), а разрешения на автоматический откат базы нет — ничего не откатываю."
    say "Экземпляр сейчас на $target с ошибками. Откат — ./ops/restore.sh --rollback: он пришлёт владельцу запрос в бот"
    say "согласований (если сервис лежит — напрямую от имени бота) и продолжит после нажатия «Разрешить»."
    if [ "$svc_stopped" = yes ]; then
      # Решение принято — сервис возвращается в то состояние, в каком был до остановки: без него сбор переписки
      # и бот согласований молчали бы до отката, а итог владельцу никто бы не отправил. Новые миграции, если он
      # их ещё догонит, решения не меняют: откат и так — с базой.
      docker start shturman-service > /dev/null 2>&1 || true
      say "Сервис переписки снова запущен (он не отвечал — смотрите docker logs --tail 50 shturman-service): итог владельцу он отправит, когда заработает. Пока он молчит, скажите владельцу сами: обновление до $target прошло с ошибками, нужен откат с его разрешением."
    fi
    tell_service failed-needs-human "$fails_tell"
    say "ИТОГ ОБНОВЛЕНИЯ: FAIL — нужен человек. Журнал: $log"
    exit 3
  fi
  if [ "$scope" = data ]; then
    say "Откат: код, база и data/ из копии $stamp (миграции применились или сменился образ работающего Hermes). Сначала — копия нынешнего состояния для разбора."
    ./ops/backup.sh --reason before-restore 2>&1 | tee -a "$log"
    [ "${PIPESTATUS[0]}" -eq 0 ] || say "Копию нынешнего (неудачного) состояния сделать не удалось — продолжаю откат: разрешение дано."
  elif [ "$not_started" = yes ]; then
    say "Откат: только код — новая версия не запускалась (прежние контейнеры работали всё время), миграции не применялись, Hermes прежний; данные не тронуты."
  elif [ "$installed" = no ]; then
    say "Откат: только код — миграции не применились, образ работающего Hermes прежний; данные не меняются."
  else
    say "Откат: только код — миграции не применялись, Hermes тот же."
  fi
  rollback_run "$scope" 2>&1 | tee -a "$log"
  rc_rb="${PIPESTATUS[0]}"
  if [ "$rc_rb" -eq 0 ]; then
    if [ "$installed" = no ] && [ "$scope" = code ]; then
      last_set result not-installed
      ./ops/state.sh log "обновление до $target: не установлено (сборка или запуск не прошли), код возвращён на ${head:0:7}" > /dev/null 2>&1 || true
      if [ "$not_started" = yes ]; then
        say "ИТОГ ОБНОВЛЕНИЯ: НЕ УСТАНОВЛЕНО — $target не установлен (сборка или запуск не прошли до перезапуска); работает прежняя версия $head_name, данные не тронуты, ./ops/doctor.sh без FAIL. Причина — строки ./ops/up.sh выше и $log; строка дефекта — владельцу и в отчёт сессии."
      else
        say "ИТОГ ОБНОВЛЕНИЯ: НЕ УСТАНОВЛЕНО — $target не заработал ($why); код возвращён, работает прежняя версия $head_name, миграции не применились — данные не тронуты, ./ops/doctor.sh без FAIL. Причина — $log; строка дефекта — владельцу и в отчёт сессии."
      fi
    else
      last_set result rolled-back
      ./ops/state.sh log "обновление до $target: провал, откат на ${head:0:7} выполнен" > /dev/null 2>&1 || true
      say "ИТОГ ОБНОВЛЕНИЯ: ОТКАТ — экземпляр снова на $head_name$( [ "$scope" = data ] && printf ', база и data/ — из копии %s' "$stamp"), ./ops/doctor.sh без FAIL. Причина провала — строки FAIL выше и $UPDATE_DIR/$run.doctor-after.txt; строка дефекта — владельцу и в отчёт сессии."
    fi
    tell_service rolled-back "$fails_tell"
    exit 1
  fi
  last_set result rollback-failed
  ./ops/state.sh log "обновление до $target: провал, откат не удался" > /dev/null 2>&1 || true
  say "ИТОГ ОБНОВЛЕНИЯ: FAIL — откат не удался (код $rc_rb). Стоп: строка дефекта и владельцу; копия до обновления — $stamp (цела ли — строки проверки выше). Журнал: $log"
  tell_service rollback-failed "$fails_tell"
  exit 3
}

main "$@"; exit $?
