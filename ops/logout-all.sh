#!/usr/bin/env bash
# Завершает все входы владельца (например, потеряно устройство, с которого входили):
#   * сессии дашборда Hermes — только в режиме с Hermes;
#   * сессии страницы настройки переписки (/shturman-setup/) — в обоих режимах.
# После этого вход в дашборд — заново по коду от бота или по ссылке из ./ops/activation-link.sh;
# на страницу настройки переписки — по коду от бота согласований или по ссылке из
# ./ops/setup-link.sh.
# Параметров нет. -h, --help — эта справка; сессии при этом не завершаются.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"
ops_no_args "$@"
MODE="$(shturman_mode)" || exit 2

rc=0
if [ "$MODE" = hermes ]; then
  docker exec -u "$(id -u):$(id -g)" shturman-hermes /opt/hermes/.venv/bin/python \
    /opt/data/plugins/shturman/cli.py logout-all || rc=1
else
  echo "пропущено: режим без Hermes — сессии дашборда"
fi

svc=shturman-service
if [ "$(container_state "$svc")" != "running" ]; then
  echo "контейнер $svc не запущен — входы на страницу настройки переписки не завершены (./ops/up.sh и повторите)" >&2
  rc=1
elif ! docker exec "$svc" shturman setup-logout-all; then
  echo "сервис не завершил входы на страницу настройки переписки. Если у него нет такой команды —" >&2
  echo "сервис прежней версии: этой страницы у него ещё нет, завершать нечего (UPGRADING.md)." >&2
  rc=1
fi
exit "$rc"
