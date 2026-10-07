#!/usr/bin/env bash
# Поднимает стек (или применяет изменения). Повторный запуск безопасен.
#   ./ops/up.sh          — скачать образы при необходимости и запустить
#   ./ops/up.sh --pull   — сначала обновить образы указанных версий
# Что запускается, зависит от режима установки (SHTURMAN_MODE в .env, ./ops/mode.sh):
#   hermes      — Hermes, сервис переписки, база; плагин и архив подключаются к Hermes;
#   standalone  — только сервис переписки и база. Шаги Hermes пропускаются, его образ
#                 не скачивается, а оставшийся от прежнего режима контейнер убирается.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh

[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }

# Служебные значения, появившиеся в новой версии (токены сервиса переписки, режим установки),
# дописываются сами. Уже заданные значения скрипт не трогает и не печатает.
./ops/init-env.sh --auto > /dev/null
load_mode

# Каталог данных принадлежит тому, кто запускает стек; Hermes и сервис переписки в контейнерах
# работают под ним же. Точки подключения создаём сами: иначе Docker создаст их от имени root.
mkdir -p data/shturman data/embeddings/model
if [ "$MODE" = hermes ]; then mkdir -p data/hermes/plugins/shturman; fi
chmod 700 data data/shturman
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
# Запись об архиве в настройках Hermes: адрес сервиса, токен по имени переменной (само значение
# в настройки не попадает), перечень инструментов — все только на чтение.
mcp_register() {
  hx /opt/hermes/.venv/bin/python -c '
from hermes_cli.config import load_config, save_config
want = {
    "url": "http://127.0.0.1:8765/mcp",
    "headers": {"Authorization": "Bearer ${MCP_SHTURMAN_API_KEY}"},
    "timeout": 60,
    "connect_timeout": 15,
    "skip_preflight": True,
    "tools": {
        "include": ["search_messages", "get_context", "list_chats", "get_chat_history", "find_person",
                    "list_commitments", "get_commitment", "get_person_page", "search_pages"],
        "resources": False,
        "prompts": False,
    },
}
cfg = load_config() or {}
servers = cfg.get("mcp_servers")
if not isinstance(servers, dict):
    servers = {}
if servers.get("shturman") == want:
    print("same")
else:
    servers["shturman"] = want
    cfg["mcp_servers"] = servers
    save_config(cfg)
    print("changed")' 2>/dev/null | tail -n 1
}
wait_service() {
  for _ in $(seq 1 45); do
    if curl -fsS -o /dev/null -m 3 http://127.0.0.1:8765/health 2>/dev/null; then return 0; fi
    sleep 2
  done
  return 1
}
wait_dashboard() {
  for _ in $(seq 1 45); do
    if curl -fsS -o /dev/null -m 3 http://127.0.0.1:9119/api/status 2>/dev/null; then return 0; fi
    sleep 2
  done
  return 1
}

[ "${1:-}" = "--pull" ] && docker compose pull --quiet --ignore-buildable

docker compose config --quiet
# Образ сервиса переписки собирается здесь же, из каталога service/.
docker compose build --quiet shturman
if [ "$MODE" != hermes ] && docker inspect "$c" >/dev/null 2>&1; then
  # Остался от режима с Hermes. Убирается только контейнер: данные Hermes (data/hermes) не трогаем.
  echo "Режим без Hermes: останавливаю и убираю контейнер $c (его данные остаются в data/hermes)…"
  docker compose --profile hermes rm --stop --force hermes || true
  # Если контейнер создавали не этим файлом Compose, первая команда его не увидит.
  if docker inspect "$c" >/dev/null 2>&1; then docker rm --force "$c" >/dev/null; fi
fi
docker compose up -d --remove-orphans

echo "Жду запуска сервиса переписки…"
if ! wait_service; then
  echo "Сервис переписки не ответил за 90 секунд. Смотрите: docker logs --tail 50 shturman-service" >&2
  exit 1
fi

if [ "$MODE" != hermes ]; then
  echo "пропущено: режим без Hermes — включение плагина shturman в Hermes"
  echo "пропущено: режим без Hermes — запись архива (MCP-сервер shturman) в настройки Hermes"
  echo "пропущено: режим без Hermes — ожидание дашборда Hermes"
  echo "Архив к своему Codex CLI или Claude Code владелец подключает по ./ops/connect.sh."
  exec ./ops/doctor.sh
fi

echo "Жду запуска контейнера…"
for _ in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)" = "running" ] && hx true 2>/dev/null && break
  sleep 2
done

# Плагин «Штурмана» включается штатной командой Hermes. Без него дашборд с заданным внешним
# адресом не запустится: Hermes требует хотя бы один способ входа.
restart=no
if [ "$(plugin_enabled)" != "yes" ]; then
  echo "Включаю плагин shturman…"
  hx hermes plugins enable shturman
  [ "$(plugin_enabled)" = "yes" ] || { echo "плагин shturman не включился" >&2; exit 1; }
  restart=yes
fi
case "$(mcp_register)" in
  changed) echo "Архив переписки подключён к Hermes (MCP-сервер shturman)."; restart=yes ;;
  same) ;;
  *) echo "не удалось записать MCP-сервер shturman в настройки Hermes" >&2; exit 1 ;;
esac
[ "$restart" = "yes" ] && docker compose restart hermes

echo "Жду запуска Hermes…"
if wait_dashboard; then
  echo "Дашборд Hermes отвечает на локальном адресе."
  exec ./ops/doctor.sh
fi
echo "Дашборд не ответил за 90 секунд. Смотрите: docker logs --tail 50 shturman-hermes" >&2
exit 1
