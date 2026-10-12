#!/usr/bin/env bash
# Подключение экземпляра к посреднику sh.namre.app (docs/relay.md, решение Р-69): адрес для тех, у кого
# нет своего домена или белого адреса. Браузер открывает ИМЯ.sh.namre.app, посредник передаёт
# соединение по имени сайта (SNI), не расшифровывая, а контейнер frpc этого сервера (shturman-relay)
# принимает его исходящим соединением и отдаёт обратному прокси Caddy. Сертификат и ключ — только здесь.
#
#   ./ops/relay.sh join КОД ИМЯ   — подключиться: код приглашения выдаёт держатель посредника (одноразовый),
#                                   ИМЯ выбирает владелец (3–30 знаков: a–z, 0–9, дефис внутри). Скрипт
#                                   получает два имени — ИМЯ.<зона> (дашборд) и setup-ИМЯ.<зона> (страница
#                                   настройки переписки), — записывает их адресами экземпляра
#                                   (./ops/set-public-url.sh, ./ops/set-setup-url.sh), а токен посредника —
#                                   в .env, и включает профиль relay. Ключ подключения создаёт сам
#                                   экземпляр (в .env, до обращения к посреднику); посреднику уходит только
#                                   его sha256. Ответ потерялся — повторите ту же команду: тот же код, имя
#                                   и ключ дают тот же успешный ответ
#   ./ops/relay.sh caddyfile      — напечатать готовую настройку Caddy для этих двух имён (вариант
#                                   «через посредника» из config/Caddyfile.example); сам прокси не меняет
#   ./ops/relay.sh status         — имя, адреса, контейнер и состояние четырёх пересылок
#   ./ops/relay.sh on             — включить подключение снова (после off): забирает сертификат посредника
#                                   заново и возвращает адреса экземпляра на имена посредника
#   ./ops/relay.sh off            — выключить подключение и убрать контейнер. Адреса в .env остаются именами
#                                   посредника: страницы по ним не откроются, пока не ./ops/relay.sh on
#                                   или пока не задан другой адрес (./ops/set-public-url.sh и правка прокси)
#   ./ops/relay.sh off --forget   — то же и забыть подключение: убрать из .env ключ, токен и имя. Имя на
#                                   посреднике остаётся занятым; освобождает его держатель посредника
#   -h, --help                    — эта справка; ничего не меняется и никуда не отправляется
#
# Только режим с Hermes: без Hermes экземпляр наружу ничего не публикует (docs/standalone.md).
#
# Подключение — решение владельца. Держатель посредника и зоны sh.namre.app технически может
# направить имя владельца на свой сервер, получить на него сертификат и показать поддельную
# страницу настройки (docs/relay.md, «Риски», 1). Кто ему не доверяет — свой домен, адрес по IP
# (docs/deployment.md, «Без домена») или туннель SSH.
#
# Секреты: ключ подключения (SHTURMAN_RELAY_KEY), токен посредника (SHTURMAN_RELAY_TOKEN) и пароль
# локальной панели frpc (SHTURMAN_RELAY_ADMIN_PASSWORD) скрипт пишет в .env и не печатает. Код
# приглашения одноразовый: скрипт его никуда не сохраняет. Несекретные строки: SHTURMAN_RELAY_NAME,
# SHTURMAN_RELAY_DOMAIN, SHTURMAN_RELAY_SERVER, SHTURMAN_RELAY_PORT. Из .env скрипт читает только их,
# режим, профили и два адреса; о секретах — только «записана ли строка». Сертификат посредника
# (не секрет) — data/relay/ca.pem: по нему frpc проверяет, что соединяется с настоящим посредником.
# Адрес службы посредника — https://relay.sh.namre.app; другой — переменная окружения SHTURMAN_RELAY_API.
#
# Правка обратного прокси — стоп-точка (sudo): скрипт её не делает, только печатает настройку.
# Входящие порты на сервере для посредника открывать не нужно.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

cmd="${1:-status}"
case "$cmd" in
  join) [ $# -eq 3 ] || { [ $# -gt 3 ] && ops_unknown "$4"; echo "использование: ./ops/relay.sh join КОД ИМЯ" >&2; exit 2; } ;;
  on|status|caddyfile) [ $# -le 1 ] || ops_unknown "$2" ;;
  off)
    forget=no
    case "${2:-}" in
      "") ;;
      --forget) forget=yes ;;
      *) ops_unknown "$2" ;;
    esac
    [ $# -le 2 ] || ops_unknown "$3" ;;
  -*) ops_unknown "$cmd" ;;
  *) echo "использование: ./ops/relay.sh join КОД ИМЯ | caddyfile | status | on | off [--forget] — справка: --help" >&2; exit 2 ;;
