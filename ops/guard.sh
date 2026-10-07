#!/usr/bin/env bash
# Включает или выключает защиту от внедрённых инструкций во входящих сообщениях (docs/guard.md).
#   ./ops/guard.sh on      — скачать модель-классификатор (~0,3 ГБ с huggingface.co, с проверкой
#                            контрольных сумм), добавить контейнер с ней (около 0,6 ГБ памяти)
#                            и включить проверку. Сам контейнер в интернет не ходит.
#   ./ops/guard.sh off     — выключить проверку и убрать контейнер. Уже скрытые сообщения остаются
#                            скрытыми, пока владелец не решит по ним в боте.
#   ./ops/guard.sh status  — что включено и счётчики: проверено, скрыто, показано, не проверено
# Значения, которые скрипт пишет в .env, не секретные. Из .env он читает только строки
# SHTURMAN_MODE, SHTURMAN_GUARD и COMPOSE_PROFILES: свой профиль добавляется и убирается,
# а поиск по смыслу (./ops/embeddings.sh) и профиль Hermes не трогаются.
#
# Защита снижает риск, а не устраняет его: классификатор пропускает часть атак и иногда прячет
# обычные сообщения. Измеренные числа — docs/guard.md.
set -eu
cd "$(dirname "$0")/.." || exit 1

. ops/lib.sh
mode="${1:-status}"
[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }
# Неизвестный режим установки — остановиться до скачивания модели, а не после.
shturman_mode > /dev/null || exit 2

# Модель и её точная версия. Смена модели — отдельным изменением: вместе с суммами ниже, именем
# в compose.yaml (--served-model-name), значением по умолчанию в service/src/shturman/config.py
# и новым измерением (service/tools/guard_eval.py, docs/guard.md).
# Лицензия модели — Apache-2.0 (карточка на huggingface.co, сверено 2026-10-07).
MODEL=Horizon-Labs/prompt-injection-guard-small
REV=3215a27edd62c5ba0bd786c57a9d243b2158e70e
# «Файл в репозитории модели : имя на диске : sha256». Квантованные веса (int8 для таблицы
# эмбеддингов) кладутся как model.onnx — под этим именем их ищет TEI.
FILES="
config.json:config.json:0a0beb515863050b6b146130025bee80e9a062adb0c10911999367bcc308eef3
tokenizer.json:tokenizer.json:47834a7dbbb0c4324fe9569fe47955241594a9ee539c9e478755d6fbc51e5f46
tokenizer_config.json:tokenizer_config.json:ea349495764b24570b4560fe2e4a557e5c0a47e6bce782c2409b1d6445cf8633
special_tokens_map.json:special_tokens_map.json:baec30ea10906f16adb8c18af7a34023002c1746542612b8b41c9f09e1351351
onnx/model_quantized.onnx:model.onnx:541471ab9de1af3e0db63b8208ec7fdc3ef74202b76c5470115e153c052dae02
"

fetch_model() {
  local dir=data/guard/model entry remote name sum
  mkdir -p "$dir"
  for entry in $FILES; do
    remote="${entry%%:*}"; name="${entry#*:}"; name="${name%%:*}"; sum="${entry##*:}"
    # Файл с верной суммой повторно не скачивается; с неверной (оборванная загрузка) — заменяется.
    if [ -s "$dir/$name" ] && printf '%s  %s\n' "$sum" "$dir/$name" | sha256sum -c --quiet - >/dev/null 2>&1; then
      continue
    fi
    echo "скачиваю $remote…"
    curl -fL --retry 3 -m 1800 -o "$dir/$name.part" "https://huggingface.co/$MODEL/resolve/$REV/$remote" \
      || { rm -f "$dir/$name.part"; echo "не удалось скачать $remote" >&2; return 1; }
    printf '%s  %s\n' "$sum" "$dir/$name.part" | sha256sum -c --quiet - \
      || { rm -f "$dir/$name.part"; echo "контрольная сумма $remote не совпала — файл не используется" >&2; return 1; }
    mv "$dir/$name.part" "$dir/$name"
  done
  chmod -R a+rX data/guard
}

case "$mode" in
  on)
    fetch_model || exit 1
    mem_mb="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
    if [ "${mem_mb:-0}" -lt 900 ]; then
      echo "Свободной памяти ${mem_mb} МБ — для модели нужно около 600 МБ. Включаю, но следите за ./ops/doctor.sh." >&2
    fi
    umask 077
    profile_set on guard
    env_set SHTURMAN_GUARD on
    env_set SHTURMAN_GUARD_URL http://guard:80
    echo "Защита от внедрённых инструкций включена в настройках. Применяю: ./ops/up.sh"
    exec ./ops/up.sh ;;
  off)
    umask 077
    profile_set off guard
    env_set SHTURMAN_GUARD ""
    env_set SHTURMAN_GUARD_URL ""
    echo "Защита от внедрённых инструкций выключена в настройках. Применяю: ./ops/up.sh"
    exec ./ops/up.sh ;;
  status)
    if [ "$(env_get SHTURMAN_GUARD)" = on ]; then echo "в настройках: включено"; else echo "в настройках: выключено"; fi
    docker exec shturman-service shturman call GET /api/guard/status 2>/dev/null \
      || echo "сервис переписки не отвечает — ./ops/doctor.sh" ;;
  *) echo "использование: $0 on|off|status" >&2; exit 2 ;;
esac
