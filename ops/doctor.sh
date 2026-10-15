#!/usr/bin/env bash
# Проверка здоровья экземпляра. Ничего не меняет и не читает секретов.
# Вывод: строки "PASS|WARN|FAIL|SKIP  имя: подробности". Код возврата 1, если есть FAIL.
# SKIP — проверка к режиму установки не относится (режим без Hermes, docs/standalone.md).
# Из .env читаются только несекретные строки: режим установки, часовой пояс (SHTURMAN_TIMEZONE)
# и два адреса — дашборда и страницы настройки переписки. По внешним адресам проверка делает по одному запросу GET к странице
# /shturman-setup/ и смотрит только на код ответа и заголовок-метку; на адреса её API не ходит.
# Если адрес записан IP-адресом (экземпляр без домена), ещё проверяется сертификат прокси на этом
# адресе — сколько ему осталось (строка proxy-cert) — и версия Caddy (proxy-version).
# Ещё читается local/plugin.sha256 — отпечаток кода плагина, с которым запущен Hermes (его пишет
# ./ops/up.sh; секретов в нём нет).
# В режиме с Hermes (Р-76): права контейнера Hermes (строка hermes-net-raw — /proc/1/status в нём)
# и путь прокси к сервису (setup-socket): каталог /run/shturman-service, один GET страницы настройки
# через сокет сервиса (код ответа и метка) и в /etc/caddy/Caddyfile — ходит ли прокси к сервису
# через сокет или через порт 8765. Файл настройки прокси не печатается.
# Если экземпляр подключён к посреднику (./ops/relay.sh, docs/relay.md) — строки relay, relay-proxies,
# relay-reach, relay-addresses: читаются несекретные строки SHTURMAN_RELAY_NAME, _DOMAIN, _SERVER,
# _PORT; о ключе и токене — только «записаны ли»; состояние пересылок — frpc status в контейнере.
# Подключение архива к Hermes (строка archive-mcp, Р-83) проверяется по-настоящему: в контейнере
# Hermes запись mcp_servers.shturman разворачивается штатной функцией Hermes так, как её получает
# шлюз, и с этим заголовком идёт один запрос к 127.0.0.1:8765/mcp — смотрится только код ответа;
# в журнале ошибок Hermes (logs/errors.log) считаются строки отказа 401 архива за последний час —
# печатается только число. Строка jobs-failing — задания модели за час по счётчику сервиса
# (llm_jobs в /api/status: числа и виды ошибок, без текстов). Строка stopped (Р-87) — остановил ли
# владелец ассистента кнопкой «Остановить всё» (WARN, пока остановлено); setup-code-login (Р-88) —
# включён ли вход на страницу по коду из бота и сколько было таких входов за 7 суток, без кодов.
# Строки hermes-tz и cron-schedule (Р-90): по какому поясу шлюз Hermes считает расписание (timezone
# в config.yaml профиля) против ожидаемого (HERMES_TIMEZONE контейнера ← SHTURMAN_TIMEZONE) и стоит ли
# следующий запуск задач shturman:* на их расписании по этому поясу; из задач — только имя, запись
# расписания и время следующего запуска, без текста задачи.
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

# Экземпляр без домена (docs/deployment.md, «Без домена»): адрес дашборда или страницы настройки
# записан внешним IPv4-адресом. Сертификат на IP-адрес Let's Encrypt выдаёт только короткий —
# на 160 часов, чуть больше 6 дней, — и продлевает его сам Caddy, заранее, за 2–3 дня до конца. Для
# каждого такого адреса проверяется сертификат, который отдаёт прокси: запрос идёт без имени (SNI),
# как его шлёт браузер, открывший адрес из цифр. Действителен ли он для этого IP, проходит ли
# проверку и сколько ему осталось: меньше двух суток — WARN, просрочен или не тот — FAIL; сервер
# не достаёт до своего внешнего адреса вовсе (бывает за NAT) — WARN с просьбой проверить из
# браузера. Плюс версия Caddy, если программа есть на сервере: сертификат на IP проверен с Caddy 2.11. Запросы — только рукопожатие TLS
# и один GET в корень адреса; на адреса API страницы настройки проверка не ходит.
#   $1, $2 — адрес дашборда и адрес страницы настройки (пустой или не из IP пропускается).
ip_cert_checks() {
  local u o host port seen="" any=no pem end end_s left left_h code v
  for u in "$@"; do
    o="$(url_origin "$u")"
    case "$o" in https://*) ;; *) continue ;; esac
    host="$(url_host "$o")"; port="$(url_port "$o")"
    case "$(ip_kind "$host")" in
      public) ;;
      private)
        # Строку вписали в .env вручную: скрипты адресов такой адрес не принимают.
        warn proxy-cert "$o — IP-адрес закрытой сети, самого сервера или в необычной записи: снаружи он не откроется, сертификат на него не выдаётся, а мастер не даст на него ссылку. Запишите внешний адрес: ./ops/set-public-url.sh или ./ops/set-setup-url.sh"
        continue ;;
      *) continue ;;
    esac
    case " $seen " in *" $host:$port "*) continue ;; esac
    seen="$seen $host:$port"; any=yes
    if ! command -v openssl >/dev/null 2>&1; then
      warn proxy-cert "$host:$port — нет программы openssl, сертификат на IP-адрес не проверен"
      continue
    fi
    # Сначала — достаёт ли сервер до своего внешнего адреса вообще (соединение TCP). Не достаёт —
    # это не обязательно неполадка: многие хостинги не пускают сервер к его же внешнему адресу
    # через NAT. Тогда проверить можно только из браузера.
    if ! timeout 5 bash -c 'exec 3<> "/dev/tcp/$1/$2"' _ "$host" "$port" 2> /dev/null; then
      warn proxy-cert "$host:$port — сервер не достаёт до своего внешнего адреса (порт закрыт, прокси его не слушает или хостинг не пускает сервер к его же адресу) — проверьте адрес из браузера; если там ошибка — порт $port в firewall сервера и хостинга и journalctl -u caddy"
      continue
    fi
    pem="$(timeout 10 openssl s_client -connect "$host:$port" -noservername < /dev/null 2> /dev/null \
      | openssl x509 2> /dev/null)" || pem=""
    if [ -z "$pem" ]; then
      fail proxy-cert "$host:$port — соединение есть, но прокси не отдал сертификат: сертификат на IP-адрес ещё не получен (journalctl -u caddy; порт 80 должен быть открыт из интернета) или сервер за NAT, а в глобальном блоке прокси нет default_sni (config/Caddyfile.example, вариант «без домена»)"
      continue
    fi
    if ! printf '%s\n' "$pem" | openssl x509 -noout -checkip "$host" 2> /dev/null | grep -q 'does match'; then
      fail proxy-cert "$host:$port — сертификат выдан не на этот IP-адрес: в блоке сайта прокси другой адрес или нет варианта «без домена» (config/Caddyfile.example)"
      continue
    fi
    if ! printf '%s\n' "$pem" | openssl x509 -noout -checkend 0 > /dev/null 2>&1; then
      fail proxy-cert "$host:$port — сертификат на IP-адрес просрочен: Caddy не смог его продлить — проверьте, что порт 80 открыт из интернета и сервер достаёт до Let's Encrypt (journalctl -u caddy)"
      continue
    fi
    code=0; curl -s -o /dev/null -m 8 "https://$host:$port/" 2> /dev/null || code=$?
    case "$code" in
      35|51|58|59|60|77|83|90|91)
        fail proxy-cert "$host:$port — сертификат не проходит проверку (код curl $code): его выдал не общепризнанный центр (например, внутренний центр Caddy) или цепочка неполна — сверьте блоки прокси с вариантом «без домена» (config/Caddyfile.example) и journalctl -u caddy"
        continue ;;
    esac
    # Пороги — средствами openssl (-checkend), без разбора даты; часы — только для показа.
    left=""
    end="$(printf '%s\n' "$pem" | openssl x509 -noout -enddate 2> /dev/null | cut -d= -f2)"
    if end_s="$(date -d "$end" +%s 2> /dev/null)"; then
      left_h=$(( (end_s - $(date +%s)) / 3600 ))
      left="$((left_h / 24)) д $((left_h % 24)) ч"
      [ "$left_h" -ge 24 ] || left="$left_h ч"
    fi
    if ! printf '%s\n' "$pem" | openssl x509 -noout -checkend 172800 > /dev/null 2>&1; then
      warn proxy-cert "$host:$port — сертификату на IP-адрес осталось меньше двух суток${left:+ ($left)}. Caddy обычно продлевает его за 2–3 дня до конца; раз не продлил — проверьте, что порт 80 открыт из интернета и сервер достаёт до Let's Encrypt (journalctl -u caddy)"
    else
      pass proxy-cert "$host:$port — сертификат на IP-адрес действителен${left:+, осталось $left} (живёт 160 ч, продлевает Caddy)"
    fi
  done
  [ "$any" = yes ] || return 0
  if command -v caddy >/dev/null 2>&1; then
    v="$(caddy version 2> /dev/null | awk 'NR == 1 {print $1}')"; v="${v#v}"
    case "$v" in
      2.1[1-9].*|2.[2-9][0-9].*|[3-9].*) pass proxy-version "Caddy $v — сертификаты на IP-адрес с профилем shortlived поддерживаются" ;;
      "") warn proxy-version "версию Caddy узнать не удалось — для адреса из IP нужна 2.11 или новее" ;;
      *) warn proxy-version "Caddy $v — для адреса из IP нужна версия 2.11 или новее (обновление прокси — стоп-точка)" ;;
    esac
  fi
}

