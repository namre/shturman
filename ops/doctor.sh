#!/usr/bin/env bash
# Проверка здоровья экземпляра. Ничего не меняет и не читает секретов.
# Вывод: строки "PASS|WARN|FAIL|SKIP  имя: подробности". Код возврата 1, если есть FAIL.
# SKIP — проверка к режиму установки не относится (режим без Hermes, docs/standalone.md).
# Из .env читаются только несекретные строки: режим установки и два адреса — дашборда и страницы
# настройки переписки. По внешним адресам проверка делает по одному запросу GET к странице
# /shturman-setup/ и смотрит только на код ответа и заголовок-метку; на адреса её API не ходит.
# Ещё читается local/plugin.sha256 — отпечаток кода плагина, с которым запущен Hermes (его пишет
# ./ops/up.sh; секретов в нём нет).
# Параметров нет. -h, --help — эта справка; проверки при этом не выполняются.
set -u
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"
ops_no_args "$@"

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

# --- управление прокси ---
# Только проверка соединения из контейнера; конфигурацию Caddy (она может содержать секреты)
# не запрашиваем. Host network делает даже loopback TCP admin доступным агенту.
caddy_tcp="$(hpy -c '
import socket
reachable = False
for address in ("127.0.0.1", "::1"):
    try:
        with socket.create_connection((address, 2019), timeout=1):
            reachable = True
    except OSError:
        pass
print("reachable" if reachable else "closed")' | tail -n 1)"
case "$caddy_tcp" in
  reachable) fail proxy-admin "из Hermes доступен TCP порт 2019 — закройте Caddy admin на защищённый Unix socket (config/Caddyfile.example)" ;;
  closed) pass proxy-admin-tcp "стандартный TCP admin недоступен из Hermes; нестандартные admin endpoints и права Unix socket проверяет оператор" ;;
  *) warn proxy-admin "не удалось проверить доступность TCP admin из Hermes" ;;
esac
if [ -d /run/caddy-admin ]; then
  if [ "$(stat -c '%a' /run/caddy-admin 2>/dev/null)" = 700 ]; then
    pass proxy-admin-dir "каталог Unix socket закрыт режимом 0700; его владелец должен отличаться от пользователя Hermes"
  else
    fail proxy-admin-dir "каталог /run/caddy-admin должен иметь режим 0700 и отдельного владельца Caddy"
  fi
else
  warn proxy-admin-dir "стандартный защищённый каталог сокета не найден; проверьте независимую защиту admin API прокси"
fi

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
# Hermes читает плагин при запуске и держит его в памяти. Если файлы плагина с тех пор изменились
# (репозиторий обновили, а ./ops/up.sh не запускали), работает прежний код. Отпечаток кода,
# с которым Hermes запущен, записывает ./ops/up.sh; до первого его запуска строки нет.
if [ -f "$PLUGIN_MARK" ] && [ "$(head -n 1 "$PLUGIN_MARK")" != "$(plugin_print)" ]; then
  warn plugin-code "файлы плагина shturman изменились после последнего ./ops/up.sh — Hermes исполняет прежний код плагина (прежний мастер и прежний проход к сервису): запустите ./ops/up.sh"
fi
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

owner_bound=no; wizard_done=no; plugin_own_bot=unknown; hermes_business=no
while IFS='=' read -r k v; do
  case "$k" in
    owner_bound) owner_bound="$v" ;; wizard_completed) wizard_done="$v" ;;
    bridge_own_bot) plugin_own_bot="$v" ;; hermes_business) hermes_business="$v" ;;
  esac
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

# --- бизнес-режим Telegram ---
# Бизнес-режим подключается только к боту согласований сервиса (способ «ассистент — отдельный
# сотрудник» на странице настройки переписки); бота-ассистента в бизнес-режиме
# не подключают, официальный плагин бизнес-режима мастер не ставит. На экземпляре, где он уже
# установлен, проверка его не трогает и только сообщает об этом: от сообщений собеседников ядро
# Hermes в любом случае закрывает плагин shturman.
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
  enabled)   pass business-plugin "официальный плагин бизнес-режима на этом экземпляре установлен и включён (так делали до 0.0.6) — проверка его не трогает" ;;
  installed) pass business-plugin "официальный плагин бизнес-режима на этом экземпляре установлен, но не включён — проверка его не трогает" ;;
  *)         pass business-plugin "бизнес-режим Telegram у бота-ассистента не подключён и не нужен" ;;
