#!/usr/bin/env bash
# Режим установки: с Hermes или без него («только архив и согласования», docs/standalone.md).
#   ./ops/mode.sh                       — показать текущий режим
#   ./ops/mode.sh set hermes|standalone — записать другой режим в .env
#
# Значение не секретное; остальное содержимое .env скрипт не читает и не печатает.
# Команда set ничего не запускает и не останавливает: изменение применяет ./ops/up.sh.
# Смена режима на работающем экземпляре — стоп-точка: сначала ./ops/backup.sh и согласие
# владельца. Что при смене происходит с ботами и бизнес-подключением — docs/standalone.md,
# раздел «Переход между режимами».
#   -h, --help — эта справка; режим не читается и не меняется.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

case "${1:-show}" in
  show)
    [ $# -le 1 ] || ops_unknown "$2"
    mode="$(shturman_mode)" || exit 2
    case "$mode" in
      hermes)     echo "Режим: hermes — стоковый Hermes, сервис переписки и база." ;;
      standalone) echo "Режим: standalone — без Hermes: сервис переписки и база (архив и согласования)." ;;
    esac
    [ -n "$(env_get SHTURMAN_MODE)" ] || echo "(строки SHTURMAN_MODE в .env нет — это прежний режим с Hermes)" ;;
  set)
    new="${2:-}"
    valid_mode "$new" || { echo "использование: $0 set hermes|standalone" >&2; exit 2; }
    [ $# -le 2 ] || ops_unknown "$3"
    [ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto --mode $new" >&2; exit 1; }
    old="$(shturman_mode)" || exit 2
    umask 077
    env_put SHTURMAN_MODE "$new"
    env_put COMPOSE_PROFILES "$(compose_profiles "$new" keep)"
    if [ "$old" = "$new" ]; then
      echo "Режим не изменился: $new."
      exit 0
    fi
    echo "Режим записан: $old → $new. Пока ничего не изменилось: применяет ./ops/up.sh."
    case "$new" in
      standalone)
        echo "После ./ops/up.sh контейнер Hermes будет остановлен и убран; его данные (data/hermes) останутся на диске."
        echo "Задания, которые выполнял Hermes (модель, сообщения владельцу), будет выполнять сам сервис:"
        echo "чтобы разбирались обязательства и приходили карточки, владельцу понадобятся бот согласований"
        echo "(первый шаг страницы настройки переписки) и ключ модели (там же, «Дополнительно») либо ./ops/init-env.sh."
        echo "Внешний адрес страницы настройки в этом режиме не нужен: ./ops/set-setup-url.sh --clear."
        echo "Бизнес-режим Telegram, если он был подключён к боту Hermes, владелец отключает в настройках Telegram." ;;
      hermes)
        echo "После ./ops/up.sh будет скачан образ Hermes (около 4 ГБ) и запущен его контейнер."
        echo "Дальше — как при первой установке с Hermes: docs/runbooks/deploy.md, шаги 4–7." ;;
    esac ;;
  -*) ops_unknown "$1" ;;
  *)
    ops_usage >&2
    exit 2 ;;
esac
