#!/usr/bin/env bash
# Договорённости (Р-96, docs/memory.md, «Договорённости»): числа и перепроверка по новым правилам.
#   ./ops/commitments.sh stats [--json]
#       только агрегаты, без текста переписки: сколько предложено, принято, отклонено, снято без владельца
#       и почему, причины «Не так», доля «между другими» и быстрых обещаний, часы от обещания до показа.
#   ./ops/commitments.sh recheck --dry-run [--limit N] [--price-in USD --price-out USD]
#       оценка перепроверки: сколько запросов к модели и примерный объём в токенах (с ценами — в долларах).
#       Ничего не ставит.
#   ./ops/commitments.sh recheck [--limit N] [--force]
#       перепроверка по решению владельца: ещё не решённые предложения прежних правил разбираются заново
#       (сделанное до показа снимается тихо, «между другими» уходит в «В команде»), принятые проверяются
#       «не сделано ли уже» с момента обещания — найденное приходит владельцу карточкой «похоже, уже
#       сделано». Отправляет переписку провайдеру модели и тратит запросы: не больше --limit (300), один
#       раз на версию правил (--force — ещё раз).
# -h, --help — эта справка.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

action="${1:-}"
case "$action" in
  stats|recheck) shift ;;
  *) ops_unknown "${action:-без действия}" ;;
esac
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --json|--dry-run|--force) args+=("$1") ;;
    --limit|--price-in|--price-out)
      [ "$#" -ge 2 ] || ops_unknown "$1 без значения"
      case "$2" in ''|*[!0-9.]*) ops_unknown "$1 $2" ;; esac
      args+=("$1" "$2"); shift ;;
    *) ops_unknown "$1" ;;
  esac
  shift
done
if [ "$action" = stats ]; then
  for a in "${args[@]+"${args[@]}"}"; do
    [ "$a" = --json ] || ops_unknown "$a (у stats только --json)"
  done
fi

svc=shturman-service
if [ "$(container_state "$svc")" != "running" ]; then
  echo "контейнер $svc не запущен — ./ops/up.sh и повторите" >&2
  exit 1
fi
docker exec "$svc" shturman commitments "$action" "${args[@]+"${args[@]}"}"