esac
if [ "$hermes_business" = "yes" ]; then
  warn business-hermes "бот-ассистент сейчас подключён в бизнес-режиме Telegram (схема до 0.0.6). Ничего не сломано; что с этим делать, решает владелец — UPGRADING.md, «0.0.5 → 0.0.6»"
fi

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
      # Со своим ботом сервиса владельца привязывает он (строка approvals-owner ниже), а плагин
      # в Hermes уходит в спокойный режим: не привязывает владельца, не передаёт нажатия и не
      # пересылает бизнес-сообщения. О своём боте плагин узнаёт не сразу — переспрашивает раз в 5 минут.
      case "$st" in
        *'"own_bot":true'*)
          if [ "$plugin_own_bot" = yes ]; then
            pass plugin-own-bot "согласования и бизнес-поток ведёт бот сервиса; плагин в Hermes их не пересылает"
          else
            warn plugin-own-bot "у сервиса свой бот согласований, а плагин в Hermes об этом ещё не знает (шлюз не запущен или не успел спросить) — повторите проверку через 5 минут"
          fi ;;
        *)
          if [ "$plugin_own_bot" = yes ]; then
            warn plugin-own-bot "плагин в Hermes считает, что согласования ведёт бот сервиса, а у сервиса своего бота нет — плагин заметит это в течение 5 минут"
          fi
          case "$st" in
            *'"owner_known":true'*) pass service-owner "сервис знает владельца" ;;
            *) warn service-owner "сервис ещё не знает владельца — карточки с обязательствами не приходят, пока бот-ассистент не привязан в мастере" ;;
          esac ;;
      esac
    fi
  else
    fail archive "сервис не отдал состояние"
  fi

  # --- страница настройки переписки ---
  # Её отдаёт сам сервис по пути /shturman-setup/, за собственным входом и только на ОТДЕЛЬНОМ
  # адресе (другой порт или другое имя): на адресе дашборда ассистент может исполнять свой код
  # и действовал бы на странице от имени вошедшего владельца. Проверяется четыре вещи: страница
  # отвечает локально (setup-page), у неё свой адрес (setup-origin), по нему отвечает именно сервис
  # (setup-page-public), а по адресу дашборда она не отдаётся (setup-isolation).
  # Из состояния сервиса берутся только признаки и числа: ни токенов, ни имён, ни ссылок входа.
  # Запросы идут только на саму страницу (GET …/shturman-setup/): на адреса её API проверка не
  # ходит. Кто ответил, узнаётся по заголовку-метке X-Shturman-Setup-Page, а не по телу ответа.
  # Из .env читаются два несекретных адреса: SHTURMAN_SETUP_URL и SHTURMAN_PUBLIC_URL.
  setup_known=no; setup_enabled=unknown; setup_fields=no; setup_reason=unknown; setup_keys=unknown
  setup_accounts=0; setup_business=unknown
  while IFS='=' read -r k v; do
    case "$k" in
      setup_known) setup_known="$v" ;; setup_enabled) setup_enabled="$v" ;; setup_fields) setup_fields="$v" ;;
      setup_reason) setup_reason="$v" ;; setup_keys) setup_keys="$v" ;; setup_accounts) setup_accounts="$v" ;;
      setup_business) setup_business="$v" ;;
    esac
  done <<EOF4
