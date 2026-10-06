#!/usr/bin/env bash
# Поднимает стек (или применяет изменения). Повторный запуск безопасен.
#   ./ops/up.sh          — скачать образы при необходимости и запустить
#   ./ops/up.sh --pull   — сначала обновить образы указанных версий
set -eu
cd "$(dirname "$0")/.." || exit 1

[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }

# Каталог данных принадлежит тому, кто запускает стек; Hermes в контейнере работает под ним же.
mkdir -p data/hermes
chmod 700 data
HERMES_UID="$(id -u)"
HERMES_GID="$(id -g)"
export HERMES_UID HERMES_GID

[ "${1:-}" = "--pull" ] && docker compose pull --quiet

docker compose config --quiet
docker compose up -d --remove-orphans

echo "Жду запуска Hermes…"
for _ in $(seq 1 45); do
  if curl -fsS -o /dev/null -m 3 http://127.0.0.1:9119/ 2>/dev/null; then
    echo "Дашборд Hermes отвечает на локальном адресе."
    exec ./ops/doctor.sh
  fi
  sleep 2
done
echo "Дашборд не ответил за 90 секунд. Смотрите: docker logs --tail 50 shturman-hermes" >&2
exit 1
