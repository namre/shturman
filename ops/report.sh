#!/usr/bin/env bash
# Отчёт сессии сопровождения — разработке, по кнопке владельца (Р-102). Каждая сессия Codex по
# prompts/operate-codex.md начинается с --start и кончается отчётом — и при успехе тоже: удачные
# обновления засчитываются в условия выхода из обкатки (docs/pilot/exit-criteria.md, К-8).
#   ./ops/report.sh --start      — в начале сессии: снимок «до» (версия, тег, коммит, коды и имена строк
#                                  ./ops/doctor.sh) в local/report/session.json. Ничего не отправляет
#   ./ops/report.sh --scenario О2 --result clean|intervened|failed [--deviation ТЕКСТ] [--helped ТЕКСТ]
#                   [--defect "| Д-? | … |"]… [--notes -] [--no-send]
#                                — в конце сессии: снимок «после», отчёт одной формы, показ здесь и карточка
#                                  владельцу в бот согласований «Отправить разработчику?» с кнопками
#                                  «Отправить» / «Не отправлять». Уходит разработчику только по его нажатию.
#        --scenario    номер сценария из docs/pilot/scenarios-ops.md (О1…О20) или учения (У1…У17)
#        --result      clean — без вмешательства; intervened — с вмешательством (что-то сделано руками или
#                      в обход runbook); failed — не прошло
#        --deviation   где агент отступил от runbook (свободный текст)
#        --helped      что помогло (свободный текст)
#        --defect      строка дефекта в формате docs/pilot/defects.md (до трёх)
#        --notes -     то же со стандартного ввода: строки «отступления: …», «помогло: …» и строки дефектов «| Д-? | … |»
#        --no-send     только собрать и показать, владельцу не отправлять
#   ./ops/report.sh --show       — последний собранный отчёт: текст и JSON
#   ./ops/report.sh --send       — отправить последний отчёт ещё раз (если сервис не отвечал)
#   -h, --help                   — эта справка; ничего не делается
# Что в отчёте — только белый список (ops/report_filter.py, как у ./ops/diag.sh): версии и коммит; коды и имена
# строк doctor до и после, пояснения — только у WARN и FAIL, без адресов, имён узлов, номеров и ключей; итог
# обновления из local/update/last.env; свободный текст агента — через тот же фильтр, замену имён, путей data/ и
# значений .env и словарь репозитория: незнакомые слова (переписка, имена, названия чатов) становятся «…».
# ТЕКСТ ОТЧЁТА УВИДИТ ВЛАДЕЛЕЦ, а по его кнопке — разработчик: не пишите в него переписку, имена, названия чатов,
# адреса, ссылки входа и секреты. Журналов Hermes, .env, data/ и копий скрипт не читает.
# Отчёт хранится в local/report/ (последние 20, права 600); в сервис он передаётся командой
# `shturman ops-report submit` в его контейнере — прямо в базу, мимо внутреннего API. Нужен python3.
# Коды: 0 — отчёт собран (и карточка поставлена или сервис не отвечает — тогда сказано, как отправить позже);
# 2 — неверные параметры; 3 — сервис отчёт не принял (причина в выводе).
set -u
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

DIR="local/report"
SESSION="$DIR/session.json"
action="" scenario="" result="" deviation="" helped="" notes="" send=yes
defects=()
while [ $# -gt 0 ]; do
  case "$1" in
    --start) action=start ;;
    --show) action=show ;;
    --send) action=send ;;
    --scenario) scenario="${2:-}"; shift ;;
    --result) result="${2:-}"; shift ;;
    --deviation) deviation="${2:-}"; shift ;;
    --helped) helped="${2:-}"; shift ;;
    --defect) defects+=("${2:-}"); shift ;;
    --notes) [ "${2:-}" = - ] || { echo "--notes: только «-» (со стандартного ввода)" >&2; exit 2; }; notes=yes; shift ;;
    --no-send) send=no ;;
    *) ops_unknown "$1" ;;
  esac
  shift
done
if [ -z "$action" ]; then
  [ -n "$scenario" ] || { echo "нужен --scenario (номер из docs/pilot/scenarios-ops.md) или --start, --show, --send" >&2; exit 2; }
  action=build
elif [ -n "$scenario$result$deviation$helped" ] || [ "${#defects[@]}" -gt 0 ]; then
  echo "--$action не сочетается с --scenario, --result и текстом отчёта" >&2; exit 2
fi
command -v python3 > /dev/null 2>&1 || { echo "Нужен python3 на сервере: отчёт собирается белым списком (ops/report_filter.py)." >&2; exit 2; }
umask 077
mkdir -p "$DIR"; chmod 700 local "$DIR" 2> /dev/null || true

# Снимок: версия, тег, коммит и строки doctor — через белый список.
snap() (
  REPORT_VERSION="$(tr -d '[:space:]' < VERSION 2> /dev/null)"; REPORT_TAG="$(code_tag)"
  REPORT_COMMIT="$(git rev-parse HEAD 2> /dev/null || true)"
  export REPORT_VERSION REPORT_TAG REPORT_COMMIT
  ./ops/doctor.sh 2> /dev/null | python3 -I ops/report_filter.py snapshot .
)
latest() { find "$DIR" -maxdepth 1 -name 'r-*.json' 2> /dev/null | sort | tail -n 1; }