if ! MODE="$(shturman_mode)"; then
  fail mode "в .env неизвестный режим — ./ops/mode.sh set hermes или ./ops/mode.sh set standalone"
  echo; echo "ИТОГ: FAIL ($fails)"; exit 1
fi
case "$MODE" in
  hermes)     pass mode "с Hermes" ;;
  standalone) pass mode "без Hermes: только архив и согласования" ;;
esac

# --- часовой пояс владельца (Р-90): несекретная строка SHTURMAN_TIMEZONE в .env ---
# По нему считаются тихие часы, ночной разбор, сроки и напоминания сервиса, а в режиме с Hermes —
# ещё сводки (там его проверяют строки hermes-tz и cron-schedule ниже).
owner_tz="$(env_get SHTURMAN_TIMEZONE)"
if [ -z "$owner_tz" ]; then
  pass timezone "часовой пояс владельца не записан — действует Europe/Moscow по умолчанию (сменить: ./ops/init-env.sh --auto --timezone <пояс>, затем ./ops/up.sh)"
elif tz_valid "$owner_tz"; then
  pass timezone "часовой пояс владельца: $owner_tz (SHTURMAN_TIMEZONE)"
else
  fail timezone "в .env неизвестный часовой пояс SHTURMAN_TIMEZONE=$owner_tz — сервис с ним не запустится, Hermes сочтёт по UTC. Записать заново: ./ops/init-env.sh --auto --timezone Europe/Moscow (или свой), затем ./ops/up.sh"
fi

# --- посредник (./ops/relay.sh, docs/relay.md) ---
# Только если экземпляр подключён: имя, зона и адрес посредника — несекретные строки .env; о ключе
# и токене — только «записаны ли». Состояние пересылок — frpc status внутри контейнера (локальная
# панель frpc с паролем из окружения контейнера); секретов в его выводе нет. Проверка идёт до строк
# Hermes: адреса через посредника проверяются и тогда, когда Hermes не работает.
relay_name="$(env_get SHTURMAN_RELAY_NAME)"
if [ -n "$relay_name" ]; then
  relay_domain="$(env_get SHTURMAN_RELAY_DOMAIN)"
  relay_server="$(env_get SHTURMAN_RELAY_SERVER)"; relay_port="$(env_get SHTURMAN_RELAY_PORT)"
  relay_dash="https://$relay_name.$relay_domain"; relay_setup="https://setup-$relay_name.$relay_domain"
  if [ "$MODE" != hermes ]; then
    warn relay "экземпляр записан подключённым к посреднику ($relay_name), но режим без Hermes — подключение не запускается, наружу ничего не публикуется; забыть подключение — ./ops/relay.sh off --forget"
  elif ! profile_on relay; then
    warn relay "подключение к посреднику выключено (./ops/relay.sh off): по адресам $relay_dash и $relay_setup страницы не открываются; включить — ./ops/relay.sh on, затем ./ops/up.sh"
  else
    rstate="$(container_state shturman-relay)"
    if [ "$rstate" = running ]; then
      pass relay "контейнер frpc работает: имя $relay_name, посредник ${relay_server:-?}:${relay_port:-?}"
    else
      fail relay "контейнер shturman-relay в состоянии: $rstate — ./ops/up.sh; docker logs --tail 50 shturman-relay"
    fi
    if ! grep -Eq '^SHTURMAN_RELAY_KEY=.+' .env || ! grep -Eq '^SHTURMAN_RELAY_TOKEN=.+' .env; then
      fail relay-key "в .env нет ключа подключения или токена посредника — подключение заново: ./ops/relay.sh off --forget, затем join с новым кодом"
    fi
    if [ ! -s data/relay/ca.pem ]; then
      fail relay-ca "нет сертификата посредника (data/relay/ca.pem): frpc не сможет проверить посредника — ./ops/relay.sh on, затем ./ops/up.sh"
    fi
    if [ "$rstate" = running ]; then
      rstatus="$(docker exec shturman-relay /frpc status -c /etc/frpc/frpc.toml 2>/dev/null || true)"
      running=0; bad=""
      for p in dashboard-https dashboard-http setup-https setup-http; do
        s="$(printf '%s\n' "$rstatus" | awk -v p="$p" '$1 == p {print $2; exit}')"
        if [ "$s" = running ]; then running=$((running + 1)); else bad="${bad:+$bad, }$p: ${s:-нет}"; fi
      done
      if [ "$running" -eq 4 ]; then
        pass relay-proxies "пересылки дашборда и страницы настройки работают (4 из 4)"
      elif [ -z "$rstatus" ]; then
        fail relay-proxies "frpc не отдал состояние пересылок — docker logs --tail 50 shturman-relay"
      else
        fail relay-proxies "работают $running из 4 ($bad): посредник не принял подключение или имена — docker logs --tail 50 shturman-relay (там причина отказа посредника)"
      fi
    fi
    if [ -n "$relay_server" ] && timeout 5 bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$relay_server" "${relay_port:-7000}" 2>/dev/null; then
      pass relay-reach "посредник $relay_server:${relay_port:-7000} принимает соединения"
    else
      warn relay-reach "посредник ${relay_server:-?}:${relay_port:-7000} не принимает соединение за 5 секунд: сеть сервера закрывает этот порт или посредник недоступен (docs/relay.md, «Риски»)"
    fi
    if [ "$(url_origin "$(env_get SHTURMAN_PUBLIC_URL)")" = "$relay_dash" ] && [ "$(url_origin "$(env_get SHTURMAN_SETUP_URL)")" = "$relay_setup" ]; then
      pass relay-addresses "адреса экземпляра — имена посредника: $relay_dash и $relay_setup"
    else
      warn relay-addresses "адреса экземпляра не совпадают с именами посредника ($relay_dash, $relay_setup) — вернуть: ./ops/relay.sh on; или выключить посредника: ./ops/relay.sh off"
    fi
  fi
