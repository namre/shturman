#!/usr/bin/env bash
# Записывает в .env адрес, по которому владелец открывает ассистента (SHTURMAN_PUBLIC_URL).
# Значение не секретное. Остальное содержимое .env скрипт не читает и не печатает.
#   ./ops/set-public-url.sh https://assistant.example.com
# После изменения — ./ops/up.sh, чтобы Hermes подхватил адрес.
# Только для режима с Hermes: без него у экземпляра нет ни дашборда, ни внешнего адреса.
# Тот же адрес получает сервис переписки — для страницы настройки переписки (/shturman-setup/).
#   -h, --help — эта справка; .env не меняется.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"
case "${1:-}" in -*) ops_unknown "$1" ;; esac
[ $# -le 1 ] || ops_unknown "$2"
if [ -f .env ]; then require_hermes "дашборда и внешнего адреса"; fi

url="${1:-}"
case "$url" in
  https://*|http://*) ;;
  *) echo "нужен адрес с https://, например https://assistant.example.com" >&2; exit 2 ;;
esac
url="${url%/}"
if ! printf '%s' "$url" | grep -Eq '^https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?$'; then
  echo "адрес содержит недопустимые символы" >&2; exit 2
fi
case "$url" in
  http://*) echo "ВНИМАНИЕ: адрес без https — вход будет работать, но данные пойдут по сети открыто." >&2 ;;
esac

[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }
umask 077
env_put SHTURMAN_PUBLIC_URL "$url"
echo "SHTURMAN_PUBLIC_URL записан: $url"
