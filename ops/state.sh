#!/usr/bin/env bash
# Состояние развёртывания: чтобы новая сессия агента продолжила с нужного места.
# Хранится в local/state (вне git). Секретов сюда не писать.
#   ./ops/state.sh show            — показать всё
#   ./ops/state.sh get KEY         — значение ключа (пусто, если нет)
#   ./ops/state.sh set KEY VALUE   — записать ключ
#   ./ops/state.sh done STEP       — отметить шаг выполненным (с отметкой времени)
#   ./ops/state.sh log "текст"     — дописать строку в журнал
set -eu
cd "$(dirname "$0")/.." || exit 1
dir="local"; file="$dir/state"; journal="$dir/journal.log"
mkdir -p "$dir"; touch "$file" "$journal"

valid_key() { printf '%s' "$1" | grep -Eq '^[A-Za-z0-9_.-]+$'; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

cmd="${1:-show}"
case "$cmd" in
  show)
    echo "# состояние ($file)"; if [ -s "$file" ]; then sort "$file"; else echo "(пусто — развёртывание не начато)"; fi
    echo; echo "# последние записи журнала"; tail -n 10 "$journal" ;;
  get)
    valid_key "${2:-}" || { echo "нужен ключ" >&2; exit 2; }
    grep -E "^$2=" "$file" | tail -n 1 | cut -d= -f2- || true ;;
  set)
    valid_key "${2:-}" || { echo "нужен ключ из букв, цифр, _ . -" >&2; exit 2; }
    [ $# -ge 3 ] || { echo "нужно значение" >&2; exit 2; }
    case "$3" in *$'\n'*) echo "значение должно быть одной строкой" >&2; exit 2 ;; esac
    tmp="$(mktemp "$dir/state.XXXXXX")"
    grep -Ev "^$2=" "$file" > "$tmp" || true
    printf '%s=%s\n' "$2" "$3" >> "$tmp"
    mv "$tmp" "$file"
    printf '%s set %s\n' "$(now)" "$2" >> "$journal" ;;
  done)
    valid_key "${2:-}" || { echo "нужно имя шага" >&2; exit 2; }
    "$0" set "step.$2" "done@$(now)" ;;
  log)
    [ $# -ge 2 ] || { echo "нужен текст" >&2; exit 2; }
    printf '%s %s\n' "$(now)" "$2" >> "$journal" ;;
  *) echo "неизвестная команда: $cmd" >&2; exit 2 ;;
esac
