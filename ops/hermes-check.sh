#!/usr/bin/env bash
# Проверка новой версии Hermes перед тем, как сказать владельцам «с вашим „Штурманом“ она проверена» (Р-101).
# Запускается по запросу разработчика, не по расписанию. Ничего не скачивает и не перезапускает.
#   ./ops/hermes-check.sh vГГГГ.М.Д                 — что сделать на стенде, чтобы проверить образ
#                                                    nousresearch/hermes-agent:vГГГГ.М.Д: шаги, строки doctor,
#                                                    ручные сценарии плагина, что сверить в документации Hermes.
#                                                    Только печать; .env и Docker не трогаются
#   ./ops/hermes-check.sh vГГГГ.М.Д --record ВЕРСИЯ "что проверено"
#                                                 — после проверки: строка в config/hermes-versions.txt (тег,
#                                                    версия Hermes, сегодняшняя дата, что проверено). Это правка
#                                                    отслеживаемого файла — только в ветке разработки, не на
#                                                    экземпляре владельца (на нём HEAD отцеплен на теге выпуска —
#                                                    тогда отказ). Затем — та же версия в releases.json («hermes»):
#                                                    из него экземпляры узнают о проверенной версии
#   -h, --help                                     — эта справка; ничего не делается
# Коды: 0 — готово; 1 — уже в списке или не записано; 2 — неверные параметры.
set -u
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

tag="" record=no version="" what=""
while [ $# -gt 0 ]; do
  case "$1" in
    --record) record=yes; version="${2:-}"; what="${3:-}"; shift 2 || { echo "--record ВЕРСИЯ \"что проверено\"" >&2; exit 2; } ;;
    v*) [ -z "$tag" ] || ops_unknown "$1"; tag="$1" ;;
    *) ops_unknown "$1" ;;
  esac
  shift
done
printf '%s' "$tag" | grep -Eq '^v[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}$' \
  || { echo "нужен тег образа Hermes вида v2026.9.24 (тег nousresearch/hermes-agent, не latest)" >&2; exit 2; }

def="$(sed -n 's/.*hermes-agent:\${HERMES_VERSION:-\([^}]*\)}.*/\1/p' compose.yaml 2> /dev/null | head -n 1)"
if [ "$record" = yes ]; then
  printf '%s' "$version" | grep -Eq '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$' || { echo "ВЕРСИЯ — номер Hermes вида 0.21.5" >&2; exit 2; }
  what="$(printf '%s' "$what" | tr -d '\t\r\n')"
  [ "${#what}" -ge 10 ] || { echo "«что проверено» — словами: сценарии, строки doctor, стенд (не короче 10 знаков)" >&2; exit 2; }
  if hermes_verified "$tag"; then echo "$tag уже в $HERMES_VERSIONS_FILE — ничего не записано."; exit 1; fi
  if ! git symbolic-ref -q HEAD > /dev/null 2>&1; then
    echo "HEAD отцеплен (экземпляр на теге выпуска): список проверенных версий правится в ветке разработки и приходит" >&2
    echo "на экземпляры с выпуском, а не правкой отслеживаемого файла на сервере (AGENTS.md). Ничего не записано." >&2
    exit 1
  fi
  printf '%s\t%s\t%s\t%s\n' "$tag" "$version" "$(date +%F)" "$what" >> "$HERMES_VERSIONS_FILE"
  echo "Записано в $HERMES_VERSIONS_FILE: $tag ($version)."
  echo "Дальше — в той же ветке:"
  echo "  1. releases.json, список «hermes»: {\"tag\": \"$tag\", \"version\": \"$version\", \"verified\": \"$(date +%F)\"} —"
  echo "     из него экземпляры в режиме с Hermes узнают «вышла версия Hermes $version, с вашим «Штурманом» проверена»;"
  echo "  2. python3 ops/release_manifest.py check releases.json --hermes-file $HERMES_VERSIONS_FILE — списки совпадают;"
  echo "  3. если $tag становится версией по умолчанию — compose.yaml (сейчас $def) и раздел UPGRADING.md выпуска;"
  echo "  4. CHANGELOG.md и PR. После слияния в main экземпляры узнают о версии при следующей суточной проверке."
  exit 0
fi

echo "Проверка образа nousresearch/hermes-agent:$tag (docs/releases.md, «Новая версия Hermes»)"
if hermes_verified "$tag"; then echo "  уже в списке проверенных ($HERMES_VERSIONS_FILE)."; else echo "  в списке проверенных его нет."; fi
echo "  версия по умолчанию сейчас (compose.yaml): ${def:-?}; проверенные: $(awk -F'\t' '!/^#/ && NF {printf "%s ", $1}' "$HERMES_VERSIONS_FILE" 2> /dev/null)"
cat <<EOF

1. Документация Hermes к $tag (заметки выпуска, website/docs/): что изменилось в
   - плагинах и их загрузке (plugins/shturman: мост, вход в дашборд, мастер настройки);
   - подключении MCP (mcp_servers в config.yaml, переменные .env профиля — строка archive-mcp);
   - планировщике и часовом поясе профиля (hermes cron, timezone в config.yaml — строки hermes-tz, cron-schedule);
   - журналах: пишет ли Hermes текст сообщений (тогда ./ops/diag.sh берёт только счётчики — ops/diag_filter.py);
   - подсказке об обновлении: updates.check, баннер, кнопка в дашборде, команда /update в Telegram — что видит
     владелец при установке из образа (docs/pilot/decisions-options.md, «б»);
   - бизнес-режиме Telegram (issue NousResearch/hermes-agent #127430: бот Hermes в бизнес-режиме не подключаем).
   Есть локальный клон — то же по коду: git -C <клон> diff <прежний тег> $tag -- hermes_cli gateway.
2. Стенд (не экземпляр владельца): ./ops/backup.sh; в .env стенда HERMES_VERSION=$tag; ./ops/up.sh --pull
   (без свежей копии остановится — так и должно быть).
3. ./ops/doctor.sh → ИТОГ: OK; отдельно строки hermes, hermes-version (WARN «нет в списке» — пока ожидаемо), plugin,
   plugin-code, archive-mcp, hermes-tz, cron-schedule, summaries, summary-last, bot-live, login.
4. Ручные сценарии плагина (нужен установленный Hermes):
EOF
for f in plugins/shturman/tests/e2e/*.py plugins/shturman/tests/e2e/*.mjs; do
  [ -e "$f" ] && echo "   - $f"
done
cat <<EOF
5. Откат на стенде: прежний HERMES_VERSION и ./ops/restore.sh --from <метка копии шага 2> — откат Hermes возможен
   только восстановлением data/hermes (его документация откат не поддерживает).
6. Всё прошло — ./ops/hermes-check.sh $tag --record <версия Hermes> "что проверено" в ветке разработки, затем
   releases.json и PR (команда сама скажет, что дальше).
EOF
