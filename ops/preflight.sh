#!/usr/bin/env bash
# Проверка сервера перед развёртыванием. Ничего не меняет.
# Вывод: строки "PASS|WARN|FAIL  имя: подробности". Код возврата 1, если есть FAIL.
# Запускается из корня репозитория: место на диске и файл .env проверяются в текущем каталоге.
# Из .env читаются только несекретные строки SHTURMAN_MODE и SHTURMAN_SETUP_URL.
# Параметров нет. -h, --help — эта справка; проверки при этом не выполняются.
set -u
. "$(dirname "$0")/lib.sh"
ops_help "$@"
ops_no_args "$@"

fails=0
pass() { printf 'PASS  %s: %s\n' "$1" "$2"; }
warn() { printf 'WARN  %s: %s\n' "$1" "$2"; }
fail() { printf 'FAIL  %s: %s\n' "$1" "$2"; fails=$((fails + 1)); }

# --- система ---
if [ "$(uname -s)" = "Linux" ]; then
  name="unknown"
  [ -r /etc/os-release ] && name="$(. /etc/os-release && printf '%s' "${PRETTY_NAME:-unknown}")"
  pass os "$name, $(uname -m)"
else
  fail os "нужен Linux, обнаружено: $(uname -s)"
fi

# Пороги — по таблице docs/voice.md, «Какой сервер нужен». Сервер «на 4 ГБ» показывает
# немного меньше 4096 МБ, поэтому нижняя граница — 3500.
mem_mb="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)"
if [ "$mem_mb" -ge 7500 ]; then pass memory "${mem_mb} МБ — помещаются все дополнения сразу: расшифровка голосовых, поиск по смыслу, защита"
elif [ "$mem_mb" -ge 3500 ]; then warn memory "${mem_mb} МБ — ассистент, архив и одно тяжёлое дополнение: расшифровка голосовых или поиск по смыслу с защитой; всё сразу — от 8 ГБ (docs/voice.md)"
else fail memory "${mem_mb} МБ — нужно не меньше 4 ГБ"; fi

case "$(uname -m)" in
  x86_64|amd64) pass cpu "$(uname -m), ядер: $(nproc 2>/dev/null || echo '?')" ;;
  *) warn cpu "$(uname -m) — расшифровка голосовых на этом сервере не включится: её образ собирается только для x86-64" ;;
esac

disk_gb="$(df -Pk . 2>/dev/null | awk 'NR==2 {printf "%d", $4/1024/1024}')"
disk_gb="${disk_gb:-0}"
if [ "$disk_gb" -ge 40 ]; then pass disk "${disk_gb} ГБ свободно"
elif [ "$disk_gb" -ge 15 ]; then warn disk "${disk_gb} ГБ свободно — мало для большого архива с медиа"
else fail disk "${disk_gb} ГБ свободно — нужно не меньше 15 ГБ"; fi

# --- docker ---
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then pass docker "$(docker --version 2>/dev/null)"
  else fail docker "установлен, но демон недоступен текущему пользователю (группа docker?)"; fi
  if docker compose version >/dev/null 2>&1; then pass compose "$(docker compose version --short 2>/dev/null)"
  else fail compose "нет плагина docker compose"; fi
else
  fail docker "не установлен (установка — стоп-точка, см. docs/runbooks/deploy.md)"
fi

for tool in git curl; do
  if command -v "$tool" >/dev/null 2>&1; then pass "$tool" "есть"; else fail "$tool" "не установлен"; fi
done

# --- порт страницы настройки переписки ---
# Только режим с Hermes: страницу настройки переписки обратный прокси отдаёт на отдельном порту
# (по умолчанию 8443, иначе — порт из SHTURMAN_SETUP_URL). Без Hermes наружу не отдаётся ничего.
# Из .env читаются две несекретные строки.
pf_mode="$(env_get SHTURMAN_MODE)"
if [ "${pf_mode:-hermes}" = hermes ]; then
  pf_port="$(url_port "$(env_get SHTURMAN_SETUP_URL)")"
  pf_port="${pf_port:-$SETUP_PORT_DEFAULT}"
  if [ "$pf_port" = 443 ]; then
    : # отдельное имя на обычном порту: его занимает тот же обратный прокси, проверять нечего
  elif ! command -v ss >/dev/null 2>&1; then
    warn setup-port "нет программы ss (пакет iproute2) — не проверено, свободен ли порт $pf_port для страницы настройки переписки"
  elif ss -tlnH 2>/dev/null | awk '{print $4}' | grep -Eq ":${pf_port}\$"; then
    warn setup-port "порт $pf_port уже кто-то слушает. Если это ваш обратный прокси со страницей настройки переписки — так и должно быть; если другая программа — выберите другой порт: ./ops/set-setup-url.sh https://имя:порт"
  else
    pass setup-port "порт $pf_port свободен — на нём обратный прокси будет отдавать страницу настройки переписки (открыть его в firewall — стоп-точка)"
  fi