fi

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

# --- права контейнера Hermes (Р-76) ---
# Контейнер работает в сети хоста. С правом CAP_NET_RAW процесс root в нём открывает сырой сокет
# и читает весь трафик сервера на 127.0.0.1 — в том числе то, что владелец вводит на странице
# настройки. compose.yaml это право убирает (cap_drop: [NET_RAW]). Смотрим ограничивающий набор
# прав (CapBnd) процесса 1 в контейнере: его наследуют все процессы, и вернуть право оттуда нельзя
# ни сменой пользователя, ни программой с file capabilities. Бит 13 — CAP_NET_RAW.
capbnd="$(docker exec -u "$(id -u):$(id -g)" "$c" cat /proc/1/status 2>/dev/null | awk '$1 == "CapBnd:" {print $2}')"
case "$capbnd" in
  ""|*[!0-9a-fA-F]*)
    warn hermes-net-raw "права контейнера Hermes прочитать не удалось — не проверено, убрано ли право CAP_NET_RAW" ;;
  *)
    if [ $(( (0x$capbnd >> 13) & 1 )) -eq 1 ]; then
      fail hermes-net-raw "у контейнера Hermes есть право CAP_NET_RAW: процесс в нём может читать трафик сервера на 127.0.0.1, в том числе ввод на странице настройки переписки. В compose.yaml у hermes — cap_drop: [NET_RAW] (Р-76): проверьте, не возвращает ли его compose.override.yaml, и запустите ./ops/up.sh"
    else
      pass hermes-net-raw "у контейнера Hermes нет права CAP_NET_RAW: сырые сокеты ему недоступны, трафик сервера он не читает"
    fi ;;
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
  # Без домена: адрес записан IP-адресом, сертификат на него короткий (6 дней), его продлевает Caddy.
  ip_cert_checks "$public_url" "$(env_get SHTURMAN_SETUP_URL)"
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

# --- часовой пояс шлюза и расписание сводок (Р-90) ---
# Шлюз Hermes, обслуживая несколько профилей, считает расписание по `timezone` из config.yaml
# профиля, а не по HERMES_TIMEZONE контейнера; без него — по часам сервера (UTC). hermes-tz сверяет
# одно с другим; cron-schedule — что у задач shturman:* следующий запуск посчитан по поясу шлюза
# и лежит на их расписании — по поясу из окружения (SHTURMAN_TIMEZONE), а если его нет, по поясу
# шлюза. Из задач печатаются только имя, запись расписания и время запуска.
tab="$(printf '\t')"
tz_started="$(date -d "$(docker inspect -f '{{.State.StartedAt}}' "$c" 2>/dev/null)" +%s 2>/dev/null || true)"
tz_state=unknown; tz_want=""; tz_have=""; tz_zone=""; tz_pending=no; tz_jobs=""; tz_listed=no
while IFS="$tab" read -r k a b c d; do
  case "$k" in
    tz) tz_state="$a"; tz_want="$b"; tz_have="$c"; tz_zone="$d" ;;
    pending) tz_pending="$a" ;;
    job) tz_jobs="$tz_jobs$a$tab$b$tab$c$tab$d"$'\n' ;;
    listed) tz_listed=yes ;;
  esac
done <<EOF_TZ
$(hpy -c '
import os, sys
sys.path.insert(0, "/opt/data/plugins/shturman")
from shturman_core import cron_jobs
from hermes_cli.config import read_raw_config
want = (os.environ.get("HERMES_TIMEZONE") or "").strip()
have = cron_jobs.config_zone(read_raw_config())
zone = cron_jobs.expected_zone(want, have)
print("tz", cron_jobs.zone_state(want, have), want or "-", have or "-", zone or "-", sep="\t")
started = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
try:
    from shturman_core.state import Store
    written = Store().read(cron_jobs.TZ_STATE).get("written_at")
except Exception:
    written = None
print("pending", "yes" if cron_jobs.restart_pending(written, started) else "no", sep="\t")
from cron.jobs import list_jobs
for job in cron_jobs.ours(list_jobs(include_disabled=True)):
    expr = str((job.get("schedule") or {}).get("expr") or "-")
    print("job", job.get("name"), cron_jobs.schedule_drift(job, zone), expr,
          cron_jobs.local_run(job.get("next_run_at"), zone) or "-", sep="\t")
print("listed")' "$tz_started")
EOF_TZ
[ "$tz_want" = - ] && tz_want=""
[ "$tz_have" = - ] && tz_have=""
[ "$tz_zone" = - ] && tz_zone=""
case "$tz_state:$tz_pending" in
  same:yes|none:yes) warn hermes-tz "пояс $tz_have записан в config.yaml после запуска Hermes (это делает плагин, когда заводит сводки), а шлюз читает его только при запуске — пока считает по прежнему. ./ops/up.sh перезапустит Hermes" ;;
  same:*) pass hermes-tz "шлюз Hermes считает расписание по поясу $tz_have (timezone в config.yaml совпадает с SHTURMAN_TIMEZONE)" ;;
  none:*)
    if [ -n "$tz_have" ]; then pass hermes-tz "шлюз Hermes считает расписание по поясу $tz_have (timezone в config.yaml; в окружении пояса нет)"
    else warn hermes-tz "часовой пояс не задан ни в окружении Hermes, ни в его config.yaml — расписание по часам сервера (обычно UTC). Задайте SHTURMAN_TIMEZONE в .env (docs/setup.md) и запустите ./ops/up.sh"; fi ;;
  missing:*) fail hermes-tz "шлюз Hermes считает расписание по часам сервера (UTC): в config.yaml профиля нет timezone, а переменную HERMES_TIMEZONE=$tz_want шлюз при нескольких профилях не читает — сводка придёт не в то время. ./ops/up.sh запишет пояс и перезапустит Hermes" ;;
  differs:*) fail hermes-tz "шлюз Hermes считает расписание по поясу $tz_have, а ожидается $tz_want (SHTURMAN_TIMEZONE) — сводка придёт не в то время. ./ops/up.sh запишет пояс и перезапустит Hermes" ;;
  invalid:*) fail hermes-tz "в окружении Hermes неизвестный часовой пояс «$tz_want» — исправьте SHTURMAN_TIMEZONE в .env (вид Europe/Moscow) и запустите ./ops/up.sh" ;;
  *) warn hermes-tz "часовой пояс шлюза Hermes не проверен: в этой версии Hermes нет нужных функций либо контейнер не ответил — docker exec shturman-hermes hermes config show | grep timezone" ;;
