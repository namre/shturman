#!/usr/bin/env bash
# Включает или выключает поиск по смыслу (локальные эмбеддинги) и выбирает его модель.
#   ./ops/embeddings.sh on [--model e5-small|user2-small]
#                               — скачать модель с huggingface.co (с проверкой контрольных сумм
#                                 каждого файла) и добавить контейнер с ней. Сам контейнер
#                                 в интернет не ходит. Без --model — модель, записанная в .env,
#                                 а если её там нет — e5-small.
#   ./ops/embeddings.sh off     — убрать контейнер; поиск остаётся, но только по словам.
#                                 Выбранная модель и скачанные файлы сохраняются.
#   ./ops/embeddings.sh status  — что включено, какая модель и сколько сообщений обработано
#
# Модели (обе на 384 измерения, поэтому схема базы от выбора не зависит; docs/search.md):
#   e5-small     intfloat/multilingual-e5-small — по умолчанию. Скачивается около 0,5 ГБ,
#                в работе около 1,2 ГБ памяти.
#   user2-small  deepvk/USER2-small — русскоязычная. Скачивается около 0,14 ГБ, в работе
#                0,2–0,7 ГБ памяти. На нашем синтетическом наборе коротких сообщений находит
#                заметно больше, на энциклопедических наборах — меньше.
#
# СМЕНА МОДЕЛИ — решение владельца (стоп-точка в AGENTS.md): все векторы пересчитываются заново,
# на 300 000 сообщений это несколько часов. Пока идёт пересчёт, поиск по смыслу видит только уже
# пересчитанные сообщения (сначала свежие), поиск по словам работает как обычно. Возврат к прежней
# модели — такой же пересчёт; файлы моделей при этом повторно не скачиваются.
#
# Значения, которые скрипт пишет в .env, не секретные: адрес контейнера, имя модели и её каталог.
# Из .env он читает только строки SHTURMAN_MODE, COMPOSE_PROFILES и SHTURMAN_EMBEDDINGS_MODEL: свой
# профиль добавляется и убирается, а защита от внедрённых инструкций (./ops/guard.sh) и профиль
# Hermes не трогаются.
#   -h, --help — эта справка; ничего не скачивается и не меняется.
set -eu
cd "$(dirname "$0")/.." || exit 1

. ops/lib.sh
ops_help "$@"

usage() { echo "использование: $0 on [--model e5-small|user2-small] | off | status" >&2; exit 2; }

action="${1:-status}"
case "$action" in
  on|off|status) ;;
  -*) ops_unknown "$action" ;;
  *) usage ;;
esac
[ $# -gt 0 ] && shift
want=""
while [ $# -gt 0 ]; do
  case "$1" in
    --model)
      [ $# -ge 2 ] || { echo "после --model нужно имя: e5-small или user2-small" >&2; exit 2; }
      want="$2"; shift ;;
    --model=*) want="${1#--model=}" ;;
    *) ops_unknown "$1" ;;
  esac
  shift
done
if [ -n "$want" ] && [ "$action" != on ]; then
  echo "--model имеет смысл только вместе с on" >&2; exit 2
fi

[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }
# Неизвестный режим установки — остановиться до скачивания модели, а не после.
shturman_mode > /dev/null || exit 2

# Перечень моделей. Добавление или смена ревизии — отдельным изменением: вместе с суммами ниже,
# приставками в service/src/shturman/embeddings.py (MODEL_PROFILES) и новым измерением
# (service/tools/search_eval.py, docs/search.md).
#   MODEL — имя на huggingface.co; под ним же векторы записаны в архиве;
#   REV   — точная ревизия репозитория модели;
#   DIR   — каталог модели внутри data/embeddings. У e5-small он называется model: так он
#           назывался, когда модель была одна, и уже развёрнутым экземплярам не нужно ничего
#           перекачивать и править в .env;
#   FILES — «файл в репозитории модели : sha256», по записи на строку. Суммы всех файлов сверены
#           скачиванием 2026-10-07; лицензии по карточкам: e5-small — MIT, USER2-small — Apache-2.0.
model_spec() {
  case "$1" in
    e5-small)
      MODEL=intfloat/multilingual-e5-small
      REV=614241f622f53c4eeff9890bdc4f31cfecc418b3
      DIR=model
      SIZE="около 0,5 ГБ"; NEED_MB=1200
      FILES="