esac

API="${SHTURMAN_RELAY_API:-https://relay.sh.namre.app}"
API="${API%/}"
case "$API" in
  https://*) ;;
  *) echo "SHTURMAN_RELAY_API — только https:// (ответ посредника содержит ключ подключения)" >&2; exit 2 ;;
esac
CA=data/relay/ca.pem
NAME_RE='^[a-z0-9]([a-z0-9-]{1,28}[a-z0-9])$'
CODE_RE='^[A-Za-z0-9 -]{16,40}$'

[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }
# Записана ли строка секрета — без чтения значения.
has_secret() { grep -Eq "^$1=.+" .env; }

need_python() {
  command -v python3 >/dev/null || { echo "нужен python3 (на Ubuntu 24.04 он есть по умолчанию)" >&2; exit 1; }
}

# Разбор ответа посредника. Печатает строки ИМЯ=значение для .env (значения проверены по шаблонам,
# без пробелов, кавычек и $), а сертификат пишет в файл $1 (права 600). Ничего не печатает в журнал.
#   $1 — куда записать сертификат; на входе — тело ответа и последней строкой код HTTP.
PARSE_JOIN='
import json, os, re, sys
raw = sys.stdin.read()
body, _, code = raw.rpartition("\n")
try:
    d = json.loads(body)
except ValueError:
    d = None
if code != "200" or not isinstance(d, dict):
    msg = d.get("message") if isinstance(d, dict) else None
    msg = re.sub(r"[^\w\s.,:;«»()/+—–-]", "", str(msg or ""))[:300]
    print("посредник ответил отказом (HTTP " + code + "): " + (msg or "ответ не разобран"), file=sys.stderr)
    sys.exit(3 if code in ("400", "403", "409", "429") else 1)
host = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+"
checks = {
    "name": r"[a-z0-9]([a-z0-9-]{1,28}[a-z0-9])",
    "domain": host,
    "server": host,
    "frp_token": r"[A-Za-z0-9+/=._~-]{8,256}",
}
for k, rx in checks.items():
    if not isinstance(d.get(k), str) or not re.fullmatch(rx, d[k]):
        print("ответ посредника не разобран: поле " + k, file=sys.stderr); sys.exit(1)
port = d.get("port")
if not isinstance(port, int) or not 1 <= port <= 65535:
    print("ответ посредника не разобран: поле port", file=sys.stderr); sys.exit(1)
n, z = d["name"], d["domain"]
if d.get("dashboard_url") != "https://" + n + "." + z or d.get("setup_url") != "https://setup-" + n + "." + z:
    print("ответ посредника не разобран: адреса не совпадают с именем", file=sys.stderr); sys.exit(1)
if len(sys.argv) > 2 and d["name"] != sys.argv[2]:
    print("ответ посредника не разобран: выдано другое имя", file=sys.stderr); sys.exit(1)
ca = d.get("ca")
pem = r"(-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\r?\n?)+"
if not isinstance(ca, str) or len(ca) > 20000 or not re.fullmatch(pem, ca):
    print("ответ посредника не разобран: сертификат", file=sys.stderr); sys.exit(1)
path = sys.argv[1]
tmp = path + ".part"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(ca)
os.replace(tmp, path)
print("SHTURMAN_RELAY_NAME=" + d["name"])
print("SHTURMAN_RELAY_DOMAIN=" + d["domain"])
print("SHTURMAN_RELAY_SERVER=" + d["server"])
print("SHTURMAN_RELAY_PORT=" + str(port))
print("SHTURMAN_RELAY_TOKEN=" + d["frp_token"])
'

