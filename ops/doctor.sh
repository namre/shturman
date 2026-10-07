#!/usr/bin/env bash
# Проверка здоровья экземпляра. Ничего не меняет и не читает секретов.
# Вывод: строки "PASS|WARN|FAIL|SKIP  имя: подробности". Код возврата 1, если есть FAIL.
# SKIP — проверка к режиму установки не относится (режим без Hermes, docs/standalone.md).
set -u
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh

fails=0
pass() { printf 'PASS  %s: %s\n' "$1" "$2"; }
warn() { printf 'WARN  %s: %s\n' "$1" "$2"; }
fail() { printf 'FAIL  %s: %s\n' "$1" "$2"; fails=$((fails + 1)); }
skip() { printf 'SKIP  %s: %s\n' "$1" "$2"; }

if ! MODE="$(shturman_mode)"; then
  fail mode "в .env неизвестный режим — ./ops/mode.sh set hermes или ./ops/mode.sh set standalone"
  echo; echo "ИТОГ: FAIL ($fails)"; exit 1
fi
case "$MODE" in
  hermes)     pass mode "с Hermes" ;;
  standalone) pass mode "без Hermes: только архив и согласования" ;;
esac

c=shturman-hermes
# Под тем же пользователем, под которым работает Hermes: файлы, созданные от root, он не прочитает.
hpy() { docker exec -u "$(id -u):$(id -g)" "$c" /opt/hermes/.venv/bin/python "$@" 2>/dev/null; }

if [ "$MODE" = hermes ]; then

# --- контейнер ---
state="$(container_state "$c")"
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

if ! command -v ss >/dev/null 2>&1; then
  warn dashboard-bind "нет программы ss (пакет iproute2) — не проверено, какой адрес слушает дашборд"
elif ss -tlnH 2>/dev/null | awk '{print $4}' | grep -Eq '^(0\.0\.0\.0|\*|\[::\]):9119$'; then
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

else
  skip hermes "пропущено: режим без Hermes — контейнер, дашборд, вход, мастер, плагины Hermes не проверяются"
  if [ "$(container_state "$c")" = "running" ]; then
    warn hermes-leftover "контейнер $c работает, хотя выбран режим без Hermes — ./ops/up.sh его уберёт"
  fi
fi

# --- сервис переписки ---
# Только счётчики и состояния: ни текста сообщений, ни токенов проверка не видит.
svc=shturman-service
if docker inspect "$svc" >/dev/null 2>&1; then
  svc_state="$(container_state "$svc")"
  code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:8765/health 2>/dev/null)"
  if [ "$svc_state" = "running" ] && [ "${code:-000}" = "200" ]; then pass service "сервис переписки работает"
  else fail service "сервис переписки не отвечает (контейнер: $svc_state, HTTP ${code:-000}) — docker logs --tail 50 $svc"; fi

  if ! command -v ss >/dev/null 2>&1; then
    warn service-bind "нет программы ss (пакет iproute2) — не проверено, какие адреса слушают сервис и база"
  elif ss -tlnH 2>/dev/null | awk '{print $4}' | grep -Eq '^(0\.0\.0\.0|\*|\[::\]):(8765|5432)$'; then
    fail service-bind "сервис переписки или база слушают внешний адрес — должны быть доступны только с сервера"
  else
    pass service-bind "сервис и база недоступны снаружи"
  fi

  pg_state="$(docker inspect -f '{{.State.Health.Status}}' shturman-postgres 2>/dev/null)" || pg_state=""
  pg_state="${pg_state:-missing}"
  if [ "$pg_state" = "healthy" ]; then pass database "база архива работает"
  else fail database "база архива в состоянии: $pg_state"; fi

  if [ "$MODE" = hermes ]; then
    mcp="$(hpy -c '
from hermes_cli.config import load_config
s = ((load_config() or {}).get("mcp_servers") or {}).get("shturman") or {}
print("yes" if str(s.get("url", "")).endswith(":8765/mcp") else "no")' | tail -n 1)"
    if [ "$mcp" = "yes" ]; then pass archive-mcp "архив подключён к Hermes"
    else fail archive-mcp "архив не подключён к Hermes — запустите ./ops/up.sh"; fi
  else
    # Без токена архив обязан отказать: так видно и что он отвечает, и что закрыт. Сам токен не нужен.
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
      -d '{}' http://127.0.0.1:8765/mcp 2>/dev/null)"
    case "${code:-000}" in
      401) pass archive-mcp "архив отвечает на 127.0.0.1:8765/mcp и без токена не открывается (подключение клиента — ./ops/connect.sh)" ;;
      000) fail archive-mcp "архив не отвечает на 127.0.0.1:8765/mcp" ;;
      *)   fail archive-mcp "архив ответил без токена кодом HTTP $code вместо отказа 401 — остановитесь и разберитесь" ;;
    esac
  fi

  st="$(docker exec "$svc" shturman call GET /api/status 2>/dev/null | tr -d ' \n')"
  num() { printf '%s' "$st" | sed -n "s/.*\"$1\":\([0-9]*\).*/\1/p"; }
  if [ -n "$st" ]; then
    pass archive "сообщений: $(num messages), чатов: $(num chats), исключено чатов: $(num chats_excluded)"
    waiting="$(num jobs_waiting)"; failed="$(num jobs_failed)"
    if [ "${waiting:-0}" -gt 50 ] && [ "$MODE" = hermes ]; then
      warn jobs "в очереди $waiting заданий — Hermes их не забирает (бот не подключён или плагин не запущен)"
    elif [ "${waiting:-0}" -gt 50 ]; then
      warn jobs "в очереди $waiting заданий — их некому выполнять: смотрите строки approvals-bot и model"
    else pass jobs "очередь заданий: ${waiting:-0}, неудачных: ${failed:-0}"; fi
    if [ "$MODE" = hermes ]; then
      case "$st" in
        *'"owner_known":true'*) pass service-owner "сервис знает владельца" ;;
        *) warn service-owner "сервис ещё не знает владельца — кнопки согласования не работают, пока бот не привязан в мастере" ;;
      esac
    fi
  else
    fail archive "сервис не отдал состояние"
  fi

  # --- свой бот согласований и своя модель сервиса ---
  # Только признаки и коды неполадок из GET /api/executor/status: ни токена, ни ключа, ни имён.
  # Без Hermes это единственные бот и модель; с Hermes строки появляются, только если они настроены.
  bot_configured=unknown; bot_polling=unknown; bot_problem=""; bot_owner=unknown; bot_business=unknown
  llm_configured=unknown; llm_model=""; llm_last=unknown; llm_problem=""; llm_calls=0; llm_failures=0
  while IFS='=' read -r k v; do
    case "$k" in
      bot_configured) bot_configured="$v" ;; bot_polling) bot_polling="$v" ;; bot_problem) bot_problem="$v" ;;
      bot_owner) bot_owner="$v" ;; bot_business) bot_business="$v" ;;
      llm_configured) llm_configured="$v" ;; llm_model) llm_model="$v" ;; llm_last) llm_last="$v" ;;
      llm_problem) llm_problem="$v" ;; llm_calls) llm_calls="$v" ;; llm_failures) llm_failures="$v" ;;
    esac
  done <<EOF3
