#!/usr/bin/env bash
# Общие функции скриптов ops/. Сам по себе не запускается: подключается строкой `. ops/lib.sh`
# после перехода в корень репозитория.
#
# Режим установки (SHTURMAN_MODE в .env):
#   hermes      — стоковый Hermes + сервис переписки + база (по умолчанию, как раньше);
#   standalone  — только сервис переписки и база: архив, свой бот согласований, своя модель
#                 (docs/standalone.md). Контейнер Hermes не создаётся и не скачивается.
#
# Сами функции читают из .env только две несекретные строки: SHTURMAN_MODE и COMPOSE_PROFILES.
# env_get и env_set работают со строкой, которую назвал вызывающий скрипт; скрипты ops/ называют
# ими только несекретные строки. Значения секретов эти функции не читают и не печатают.
#
# Справка и неизвестные ключи — одинаково у всех скриптов ops/:
#   ops_help "$@"     — если среди параметров есть -h или --help, печатает шапку скрипта и выходит
#                       с кодом 0. Вызывается ДО любых действий: справка ничего не создаёт,
#                       не читает .env и не обращается к Docker;
#   ops_no_args "$@"  — для скриптов без параметров: любой параметр — ошибка с кодом 2;
#   ops_unknown X     — сообщение о неизвестном параметре и код 2.

# Каталог ops/ полным путём: скрипты меняют текущий каталог, а справка читает шапку из файла.
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Шапка вызывающего скрипта: строки-комментарии от второй до первой строки кода.
ops_usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$OPS_DIR/$(basename "$0")"
}

ops_help() {
  local a
  for a in "$@"; do
    case "$a" in
      -h|--help) ops_usage; exit 0 ;;
      --) return 0 ;;
    esac
  done
}

ops_unknown() {
  echo "неизвестный параметр: $1 — справка: ./ops/$(basename "$0") --help" >&2
  exit 2
}

ops_no_args() {
  if [ $# -gt 0 ]; then ops_unknown "$1"; fi
}

# Запущен сам файл, а не подключён: показать справку и ничего не делать.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in
    -h|--help|"") ops_usage; echo; echo "Это библиотека: сама по себе она ничего не делает."; exit 0 ;;
    *) ops_unknown "$1" ;;
  esac
fi

# Значение несекретной строки .env (пусто, если её нет).
env_get() {
  [ -f .env ] || return 0
  grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true
}

# Записать строку в .env, не трогая и не печатая остальные.
env_put() {
  local tmp
  tmp="$(mktemp .env.XXXXXX)"
  grep -Ev "^$1=" .env > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  chmod 600 "$tmp"; mv "$tmp" .env
}

# Убрать строку из .env, не трогая и не печатая остальные.
env_del() {
  local tmp
  tmp="$(mktemp .env.XXXXXX)"
  grep -Ev "^$1=" .env > "$tmp" || true
  chmod 600 "$tmp"; mv "$tmp" .env
}

# Записать строку, а при пустом значении — убрать её.
env_set() {
  if [ -n "$2" ]; then env_put "$1" "$2"; else env_del "$1"; fi
}

# Включён ли профиль Compose в .env.
profile_on() {
  case ",$(env_get COMPOSE_PROFILES)," in *,"$1",*) return 0 ;; *) return 1 ;; esac
}

# Добавить (on) или убрать (off) один профиль Compose в .env.
#   $1 — on или off; $2 — имя профиля (embeddings, guard).
# Профили включаются независимо друг от друга, поэтому меняется только названный: остальные,
# включая hermes, остаются как были.
profile_set() {
  local want="$1" name="$2" cur out="" p
  cur="$(env_get COMPOSE_PROFILES)"
  local IFS=,
  # shellcheck disable=SC2086
  for p in $cur; do
    if [ -n "$p" ] && [ "$p" != "$name" ]; then out="${out:+$out,}$p"; fi
  done
  if [ "$want" = on ]; then out="${out:+$out,}$name"; fi
  env_set COMPOSE_PROFILES "$out"
}

# Состояние контейнера (running, exited…); контейнера нет — значение по умолчанию ($2, иначе missing).
# Отдельная функция, потому что docker inspect для отсутствующего контейнера печатает пустую
# строку и завершается с ошибкой: привычное `$(docker inspect … || echo missing)` даёт две строки.
container_state() {
  local s
  s="$(docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null)" || s=""
  printf '%s\n' "${s:-${2:-missing}}"
}

valid_mode() { case "$1" in hermes|standalone) return 0 ;; *) return 1 ;; esac; }

# Печатает режим установки. Строки нет — режим прежний, hermes.
shturman_mode() {
  local m
  m="$(env_get SHTURMAN_MODE)"
  m="${m:-hermes}"
  if ! valid_mode "$m"; then
    echo "в .env неизвестный режим SHTURMAN_MODE — допустимы hermes и standalone (./ops/mode.sh)" >&2
    return 1
  fi
  printf '%s\n' "$m"
}

# Профили Compose для режима: hermes — контейнер Hermes, embeddings — поиск по смыслу.
#   $1 — режим; $2 — эмбеддинги: on, off или keep (как записано в .env сейчас).
# Прочие профили, если владелец добавил свои, сохраняются.
compose_profiles() {
  local mode="$1" emb="${2:-keep}" cur out="" p
  cur="$(env_get COMPOSE_PROFILES)"
  if [ "$emb" = keep ]; then
    case ",$cur," in *,embeddings,*) emb=on ;; *) emb=off ;; esac
  fi
  [ "$mode" = hermes ] && out="hermes"
  [ "$emb" = on ] && out="${out:+$out,}embeddings"
  local IFS=,
  # shellcheck disable=SC2086
  for p in $cur; do
    case "$p" in ""|hermes|embeddings) ;; *) out="${out:+$out,}$p" ;; esac
  done
  printf '%s\n' "$out"
}

# Определяет режим и выставляет профили Compose для всех вызовов docker compose в скрипте.
# После вызова: MODE — режим, COMPOSE_PROFILES — в окружении.
load_mode() {
  MODE="$(shturman_mode)" || exit 2
  COMPOSE_PROFILES="$(compose_profiles "$MODE" keep)"
  export COMPOSE_PROFILES
}

# Для скриптов, которым без Hermes делать нечего (ссылка входа в дашборд и т.п.).
require_hermes() {
  load_mode
  if [ "$MODE" != hermes ]; then
    echo "Режим без Hermes (SHTURMAN_MODE=standalone): $1 в этом режиме нет — команда неприменима." >&2
    echo "Что есть в этом режиме — docs/standalone.md." >&2
    exit 2
  fi
}
