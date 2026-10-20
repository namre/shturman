#!/usr/bin/env bash
# Внешний сторож (Р-98): адрес, который сервис переписки раз в 5 минут зовёт «жив, всё в порядке», а при
# неполадке (сессия Telegram слетела, сводка не ушла, база не отвечает) — тот же адрес с /fail. Служба
# сторожа сама пишет владельцу, если пульс пришёл с /fail или перестал приходить вовсе — сервер выключился,
# пропала сеть. Какие службы подходят и как завести бесплатно — docs/deployment.md, «Внешний сторож».
# Службу заводит ВЛАДЕЛЕЦ на своё имя (почта, Telegram для уведомлений); агент ничего не регистрирует.
#
#   ./ops/pulse.sh set https://hc-ping.com/<ваш-uuid>   — записать адрес (SHTURMAN_PULSE_URL в .env);
#   ./ops/pulse.sh set -                                — то же, адрес читается из ввода (не попадает
#                                                          в историю команд и в список процессов);
#   ./ops/pulse.sh off                                  — убрать адрес: сторожа нет;
#   ./ops/pulse.sh status                               — задан ли адрес (только имя узла) и что сервис
#                                                          отправил последним;
#   ./ops/pulse.sh test                                 — отправить один пробный пульс «ok» сейчас, с сервера.
# После set и off — ./ops/up.sh: сервис читает адрес при запуске.
#
# Адрес — не секрет уровня токена (по нему можно только отметиться «жив»), но целиком он нигде не
# печатается: только имя узла. Принимается только https. Из .env читается и пишется одна строка —
# SHTURMAN_PULSE_URL.
#   -h, --help — эта справка; ничего не меняется.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

cmd="${1:-status}"
case "$cmd" in
  set) [ $# -eq 2 ] || { echo "нужен адрес: ./ops/pulse.sh set https://… (или «-» — прочитать из ввода)" >&2; exit 2; } ;;
  off|status|test) [ $# -le 1 ] || ops_unknown "$2" ;;
  *) ops_unknown "$cmd" ;;
esac

# Имя узла из адреса https://имя[:порт]/путь — для показа.
host_of() { printf '%s' "$1" | sed -E 's|^https://([^/:?#]+).*|\1|'; }

# Годится ли адрес: https, имя узла, путь без пробелов, кавычек и обратных слэшей; без логина и пароля.
pulse_valid() {
  printf '%s' "$1" | grep -Eq '^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%!$&()*+,;=:@/-]*)?(\?[A-Za-z0-9._~%!$&()*+,;=:@/?-]*)?$' || return 1
  [ "${#1}" -le 400 ]
}

case "$cmd" in
  set)
    url="$2"
    if [ "$url" = "-" ]; then
      printf 'Вставьте адрес пульса (https://…) и нажмите Enter: ' >&2
      IFS= read -r url || true
    fi
    url="$(printf '%s' "$url" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
    if ! pulse_valid "$url"; then
      echo "адрес не принят: нужен https://… без пробелов и кавычек, например https://hc-ping.com/<uuid> (docs/deployment.md, «Внешний сторож»)" >&2
      exit 2
    fi
    [ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }
    env_put SHTURMAN_PULSE_URL "$url"
    echo "Адрес внешнего сторожа записан ($(host_of "$url")/…). Чтобы сервис начал пульс — ./ops/up.sh;"
    echo "проверка — ./ops/pulse.sh test и строка pulse в ./ops/doctor.sh."
    ;;
  off)
    [ -f .env ] || { echo "нет .env" >&2; exit 1; }
    if env_has SHTURMAN_PULSE_URL; then
      env_del SHTURMAN_PULSE_URL
      echo "Адрес внешнего сторожа убран. ./ops/up.sh — и сервис перестанет слать пульс."
      echo "Сама служба сторожа через свой срок сообщит владельцу «пульса нет» — приостановите проверку там."
    else
      echo "Адрес внешнего сторожа не задан — менять нечего."
    fi
    ;;
  status)
    url="$(env_get SHTURMAN_PULSE_URL)"
    if [ -z "$url" ]; then
      echo "внешний сторож: адрес не задан — сторожа нет (docs/deployment.md, «Внешний сторож»)"
    else
      echo "внешний сторож: адрес задан ($(host_of "$url")/…)"
    fi
    out="$(docker exec shturman-service shturman call GET /api/status 2>/dev/null | python3 -c '
import json, sys
try:
    p = json.load(sys.stdin).get("pulse") or {}
except Exception:
    sys.exit(0)
if not p.get("configured"):
    print("сервис: пульса нет (адрес не передан сервису — после set нужен ./ops/up.sh)")
elif not p.get("enabled"):
    print("сервис: адрес не годится (%s)" % p.get("problem"))
elif p.get("last_age_s") is None:
    print("сервис: пульс включён, первый — через минуту после запуска")
else:
    sent = "«всё в порядке»" if p.get("last_sent") == "ok" else "«неполадка»: " + " ".join(p.get("failing") or [])
    deliv = "служба приняла" if p.get("last_delivered") else "служба НЕ приняла (%s)" % p.get("last_error")
    print("сервис: последний пульс %d мин назад — %s; %s" % (p["last_age_s"] // 60, sent, deliv))' 2>/dev/null || true)"
    if [ -n "$out" ]; then echo "$out"; fi
    ;;
  test)
    url="$(env_get SHTURMAN_PULSE_URL)"
    [ -n "$url" ] || { echo "адрес не задан: ./ops/pulse.sh set …" >&2; exit 1; }
    pulse_valid "$url" || { echo "адрес в .env не годится — ./ops/pulse.sh set заново" >&2; exit 1; }
    # Адрес передаётся curl через ввод (-K -), а не в командной строке: так его нет в списке процессов.
    code="$(printf 'url = "%s"\n' "$url" | curl -s -o /dev/null -m 15 -w '%{http_code}' -X POST \
      -H 'Content-Type: text/plain' --data-binary 'test from ./ops/pulse.sh' -K - 2>/dev/null)" || code="${code:-000}"
    case "$code" in
      2??) echo "Пробный пульс принят службой ($(host_of "$url"), HTTP $code). В её журнале появится запись «test from ./ops/pulse.sh»." ;;
      000) echo "Пробный пульс не дошёл: нет связи с $(host_of "$url") (сеть сервера, прокси, firewall)." >&2; exit 1 ;;
      *)   echo "Служба $(host_of "$url") ответила HTTP $code — адрес неверный или проверку удалили; запишите адрес заново: ./ops/pulse.sh set -" >&2; exit 1 ;;
    esac
    ;;
esac
