#!/usr/bin/env bash
# Диагностический пакет для пересылки за помощью (Р-98): архив, который можно отправить разработчику
# или вставить в чат с агентом, — без текста переписки, имён, названий чатов, телефонов, токенов,
# ключей и адресов страниц входа.
#
#   ./ops/diag.sh           — собрать пакет в local/diag/, показать список файлов и размер, упаковать
#                             (shturman-diag-<время>.tar.gz, права 600). Хранятся 5 последних пакетов.
#   ./ops/diag.sh --print   — то же и напечатать содержимое пакета (оно безопасно для пересылки).
#
# Что внутри — только то, что собрано белым списком (ops/diag_filter.py: берутся поля известного вида,
# остальное отбрасывается):
#   versions.txt    — VERSION, коммит, число изменённых файлов, режим, версии Docker и Compose, ядро;
#   state.txt       — ./ops/state.sh show: ключи с простыми значениями, журнал — время и «set ключ»;
#   doctor.txt      — ./ops/doctor.sh: строки проверок, в тексте заменены адреса, имена узлов, номера;
#   containers.txt  — контейнеры shturman-*: имя, образ, состояние;
#   disk.txt        — свободное место и что занимает диск (размеры каталогов целиком);
#   service-log.txt — журнал сервиса: время, уровень, модуль, вид ошибки; текста сообщений нет;
#   hermes-log.txt  — журнал ошибок Hermes: только счётчики по уровню и модулю. Hermes пишет в свои
#                     журналы начало текста сообщений владельца, поэтому строк из них не берётся;
#   other-logs.txt  — база и дополнения: только число строк и число ошибок.
# Нужен python3 на сервере (для очистки журналов); без него журналы в пакет не попадают.
# Файлы .env, data/, local/backups скрипт не читает; секретов не печатает. Агенту запускать можно.
#   -h, --help — эта справка; ничего не собирается.
set -u
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

show=no
case "${1:-}" in
  "") ;;
  --print) show=yes ;;
  *) ops_unknown "$1" ;;
esac
[ $# -le 1 ] || ops_unknown "$2"

umask 077
mkdir -p local/diag; chmod 700 local local/diag
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
work="local/diag/shturman-diag-$stamp"
mkdir "$work" || exit 1
py=""
if command -v python3 > /dev/null 2>&1; then py="python3 -I ops/diag_filter.py"; fi
# shellcheck disable=SC2086  # $py — команда из двух слов
clean() { if [ -n "$py" ]; then $py "$1"; else cat > /dev/null; echo "# python3 нет на сервере — не взято"; fi; }
svc=shturman-service; hermes=shturman-hermes

# --- версии ---
{
  echo "VERSION: $(head -n 1 VERSION 2>/dev/null || echo ?)"
  echo "коммит: $(git rev-parse --short=12 HEAD 2>/dev/null || echo ?) от $(git log -1 --format=%cs 2>/dev/null || echo ?)"
  echo "изменённых отслеживаемых файлов: $(git status --porcelain --untracked-files=no 2>/dev/null | wc -l)"
  echo "режим: $(shturman_mode 2>/dev/null || echo неизвестен)"
  echo "Docker: $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo нет ответа)"
  echo "Compose: $(docker compose version --short 2>/dev/null || echo нет)"
  echo "ядро: $(uname -sr 2>/dev/null)"
  echo "собрано: $stamp (UTC)"
} > "$work/versions.txt"

# --- состояние развёртывания и проверка здоровья ---
./ops/state.sh show 2>/dev/null | clean state > "$work/state.txt"
./ops/doctor.sh 2>/dev/null | clean doctor > "$work/doctor.txt"

# --- контейнеры ---
docker ps -a --filter name=shturman --format '{{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null \
  | awk -F'\t' '$1 ~ /^shturman-[a-z0-9-]+$/ {print}' > "$work/containers.txt"
[ -s "$work/containers.txt" ] || echo "Docker не ответил или контейнеров нет" > "$work/containers.txt"

# --- диск ---
{
  df -Ph . 2>/dev/null | awk 'NR == 2 {print "диск: размер " $2 ", занято " $3 ", свободно " $4 " (" $5 ")"}'
  echo "занимают: $(disk_usage)"
} > "$work/disk.txt"

# --- журналы ---
# Сервис: последние 3000 строк, только поля известного вида.
docker logs --tail 3000 "$svc" 2>&1 | clean service-log > "$work/service-log.txt"
# Hermes: журнал ошибок читается внутри контейнера и не выходит из него — наружу только счётчики.
if [ "$(container_state "$hermes")" = running ]; then
  docker exec -u "$(id -u):$(id -g)" "$hermes" sh -c 'cat /opt/data/logs/errors.log.1 /opt/data/logs/errors.log 2>/dev/null' \
    2>/dev/null | clean hermes-errors > "$work/hermes-log.txt"
else
  echo "# Hermes не запущен или режим без Hermes — журнал не смотрелся" > "$work/hermes-log.txt"
fi
{
  for c in shturman-postgres shturman-embeddings shturman-guard shturman-asr shturman-relay; do
    st="$(container_state "$c")"
    [ "$st" = missing ] && continue
    echo "== $c ($st)"
    docker logs --tail 2000 "$c" 2>&1 | clean counts
  done
} > "$work/other-logs.txt"

# --- показать, что попало, и упаковать ---
echo "Диагностический пакет собран: $work/"
total=0
for f in "$work"/*; do
  b="$(wc -c < "$f")"; total=$((total + b))
  printf '  %-18s %8s байт  %s строк\n' "$(basename "$f")" "$b" "$(wc -l < "$f")"
done
echo "  всего: $total байт"
archive="$work.tar.gz"
tar -czf "$archive" -C local/diag "$(basename "$work")" && chmod 600 "$archive"
rm -rf "$work"
echo "Архив: $archive ($(du -h "$archive" | cut -f1)) — его можно переслать: текста переписки, имён, токенов"
echo "и адресов входа в нём нет (что именно взято — ./ops/diag.sh --help)."
# shellcheck disable=SC2012  # имена пакетов — только дата и время
ls -1t local/diag/shturman-diag-*.tar.gz 2>/dev/null | tail -n +6 | while IFS= read -r old; do rm -f -- "$old"; done

if [ "$show" = yes ]; then
  tar -tzf "$archive" 2>/dev/null | grep -v '/$' | while IFS= read -r member; do
    echo; echo "===== ${member#*/}"
    tar -xzOf "$archive" "$member" 2>/dev/null
  done
fi
exit 0
