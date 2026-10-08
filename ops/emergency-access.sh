#!/usr/bin/env bash
# Аварийный вход, когда обычный не работает: дашборд не запускается (например, после обновления
# Hermes не загрузился плагин shturman) или сломана страница входа.
#
#   ./ops/emergency-access.sh on    — запустить Hermes без внешнего адреса. Дашборд работает только
#                                     на самом сервере, входа не требует; снаружи адрес перестаёт
#                                     открываться. Заходить через SSH-туннель со своего компьютера:
#                                       ssh -L 9119:127.0.0.1:9119 <сервер>
#                                     и открыть http://127.0.0.1:9119
#   ./ops/emergency-access.sh off   — вернуть обычный режим (то же, что ./ops/up.sh)
#
# Файл .env не меняется: адрес отключается только на время этого запуска.
# Страница настройки переписки (/shturman-setup/) в аварийном режиме открывается так же, через
# туннель: ./ops/setup-link.sh --local.
#   -h, --help — эта справка; ничего не перезапускается.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"
[ $# -le 1 ] || ops_unknown "$2"

case "${1:-}" in
  on)
    [ -f .env ] || { echo "нет .env" >&2; exit 1; }
    require_hermes "дашборда"
    HERMES_UID="$(id -u)"; HERMES_GID="$(id -g)"
    export HERMES_UID HERMES_GID
    SHTURMAN_PUBLIC_URL="" docker compose up -d --force-recreate hermes
    echo "Жду запуска дашборда на локальном адресе…"
    for _ in $(seq 1 45); do
      if curl -fsS -o /dev/null -m 3 http://127.0.0.1:9119/api/status 2>/dev/null; then
        echo "Аварийный режим включён. Снаружи дашборд закрыт."
        echo "Вход: ssh -L 9119:127.0.0.1:9119 <сервер>, затем http://127.0.0.1:9119"
        echo "Когда закончите: ./ops/emergency-access.sh off"
        exit 0
      fi
      sleep 2
    done
    echo "Дашборд не ответил. Смотрите: docker logs --tail 50 shturman-hermes" >&2
    exit 1 ;;
  off)
    exec ./ops/up.sh ;;
  -*) ops_unknown "$1" ;;
  *)
    ops_usage >&2
    exit 2 ;;
esac