# Сертификат посредника (не секрет) — заново с https://…/api/ca: соединение проверяется обычным
# сертификатом Let's Encrypt, так что подменить его по дороге нельзя.
fetch_ca() {
  local pem
  pem="$(curl -fsS --proto '=https' --tlsv1.2 -m 30 "$API/api/ca")" || { echo "не удалось получить сертификат посредника: $API/api/ca" >&2; return 1; }
  printf '%s\n' "$pem" | python3 -I -c '
import os, re, sys
ca = sys.stdin.read()
pem = r"(-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\r?\n?)+\n?"
if len(ca) > 20000 or not re.fullmatch(pem, ca):
    sys.exit("ответ посредника — не сертификат")
tmp = sys.argv[1] + ".part"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(ca)
os.replace(tmp, sys.argv[1])' "$CA"
}

ensure_admin_password() {
  if ! has_secret SHTURMAN_RELAY_ADMIN_PASSWORD; then
    env_put SHTURMAN_RELAY_ADMIN_PASSWORD "$(python3 -I -c 'import secrets; print(secrets.token_urlsafe(24))')"
  fi
}

# Адреса экземпляра — имена посредника. Скрипты адресов сами проверяют, что адреса разные (origin).
set_addresses() {
  local name="$1" domain="$2" out
  if ! out="$(./ops/set-public-url.sh "https://$name.$domain" 2>&1)"; then
    printf '%s\n' "$out" >&2; return 1
  fi
  if ! out="$(./ops/set-setup-url.sh "https://setup-$name.$domain" 2>&1)"; then
    printf '%s\n' "$out" >&2; return 1
  fi
}

remove_container() {
  if docker inspect shturman-relay >/dev/null 2>&1; then
    echo "Останавливаю и убираю контейнер shturman-relay…"
    docker compose --profile relay rm --stop --force relay >/dev/null 2>&1 || true
    if docker inspect shturman-relay >/dev/null 2>&1; then docker rm --force shturman-relay >/dev/null; fi
  fi
}

# Готовая настройка Caddy: основная часть config/Caddyfile.example (глобальный блок и два блока сайта)
# с именами посредника, путём к репозиторию и чтением протокола PROXY от frpc.
print_caddyfile() {
  local name="$1" domain="$2"
  python3 -I - config/Caddyfile.example "$name.$domain" "setup-$name.$domain" "$PWD" <<'PY'
import sys
src, dash, setup, repo = sys.argv[1:5]
text = open(src, encoding="utf-8").read()
start = text.index("\n{\n\tadmin ") + 1
end_head = "\nassistant.example.com:8443 {\n"
end = text.index("\n}\n", text.index(end_head)) + 3
body = text[start:end]
glob = "{\n\tadmin unix//run/caddy-admin/admin.sock\n}\n"
assert body.count(glob) == 1 and body.count("\nassistant.example.com {\n") == 1 and body.count(end_head) == 1
body = body.replace(glob, "{\n\tadmin unix//run/caddy-admin/admin.sock\n"
                          "\t# Посредник: frpc (контейнер shturman-relay) сообщает адрес браузера протоколом PROXY;\n"
                          "\t# заголовок принимается только от 127.0.0.1.\n"
                          "\tservers {\n\t\tlistener_wrappers {\n\t\t\tproxy_protocol {\n\t\t\t\tallow 127.0.0.1/32\n"
                          "\t\t\t}\n\t\t\ttls\n\t\t}\n\t}\n}\n")
dash_head = "\nassistant.example.com {\n\tencode gzip\n"
assert body.count(dash_head) == 1
guard = ("\n\t# Посредник: все экземпляры — один сайт (sh.namre.app), cookie SameSite от соседнего экземпляра\n"
         "\t# не защищают. Изменяющий запрос, который браузер пометил как пришедший не с этого адреса, — отказ.\n"
         "\t@cross_site {\n\t\tnot method GET HEAD OPTIONS\n\t\theader Sec-Fetch-Site *\n"
         "\t\tnot header Sec-Fetch-Site same-origin\n\t}\n\thandle @cross_site {\n\t\trespond 403\n\t}\n")
body = body.replace(dash_head, f"\n{dash} {{\n\tencode gzip\n" + guard).replace(end_head, f"\n{setup} {{\n")
body = body.replace("/opt/shturman/", repo.rstrip("/") + "/")
print(f"# Настройка Caddy для экземпляра за посредником: {dash} и {setup}.")
print("# Создано ./ops/relay.sh caddyfile из config/Caddyfile.example (вариант «через посредника»); пояснения — там.")
print("# Применение — стоп-точка: показать владельцу, получить согласие, затем под sudo положить в")
print("# /etc/caddy/Caddyfile и перечитать: caddy reload --address unix//run/caddy-admin/admin.sock --config /etc/caddy/Caddyfile")
print()
sys.stdout.write(body)
PY
}