$(docker exec "$svc" shturman call GET /api/executor/status 2>/dev/null | docker exec -i "$svc" python -c '
import json, re, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
b, m = d.get("bot") or {}, d.get("llm") or {}
yn = lambda v: "yes" if v is True else "no" if v is False else "unknown"
safe = lambda v: re.sub(r"[^A-Za-z0-9._:/@-]", "", str(v or ""))[:80]
num = lambda v: str(v) if isinstance(v, int) and not isinstance(v, bool) else "0"
print("bot_configured=" + yn(b.get("configured")))
print("bot_polling=" + yn(b.get("polling")))
print("bot_problem=" + safe(b.get("problem")))
print("bot_owner=" + yn(b.get("owner_bound")))
print("bot_business=" + yn(b.get("business_capable")))
print("llm_configured=" + yn(m.get("configured")))
print("llm_model=" + safe(m.get("model")))
print("llm_last=" + yn(m.get("last_call_ok")))
print("llm_problem=" + safe(m.get("problem")))
print("llm_calls=" + num(m.get("calls")))
print("llm_failures=" + num(m.get("failures")))' 2>/dev/null)
EOF3

  if [ "$bot_configured" = unknown ]; then
    if [ "$MODE" = standalone ]; then fail approvals-bot "сервис не отдал состояние бота согласований — docker logs --tail 50 $svc"; fi
  elif [ "$bot_configured" = no ]; then
    if [ "$MODE" = standalone ]; then
      warn approvals-bot "бот согласований не настроен — карточки и кнопки владельцу доставить некому; токен вводит владелец: ./ops/init-env.sh, затем ./ops/up.sh"
    fi
  else
    if [ -n "$bot_problem" ]; then
      fail approvals-bot "бот согласований не работает, код неполадки: $bot_problem — расшифровка: docker exec $svc shturman bot-status"
    elif [ "$bot_polling" = yes ]; then pass approvals-bot "бот согласований на связи с Telegram"
    else warn approvals-bot "бот согласований ещё не начал опрос Telegram — повторите проверку через минуту"; fi
    if [ "$bot_owner" = yes ]; then pass approvals-owner "владелец привязан к боту согласований"
    else warn approvals-owner "владелец не привязан — карточки не отправляются, кнопки не действуют: ./ops/bot-bind.sh"; fi
    if [ "$bot_business" = no ]; then
      warn approvals-business "у бота выключен бизнес-режим — он нужен, только если подключать личные чаты (у @BotFather: Bot Settings → Business Mode)"
    fi
  fi

  if [ "$llm_configured" = no ]; then
    if [ "$MODE" = standalone ]; then
      warn model "своя модель не настроена — обязательства и страницы памяти не разбираются; ключ и имя модели вводит владелец: ./ops/init-env.sh, затем ./ops/up.sh"
    fi
  elif [ "$llm_configured" = yes ]; then
    if [ "$llm_last" = no ] || { [ "$llm_last" = unknown ] && [ -n "$llm_problem" ]; }; then
      warn model "модель $llm_model: последнее обращение не удалось (код: ${llm_problem:-нет}); обращений $llm_calls, неудач $llm_failures"
    elif [ "$llm_last" = yes ]; then pass model "модель $llm_model: последнее обращение успешно; обращений $llm_calls, неудач $llm_failures"
    else pass model "модель $llm_model настроена, обращений ещё не было (первое — при ночной обработке)"; fi
  fi

  emb="$(container_state shturman-embeddings off)"
  case "$emb" in
    running) pass embeddings "поиск по смыслу включён" ;;
    off)     warn embeddings "поиск по смыслу выключен — работает поиск по словам (./ops/embeddings.sh on)" ;;
    *)       fail embeddings "контейнер эмбеддингов в состоянии: $emb" ;;
  esac
elif [ "$MODE" = standalone ]; then
  fail service "сервис переписки не развёрнут — запустите ./ops/up.sh"
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
