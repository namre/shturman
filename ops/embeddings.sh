#!/usr/bin/env bash
# Включает или выключает поиск по смыслу (локальные эмбеддинги).
#   ./ops/embeddings.sh on      — добавить контейнер с моделью (около 1,2 ГБ памяти; при первом
#                                 запуске модель скачивается с huggingface.co, ~0,5 ГБ)
#   ./ops/embeddings.sh off     — убрать; поиск остаётся, но только по словам
#   ./ops/embeddings.sh status  — показать, что включено и сколько сообщений обработано
# Значения, которые скрипт пишет в .env, не секретные. Остальное содержимое .env он не читает.
set -eu
cd "$(dirname "$0")/.." || exit 1

mode="${1:-status}"
[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }

set_env() {
  local tmp; tmp="$(mktemp .env.XXXXXX)"
  grep -Ev "^$1=" .env > "$tmp" || true
  [ -n "$2" ] && printf '%s=%s\n' "$1" "$2" >> "$tmp"
  chmod 600 "$tmp"; mv "$tmp" .env
}

case "$mode" in
  on)
    mem_mb="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
    if [ "${mem_mb:-0}" -lt 1500 ]; then
      echo "Свободной памяти ${mem_mb} МБ — для модели нужно около 1200 МБ. Включаю, но следите за ./ops/doctor.sh." >&2
    fi
    umask 077
    set_env COMPOSE_PROFILES embeddings
    set_env SHTURMAN_EMBEDDINGS_URL http://embeddings:80
    echo "Поиск по смыслу включён в настройках. Применяю: ./ops/up.sh"
    exec ./ops/up.sh ;;
  off)
    umask 077
    set_env COMPOSE_PROFILES ""
    set_env SHTURMAN_EMBEDDINGS_URL ""
    echo "Поиск по смыслу выключен в настройках. Применяю: ./ops/up.sh"
    exec ./ops/up.sh ;;
  status)
    if grep -Eq '^COMPOSE_PROFILES=.*embeddings' .env; then echo "в настройках: включено"; else echo "в настройках: выключено"; fi
    docker exec shturman-service shturman call GET /api/embeddings/status 2>/dev/null \
      || echo "сервис переписки не отвечает — ./ops/doctor.sh" ;;
  *) echo "использование: $0 on|off|status" >&2; exit 2 ;;
esac