relay_name="$(env_get SHTURMAN_RELAY_NAME)"
relay_domain="$(env_get SHTURMAN_RELAY_DOMAIN)"

case "$cmd" in
  join)
    require_hermes "дашборда и внешнего адреса"
    need_python
    code="$2"; name="$3"
    if [ -n "$relay_name" ]; then
      echo "Экземпляр уже подключён к посреднику (имя $relay_name). Включить снова — ./ops/relay.sh on." >&2
      echo "Подключиться заново под другим именем — сначала ./ops/relay.sh off --forget, затем новый код приглашения." >&2
      exit 2
    fi
    # [[ =~ ]], а не grep: grep проверяет каждую строку отдельно и пропустил бы значение с переводом строки.
    if [[ ! "$name" =~ $NAME_RE ]] || [ "${name#setup-}" != "$name" ]; then
      echo "Имя не подходит: 3–30 знаков, строчные латинские буквы, цифры и дефис внутри, не начинается с setup-." >&2
      exit 2
    fi
    if [[ ! "$code" =~ $CODE_RE ]]; then
      echo "Код приглашения не похож на код: 20 букв и цифр, обычно группами через дефис (XXXX-XXXX-…)." >&2
      exit 2
    fi
    umask 077
    mkdir -p data/relay; chmod 700 data/relay
    # Ключ подключения создаёт сам экземпляр и записывает в .env ДО обращения к посреднику; посреднику
    # уходит только его sha256. Если ответ потеряется в сети, повтор той же команды пошлёт тот же
    # sha256, и посредник ответит тем же успехом. Ключ из .env скрипт читает сам и нигде не печатает.
    if ! has_secret SHTURMAN_RELAY_KEY; then
      env_put SHTURMAN_RELAY_KEY "$(python3 -I -c 'import secrets; print(secrets.token_urlsafe(32))')"
    fi
    key_line="$(grep -E '^SHTURMAN_RELAY_KEY=' .env | tail -n 1)"
    key_hash="$(printf '%s' "shturman-relay-key:${key_line#*=}" | sha256sum | cut -d' ' -f1)"
    unset key_line
    echo "Подключаюсь к посреднику $API как «$name»…"
    # Код, имя и хеш уже проверены по шаблонам: кавычек и обратной черты в них нет.
    body="{\"code\": \"$code\", \"name\": \"$name\", \"key_hash\": \"$key_hash\"}"
    rc=0
    resp="$(printf '%s' "$body" | curl -sS --proto '=https' --tlsv1.2 -m 30 -X POST -H 'Content-Type: application/json' \
            --data-binary @- -w '\n%{http_code}' "$API/api/join")" || rc=$?
    if [ "$rc" -ne 0 ]; then
      echo "Посредник не ответил ($API, код curl $rc): неизвестно, принял ли он код." >&2
      echo "Повторите ту же команду с тем же кодом и именем: ключ уже сохранён, и если посредник код принял," >&2
      echo "повтор завершит подключение, а если нет — подключит заново." >&2
      exit 1
    fi
    rc=0
    lines="$(printf '%s' "$resp" | python3 -I -c "$PARSE_JOIN" "$CA" "$name")" || rc=$?
    unset resp
    if [ "$rc" -ne 0 ]; then
      [ "$rc" -eq 3 ] && exit 2
      exit 1
    fi
    # Строки ИМЯ=значение делятся по первому «=»: значение может само содержать «=» (base64).
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      env_put "${line%%=*}" "${line#*=}"
    done <<EOF_LINES
$lines
EOF_LINES
    unset lines
    ensure_admin_password
    relay_name="$(env_get SHTURMAN_RELAY_NAME)"; relay_domain="$(env_get SHTURMAN_RELAY_DOMAIN)"
    set_addresses "$relay_name" "$relay_domain" || { echo "Ключ подключения записан, но адреса записать не удалось — см. выше; затем ./ops/relay.sh on." >&2; exit 1; }
    profile_set on relay
    echo "Экземпляр подключён к посреднику: имя $relay_name."
    echo "  адрес ассистента (дашборд):          https://$relay_name.$relay_domain"
    echo "  адрес страницы настройки переписки:  https://setup-$relay_name.$relay_domain"
    echo "Ключ подключения и токен посредника записаны в .env (секреты — не показываются); код приглашения израсходован."
    echo "Держатель посредника технически может подменить эти имена — docs/relay.md, «Риски», 1."
    echo "Дальше:"
    echo "  1. настройка обратного прокси на эти два имени — стоп-точка (sudo): ./ops/relay.sh caddyfile печатает"
    echo "     готовый файл — показать владельцу, после согласия положить в /etc/caddy/Caddyfile и перечитать Caddy;"
    echo "  2. ./ops/up.sh — соберёт и запустит контейнер shturman-relay;"
    echo "  3. ./ops/doctor.sh — строки relay, relay-proxies, setup-page-public, setup-isolation."
    echo "Входящие порты на сервере для посредника открывать не нужно." ;;

  caddyfile)
    [ -n "$relay_name" ] && [ -n "$relay_domain" ] || { echo "экземпляр не подключён к посреднику — ./ops/relay.sh join КОД ИМЯ" >&2; exit 1; }
    need_python
    print_caddyfile "$relay_name" "$relay_domain" ;;

  on)
    require_hermes "дашборда и внешнего адреса"
    need_python
    if [ -z "$relay_name" ] || [ -z "$relay_domain" ] || ! has_secret SHTURMAN_RELAY_KEY || ! has_secret SHTURMAN_RELAY_TOKEN; then
      echo "Подключения к посреднику нет — ./ops/relay.sh join КОД ИМЯ" >&2; exit 1
    fi
    umask 077
    mkdir -p data/relay; chmod 700 data/relay
    fetch_ca || exit 1
    ensure_admin_password
    set_addresses "$relay_name" "$relay_domain" || exit 1
    profile_set on relay
    echo "Подключение к посреднику включено: https://$relay_name.$relay_domain и https://setup-$relay_name.$relay_domain."
    echo "Дальше: настройка прокси на эти имена (./ops/relay.sh caddyfile; правка — стоп-точка), ./ops/up.sh, ./ops/doctor.sh." ;;

  off)
    umask 077
    profile_set off relay
    remove_container
    if [ "$forget" = yes ]; then
      for k in SHTURMAN_RELAY_KEY SHTURMAN_RELAY_TOKEN SHTURMAN_RELAY_ADMIN_PASSWORD SHTURMAN_RELAY_NAME \
               SHTURMAN_RELAY_DOMAIN SHTURMAN_RELAY_SERVER SHTURMAN_RELAY_PORT; do
        if env_has "$k"; then env_del "$k"; fi
      done
      rm -f "$CA"
      echo "Подключение к посреднику выключено и забыто: ключ, токен и имя убраны из .env."
      echo "Имя ${relay_name:-экземпляра} на посреднике остаётся занятым — освобождает его держатель посредника."
    else
      echo "Подключение к посреднику выключено. Ключ и имя остались в .env: включить снова — ./ops/relay.sh on."
    fi
    public="$(env_get SHTURMAN_PUBLIC_URL)"
    if [ -n "$relay_domain" ] && [ "${public%".$relay_domain"}" != "$public" ]; then
      echo "Адреса экземпляра всё ещё — имена посредника ($public): страницы по ним не откроются."
      echo "Другой адрес — ./ops/set-public-url.sh https://имя (и правка прокси — стоп-точка), затем ./ops/up.sh."
    fi
    echo "Применить: ./ops/up.sh" ;;

  status)
    if [ -z "$relay_name" ]; then
      echo "Экземпляр к посреднику не подключён (./ops/relay.sh join КОД ИМЯ)."
      exit 0
    fi
    echo "имя: $relay_name (зона $relay_domain, посредник $(env_get SHTURMAN_RELAY_SERVER):$(env_get SHTURMAN_RELAY_PORT))"
    echo "адрес дашборда: $(env_get SHTURMAN_PUBLIC_URL)"
    echo "адрес страницы настройки: $(env_get SHTURMAN_SETUP_URL)"
    if profile_on relay; then echo "подключение: включено"; else echo "подключение: выключено (./ops/relay.sh on)"; fi
    state="$(container_state shturman-relay)"
    echo "контейнер shturman-relay: $state"
    if [ "$state" = running ]; then
      docker exec shturman-relay /frpc status -c /etc/frpc/frpc.toml 2>&1 || true
    fi ;;
esac