fi

# --- время ---
if command -v timedatectl >/dev/null 2>&1; then
  if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ]; then pass clock "синхронизировано"
  else warn clock "синхронизация времени не подтверждена — Telegram и TLS чувствительны к сдвигу часов"; fi
else
  warn clock "timedatectl недоступен, синхронизацию не проверить"
fi

# --- исходящая сеть ---
# Что откуда скачивается — docs/deployment.md, «Что откуда скачивается».
# Любой HTTP-ответ означает, что узел достижим. 000 — нет соединения.
#   probe <метка> <адрес> <need> <proxy> <что не заработает без него>
#   need=yes — без узла установка невозможна (FAIL); иначе WARN.
#   proxy=yes — узел нужен сервису в работе: проверяется через EGRESS_PROXY_URL, если он задан.
#   Узлы для установки и сборки образов проверяются напрямую: исходящий прокси сервиса на них не действует.
probe() {
  local label="$1" url="$2" required="$3" via="$4" what="$5" code
  if ! command -v curl >/dev/null 2>&1; then warn "net:$label" "curl отсутствует"; return; fi
  if [ "$via" = yes ]; then
    code="$(curl -sS -o /dev/null -m 12 -w '%{http_code}' ${EGRESS_PROXY_URL:+--proxy "$EGRESS_PROXY_URL"} "$url" 2>/dev/null)"
  else
    code="$(curl -sS -o /dev/null -m 12 -w '%{http_code}' "$url" 2>/dev/null)"
  fi
  code="${code:-000}"
  if [ "$code" = "000" ]; then
    if [ "$required" = "yes" ]; then fail "net:$label" "нет соединения с $url — $what"
    else warn "net:$label" "нет соединения с $url — $what"; fi
  elif [ "$label" = "openai" ] && [ "$code" = "403" ]; then
    warn "net:$label" "ответ 403 — вероятно, регион сервера не поддерживается провайдером"
  else
    pass "net:$label" "HTTP $code"
  fi
}
probe github      https://github.com                    yes no  "без него не скачать и не обновить репозиторий"
probe registry    https://registry-1.docker.io/v2/      yes no  "без Docker Hub не скачать образы Hermes, базы и основу образов"
probe debian      https://deb.debian.org/debian/        yes no  "без него не собрать образ сервиса (системные пакеты внутри образа)"
probe pypi        https://pypi.org/simple/              yes no  "без него не собрать образ сервиса (библиотеки Python)"
probe telegram    https://api.telegram.org              no  yes "сервису нужен Telegram; понадобится исходящий прокси (EGRESS_PROXY_URL)"
probe openai      https://api.openai.com/v1/models      no  yes "нужен, если модель ассистента — OpenAI; иначе понадобится исходящий прокси (EGRESS_PROXY_URL) или другой провайдер"
probe huggingface https://huggingface.co                no  no  "не скачать модели поиска по смыслу, защиты и расшифровки голосовых — эти дополнения не включатся"
probe ghcr        https://ghcr.io/v2/                   no  no  "не скачать образ сервера моделей — не включатся поиск по смыслу и защита"
probe pytorch     https://download.pytorch.org/whl/cpu/ no  no  "не собрать контейнер расшифровки голосовых"

# --- репозиторий ---
if [ -f .env ]; then
  perms="$(stat -c '%a' .env 2>/dev/null || echo '?')"
  if [ "$perms" = "600" ]; then pass env "есть, права 600"; else warn env "есть, права $perms — должно быть 600"; fi
else
  warn env "нет файла .env — запустите ./ops/init-env.sh --auto"
fi

echo
if [ "$fails" -gt 0 ]; then echo "ИТОГ: FAIL ($fails)"; exit 1; fi
echo "ИТОГ: OK"
