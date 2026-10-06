#!/usr/bin/env bash
# Проверка здоровья экземпляра. Ничего не меняет и не читает секретов.
# Вывод: строки "PASS|WARN|FAIL  имя: подробности". Код возврата 1, если есть FAIL.
set -u
cd "$(dirname "$0")/.." || exit 1

fails=0
pass() { printf 'PASS  %s: %s\n' "$1" "$2"; }
warn() { printf 'WARN  %s: %s\n' "$1" "$2"; }
fail() { printf 'FAIL  %s: %s\n' "$1" "$2"; fails=$((fails + 1)); }

c=shturman-hermes
# Под тем же пользователем, под которым работает Hermes: файлы, созданные от root, он не прочитает.
hpy() { docker exec -u "$(id -u):$(id -g)" "$c" /opt/hermes/.venv/bin/python "$@" 2>/dev/null; }

# --- контейнер ---
state="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
if [ "$state" = "running" ]; then
  img="$(docker inspect -f '{{.Config.Image}}' "$c")"
  restarts="$(docker inspect -f '{{.RestartCount}}' "$c")"
  pass hermes "работает, образ $img"
  [ "$restarts" -gt 3 ] && warn hermes-restarts "контейнер перезапускался $restarts раз"
else
  fail hermes "контейнер в состоянии: $state"
  echo; echo "ИТОГ: FAIL ($fails)"; exit 1
fi

case "$(docker inspect -f '{{.Config.Image}}' "$c")" in
  *:latest|*:main|*:stable) warn hermes-version "образ без явной версии — укажите HERMES_VERSION" ;;
esac

# --- дашборд ---
code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:9119/api/status 2>/dev/null)"
if [ "${code:-000}" = "200" ]; then pass dashboard "отвечает на локальном адресе"
else fail dashboard "не отвечает на 127.0.0.1:9119 (HTTP ${code:-000})"; fi

if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -Eq '^(0\.0\.0\.0|\*|\[::\]):9119$'; then
  fail dashboard-bind "дашборд слушает внешний адрес — должен только 127.0.0.1"
else
  pass dashboard-bind "слушает только локальный адрес"
fi

# --- вход и мастер настройки ---
public_url="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$c" | sed -n 's/^HERMES_DASHBOARD_PUBLIC_URL=//p')"
plugin_on="$(hpy -c '
from hermes_cli.config import load_config
p = (load_config() or {}).get("plugins") or {}
print("yes" if "shturman" in (p.get("enabled") or []) and "shturman" not in (p.get("disabled") or []) else "no")' | tail -n 1)"
if [ "$plugin_on" = "yes" ]; then pass plugin "плагин shturman включён"
else fail plugin "плагин shturman не включён — запустите ./ops/up.sh"; fi
if [ -n "$public_url" ]; then
  pass public-url "$public_url"
  # Список способов входа Hermes отдаёт без сессии только когда вход включён, то есть при заданном адресе.
  if curl -s -m 5 http://127.0.0.1:9119/api/auth/providers 2>/dev/null | grep -q '"name":"shturman"'; then
    pass login "способ входа «Штурман» подключён"
  else
    fail login "плагин shturman не зарегистрировал вход — смотрите docker logs --tail 50 shturman-hermes"
  fi
  page="$(curl -s -o /dev/null -m 8 -w '%{http_code}' "$public_url/shturman-auth/login.html" 2>/dev/null)"
  case "${page:-000}" in
    200) pass login-page "страница входа открывается по внешнему адресу" ;;
    30[1-8]|401|403) warn login-page "перед адресом стоит внешняя защита (HTTP $page) — проверьте страницу входа из браузера" ;;
    *) fail login-page "страница входа не открывается (HTTP ${page:-000}) — проверьте настройку прокси по config/Caddyfile.example" ;;
  esac
else
  warn public-url "внешний адрес не задан — дашборд доступен только с самого сервера (./ops/set-public-url.sh)"
fi

owner_bound=no; wizard_done=no
while IFS='=' read -r k v; do
  case "$k" in owner_bound) owner_bound="$v" ;; wizard_completed) wizard_done="$v" ;; esac
done <<EOF2
$(hpy /opt/data/plugins/shturman/cli.py status)
EOF2

# --- настройки владельца: только факт наличия, без значений ---
envf=data/hermes/.env
has() { [ -r "$envf" ] && grep -Eq "^$1=.+" "$envf"; }
if has TELEGRAM_BOT_TOKEN; then pass bot-token "задан"
else warn bot-token "не задан — владелец вводит его в мастере настройки, шаг «Бот в Telegram»"; fi
if [ "$owner_bound" = "yes" ] && has TELEGRAM_ALLOWED_USERS; then pass bot-owner "владелец привязан к боту"
elif has TELEGRAM_ALLOWED_USERS; then warn bot-owner "разрешённый пользователь задан вручную, но в мастере бот не привязан — вход по коду от бота не работает"
else warn bot-owner "владелец не привязан — бот никому не ответит, войти можно только по ссылке (./ops/activation-link.sh)"; fi
if [ "$wizard_done" = "yes" ]; then pass wizard "мастер настройки пройден"
else warn wizard "мастер настройки не завершён"; fi

# --- плагин бизнес-режима ---
# Состояние берём из настроек и каталога плагинов, а не из таблицы `hermes plugins list`:
# в ней статус «not enabled» содержит слово «enabled».
biz="$(hpy -c '
import os
from hermes_cli.config import load_config
p = (load_config() or {}).get("plugins") or {}
name = "telegram-business"
installed = os.path.isdir(os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), "plugins", name))
enabled = name in (p.get("enabled") or []) and name not in (p.get("disabled") or [])
print("enabled" if installed and enabled else "installed" if installed else "absent")' | tail -n 1)"
case "$biz" in
  enabled)   pass business-plugin "установлен и включён" ;;
  installed) fail business-plugin "установлен, но не включён — бизнес-режим в Telegram подключать нельзя" ;;
  *)         warn business-plugin "не установлен — бота в бизнес-режиме в Telegram НЕ подключать (issue #127430)" ;;
esac

echo
if [ "$fails" -gt 0 ]; then echo "ИТОГ: FAIL ($fails)"; exit 1; fi
echo "ИТОГ: OK"
