#!/usr/bin/env bash
# Включает или выключает расшифровку голосовых сообщений и «кружков» (docs/voice.md).
#   ./ops/asr.sh on      — скачать модель распознавания речи GigaAM-Multilingual (ai-sage, MIT;
#                          ~0,9 ГБ с huggingface.co, с проверкой контрольных сумм), собрать контейнер
#                          распознавания (образ ~2,3 ГБ) и включить расшифровку. В работе контейнер
#                          занимает около 1,4 ГБ памяти и в интернет не ходит.
#   ./ops/asr.sh off     — выключить расшифровку и убрать контейнер. Готовые расшифровки остаются
#                          в архиве; файлы модели — в data/asr.
#   ./ops/asr.sh status  — что включено и счётчики: ждут, расшифровано, пропущено, не получилось
# Значения, которые скрипт пишет в .env, не секретные. Из .env он читает только строки
# SHTURMAN_MODE, SHTURMAN_ASR и COMPOSE_PROFILES: свой профиль добавляется и убирается,
# остальные профили не трогаются.
#
# Модель запускается несжатой, как её выложил производитель. Образ рассчитан на процессоры x86-64.
#   -h, --help — эта справка; ничего не скачивается и не меняется.
set -eu
cd "$(dirname "$0")/.." || exit 1

. ops/lib.sh
ops_help "$@"
mode="${1:-status}"
case "$mode" in
  on|off|status) ;;
  -*) ops_unknown "$mode" ;;
  *) echo "использование: $0 on|off|status" >&2; exit 2 ;;
esac
[ $# -le 1 ] || ops_unknown "$2"
[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }
shturman_mode > /dev/null || exit 2

# Модель и её точная версия (ветка ctc репозитория производителя). Смена модели — отдельным
# изменением: вместе с суммами ниже, именем в asr/server.py и замером в docs/voice.md.
# Лицензия модели — MIT (карточка на huggingface.co, сверено 2026-10-08).
MODEL=ai-sage/GigaAM-Multilingual
REV=2f8a57144e6ec3adfd32fe0484d9ea9913305bc8
# «Файл в репозитории модели : sha256». modeling_gigaam.py — код модели производителя: контейнер
# исполняет его, поэтому он закреплён суммой так же, как веса.
FILES="
config.json:c830232c7d51688a630a221517b52585ab5ee57e1d3c21bcbae01759351d2653
modeling_gigaam.py:6d02e640fbb5738ab11c030520a68654ef32f4ff363723db10534cf8b5d5c0e7
pytorch_model.bin:e1db43873ec5e296f229572e06e2470fc157ac9f8d4aacabda295630b9b91728
"

fetch_model() {
  local dir=data/asr/model entry name sum
  mkdir -p "$dir"
  for entry in $FILES; do
    name="${entry%%:*}"; sum="${entry##*:}"
    if [ -s "$dir/$name" ] && printf '%s  %s\n' "$sum" "$dir/$name" | sha256sum -c --quiet - >/dev/null 2>&1; then
      continue
    fi
    echo "скачиваю $name…"
    curl -fL --retry 3 -m 3600 -o "$dir/$name.part" "https://huggingface.co/$MODEL/resolve/$REV/$name" \
      || { rm -f "$dir/$name.part"; echo "не удалось скачать $name" >&2; return 1; }
    printf '%s  %s\n' "$sum" "$dir/$name.part" | sha256sum -c --quiet - \
      || { rm -f "$dir/$name.part"; echo "контрольная сумма $name не совпала — файл не используется" >&2; return 1; }
    mv "$dir/$name.part" "$dir/$name"
  done
  chmod -R a+rX data/asr
}

case "$mode" in
  on)
    case "$(uname -m)" in x86_64|amd64) ;; *)
      echo "Контейнер распознавания собирается только для процессоров x86-64, а здесь $(uname -m)." >&2; exit 1 ;;
    esac
    fetch_model || exit 1
    mem_mb="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
    if [ "${mem_mb:-0}" -lt 1800 ]; then
      echo "Свободной памяти ${mem_mb} МБ — распознаванию нужно около 1,4 ГБ. Включаю, но следите за ./ops/doctor.sh;" >&2
      echo "рекомендации по памяти сервера — docs/voice.md." >&2
    fi
    umask 077
    profile_set on asr
    env_set SHTURMAN_ASR on
    env_set SHTURMAN_ASR_URL http://asr:8000
    echo "Расшифровка голосовых включена в настройках. Собираю контейнер и применяю: ./ops/up.sh"
    exec ./ops/up.sh ;;
  off)
    umask 077
    profile_set off asr
    env_set SHTURMAN_ASR ""
    env_set SHTURMAN_ASR_URL ""
    if docker inspect shturman-asr >/dev/null 2>&1; then
      echo "Останавливаю и убираю контейнер shturman-asr (файлы модели остаются в data/asr)…"
      docker compose --profile asr rm --stop --force asr >/dev/null 2>&1 || true
      if docker inspect shturman-asr >/dev/null 2>&1; then docker rm --force shturman-asr >/dev/null; fi
    fi
    echo "Расшифровка голосовых выключена в настройках. Применяю: ./ops/up.sh"
    exec ./ops/up.sh ;;
  status)
    if [ "$(env_get SHTURMAN_ASR)" = on ]; then echo "в настройках: включено"; else echo "в настройках: выключено"; fi
    docker exec shturman-service shturman call GET /api/voice/status 2>/dev/null \
      || echo "сервис переписки не отвечает — ./ops/doctor.sh" ;;
  *) echo "использование: $0 on|off|status" >&2; exit 2 ;;
esac
