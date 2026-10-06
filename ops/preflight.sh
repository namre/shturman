#!/usr/bin/env bash
# Проверка сервера перед развёртыванием. Ничего не меняет.
# Вывод: строки "PASS|WARN|FAIL  имя: подробности". Код возврата 1, если есть FAIL.
set -u

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

mem_mb="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)"
if [ "$mem_mb" -ge 7500 ]; then pass memory "${mem_mb} МБ"
elif [ "$mem_mb" -ge 3500 ]; then warn memory "${mem_mb} МБ — хватит без локальных эмбеддингов и распознавания голосовых (EMBEDDINGS_MODE/STT_MODE не local)"
else fail memory "${mem_mb} МБ — нужно не меньше 4 ГБ"; fi

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

# --- время ---
if command -v timedatectl >/dev/null 2>&1; then
  if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ]; then pass clock "синхронизировано"
  else warn clock "синхронизация времени не подтверждена — Telegram и TLS чувствительны к сдвигу часов"; fi
else
  warn clock "timedatectl недоступен, синхронизацию не проверить"
fi

# --- исходящая сеть ---
# Любой HTTP-ответ означает, что узел достижим. 000 — нет соединения.
probe() {
  local label="$1" url="$2" required="$3" code
  if ! command -v curl >/dev/null 2>&1; then warn "net:$label" "curl отсутствует"; return; fi
  code="$(curl -sS -o /dev/null -m 12 -w '%{http_code}' ${EGRESS_PROXY_URL:+--proxy "$EGRESS_PROXY_URL"} "$url" 2>/dev/null)"
  code="${code:-000}"
  if [ "$code" = "000" ]; then
    if [ "$required" = "yes" ]; then fail "net:$label" "нет соединения с $url"
    else warn "net:$label" "нет соединения с $url — понадобится исходящий прокси (EGRESS_PROXY_URL)"; fi
  elif [ "$label" = "openai" ] && [ "$code" = "403" ]; then
    warn "net:$label" "ответ 403 — вероятно, регион сервера не поддерживается провайдером"
  else
    pass "net:$label" "HTTP $code"
  fi
}
probe github   https://github.com               yes
probe registry https://registry-1.docker.io/v2/ yes
probe telegram https://api.telegram.org         no
probe openai   https://api.openai.com/v1/models no

# --- репозиторий ---
if [ -f .env ]; then
  perms="$(stat -c '%a' .env 2>/dev/null || echo '?')"
  if [ "$perms" = "600" ]; then pass env "есть, права 600"; else warn env "есть, права $perms — должно быть 600"; fi
else
  warn env "нет файла .env — запустите ./ops/init-env.sh (это делает человек, не агент)"
fi

echo
if [ "$fails" -gt 0 ]; then echo "ИТОГ: FAIL ($fails)"; exit 1; fi
echo "ИТОГ: OK"