$(printf '%s' "$st" | docker exec -i "$svc" python -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
s = d.get("setup") if isinstance(d, dict) else None
if not isinstance(s, dict):
    print("setup_known=no")
    sys.exit(0)
yn = lambda v: "yes" if v is True else "no" if v is False else "unknown"
num = lambda v: str(v) if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10**6 else "0"
reason = s.get("reason")
print("setup_known=yes")
print("setup_enabled=" + yn(s.get("enabled")))
# Поля origin и reason появились вместе с отдельным адресом страницы; сервис прежней сборки их не отдаёт.
print("setup_fields=" + ("yes" if "origin" in s or "reason" in s else "no"))
print("setup_reason=" + (reason if reason in ("same_origin", "no_origin") else "none" if reason is None else "unknown"))
print("setup_keys=" + yn(s.get("tg_keys")))
print("setup_accounts=" + num(s.get("accounts")))
print("setup_business=" + yn(s.get("business_connected")))' 2>/dev/null)
EOF4
  word() { case "$1" in yes) printf '%s' "$2" ;; no) printf '%s' "$3" ;; *) printf 'неизвестно' ;; esac; }
  # Один запрос GET <адрес>/shturman-setup/. После вызова: probe_code — код ответа, probe_mark —
  # есть ли заголовок-метка страницы (yes/no), probe_err — код завершения curl. Тело ответа
  # не сохраняется.
  setup_probe() {
    local hdr rc=0
    hdr="$(mktemp)"
    probe_code="$(curl -s -o /dev/null -D "$hdr" -m 8 -w '%{http_code}' "$1/shturman-setup/" 2>/dev/null)" || rc=$?
    probe_err="$rc"
    if tr -d '\r' < "$hdr" | grep -Eqi '^x-shturman-setup-page:[[:space:]]*1[[:space:]]*$'; then probe_mark=yes; else probe_mark=no; fi
    rm -f "$hdr"
  }
  if [ -z "$st" ]; then
    :   # сервис не отдал состояние — об этом уже сказано строкой archive
  elif [ "$setup_known" != "yes" ]; then
    warn setup-page "страницы настройки переписки у этого сервиса нет: это сервис прежней версии — обновите экземпляр (UPGRADING.md) и запустите ./ops/up.sh"
  else
    # Адреса: записанный в .env, тот, с которым сервис запущен на самом деле, и адрес дашборда.
    # Сравниваются целиком — схема, имя и порт: одно имя на разных портах — разные адреса.
    setup_url="$(url_origin "$(env_get SHTURMAN_SETUP_URL)")"
    svc_setup="$(url_origin "$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$svc" 2>/dev/null \
      | sed -n 's/^SHTURMAN_SETUP_ORIGIN=//p' | tail -n 1)")"
    dash_url=""
    if [ "$MODE" = hermes ]; then dash_url="${public_url:-}"; dash_url="${dash_url:-$(env_get SHTURMAN_PUBLIC_URL)}"; fi
    dash_origin="$(url_origin "$dash_url")"
    same=no
    if [ "$setup_reason" = same_origin ]; then same=yes; fi
    if [ -n "$dash_origin" ] && { [ "$setup_url" = "$dash_origin" ] || [ "$svc_setup" = "$dash_origin" ]; }; then same=yes; fi

    # 1. Страница отвечает на локальном адресе, и отвечает именно сервис.
    setup_probe http://127.0.0.1:8765
    setup_marked=no
    if [ "${probe_code:-000}" = "200" ] && [ "$probe_mark" = yes ]; then
      setup_marked=yes
      pass setup-page "страница настройки переписки отвечает на локальном адресе (вход — по одноразовой ссылке: ./ops/setup-link.sh, по просьбе владельца)"
    elif [ "${probe_code:-000}" = "200" ]; then
      warn setup-page "страница настройки переписки отвечает на локальном адресе, но без метки X-Shturman-Setup-Page — это сервис прежней сборки: обновите экземпляр (UPGRADING.md) и запустите ./ops/up.sh. Без метки проверки по внешним адресам не выполняются"
    elif [ "$setup_enabled" != "yes" ] && [ "$same" != yes ]; then
      # При совпавших адресах сервис тоже сообщает enabled=false, но на локальном адресе страницу
      # отдаёт — её отсутствие там уже неполадка (ветка ниже).
      warn setup-page "страница настройки переписки в сервисе не запущена (HTTP ${probe_code:-000} на локальном адресе) — docker logs --tail 50 $svc"
    else
      fail setup-page "страница настройки переписки не отвечает на 127.0.0.1:8765/shturman-setup/ (HTTP ${probe_code:-000}) — docker logs --tail 50 $svc"
    fi

    # 2. У страницы свой адрес — не тот, что у дашборда.
    setup_public=no
    if [ "$same" = yes ]; then
      fail setup-origin "адрес страницы настройки совпал с адресом дашборда — по внешнему адресу сервис её не отдаёт (через туннель SSH она открывается: ./ops/setup-link.sh --local). Нужен отдельный адрес — ./ops/set-setup-url.sh (проще всего то же имя на другом порту), затем ./ops/up.sh. На общем адресе ассистент мог бы действовать на странице от имени владельца"
    elif [ "$MODE" != hermes ]; then
      if [ -z "$setup_url" ] && [ -z "$svc_setup" ]; then
        pass setup-origin "внешнего адреса у страницы настройки нет, как и положено без Hermes: она открывается с сервера и через туннель SSH (./ops/setup-link.sh)"
      else
        warn setup-origin "режим без Hermes, а у страницы настройки задан внешний адрес (${svc_setup:-$setup_url}) — наружу в этом режиме ничего не публикуется: ./ops/set-setup-url.sh --clear, затем ./ops/up.sh"
      fi
    elif [ -z "$setup_url" ] && [ -z "$svc_setup" ]; then
      if [ -z "$dash_origin" ]; then
        warn setup-origin "адрес страницы настройки не задан: сначала адрес дашборда — ./ops/set-public-url.sh (он запишет и адрес страницы). До тех пор страница открывается через туннель SSH: ./ops/setup-link.sh"
      else
        warn setup-origin "адрес страницы настройки не задан: она открывается только через туннель SSH (./ops/setup-link.sh). Чтобы открывать её по кнопке из мастера — ./ops/set-setup-url.sh"
      fi
    elif [ "$setup_url" != "$svc_setup" ]; then
      warn setup-origin "адрес страницы настройки в .env (${setup_url:-не задан}) отличается от того, с которым запущен сервис переписки (${svc_setup:-без адреса}) — запустите ./ops/up.sh"
    elif [ "$setup_fields" != yes ]; then
      warn setup-origin "адрес задан ($svc_setup), но сервис переписки прежней сборки и не сверяет его с адресом дашборда — обновите экземпляр (UPGRADING.md) и запустите ./ops/up.sh"
    else
      setup_public=yes
      pass setup-origin "у страницы настройки свой адрес: $svc_setup (дашборд — ${dash_origin:-не задан})"
    fi

    # 3. По своему адресу страницу отдаёт сервис переписки.
    if [ "$setup_public" = yes ] && [ "$setup_marked" = yes ]; then
      setup_probe "$svc_setup"
      setup_port="$(url_port "$svc_setup")"
      fix_proxy="нужен блок сайта для этого адреса в обратном прокси по образцу config/Caddyfile.example (правка прокси — стоп-точка); до тех пор страница открывается через туннель SSH: ./ops/setup-link.sh --local"
      fix_port="проверьте, что порт $setup_port открыт в firewall сервера и хостинга (стоп-точка) и что в обратном прокси есть блок сайта на этом порту (config/Caddyfile.example); до тех пор страница открывается через туннель SSH: ./ops/setup-link.sh --local"
      case "$probe_err" in
        0)
          case "${probe_code:-000}" in
            200)
              if [ "$probe_mark" = yes ]; then
                pass setup-page-public "страница настройки переписки открывается по своему адресу"
              else
                warn setup-page-public "по адресу страницы настройки отвечает не сервис переписки (HTTP 200 без метки): блок сайта в прокси ведёт не на 127.0.0.1:8765 или перед адресом стоит внешняя защита со своей страницей — $fix_proxy"
              fi ;;
            404) warn setup-page-public "по адресу страницы настройки прокси отвечает «не найдено» (HTTP 404): в его блоке нет пути /shturman-setup/* — $fix_proxy" ;;
            421) warn setup-page-public "сервис переписки не узнаёт этот адрес (HTTP 421): он запущен с другим адресом — ./ops/up.sh; если не помогло, прокси подменяет заголовок Host (он должен доходить как есть, вместе с портом)" ;;
            502|503|504) warn setup-page-public "блок сайта для страницы настройки есть, но сервис переписки прокси не отвечает (HTTP $probe_code) — смотрите строку service и адрес 127.0.0.1:8765 в блоке прокси" ;;
            52[0-9]|530) warn setup-page-public "внешняя защита перед адресом (Cloudflare или подобная) не достучалась до сервера (HTTP $probe_code): $fix_port" ;;
            30[1-8]|401|403) warn setup-page-public "перед адресом страницы настройки стоит внешняя защита или перенаправление (HTTP $probe_code) — проверьте страницу из браузера; защита должна пропускать HTTPS на порт $setup_port" ;;
            *) warn setup-page-public "страница настройки переписки по своему адресу не открывается (HTTP ${probe_code:-000}) — $fix_proxy" ;;
          esac ;;
        6) warn setup-page-public "имя из адреса страницы настройки не находится: записи DNS нет или она ещё не разошлась (запись делает владелец)" ;;
        7) warn setup-page-public "по адресу страницы настройки соединение не принято: порт $setup_port закрыт или обратный прокси его не слушает — $fix_port" ;;
        28) warn setup-page-public "адрес страницы настройки не ответил за 8 секунд: похоже, порт $setup_port закрыт — $fix_port" ;;
        35|51|58|59|60|77|83|90|91) warn setup-page-public "по адресу страницы настройки нет подходящего сертификата (код curl $probe_err): у прокси нет блока сайта для этого адреса или сертификат ещё не получен — $fix_proxy" ;;
        *) warn setup-page-public "адрес страницы настройки не проверен: запрос не удался (код curl $probe_err) — $fix_port" ;;
      esac
    elif [ "$setup_public" = yes ]; then
      warn setup-page-public "не проверено: без метки сервиса его ответ по внешнему адресу не отличить от чужого"
    fi

    # 4. По адресу дашборда страница настройки не отдаётся.
    if [ "$MODE" = hermes ] && [ -n "$dash_origin" ] && [ -n "${public_url:-}" ]; then
      # Запрос идёт в корень адреса дашборда (схема, имя, порт): для браузера «общий адрес» — это
      # они, а путь, если он записан в SHTURMAN_PUBLIC_URL, значения не имеет.
      setup_probe "$dash_origin"
      fix_dash="уберите /shturman-setup/* из блока дашборда в обратном прокси и поставьте на этот путь отказ (config/Caddyfile.example; правка прокси — стоп-точка)"
      if [ "$probe_mark" = yes ]; then
        fail setup-isolation "по адресу дашборда отдаётся страница настройки переписки (HTTP ${probe_code:-000}, метка сервиса) — так быть не должно: $fix_dash. На общем адресе ассистент может действовать на странице от имени владельца"
      elif [ "$probe_err" != 0 ]; then
        warn setup-isolation "не проверено: адрес дашборда не ответил (код curl $probe_err)"
      elif [ "$setup_marked" != yes ] && [ "$setup_fields" != yes ]; then
        warn setup-isolation "не проверено: сервис прежней сборки не ставит метку, и его ответ по адресу дашборда не отличить от чужого (HTTP ${probe_code:-000})"
      elif [ "${probe_code:-000}" = "421" ]; then
        warn setup-isolation "по адресу дашборда запрос к /shturman-setup/ всё ещё доходит до сервиса переписки (он отвечает отказом, HTTP 421): $fix_dash"
      else
        pass setup-isolation "по адресу дашборда страница настройки переписки не отдаётся (HTTP ${probe_code:-000})"
      fi
    fi

    # 5. Что уже настроено: ключи, аккаунты, бизнес-режим, архив.
    setup_line="ключи приложения Telegram: $(word "$setup_keys" "заданы" "не заданы"), аккаунтов Telegram: ${setup_accounts:-0}, сообщений в архиве: $(num messages)"
    setup_messages="$(num messages)"
    if [ "${setup_accounts:-0}" -gt 0 ] || [ "${setup_messages:-0}" -gt 0 ] || [ "$setup_business" = "yes" ]; then
      pass setup-state "$setup_line"
    else
      warn setup-state "$setup_line. Переписка ещё не настроена — это делает владелец на странице настройки переписки: бот согласований, способ подключения, дальше шаги выбранного способа"
    fi
  fi

  # Защита от внедрённых инструкций: включена ли, отвечает ли модель и сколько входящих сообщений
  # ассистент видит непроверенными. Только числа и состояния.
  grd="$(container_state shturman-guard off)"
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

  # --- свой бот согласований и своя модель сервиса ---
  # Только признаки и коды неполадок из GET /api/executor/status: ни токена, ни ключа, ни имён.
  # Бот согласований — первый шаг страницы настройки переписки в обоих режимах: без него запросы
  # ассистента, которые должен подтвердить владелец (принять обязательство, включить чат), отклоняются.
  # Строка model с Hermes появляется, только если своя модель настроена: разбор делает ассистент.
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
    warn approvals-bot "бот согласований не настроен: решения, которые должен подтвердить владелец (принять обязательство, включить чат), не применяются. Это первый шаг страницы настройки переписки; запасной путь — ./ops/init-env.sh (вводит владелец), затем ./ops/up.sh"
  else
    if [ -n "$bot_problem" ]; then
      fail approvals-bot "бот согласований не работает, код неполадки: $bot_problem — расшифровка: docker exec $svc shturman bot-status"
    elif [ "$bot_polling" = yes ]; then pass approvals-bot "бот согласований на связи с Telegram"
    else warn approvals-bot "бот согласований ещё не начал опрос Telegram — повторите проверку через минуту"; fi
    if [ "$bot_owner" = yes ]; then pass approvals-owner "владелец привязан к боту согласований"
    else warn approvals-owner "владелец не привязан к боту согласований — карточки не отправляются, кнопки не действуют: привязка — первый шаг страницы настройки переписки, либо ./ops/bot-bind.sh"; fi
    if [ "$bot_business" = no ] && [ "${setup_business:-}" != yes ]; then
      pass approvals-business "бизнес-режим у бота выключен — он нужен для способа «ассистент — отдельный сотрудник» (у @BotFather: Bot Settings → Secretary Mode)"
    fi
  fi

  if [ "$llm_configured" = no ]; then
    if [ "$MODE" = standalone ]; then
      warn model "своя модель не настроена: архив и поиск работают, а обязательства и страницы памяти не разбираются. Ключ и имя модели вводит владелец — на странице настройки переписки, раздел «Дополнительно», либо в терминале: ./ops/init-env.sh, затем ./ops/up.sh"
    fi
  elif [ "$llm_configured" = yes ]; then
    if [ "$llm_last" = no ] || { [ "$llm_last" = unknown ] && [ -n "$llm_problem" ]; }; then
      warn model "модель $llm_model: последнее обращение не удалось (код: ${llm_problem:-нет}); обращений $llm_calls, неудач $llm_failures"
    elif [ "$llm_last" = yes ]; then pass model "модель $llm_model: последнее обращение успешно; обращений $llm_calls, неудач $llm_failures"
    else pass model "модель $llm_model настроена, обращений ещё не было (первое — при ночной обработке)"; fi
  fi

  # Поиск по смыслу: включён ли, какой моделью и сколько сообщений посчитано. После смены модели
  # (./ops/embeddings.sh on --model) векторы пересчитываются заново — до конца пересчёта поиск
  # по смыслу видит только уже посчитанные сообщения. Только имя модели и числа.
  emb="$(container_state shturman-embeddings off)"
  # Сразу после запуска или смены модели контейнер ещё прогревает модель и не отвечает — это не
  # неполадка. Пока он в состоянии running, ждём до полуминуты и спрашиваем сервис заново.
  for _ in 1 2 3 4 5 6; do
    case "$st" in *'"embeddings_problem":"unreachable"'*) ;; *) break ;; esac
    [ "$emb" = "running" ] || break
    sleep 5
    st="$(docker exec "$svc" shturman call GET /api/status 2>/dev/null | tr -d ' \n')"
    emb="$(container_state shturman-embeddings off)"
  done
  emb_model="$(printf '%s' "$st" | sed -n 's/.*"embeddings_model":"\([A-Za-z0-9._\/-]*\)".*/\1/p')"
  emb_done="$(num embeddings_embedded)"; emb_left="$(num embeddings_left)"
  case "$st" in
    *'"embeddings_enabled":true'*)
      case "$st" in
        *'"embeddings_problem":"model_mismatch"'*)
          fail embeddings "контейнер эмбеддингов отдаёт не ту модель, что записана в настройках (${emb_model:-?}) — векторы не считаются, поиск идёт по словам; запустите ./ops/embeddings.sh on" ;;
        *'"embeddings_problem":"unreachable"'*)
          fail embeddings "сервер эмбеддингов не отвечает (контейнер: $emb) — поиск идёт только по словам; docker logs --tail 50 shturman-embeddings" ;;
        *)
          if [ "$emb" != "running" ]; then
            fail embeddings "контейнер эмбеддингов в состоянии: $emb — запустите ./ops/embeddings.sh on"
          elif [ "${emb_left:-0}" -gt 0 ]; then
            warn embeddings "поиск по смыслу включён, модель ${emb_model:-?}; идёт подсчёт векторов: готово ${emb_done:-0}, осталось ${emb_left} — до конца подсчёта по смыслу ищутся только готовые сообщения, по словам — все"
          else
            pass embeddings "поиск по смыслу включён, модель ${emb_model:-?}; сообщений с вектором: ${emb_done:-0}, осталось посчитать: 0"
          fi ;;
      esac ;;
    "") ;;
    *)
      if [ "$emb" = "off" ]; then
        warn embeddings "поиск по смыслу выключен — работает поиск по словам (./ops/embeddings.sh on)"
      else
        warn embeddings "контейнер эмбеддингов запущен ($emb), но поиск по смыслу в сервисе выключен — запустите ./ops/embeddings.sh on или off"
      fi ;;
  esac

  # Расшифровка голосовых: включена ли, отвечает ли контейнер, сколько ждёт и сколько готово.
  # Только числа и состояние: текстов расшифровок здесь нет.
  asr="$(container_state shturman-asr off)"
  v_pending="$(num voice_pending)"; v_done="$(num voice_done)"; v_failed="$(num voice_failed)"
  case "$st" in
    *'"voice_enabled":true'*)
      if [ "$asr" != "running" ]; then
        fail voice "контейнер распознавания речи в состоянии: $asr — голосовые ждут; docker logs --tail 50 shturman-asr, ./ops/asr.sh on"
      else
        case "$st" in
          *'"voice_problem":"unreachable"'*)
            fail voice "контейнер распознавания речи не отвечает — голосовые ждут (${v_pending:-0}); docker logs --tail 50 shturman-asr" ;;
          *)
            pass voice "расшифровка голосовых включена; готово: ${v_done:-0}, ждут: ${v_pending:-0}, не получилось: ${v_failed:-0}" ;;
        esac
      fi ;;
    "") ;;
    *)
      if [ "$asr" = "off" ]; then
        pass voice "расшифровка голосовых выключена — ассистент видит, что было голосовое, но не его содержание (./ops/asr.sh on, docs/voice.md)"
      else
        warn voice "контейнер распознавания запущен ($asr), но расшифровка в сервисе выключена — запустите ./ops/asr.sh on или off"
      fi ;;
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