esac
tz_ok=""; tz_bad=""
while IFS="$tab" read -r name drift expr at; do
  [ -n "$name" ] || continue
  case "$name" in shturman:morning-brief) title="утренняя сводка" ;; shturman:weekly-review) title="обзор недели" ;; *) title="$name" ;; esac
  case "$drift" in
    ok) tz_ok="${tz_ok:+$tz_ok; }$title — $at" ;;
    paused) tz_ok="${tz_ok:+$tz_ok; }$title — выключена" ;;
    unchecked) tz_ok="${tz_ok:+$tz_ok; }$title — расписание «$expr» не проверено" ;;
    offset) tz_bad="${tz_bad:+$tz_bad; }$name ($expr): следующий запуск $at по поясу ${tz_zone:-сервера} — посчитан по другому поясу" ;;
    off-schedule) tz_bad="${tz_bad:+$tz_bad; }$name ($expr): следующий запуск $at по поясу ${tz_zone:-сервера} — не по расписанию" ;;
    no-next) tz_bad="${tz_bad:+$tz_bad; }$name ($expr): включена, но следующего запуска нет" ;;
  esac
done <<EOF_JOBS
$tz_jobs
EOF_JOBS
if [ -n "$tz_bad" ]; then
  warn cron-schedule "$tz_bad. ./ops/up.sh пересчитает расписание (hermes cron edit <id> --schedule <та же запись>)"
elif [ -n "$tz_ok" ]; then
  pass cron-schedule "следующий запуск по поясу ${tz_zone:-сервера (UTC)}: $tz_ok"
elif [ "$tz_listed" != yes ]; then
  warn cron-schedule "расписание задач shturman:* не проверено: список задач Hermes прочитать не удалось — docker exec shturman-hermes hermes cron list --all"
else
  pass cron-schedule "задач сводок shturman:* в Hermes нет — их заводит плагин (POST /api/plugins/shturman/cron/install)"
fi

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
    # Настоящее подключение, а не только запись в настройках (Р-83). В контейнере Hermes запись
    # mcp_servers.shturman разворачивается штатной функцией Hermes в самом строгом виде, в каком
    # её получает шлюз: внутри области секретов профиля и с включённым мультиплексом — тогда
    # переменная берётся только из .env профиля. С получившимся заголовком идёт один запрос POST
    # к 127.0.0.1:8765/mcp; смотрится только код ответа (401 — токен не тот). Плюс в журнале ошибок
    # Hermes (logs/errors.log) считаются строки отказа 401 архива «shturman» за последний час,
    # но не раньше запуска контейнера, — только число. Значение токена нигде не печатается.
    mcp_started="$(docker inspect -f '{{.State.StartedAt}}' "$c" 2>/dev/null || true)"
    mcp_started_s="$(date -d "$mcp_started" +%s 2>/dev/null || echo 0)"
    mcp_probe=unknown; mcp_code=""; mcp_rejects=0; mcp_served=unknown
    while IFS='=' read -r k v; do
      case "$k" in
        probe) mcp_probe="$v" ;; code) mcp_code="$v" ;; rejects) mcp_rejects="$v" ;; served) mcp_served="$v" ;;
      esac
    done <<EOF5
$(hpy -c '
import sys, time
from datetime import datetime
started = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0.0
try:
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
except Exception:
    print("probe=unsupported")
    sys.exit(0)
try:
    from hermes_cli.gateway_multiplex_served import recorded_served_profiles
    served = recorded_served_profiles()
    print("served=" + ("unknown" if served is None else str(len(served))))
except Exception:
    pass
since, rejects = max(time.time() - 3600, started), 0
for name in ("errors.log.1", "errors.log"):
    try:
        fh = open(home / "logs" / name, encoding="utf-8", errors="replace")
    except OSError:
        continue
    with fh:
        for line in fh:
            if "MCP server " not in line or "shturman" not in line or "401" not in line:
                continue
            try:
                at = time.mktime(datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timetuple())
            except ValueError:
                continue
            rejects += at >= since
print("rejects=%d" % rejects)
try:
    from hermes_cli.config import read_raw_config
    from agent.secret_scope import build_profile_secret_scope, set_multiplex_active, set_secret_scope
    from tools.mcp_tool_config import _interpolate_env_vars
except Exception:
    print("probe=unsupported")
    sys.exit(0)
raw = ((read_raw_config() or {}).get("mcp_servers") or {}).get("shturman")
if not isinstance(raw, dict) or not str(raw.get("url", "")).endswith(":8765/mcp"):
    print("probe=absent")
    sys.exit(0)
set_multiplex_active(True)
set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
cfg = _interpolate_env_vars(raw)
headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
if any("${" in v for v in headers.values()):
    print("probe=unresolved")
    sys.exit(0)
import urllib.error, urllib.request
headers.update({"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(urllib.request.Request(str(cfg["url"]), data=b"{}", headers=headers, method="POST"), timeout=5) as r:
        code = r.status
except urllib.error.HTTPError as exc:
    code = exc.code
except Exception:
    code = 0
print("probe=done")
print("code=%d" % code)' "$mcp_started_s")
EOF5
    case "$mcp_served" in ''|unknown|0|1) mcp_mux="" ;; *) mcp_mux=" (шлюз обслуживает профилей: $mcp_served)" ;; esac
    case "$mcp_probe:$mcp_code" in
      absent:*) fail archive-mcp "архив не подключён к Hermes — запустите ./ops/up.sh" ;;
      unresolved:*) fail archive-mcp "Hermes не видит токен архива: в .env профиля Hermes нет MCP_SHTURMAN_API_KEY, и шлюз шлёт архиву пустую подстановку$mcp_mux — ./ops/up.sh" ;;
      done:401|done:403) fail archive-mcp "архив отказал Hermes (HTTP $mcp_code): токен в .env профиля Hermes не совпадает с токеном сервиса — ./ops/up.sh" ;;
      done:0) fail archive-mcp "архив не отвечает на 127.0.0.1:8765/mcp" ;;
      done:*)
        if [ "${mcp_rejects:-0}" -gt 0 ] 2>/dev/null; then
          fail archive-mcp "токен виден, но шлюз Hermes за последний час получил от архива отказ 401 ($mcp_rejects строк в журнале ошибок Hermes)$mcp_mux: шлюз запущен с прежним токеном — ./ops/up.sh (перезапустит Hermes)"
        else
          pass archive-mcp "архив подключён к Hermes: токен виден в области профиля, архив его принимает$mcp_mux"
        fi ;;
      *) warn archive-mcp "подключение архива к Hermes не проверено: в этой версии Hermes нет нужных функций либо контейнер не ответил — проверьте вручную: docker exec shturman-hermes hermes mcp test shturman" ;;
    esac
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
    # Задания модели за последний час (Р-83): сколько попыток не удалось и чаще всего почему —
    # объект llm_jobs ответа /api/status (только числа и виды ошибок: имя класса или короткий код).
    # Не меньше половины попыток (и хотя бы 10) не удались — FAIL; не меньше пятой части (и хотя
    # бы 5) — WARN. Сервис прежней версии этого объекта не отдаёт — строки нет.
    lj="$(printf '%s' "$st" | docker exec -i "$svc" python -c '
