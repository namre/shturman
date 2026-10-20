#!/usr/bin/env bash
# Ловушки в выдаче архива ассистенту (Р-104, docs/service.md, «Граница выдачи архива»).
# Ловушка — ненастоящий чат, который существует только в выдаче архива по токену: в списках чатов владельца, в
# сводках, в поиске приложения и в обработке его нет. Ассистенту он виден только в широком списке чатов, при
# листании всей истории страницами и по своему редкому слову в поиске. Выдано его содержимое — тревога владельцу
# и закрытие выдачи до его решения. Одну ловушку сервис заводит сам при первом запуске (SHTURMAN_CANARY=off —
# не заводить и не показывать).
#   ./ops/canary.sh list            — ловушки: номер, название, когда заведена, сколько раз выдана
#   ./ops/canary.sh add             — ещё одна ловушка (ужесточение, можно без владельца)
#   ./ops/canary.sh remove --id N   — убрать ловушку: ОСЛАБЛЕНИЕ, только по явной просьбе владельца
# -h, --help — эта справка.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

action="${1:-}"
case "$action" in
  list|add|remove) shift ;;
  *) ops_unknown "${action:-без действия}" ;;
esac
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --id)
      [ "$action" = remove ] && [ "$#" -ge 2 ] || ops_unknown "$1"
      case "$2" in ''|*[!0-9]*) ops_unknown "$1 $2" ;; esac
      args+=("$1" "$2"); shift ;;
    *) ops_unknown "$1" ;;
  esac
  shift
done
if [ "$action" = remove ] && [ "${#args[@]}" -eq 0 ]; then
  echo "remove: нужен --id (номер из ./ops/canary.sh list)" >&2
  exit 2
fi

svc=shturman-service
if [ "$(container_state "$svc")" != "running" ]; then
  echo "контейнер $svc не запущен — ./ops/up.sh и повторите" >&2
  exit 1
fi
docker exec "$svc" shturman canary "$action" "${args[@]+"${args[@]}"}"
