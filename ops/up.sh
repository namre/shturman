#!/usr/bin/env bash
# Поднимает стек (или применяет изменения). Повторный запуск безопасен.
#   ./ops/up.sh          — скачать образы при необходимости и запустить
#   ./ops/up.sh --pull   — сначала обновить образы указанных версий
set -eu
cd "$(dirname "$0")/.." || exit 1

[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }

# Каталог данных принадлежит тому, кто запускает стек; Hermes в контейнере работает под ним же.
# Точку подключения плагина создаём сами: иначе Docker создаст её от имени root.
mkdir -p data/hermes/plugins/shturman
chmod 700 data
HERMES_UID="$(id -u)"
HERMES_GID="$(id -g)"
export HERMES_UID HERMES_GID

c=shturman-hermes
hx() { docker exec -u "$HERMES_UID:$HERMES_GID" "$c" "$@"; }
plugin_enabled() {
  hx /opt/hermes/.venv/bin/python -c '
from hermes_cli.config import load_config
p = (load_config() or {}).get("plugins") or {}
ok = "shturman" in (p.get("enabled") or []) and "shturman" not in (p.get("disabled") or [])
print("yes" if ok else "no")' 2>/dev/null | tail -n 1
}
wait_dashboard() {
  for _ in $(seq 1 45); do
    if curl -fsS -o /dev/null -m 3 http://127.0.0.1:9119/api/status 2>/dev/null; then return 0; fi
    sleep 2
  done
  return 1
}

[ "${1:-}" = "--pull" ] && docker compose pull --quiet

docker compose config --quiet
docker compose up -d --remove-orphans

echo "Жду запуска контейнера…"
for _ in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)" = "running" ] && hx true 2>/dev/null && break
  sleep 2
done

# Плагин «Штурмана» включается штатной командой Hermes. Без него дашборд с заданным внешним
# адресом не запустится: Hermes требует хотя бы один способ входа.
if [ "$(plugin_enabled)" != "yes" ]; then
  echo "Включаю плагин shturman…"
  hx hermes plugins enable shturman
  [ "$(plugin_enabled)" = "yes" ] || { echo "плагин shturman не включился" >&2; exit 1; }
  docker compose restart hermes
fi

echo "Жду запуска Hermes…"
if wait_dashboard; then
  echo "Дашборд Hermes отвечает на локальном адресе."
  exec ./ops/doctor.sh
fi
echo "Дашборд не ответил за 90 секунд. Смотрите: docker logs --tail 50 shturman-hermes" >&2
exit 1
