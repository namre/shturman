#!/usr/bin/env bash
# Вход в аккаунт Telegram по QR-коду — запасной путь, в терминале. Основной путь — страница
# настройки переписки (./ops/setup-link.sh): там тот же вход идёт в браузере владельца.
# ЗАПУСКАЕТ ЧЕЛОВЕК в своём терминале: QR нужно отсканировать телефоном, а пароль облачного
# хранения (если он включён) вводится скрыто и нигде не сохраняется. Агент этот скрипт
# не запускает и вывод его не читает: вход в аккаунт — стоп-точка (AGENTS.md).
#   ./ops/tg-login.sh assistant   — дополнительный аккаунт-помощник (может отправлять после согласования)
#   ./ops/tg-login.sh owner       — основной аккаунт владельца, ТОЛЬКО ЧТЕНИЕ
# Нужны TELEGRAM_API_ID и TELEGRAM_API_HASH в .env (my.telegram.org → API development tools).
#   -h, --help — эта справка; вход при этом не начинается.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

role="${1:-}"
case "$role" in
  assistant|owner) ;;
  -*) ops_unknown "$role" ;;
  *) echo "использование: $0 assistant|owner" >&2; exit 2 ;;
esac
[ $# -le 1 ] || ops_unknown "$2"
if [ ! -t 0 ] || [ ! -t 1 ]; then
  echo "Этот скрипт запускает человек в интерактивном терминале." >&2; exit 2
fi
exec docker exec -it shturman-service shturman tg-login "$role"