submit() {
  local file="$1" rc=0
  if [ "$(container_state shturman-service)" != running ]; then
    echo "Сервис переписки не запущен — отчёт владельцу не отправлен. Он сохранён: $file; отправить позже — ./ops/report.sh --send."
    return 0
  fi
  docker exec -i shturman-service shturman ops-report submit < "$file" || rc=$?
  case "$rc" in
    0) echo "Скажите владельцу: «Отчёт сессии пришёл вам в бот согласований — прочитайте и нажмите «Отправить», если согласны»." ;;
    2) echo "Сервис отчёт не принял (форма, выше) — так быть не должно: строка дефекта." >&2; return 3 ;;
    *) echo "Сервис отчёт не принял (код $rc; в образе сервиса старой версии этой команды нет). Отчёт сохранён: $file — покажите его владельцу; переслать разработчику он может текстом." >&2; return 3 ;;
  esac
}

case "$action" in
  start)
    snap > "$SESSION.tmp" && mv "$SESSION.tmp" "$SESSION" || { echo "Снимок не снят." >&2; exit 2; }
    chmod 600 "$SESSION"
    echo "Снимок «до» снят ($(date -u +%H:%M) UTC): версия, коды и имена строк doctor. В конце сессии — ./ops/report.sh --scenario … --result …"
    exit 0 ;;
  show)
    f="$(latest)"; [ -n "$f" ] || { echo "Отчётов ещё нет."; exit 0; }
    python3 -I ops/report_filter.py render "$f"; echo; echo "JSON: $f"; cat "$f"; echo
    exit 0 ;;
  send)
    f="$(latest)"; [ -n "$f" ] || { echo "Отчётов ещё нет — сначала ./ops/report.sh --scenario …" >&2; exit 2; }
    submit "$f"; exit $? ;;
esac

# --- build ---
if [ "$notes" = yes ]; then
  while IFS= read -r line; do
    case "$line" in
      "|"*) defects+=("$line") ;;
      отступ*:*|Отступ*:*) deviation="${deviation:+$deviation; }${line#*:}" ;;
      помогло:*|Помогло:*) helped="${helped:+$helped; }${line#*:}" ;;
      "") ;;
      *) deviation="${deviation:+$deviation; }$line" ;;
    esac
  done
fi
work="$DIR/work.$$"
mkdir -p "$work"
trap 'rm -rf "$work"' EXIT
snap > "$work/after.json" || echo null > "$work/after.json"
[ -s "$work/after.json" ] || echo null > "$work/after.json"
echo null > "$work/before.json"; since=""
if [ -f "$SESSION" ] && [ -n "$(find "$SESSION" -mmin -1440 2> /dev/null)" ]; then
  cp "$SESSION" "$work/before.json"; since="$(stat -c %Y "$SESSION")"
fi
# Итог обновления — если оно было в этой сессии (или за последние сутки, когда снимка «до» нет).
echo null > "$work/update.json"
if [ -f local/update/last.env ]; then
  ustart="$(stat -c %Y local/update/last.env)"
  if { [ -n "$since" ] && [ "$ustart" -ge "$since" ]; } || { [ -z "$since" ] && [ $(( $(date +%s) - ustart )) -lt 86400 ]; }; then
    python3 -I - local/update/last.env > "$work/update.json" <<'PY' || echo null > "$work/update.json"
import json, sys
d = {}
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    k, _, v = line.rstrip("\n").partition("=")
    d[k] = v
print(json.dumps({"from": d.get("from_name", ""), "to": d.get("to_name", ""), "result": d.get("result", ""),
                  "backup": bool(d.get("backup"))}, ensure_ascii=False))
PY
  fi
fi
python3 -I - "$work" "$scenario" "$result" "$deviation" "$helped" "$(shturman_mode 2> /dev/null || echo '?')" \
  ${defects[@]+"${defects[@]}"} <<'PY' || exit 2
import json, sys
from pathlib import Path
work, scenario, result, deviation, helped, mode, *defects = sys.argv[1:]
def load(name):
    try:
        return json.loads((Path(work) / name).read_text("utf-8") or "null")
    except ValueError:
        return None
json.dump({"scenario": scenario, "result": result, "deviations": deviation, "helped": helped, "mode": mode,
           "defects": defects, "before": load("before.json"), "after": load("after.json"), "update": load("update.json")},
          open(Path(work) / "args.json", "w", encoding="utf-8"), ensure_ascii=False)
PY
argsfile="$work/args.json"
out="$DIR/r-$(date -u +%Y%m%dT%H%M%SZ).json"
if ! python3 -I ops/report_filter.py build . "$argsfile" > "$out.tmp"; then rm -f "$out.tmp"; exit 2; fi
mv "$out.tmp" "$out"; chmod 600 "$out"
rm -rf "$work"; rm -f "$SESSION"
# shellcheck disable=SC2012  # имена отчётов — только дата и время
ls -1t "$DIR"/r-*.json 2> /dev/null | tail -n +21 | while IFS= read -r old; do rm -f -- "$old"; done
echo "Отчёт собран: $out. Его увидит владелец, а по его кнопке — разработчик:"
echo
python3 -I ops/report_filter.py render "$out"
echo
if [ "$send" = no ]; then echo "Не отправлен (--no-send). Отправить — ./ops/report.sh --send."; exit 0; fi
submit "$out"
