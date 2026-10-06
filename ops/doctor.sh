#!/usr/bin/env bash
# Проверка здоровья экземпляра. Ничего не меняет и не читает секретов.
# Вывод: строки "PASS|WARN|FAIL  имя: подробности". Код возврата 1, если есть FAIL.
set -u
cd "$(dirname "$0")/.."

fails=0
pass() { printf 'PASS  %s: %s\n' "$1" "$2"; }
warn() { printf 'WARN  %s: %s\n' "$1" "$2"; }
fail() { printf 'FAIL  %s: %s\n' "$1" "$2"; fails=$((fails + 1)); }

c=shturman-hermes
hx() { docker exec "$c" hermes "$@" 2>&1; }

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
code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:9119/ 2>/dev/null)"
if [ "${code:-000}" = "200" ]; then pass dashboard "отвечает на локальном адресе"
else fail dashboard "не отвечает на 127.0.0.1:9119 (HTTP ${code:-000})"; fi

if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -Eq '^(0\.0\.0\.0|\*|\[::\]):9119$'; then
  fail dashboard-bind "дашборд слушает внешний адрес — должен только 127.0.0.1"
else
  pass dashboard-bind "слушает только локальный адрес"
fi

# --- настройки владельца: только факт наличия, без значений ---
envf=data/hermes/.env
has() { [ -r "$envf" ] && grep -Eq "^$1=.+" "$envf"; }
if has TELEGRAM_BOT_TOKEN; then pass bot-token "задан"
else warn bot-token "не задан — владелец вводит его в дашборде, страница Channels"; fi
if has TELEGRAM_ALLOWED_USERS; then pass bot-owner "разрешённый пользователь задан"
else warn bot-owner "не задан Telegram ID владельца — бот никому не ответит"; fi

# --- плагин бизнес-режима ---
plugins="$(hx plugins list)"
if printf '%s' "$plugins" | grep -qi 'telegram-business'; then
  if printf '%s' "$plugins" | grep -i 'telegram-business' | grep -Eqi 'enabled|✓|on'; then
    pass business-plugin "установлен и включён"
  else
    fail business-plugin "установлен, но не включён — бизнес-режим в Telegram подключать нельзя"
  fi
else
  warn business-plugin "не установлен — бота в бизнес-режиме в Telegram НЕ подключать (issue #127430)"
fi

echo
if [ "$fails" -gt 0 ]; then echo "ИТОГ: FAIL ($fails)"; exit 1; fi
echo "ИТОГ: OK"
