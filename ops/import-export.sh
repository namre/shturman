#!/usr/bin/env bash
# Импорт выгрузки Telegram Desktop (result.json) в архив — запасной путь, в терминале. Основной
# путь — страница настройки переписки (./ops/setup-link.sh): выгрузка загружается там, в браузере.
#   ./ops/import-export.sh scan  /путь/к/result.json            — показать чаты, ничего не записывая
#   ./ops/import-export.sh import /путь/к/result.json [--exclude user:123 ...] [--owner-id N]
# Файл подключается контейнеру только для чтения и никуда не копируется.
# Сначала scan: по его списку владелец решает, какие чаты исключить. Исключение запоминается
# для всех источников. Повторный импорт того же файла дублей не создаёт.
# Параметры после пути к файлу передаются команде импорта как есть.
#   -h, --help — эта справка; ничего не запускается.
#
# Вывод scan — названия чатов владельца: агент запускает его только по явной просьбе владельца.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

cmd="${1:-}"; file="${2:-}"
case "$cmd" in
  scan|import) ;;
  -*) ops_unknown "$cmd" ;;
  *) echo "использование: $0 scan|import /путь/к/result.json [параметры]" >&2; exit 2 ;;
esac
[ -f "$file" ] || { echo "файл не найден: $file" >&2; exit 2; }
shift 2
abs="$(cd "$(dirname "$file")" && pwd)/$(basename "$file")"

HERMES_UID="$(id -u)"; HERMES_GID="$(id -g)"; export HERMES_UID HERMES_GID
exec docker compose run --rm --no-deps -T \
  -v "$abs:/import/result.json:ro" shturman "$cmd" /import/result.json "$@"