import json, re, sys
try:
    j = json.load(sys.stdin).get("llm_jobs")
except Exception:
    sys.exit(0)
if not isinstance(j, dict):
    sys.exit(0)
num = lambda v: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0
top = []
for item in (j.get("errors") or [])[:3]:
    if isinstance(item, list) and len(item) == 2 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_:.]{0,80}", str(item[0])):
        top.append("%s ×%d" % (item[0], num(item[1])))
print("%d %d %d %s" % (num(j.get("done")), num(j.get("failed")), num(j.get("window_s")) // 60, ", ".join(top)))' 2>/dev/null)"
    if [ -n "$lj" ]; then
      read -r lj_done lj_failed lj_min lj_top <<EOF6
$lj
EOF6
      lj_total=$((lj_done + lj_failed))
      lj_where="за последние $lj_min мин"
      [ "$lj_min" -ge 60 ] && lj_where="за последний час"
      if [ "$lj_total" -eq 0 ]; then
        pass jobs-failing "$lj_where заданий модели не было"
      else
        lj_pct=$((lj_failed * 100 / lj_total))
        lj_msg="$lj_where заданий модели: $lj_total, не удалось: $lj_failed ($lj_pct%)${lj_top:+; чаще всего: $lj_top}"
        if [ "$MODE" = hermes ]; then lj_hint="причина — в журнале Hermes: docker exec $c grep -a 'shturman.executor' /opt/data/logs/errors.log | tail -n 5"
        else lj_hint="состояние своей модели — строка model ниже и docker exec $svc shturman bot-status"; fi
        if [ "$lj_failed" -ge 10 ] && [ "$lj_pct" -ge 50 ]; then fail jobs-failing "$lj_msg — $lj_hint"
        elif [ "$lj_failed" -ge 5 ] && [ "$lj_pct" -ge 20 ]; then warn jobs-failing "$lj_msg — $lj_hint"
        else pass jobs-failing "$lj_msg"; fi
      fi
    fi
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
    # «Остановить всё» (Р-87): остановлен ли ассистент, с какого времени и откуда. Только признаки
    # и время. Продолжить может только владелец — нажатием в боте согласований.
    hl="$(printf '%s' "$st" | docker exec -i "$svc" python -c '
import json, re, sys
try:
    h = json.load(sys.stdin).get("stopped")
except Exception:
    sys.exit(0)
if not isinstance(h, dict):
    sys.exit(0)
since = h.get("since") if isinstance(h.get("since"), str) else ""
since = since[:16].replace("T", " ") if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d[0-9:.+\-Z]*", since) else "?"
via = h.get("via") if h.get("via") in ("page", "app", "bot") else "?"
n = h.get("reading_paused")
n = n if isinstance(n, int) and not isinstance(n, bool) and 0 <= n < 1000 else 0
print("%s %s %s %d" % ("yes" if h.get("stopped") is True else "no", since.replace(" ", "_"), via, n))' 2>/dev/null)"
    if [ -n "$hl" ]; then
      read -r hl_on hl_since hl_via hl_reading <<EOF7
$hl
EOF7
      case "$hl_via" in page) hl_where="в «Настройках Штурмана»" ;; app) hl_where="в приложении «Штурман»" ;;
        bot) hl_where="в боте согласований" ;; *) hl_where="" ;; esac
      if [ "$hl_on" = yes ]; then
        hl_read="переписка по-прежнему собирается"
        [ "${hl_reading:-0}" -gt 0 ] && hl_read="чтение переписки тоже на паузе (аккаунтов: $hl_reading)"
        warn stopped "владелец остановил ассистента (с ${hl_since/_/ } UTC${hl_where:+, $hl_where}): ответы, вопросы, черновики, репетиция, наблюдатель и отправка стоят, уведомления приходят; $hl_read. Продолжить может только владелец — нажатием в боте согласований (/resume)"
      else
        pass stopped "ассистент не остановлен владельцем"
      fi
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
  setup_accounts=0; setup_business=unknown; setup_socket=absent
  setup_code=absent; setup_code_reason=none; setup_code_logins=0
  while IFS='=' read -r k v; do
    case "$k" in
      setup_known) setup_known="$v" ;; setup_enabled) setup_enabled="$v" ;; setup_fields) setup_fields="$v" ;;
      setup_reason) setup_reason="$v" ;; setup_keys) setup_keys="$v" ;; setup_accounts) setup_accounts="$v" ;;
      setup_business) setup_business="$v" ;; setup_socket) setup_socket="$v" ;;
      setup_code) setup_code="$v" ;; setup_code_reason) setup_code_reason="$v" ;; setup_code_logins) setup_code_logins="$v" ;;
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
print("setup_business=" + yn(s.get("business_connected")))
# Сокет для обратного прокси (Р-76); сервис прежней сборки этого поля не отдаёт.
sock = s.get("socket")
codes = ("listening", "off", "dir_missing", "dir_owner", "dir_mode", "not_socket", "error")
print("setup_socket=" + (sock if sock in codes else "unknown" if "socket" in s else "absent"))
# Вход по коду из бота (Р-88): включён ли, почему нет и сколько входов за 7 суток — без кодов и адресов.
c = s.get("code_login")
if isinstance(c, dict):
    print("setup_code=" + yn(c.get("enabled")))
    r = c.get("reason")
    print("setup_code_reason=" + (r if r in ("no_bot", "no_owner", "managed", "off", "no_socket") else "none"))
    print("setup_code_logins=" + num(c.get("logins_7d")))' 2>/dev/null)
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
                warn setup-page-public "по адресу страницы настройки отвечает не сервис переписки (HTTP 200 без метки): блок сайта в прокси ведёт не к сервису (сокет /run/shturman-service/proxy.sock) или перед адресом стоит внешняя защита со своей страницей — $fix_proxy"
              fi ;;
            404) warn setup-page-public "по адресу страницы настройки прокси отвечает «не найдено» (HTTP 404): в его блоке нет пути /shturman-setup/* — $fix_proxy" ;;
            421) warn setup-page-public "сервис переписки не узнаёт этот адрес (HTTP 421): он запущен с другим адресом — ./ops/up.sh; если не помогло, прокси подменяет заголовок Host (он должен доходить как есть, вместе с портом)" ;;
            502|503|504) warn setup-page-public "блок сайта для страницы настройки есть, но сервис переписки прокси не отвечает (HTTP $probe_code) — смотрите строки service и setup-socket и путь к сервису в блоке прокси" ;;
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

    # 5. Прокси ходит к сервису через Unix-сокет, а не через порт 8765 (Р-76). Порт, пока сервис
    # перезапускается, может занять процесс из Hermes (сеть хоста); каталога сокета в файловой
    # системе Hermes нет. Проверяется: каталог (владелец, группа caddy, режим 2710), сокет сервиса
    # (один GET страницы через сокет — код ответа и метка, как в setup-page) и путь к сервису
    # в файле настройки прокси /etc/caddy/Caddyfile (он читается всеми; комментарии пропускаются,
    # значения не печатаются). Только в режиме с Hermes: без него прокси нет.
    if [ "$MODE" = hermes ]; then
      sock_dir=/run/shturman-service; sock="$sock_dir/proxy.sock"; caddyfile=/etc/caddy/Caddyfile
      sock_want="$(id -un):caddy 2710"
      fix_dir="каталог создаёт systemd-tmpfiles по config/shturman-service-socket.conf (стоп-точка, sudo: docs/runbooks/deploy.md, шаг 5б; на работающем экземпляре — UPGRADING.md), затем ./ops/up.sh"
      fix_caddy="в блоке страницы настройки обратного прокси — reverse_proxy unix/$sock вместо 127.0.0.1:8765, как в config/Caddyfile.example (правка прокси — стоп-точка)"
      caddy_way=unknown
      if [ -r "$caddyfile" ]; then
        caddy_text="$(grep -Ev '^[[:space:]]*#' "$caddyfile" 2>/dev/null || true)"
        caddy_way=none
        if grep -Eq ':8765([^0-9]|$)' <<< "$caddy_text"; then caddy_way=tcp
        elif grep -Eq "reverse_proxy[[:space:]]+unix/$sock([[:space:]]|$)" <<< "$caddy_text"; then caddy_way=socket; fi
        caddy_text=""
      fi
      # Неполадка сокета — FAIL, если прокси уже настроен на сокет (страница не откроется), иначе WARN.
      sock_bad() { if [ "$caddy_way" = socket ]; then fail setup-socket "$1"; else warn setup-socket "$1"; fi; }
      sock_got="$(stat -c '%U:%G %a' "$sock_dir" 2>/dev/null || true)"
      if [ ! -d "$sock_dir" ] && [ "$caddy_way" = socket ]; then
        fail setup-socket "прокси настроен ходить к сервису через сокет, а каталога сокета $sock_dir нет — страница настройки не откроется: $fix_dir"
      elif [ ! -d "$sock_dir" ]; then
        warn setup-socket "каталога сокета $sock_dir нет: прокси ходит к сервису по порту 127.0.0.1:8765, а его, пока сервис перезапускается, может занять процесс из Hermes (Р-76). Сначала $fix_dir; затем $fix_caddy"
      elif [ "${sock_got%%:*}" != "$(id -un)" ]; then
        # Обычно это пустой каталог, который Docker создал от root, пока systemd-tmpfiles его не создавал:
        # сервис им не пользуется и остаётся на порту.
        sock_bad "каталог сокета $sock_dir не такой, как нужно ($sock_got вместо $sock_want; так бывает, если его создал Docker от root, — сервис им не пользуется): $fix_dir"
      elif case "$sock_got" in *[1-7]) true ;; *) false ;; esac; then
        fail setup-socket "каталог сокета $sock_dir открыт другим пользователям ($sock_got вместо $sock_want): $fix_dir"
      elif [ "$sock_got" != "$sock_want" ]; then
        sock_bad "каталог сокета $sock_dir не такой, как нужно ($sock_got вместо $sock_want): $fix_dir"
      else
        case "$setup_socket" in
          listening)
            sock_host="127.0.0.1:8765"
            # Имя узла — как его передаёт прокси: внешний адрес страницы без схемы (обычный порт не пишется).
            if [ "$setup_public" = yes ]; then sock_host="${svc_setup#*://}"; fi
            hdr="$(mktemp)"
            sock_code="$(curl -s -o /dev/null -D "$hdr" -m 8 -w '%{http_code}' --unix-socket "$sock" -H "Host: $sock_host" http://localhost/shturman-setup/ 2>/dev/null)" || true
            if tr -d '\r' < "$hdr" | grep -Eqi '^x-shturman-setup-page:[[:space:]]*1[[:space:]]*$'; then sock_mark=yes; else sock_mark=no; fi
            rm -f "$hdr"
            sock_perm="$(stat -c '%G %a' "$sock" 2>/dev/null || true)"
            if [ "${sock_code:-000}" != 200 ] || [ "$sock_mark" != yes ]; then
              sock_bad "сервис открыл сокет, но страница настройки через него не отвечает (HTTP ${sock_code:-000}) — docker logs --tail 50 $svc"
            elif [ "$sock_perm" != "caddy 660" ]; then
              sock_bad "сокет $sock с правами «${sock_perm:-нет}» вместо «caddy 660» — прокси не сможет к нему подключиться: $fix_dir"
            else
              case "$caddy_way" in
                socket) pass setup-socket "обратный прокси ходит к сервису через сокет $sock (каталог $sock_got); каталога нет в контейнере Hermes, порт 8765 прокси не нужен" ;;
                tcp) warn setup-socket "сокет сервиса готов, а обратный прокси ещё ходит к сервису по порту 8765 — его, пока сервис перезапускается, может занять процесс из Hermes: $fix_caddy; затем перечитать настройку прокси" ;;
                none) warn setup-socket "сокет сервиса готов, но в $caddyfile нет пути к сервису ни через сокет, ни через порт 8765 — проверьте блок страницы настройки (config/Caddyfile.example)" ;;
                *) warn setup-socket "сокет сервиса готов; файл настройки прокси $caddyfile прочитать не удалось — проверьте, что в блоке страницы настройки reverse_proxy unix/$sock" ;;
              esac
            fi ;;
          absent) sock_bad "сервис переписки прежней сборки не открывает сокет для прокси — ./ops/up.sh" ;;
          off) sock_bad "сервис переписки запущен без SHTURMAN_PROXY_SOCKET — ./ops/up.sh (compose.yaml задаёт его сам); если не помогло, проверьте compose.override.yaml" ;;
          dir_missing|dir_owner|dir_mode)
            sock_bad "каталог сокета теперь в порядке, но сервис переписки запущен, когда он ещё не был готов ($setup_socket) — ./ops/up.sh перезапустит сервис, и он откроет сокет" ;;
          *) sock_bad "сервис переписки не открыл сокет ($setup_socket) — docker logs --tail 50 $svc" ;;
        esac
      fi
    fi

    # 6. Что уже настроено: ключи, аккаунты, бизнес-режим, архив.
    setup_line="ключи приложения Telegram: $(word "$setup_keys" "заданы" "не заданы"), аккаунтов Telegram: ${setup_accounts:-0}, сообщений в архиве: $(num messages)"
    setup_messages="$(num messages)"
    if [ "${setup_accounts:-0}" -gt 0 ] || [ "${setup_messages:-0}" -gt 0 ] || [ "$setup_business" = "yes" ]; then
      pass setup-state "$setup_line"
    else
      warn setup-state "$setup_line. Переписка ещё не настроена — это делает владелец на странице настройки переписки: бот согласований, способ подключения, дальше шаги выбранного способа"
    fi

    # 7. Вход на страницу по коду из бота (Р-88). Кодов и адресов здесь нет — только состояние и число входов.
    code_logins="входов по коду за 7 суток: ${setup_code_logins:-0}"
    case "$setup_code:$setup_code_reason" in
      absent:*) ;;   # сервис прежней сборки — входа по коду у него нет
      yes:*) pass setup-code-login "вход по коду из бота включён; $code_logins (вход по ссылке — ./ops/setup-link.sh, по просьбе владельца)" ;;
      *:managed) pass setup-code-login "вход по коду из бота выключен: бот согласований заведён посредником (так по умолчанию). Включает владелец в «Настройках Штурмана» и подтверждает в боте; $code_logins" ;;
      *:off) pass setup-code-login "вход по коду из бота выключил владелец; $code_logins" ;;
      *:no_bot|*:no_owner) pass setup-code-login "вход по коду из бота появится после настройки бота согласований и привязки владельца; пока вход — по ссылке ./ops/setup-link.sh" ;;
      *:no_socket) warn setup-code-login "вход по коду из бота не работает: с Hermes он принимается только через сокет обратного прокси, а сокета нет — смотрите строку setup-socket; пока вход — по ссылке ./ops/setup-link.sh" ;;
      *) warn setup-code-login "состояние входа по коду из бота неизвестно; $code_logins" ;;
    esac
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
  bot_configured=unknown; bot_polling=unknown; bot_problem=""; bot_owner=unknown; bot_business=unknown; bot_menu=""; bot_via=""
  llm_configured=unknown; llm_model=""; llm_last=unknown; llm_problem=""; llm_calls=0; llm_failures=0
  llm_way=""; llm_sub=""
  while IFS='=' read -r k v; do
    case "$k" in
      bot_configured) bot_configured="$v" ;; bot_polling) bot_polling="$v" ;; bot_problem) bot_problem="$v" ;;
      bot_owner) bot_owner="$v" ;; bot_business) bot_business="$v" ;; bot_menu) bot_menu="$v" ;; bot_via) bot_via="$v" ;;
      llm_configured) llm_configured="$v" ;; llm_model) llm_model="$v" ;; llm_last) llm_last="$v" ;;
      llm_problem) llm_problem="$v" ;; llm_calls) llm_calls="$v" ;; llm_failures) llm_failures="$v" ;;
      llm_way) llm_way="$v" ;; llm_sub) llm_sub="$v" ;;
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
print("bot_menu=" + safe(b.get("menu_button")))
print("bot_via=" + safe(b.get("created_via")))
print("llm_configured=" + yn(m.get("configured")))
print("llm_model=" + safe(m.get("model")))
print("llm_last=" + yn(m.get("last_call_ok")))
print("llm_problem=" + safe(m.get("problem")))
print("llm_calls=" + num(m.get("calls")))
print("llm_failures=" + num(m.get("failures")))
print("llm_way=" + safe(m.get("way")))
print("llm_sub=" + safe(m.get("subscription")))' 2>/dev/null)
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
    # Как создан бот (Р-77). В одно нажатие — выбор владельца, но он должен его видеть: держатель
    # посредника (бот-менеджера) технически может получить токен этого бота.
    case "$bot_via" in
      managed) pass approvals-origin "бот согласований создан в одно нажатие через бот-менеджер посредника: его держатель технически может получить токен бота. Перейти на своего бота — docs/approvals-bot.md, «Перейти на своего бота»" ;;
      manual) pass approvals-origin "бот согласований создан владельцем у @BotFather" ;;
    esac
    if [ "$bot_owner" = yes ]; then pass approvals-owner "владелец привязан к боту согласований"
    else warn approvals-owner "владелец не привязан к боту согласований — карточки не отправляются, кнопки не действуют: привязка — первый шаг страницы настройки переписки, либо ./ops/bot-bind.sh"; fi
    if [ "$bot_business" = no ] && [ "${setup_business:-}" != yes ]; then
      pass approvals-business "бизнес-режим у бота выключен — он нужен для способа «ассистент — отдельный сотрудник» (у @BotFather: Bot Settings → Secretary Mode)"
    fi
  fi

  # Mini App «Штурман» в боте согласований (docs/miniapp.md): кнопку меню ставит сервис сам, только
  # в чате привязанного владельца и только при внешнем адресе страницы настройки по https — сам
  # адрес здесь не спрашивается: он уже в строке setup-origin. Без Hermes наружу ничего не
  # публикуется, поэтому без внешнего адреса приложения там нет — это норма, а не неполадка.
  if [ "$bot_configured" = no ]; then
    warn miniapp "приложение «Штурман» в Telegram откроется после настройки бота согласований — первый шаг страницы настройки переписки"
  elif [ "$bot_configured" = yes ] && { [ "$bot_problem" = other_poller ] || [ "$bot_problem" = conflict ] || [ "$bot_problem" = webhook ]; }; then
    # Ботом согласований пользуется ещё кто-то — так выглядит и тот же токен у бота ассистента в
    # Hermes. Тогда Hermes мог бы сам подписать данные входа в приложение: вход в него закрыт.
    fail miniapp "вход в приложение «Штурман» закрыт: ботом согласований пользуется другая программа (код: $bot_problem). Если его токен — тот же, что у бота ассистента в Hermes, создайте у @BotFather отдельного бота и введите его токен на странице настройки переписки: с общим токеном ассистент мог бы подтверждать решения за владельца"
  elif [ "$bot_configured" = yes ]; then
    case "$bot_menu" in
      set) pass miniapp "кнопка «Штурман» в боте согласований открывает приложение: ${svc_setup:-адрес страницы настройки}/shturman-setup/app/" ;;
      no_address)
        if [ "$MODE" = standalone ]; then
          pass miniapp "приложения в Telegram нет: без Hermes наружу ничего не публикуется, а приложению нужен внешний адрес страницы настройки по https (docs/miniapp.md). Решения — кнопками в боте согласований"
        else
          warn miniapp "у страницы настройки нет внешнего адреса https — приложению «Штурман» в Telegram он нужен: ./ops/set-setup-url.sh (docs/miniapp.md). Решения пока — кнопками в боте согласований"
        fi ;;
      no_owner) warn miniapp "кнопка «Штурман» появится в боте согласований после привязки владельца" ;;
      failed) warn miniapp "Telegram не принял кнопку «Штурман» — сервис повторит через 10 минут; подробности: docker logs --tail 50 $svc" ;;
      *) warn miniapp "кнопка «Штурман» ещё не сверялась — повторите проверку через минуту" ;;
    esac
  fi

  if [ "$llm_configured" = no ]; then
    if [ "$MODE" = standalone ]; then
      warn model "своя модель не настроена: архив и поиск работают, а обязательства и страницы памяти не разбираются. Подписку ChatGPT либо ключ и имя модели подключает владелец — на странице настройки переписки, раздел «Дополнительно»; ключ — также в терминале: ./ops/init-env.sh, затем ./ops/up.sh"
    fi
  elif [ "$llm_configured" = yes ] && [ "$llm_way" = subscription ]; then
    # Своя модель — по подписке ChatGPT владельца. Почты учётной записи и токенов здесь нет.
    case "$llm_sub" in
      relogin) fail model "модель сервиса по подписке ChatGPT: нужно войти заново — OpenAI больше не принимает сохранённый вход; задания модели ждут. Войти — на странице настройки переписки, «Дополнительно» → «Подписка ChatGPT» (это делает владелец)" ;;
      limit) warn model "модель сервиса по подписке ChatGPT ($llm_model): лимит подписки исчерпан — сервис повторит позже сам; лимиты: https://chatgpt.com/settings/usage" ;;
      denied) fail model "модель сервиса по подписке ChatGPT: OpenAI отказал в доступе (код: ${llm_problem:-нет}) — тариф, рабочее пространство или страна сервера; подробности на странице настройки переписки" ;;
      *)
        if [ "$llm_last" = no ]; then
          warn model "модель сервиса по подписке ChatGPT ($llm_model): последнее обращение не удалось (код: ${llm_problem:-нет}); обращений $llm_calls, неудач $llm_failures"
        elif [ "$llm_last" = yes ]; then pass model "модель сервиса по подписке ChatGPT ($llm_model): последнее обращение успешно; обращений $llm_calls, неудач $llm_failures"
        else pass model "модель сервиса по подписке ChatGPT ($llm_model), обращений ещё не было (первое — при ночной обработке)"; fi ;;
    esac
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

  # Разбор фото и документов: включает владелец на странице настройки переписки. Только числа.
  m_pending="$(num media_pending)"; m_asking="$(num media_asking)"; m_done="$(num media_done)"
  m_failed="$(num media_failed)"; m_skipped="$(num media_skipped)"
  case "$st" in
    *'"media_enabled":true'*)
      pass media "разбор фото и документов включён владельцем; готово: ${m_done:-0}, ждут: $(( ${m_pending:-0} + ${m_asking:-0} )), пропущено: ${m_skipped:-0}, не получилось: ${m_failed:-0}" ;;
    "") ;;
    *)
      pass media "разбор фото и документов выключен — включает владелец на странице настройки переписки (docs/media.md)" ;;
  esac

  # Уведомления владельцу «вам написали», «вас упомянули» (Р-84): что включено и сколько ушло. Только
  # признаки и числа. Без своего бота согласований уведомлений нет — об этом строка approvals-bot.
  n_wait="$(num notify_waiting)"; n_sent="$(num notify_sent_24h)"
  n_personal="$(printf '%s' "$st" | sed -n 's/.*"notify_personal":"\([a-z]*\)".*/\1/p')"
  case "$n_personal" in
    off) n_words="личные — выключены" ;;
    people) n_words="личные — от выбранных людей" ;;
    spheres) n_words="личные — из выбранных сфер" ;;
    all) n_words="личные — от всех" ;;
    *) n_words="" ;;
  esac
  if [ -n "$n_words" ]; then
    case "$st" in
      *'"notify_groups":true'*) n_words="$n_words, обращения в группах — да" ;;
      *) n_words="$n_words, обращения в группах — нет" ;;
    esac
    pass notify "уведомления владельцу: $n_words; за сутки отправлено: ${n_sent:-0}, ждут: ${n_wait:-0} (настройки — на странице настройки переписки)"
  fi

  # Повтор того, что не получилось из-за сбоя модели (Р-84): сколько можно вернуть и что вернул прошлый
  # повтор. Повтор запускает владелец (кнопка на странице) или оператор по его решению: shturman retry-failed.
  r_media="$(num retry_media_failed)"; r_req="$(num retry_requests_given_up)"
  r_last_media="$(num retry_last_media)"; r_last_req="$(num retry_last_requests)"
  if [ -n "$r_media" ]; then
    r_last=""
    case "$st" in
      *'"retry_last_at":"'*) r_last="; прошлый повтор вернул вложений: ${r_last_media:-0}, запросов: ${r_last_req:-0}" ;;
    esac
    if [ "${r_media:-0}" -gt 0 ] || [ "${r_req:-0}" -gt 0 ]; then
      warn retry "не получилось из-за сбоя модели: вложений ${r_media}, запросов разбора переписки ${r_req:-0} — повторить по решению владельца: кнопка на странице или docker exec shturman-service shturman retry-failed (снова тратит запросы к модели)$r_last"
    else
      pass retry "повторять нечего$r_last"
    fi
  fi
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
