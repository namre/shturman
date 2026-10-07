#!/usr/bin/env bash
# Выдаёт одноразовую ссылку для входа владельца: первую (активация) или повторную (восстановление,
# если у владельца пропал доступ к Telegram). Ссылка действует 30 минут и срабатывает один раз;
# новая ссылка отменяет предыдущую.
#
# ВАЖНО для агента: то, что печатает скрипт, — одноразовый пропуск в дашборд. Передай ссылку
# владельцу как есть и больше нигде её не сохраняй: ни в состоянии, ни в журнале, ни в отчёте.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
# Без Hermes дашборда нет, а владелец привязывается к боту согласований: ./ops/bot-bind.sh.
require_hermes "дашборда и входа в него"

c=shturman-hermes
[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }
url="$(grep -E '^SHTURMAN_PUBLIC_URL=' .env | tail -n 1 | cut -d= -f2- || true)"
[ -n "$url" ] || { echo "не задан адрес — сначала ./ops/set-public-url.sh https://ваш-адрес" >&2; exit 1; }
[ "$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)" = "running" ] \
  || { echo "контейнер $c не запущен — сначала ./ops/up.sh" >&2; exit 1; }

# Под тем же пользователем, под которым работает Hermes: иначе он не прочитает созданный файл.
docker exec -u "$(id -u):$(id -g)" "$c" /opt/hermes/.venv/bin/python \
  /opt/data/plugins/shturman/cli.py activation-link "$url"
