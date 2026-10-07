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

# --- сервис переписки ---
# Только счётчики и состояния: ни текста сообщений, ни токенов проверка не видит.
svc=shturman-service
if docker inspect "$svc" >/dev/null 2>&1; then
  svc_state="$(docker inspect -f '{{.State.Status}}' "$svc" 2>/dev/null || echo missing)"
  code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:8765/health 2>/dev/null)"
  if [ "$svc_state" = "running" ] && [ "${code:-000}" = "200" ]; then pass service "сервис переписки работает"
  else fail service "сервис переписки не отвечает (контейнер: $svc_state, HTTP ${code:-000}) — docker logs --tail 50 $svc"; fi

  if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -Eq '^(0\.0\.0\.0|\*|\[::\]):(8765|5432)$'; then
    fail service-bind "сервис переписки или база слушают внешний адрес — должны быть доступны только с сервера"
  else
    pass service-bind "сервис и база недоступны снаружи"
  fi

  pg_state="$(docker inspect -f '{{.State.Health.Status}}' shturman-postgres 2>/dev/null || echo missing)"
  if [ "$pg_state" = "healthy" ]; then pass database "база архива работает"
  else fail database "база архива в состоянии: $pg_state"; fi

  mcp="$(hpy -c '
from hermes_cli.config import load_config
s = ((load_config() or {}).get("mcp_servers") or {}).get("shturman") or {}
print("yes" if str(s.get("url", "")).endswith(":8765/mcp") else "no")' | tail -n 1)"
  if [ "$mcp" = "yes" ]; then pass archive-mcp "архив подключён к Hermes"
  else fail archive-mcp "архив не подключён к Hermes — запустите ./ops/up.sh"; fi

  st="$(docker exec "$svc" shturman call GET /api/status 2>/dev/null | tr -d ' \n')"
  num() { printf '%s' "$st" | sed -n "s/.*\"$1\":\([0-9]*\).*/\1/p"; }
  if [ -n "$st" ]; then
    pass archive "сообщений: $(num messages), чатов: $(num chats), исключено чатов: $(num chats_excluded)"
    waiting="$(num jobs_waiting)"; failed="$(num jobs_failed)"
    if [ "${waiting:-0}" -gt 50 ]; then
      warn jobs "в очереди $waiting заданий — Hermes их не забирает (бот не подключён или плагин не запущен)"
    else pass jobs "очередь заданий: ${waiting:-0}, неудачных: ${failed:-0}"; fi
    case "$st" in
      *'"owner_known":true'*) pass service-owner "сервис знает владельца" ;;
      *) warn service-owner "сервис ещё не знает владельца — кнопки согласования не работают, пока бот не привязан в мастере" ;;
    esac
  else
    fail archive "сервис не отдал состояние"
  fi

  # Защита от внедрённых инструкций: включена ли, отвечает ли модель и сколько входящих сообщений
  # ассистент видит непроверенными. Только числа и состояния.
  grd="$(docker inspect -f '{{.State.Status}}' shturman-guard 2>/dev/null || echo off)"
  case "$st" in
    *'"guard_enabled":true'*)
      unchecked="$(num guard_unchecked)"; hidden="$(num guard_hidden)"; waiting="$(num guard_waiting_owner)"
      case "$st" in
        *'"guard_problem":"unreachable"'*)
          fail guard "модель-классификатор не отвечает (контейнер: $grd) — входящие сообщения видны ассистенту НЕПРОВЕРЕННЫМИ: ${unchecked:-0}; docker logs --tail 50 shturman-guard" ;;
        *'"guard_problem":"model_mismatch"'*)
          fail guard "контейнер классификатора отдаёт не ту модель — входящие сообщения не проверяются: ${unchecked:-0}; запустите ./ops/guard.sh on" ;;
        *'"guard_model_used":false'*)
          warn guard "защита работает только на правилах, без модели — это заметно слабее (./ops/guard.sh on); скрыто: ${hidden:-0}" ;;
        *)
          if [ "$grd" != "running" ]; then
            fail guard "контейнер классификатора в состоянии: $grd — запустите ./ops/guard.sh on"
          elif [ "${unchecked:-0}" -gt 500 ]; then
            warn guard "защита включена; ещё не проверено сообщений: $unchecked (очередь разбирается фоном), скрыто: ${hidden:-0}, ждут вашего решения: ${waiting:-0}"
          else
            pass guard "защита от внедрённых инструкций включена; скрыто: ${hidden:-0}, ждут вашего решения: ${waiting:-0}, не проверено: ${unchecked:-0}"
          fi ;;
      esac ;;
    "") ;;
    *)
      if [ "$grd" = "off" ]; then
        warn guard "защита от внедрённых инструкций выключена — входящие сообщения ассистент читает без проверки (./ops/guard.sh on, docs/guard.md)"
      else
        warn guard "контейнер классификатора запущен ($grd), но защита в сервисе выключена — запустите ./ops/guard.sh on или off"
      fi ;;
  esac

  emb="$(docker inspect -f '{{.State.Status}}' shturman-embeddings 2>/dev/null || echo off)"
  case "$emb" in
    running) pass embeddings "поиск по смыслу включён" ;;
    off)     warn embeddings "поиск по смыслу выключен — работает поиск по словам (./ops/embeddings.sh on)" ;;
    *)       fail embeddings "контейнер эмбеддингов в состоянии: $emb" ;;
  esac
else
  warn service "сервис переписки не развёрнут — запустите ./ops/up.sh"
fi

# --- место на диске ---
free_mb="$(df -Pm . | awk 'NR==2 {print $4}')"
if [ "${free_mb:-0}" -lt 2048 ]; then warn disk "свободно ${free_mb} МБ — меньше 2 ГБ"
else pass disk "свободно $((free_mb / 1024)) ГБ"; fi

echo
if [ "$fails" -gt 0 ]; then echo "ИТОГ: FAIL ($fails)"; exit 1; fi
echo "ИТОГ: OK"
