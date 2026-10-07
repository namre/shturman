#!/usr/bin/env bash
# Включает или выключает поиск по смыслу (локальные эмбеддинги).
#   ./ops/embeddings.sh on      — скачать модель (~0,5 ГБ с huggingface.co, с проверкой контрольных
#                                 сумм) и добавить контейнер с ней (около 1,2 ГБ памяти).
#                                 Сам контейнер в интернет не ходит.
#   ./ops/embeddings.sh off     — убрать; поиск остаётся, но только по словам
#   ./ops/embeddings.sh status  — показать, что включено и сколько сообщений обработано
# Значения, которые скрипт пишет в .env, не секретные. Из .env он читает только строку
# COMPOSE_PROFILES.
set -eu
cd "$(dirname "$0")/.." || exit 1

mode="${1:-status}"
[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }

set_env() {
  local tmp; tmp="$(mktemp .env.XXXXXX)"
  grep -Ev "^$1=" .env > "$tmp" || true
  [ -n "$2" ] && printf '%s=%s\n' "$1" "$2" >> "$tmp"
  chmod 600 "$tmp"; mv "$tmp" .env
}

# Профили Compose — общий список через запятую: рядом с поиском по смыслу может быть включена
# защита от внедрённых инструкций (./ops/guard.sh), поэтому свой профиль добавляется и убирается,
# а чужой не трогается. Из .env читается только строка COMPOSE_PROFILES.
set_profile() {
  local want="$1" name="$2" now out="" p
  now="$(grep -E '^COMPOSE_PROFILES=' .env | tail -n 1 | cut -d= -f2- || true)"
  local IFS=','
  for p in $now; do
    [ -n "$p" ] && [ "$p" != "$name" ] && out="${out:+$out,}$p"
  done
  [ "$want" = "on" ] && out="${out:+$out,}$name"
  set_env COMPOSE_PROFILES "$out"
}

# Модель и её точная версия. Смена модели — отдельным изменением: вместе с суммами ниже
# и настройкой SHTURMAN_EMBEDDINGS_MODEL сервиса.
MODEL=intfloat/multilingual-e5-small
REV=614241f622f53c4eeff9890bdc4f31cfecc418b3
FILES="config.json tokenizer.json tokenizer_config.json special_tokens_map.json modules.json sentence_bert_config.json 1_Pooling/config.json onnx/model.onnx"
SUM_ONNX=ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665
SUM_TOKENIZER=0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39

fetch_model() {
  local dir=data/embeddings/model f
  mkdir -p "$dir/onnx" "$dir/1_Pooling"
  for f in $FILES; do
    [ -s "$dir/$f" ] && continue
    echo "скачиваю $f…"
    curl -fL --retry 3 -m 1800 -o "$dir/$f.part" "https://huggingface.co/$MODEL/resolve/$REV/$f" \
      || { rm -f "$dir/$f.part"; echo "не удалось скачать $f" >&2; return 1; }
    mv "$dir/$f.part" "$dir/$f"
  done
  printf '%s  %s\n%s  %s\n' "$SUM_ONNX" "$dir/onnx/model.onnx" "$SUM_TOKENIZER" "$dir/tokenizer.json" \
    | sha256sum -c --quiet - || { echo "контрольная сумма модели не совпала — файлы в $dir не используются" >&2; return 1; }
  chmod -R a+rX data/embeddings
}

case "$mode" in
  on)
    fetch_model || exit 1
    mem_mb="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
    if [ "${mem_mb:-0}" -lt 1500 ]; then
      echo "Свободной памяти ${mem_mb} МБ — для модели нужно около 1200 МБ. Включаю, но следите за ./ops/doctor.sh." >&2
    fi
    umask 077
    set_profile on embeddings
    set_env SHTURMAN_EMBEDDINGS_URL http://embeddings:80
    echo "Поиск по смыслу включён в настройках. Применяю: ./ops/up.sh"
    exec ./ops/up.sh ;;
  off)
    umask 077
    set_profile off embeddings
    set_env SHTURMAN_EMBEDDINGS_URL ""
    echo "Поиск по смыслу выключен в настройках. Применяю: ./ops/up.sh"
    exec ./ops/up.sh ;;
  status)
    if grep -Eq '^COMPOSE_PROFILES=.*embeddings' .env; then echo "в настройках: включено"; else echo "в настройках: выключено"; fi
    docker exec shturman-service shturman call GET /api/embeddings/status 2>/dev/null \
      || echo "сервис переписки не отвечает — ./ops/doctor.sh" ;;
  *) echo "использование: $0 on|off|status" >&2; exit 2 ;;
esac
