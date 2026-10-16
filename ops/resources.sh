#!/usr/bin/env bash
# Память и процессор: сервер и контейнеры экземпляра (shturman-*). Ничего не меняет и не читает секретов.
# Для каждого контейнера: состояние, сколько памяти занято сейчас, предел памяти (задан ли в compose.yaml),
# доля процессора, предел процессора, сколько раз перезапускался и был ли убит за нехватку памяти (OOM).
# Для сервера: память всего и доступно, подкачка, число процессоров (/proc/meminfo, nproc).
# Откуда числа: docker stats --no-stream и docker inspect — оба только с шаблоном --format из перечня
# полей ниже (State.Status, HostConfig.Memory, HostConfig.NanoCpus, RestartCount, State.OOMKilled).
# Шаблон не выводит окружение контейнера (Config.Env), команду запуска и подключённые каталоги.
# Оператору этот скрипт вместо docker inspect: docker inspect без шаблона печатает переменные
# окружения с токенами (docs/pilot/locks-audit.md, Д-25).
# Параметров нет. -h, --help — эта справка. Код 1 — Docker недоступен.
set -u
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"
ops_no_args "$@"

# Байты → «1.5 ГБ» / «512 МБ».
human() { awk -v b="$1" 'BEGIN { if (b >= 1073741824) printf "%.1f ГБ", b / 1073741824; else printf "%d МБ", b / 1048576 }'; }

kb() { awk -v k="$1" '$1 == k":" { print $2 * 1024; exit }' /proc/meminfo 2> /dev/null; }
mt="$(kb MemTotal)"; ma="$(kb MemAvailable)"; st="$(kb SwapTotal)"; sf="$(kb SwapFree)"
if [ -n "$mt" ]; then
  line="Сервер: память $(human "$mt"), доступно $(human "${ma:-0}")"
  if [ "${st:-0}" -gt 0 ]; then line="$line; подкачка $(human "$st"), занято $(human $(( st - ${sf:-0} )))"
  else line="$line; подкачки нет"; fi
  echo "$line; процессоров: $(nproc 2> /dev/null || echo ?)"
fi

if ! names="$(docker ps -a --format '{{.Names}}' 2> /dev/null)"; then
  echo "Docker недоступен: docker ps не ответил" >&2
  exit 1
fi
names="$(printf '%s\n' "$names" | grep -E '^shturman-' | sort || true)"
if [ -z "$names" ]; then echo "Контейнеров shturman-* нет"; exit 0; fi

# Потребление — одним вызовом по запущенным: имя|занято / предел|доля памяти|доля процессора.
stats="$(docker stats --no-stream --format '{{.Name}}|{{.MemUsage}}|{{.MemPerc}}|{{.CPUPerc}}' 2> /dev/null || true)"

for n in $names; do
  info="$(docker inspect --format '{{.State.Status}}|{{.HostConfig.Memory}}|{{.HostConfig.NanoCpus}}|{{.RestartCount}}|{{.State.OOMKilled}}' "$n" 2> /dev/null)" || continue
  IFS='|' read -r state mem cpus restarts oom <<EOF
$info
EOF
  case "$state" in running) s="работает" ;; exited) s="остановлен" ;; restarting) s="перезапускается" ;; *) s="$state" ;; esac
  if [ "${mem:-0}" -gt 0 ] 2> /dev/null; then limit="предел $(human "$mem")"; else limit="предела нет (до памяти сервера)"; fi
  cpu_limit=""
  if [ "${cpus:-0}" -gt 0 ] 2> /dev/null; then cpu_limit=", предел $(awk -v c="$cpus" 'BEGIN { printf "%g", c / 1e9 }') проц."; fi
  row="$(printf '%s\n' "$stats" | awk -F'|' -v n="$n" '$1 == n { print; exit }')"
  if [ -n "$row" ]; then
    IFS='|' read -r _ usage mperc cperc <<EOF
$row
EOF
    used="${usage%% / *}"
    if [ "${mem:-0}" -gt 0 ] 2> /dev/null; then memtxt="память $used из $(human "$mem") ($mperc)"
    else memtxt="память $used, $limit"; fi
    note=""
    case "$mperc" in 9[0-9].*|9[0-9]%|1[0-9][0-9]*) [ "${mem:-0}" -gt 0 ] 2> /dev/null && note="  ВНИМАНИЕ: у предела памяти" ;; esac
    out="$s  $memtxt  процессор $cperc$cpu_limit"
  else
    out="$s  $limit$cpu_limit"; note=""
  fi
  [ "$oom" = true ] && note="$note  ВНИМАНИЕ: последний раз убит за нехватку памяти (OOM)"
  printf '%-22s %s  перезапусков: %s%s\n' "$n" "$out" "${restarts:-?}" "$note"
done