config.json:69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959
tokenizer.json:0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39
tokenizer_config.json:a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b
special_tokens_map.json:d05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7
modules.json:c6e29747481e8b5dd2b58401966aeac910de39092f90cda9a704b1545f902b04
sentence_bert_config.json:948201d8329907aae938fa62f9ceeed53f5694dacc2b87b9f3b78b37ee986529
1_Pooling/config.json:987f7a67a38fa564c849bb5d277c52ab9088a84368fc0be31a354125aebb12a0
onnx/model.onnx:ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665
" ;;
    user2-small)
      # Весов в формате ONNX у модели нет: TEI запускает её из model.safetensors (бэкенд Candle).
      MODEL=deepvk/USER2-small
      REV=23f65b34cf7632032061f5cc66c14714e6d4cee4
      DIR=user2-small
      SIZE="около 0,14 ГБ"; NEED_MB=700
      FILES="
config.json:ccf3bfc44ddc6eb2184c35347b975c17d1bffe7a63103e5fdc92c6a48adc062c
tokenizer.json:8bdc337bdee65a5fdb697df1ac7453b2ae5c34c105125eac5f68c87827395c4e
tokenizer_config.json:818f4315a225493cfb33e07d6cb49e56432a90364ef080cbe0c594ceb28ec59a
special_tokens_map.json:2ea3c1eb27baf06d75115d453c067c1832d7101a97243b298a4af8d59c916d62
modules.json:8f4b264b80206c830bebbdcae377e137925650a433b689343a63bdc9b3145460
sentence_bert_config.json:eb9b44b13c0f52a3b3685c3b1cbdea1ba8b04bea123b98f61610048940776eb1
config_sentence_transformers.json:3a032a6eacfd7c2ed17dbad876d0b940ed7d2ac0091da43c2a89a880e692ddb9
1_Pooling/config.json:a19c83805e1ce4174f3fbfec4ac8d3b8dbae0c958f8fd51b80937eb33e0c5335
model.safetensors:f4fb765b2302192d3ac2315fd3ee0951f23efca4ad4ad19636b637e81d790529
" ;;
    *) return 1 ;;
  esac
}

# Ярлык модели по имени, записанному в .env. Пусто — модель по умолчанию.
label_of() {
  case "$1" in
    ""|intfloat/multilingual-e5-small) echo e5-small ;;
    deepvk/USER2-small) echo user2-small ;;
    *) return 1 ;;
  esac
}

# Скачивает файлы модели в её каталог и сверяет сумму каждого. Файл с верной суммой повторно
# не скачивается; с неверной (оборванная загрузка, подмена) — заменяется.
fetch_model() {
  local dir="data/embeddings/$DIR" entry remote sum
  for entry in $FILES; do
    remote="${entry%%:*}"; sum="${entry##*:}"
    mkdir -p "$(dirname "$dir/$remote")"
    if [ -s "$dir/$remote" ] && printf '%s  %s\n' "$sum" "$dir/$remote" | sha256sum -c --quiet - >/dev/null 2>&1; then
      continue
    fi
    echo "скачиваю $remote…"
    curl -fsSL --retry 3 -m 1800 -o "$dir/$remote.part" "https://huggingface.co/$MODEL/resolve/$REV/$remote" \
      || { rm -f "$dir/$remote.part"; echo "не удалось скачать $remote" >&2; return 1; }
    printf '%s  %s\n' "$sum" "$dir/$remote.part" | sha256sum -c --quiet - >/dev/null 2>&1 \
      || { rm -f "$dir/$remote.part"; echo "контрольная сумма $remote не совпала — файл не используется" >&2; return 1; }
    mv "$dir/$remote.part" "$dir/$remote"
  done
  chmod -R a+rX data/embeddings
}

case "$action" in
  on)
    cur="$(env_get SHTURMAN_EMBEDDINGS_MODEL)"
    old="$(label_of "$cur")" || old=""
    if [ -z "$old" ] && [ -z "$want" ]; then
      # Имя, которого скрипт не знает (вписано вручную), — не повод молча заменить его моделью
      # по умолчанию: это стоило бы пересчёта всех векторов.
      echo "в .env записана модель поиска по смыслу, которую этот скрипт не ставит (SHTURMAN_EMBEDDINGS_MODEL)." >&2
      echo "Назовите модель явно: $0 on --model e5-small или --model user2-small — это пересчёт всех векторов." >&2
      exit 2
    fi
    label="${want:-$old}"
    # Считалось ли уже что-то прежней моделью: поиск по смыслу включён или включался раньше.
    used=no
    if profile_on embeddings || [ -n "$cur" ]; then used=yes; fi
    model_spec "$label" || { echo "неизвестная модель: $label — допустимы e5-small и user2-small" >&2; exit 2; }
    echo "Модель поиска по смыслу: $label ($MODEL), скачивается $SIZE."
    if [ "$label" != "$old" ] && [ "$used" = yes ]; then
      echo "ВНИМАНИЕ: модель меняется${old:+ ($old → $label)}. Векторы всех сообщений будут пересчитаны заново:"
      echo "на 300 000 сообщений это несколько часов. До конца пересчёта поиск по смыслу видит только уже"
      echo "пересчитанные сообщения (сначала свежие); поиск по словам работает как обычно."
    fi
    fetch_model || exit 1
    mem_mb="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
    if [ "${mem_mb:-0}" -lt $((NEED_MB + 300)) ]; then
      echo "Свободной памяти ${mem_mb} МБ — для модели нужно около ${NEED_MB} МБ. Включаю, но следите за ./ops/doctor.sh." >&2
    fi
    umask 077
    profile_set on embeddings
    env_set SHTURMAN_EMBEDDINGS_URL http://embeddings:80
    # Одна и та же строка задаёт и имя, под которым контейнер отдаёт модель, и имя, которым сервис
    # подписывает векторы (compose.yaml): разойтись они не могут.
    env_set SHTURMAN_EMBEDDINGS_MODEL "$MODEL"
    env_set SHTURMAN_EMBEDDINGS_MODEL_DIR "$DIR"
    echo "Поиск по смыслу включён в настройках. Применяю: ./ops/up.sh"
    exec ./ops/up.sh ;;
  off)
    umask 077
    profile_set off embeddings
    env_set SHTURMAN_EMBEDDINGS_URL ""
    # Контейнер выключенного профиля Compose сам не останавливает: для него это не «лишний»
    # контейнер. Убираем явно, иначе модель продолжала бы занимать память. Файлы модели остаются.
    if docker inspect shturman-embeddings >/dev/null 2>&1; then
      echo "Останавливаю и убираю контейнер shturman-embeddings (файлы модели остаются в data/embeddings)…"
      docker compose --profile embeddings rm --stop --force embeddings >/dev/null 2>&1 || true
      # Если контейнер создавали не этим файлом Compose, первая команда его не увидит.
      if docker inspect shturman-embeddings >/dev/null 2>&1; then docker rm --force shturman-embeddings >/dev/null; fi
    fi
    echo "Поиск по смыслу выключен в настройках. Применяю: ./ops/up.sh"
    exec ./ops/up.sh ;;
  status)
    if profile_on embeddings; then echo "в настройках: включено"; else echo "в настройках: выключено"; fi
    cur="$(env_get SHTURMAN_EMBEDDINGS_MODEL)"
    if label="$(label_of "$cur")"; then
      model_spec "$label"
      echo "модель: $label ($MODEL)"
    else
      echo "модель: записана вручную, этот скрипт её не ставит"
    fi
    docker exec shturman-service shturman call GET /api/embeddings/status 2>/dev/null \
      || echo "сервис переписки не отвечает — ./ops/doctor.sh" ;;
  *) usage ;;
esac
