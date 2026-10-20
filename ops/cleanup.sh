#!/usr/bin/env bash
# Безопасная очистка диска (Р-98). Данные экземпляра (data/), тома и базу не трогает НИКОГДА: здесь нет
# ни docker system prune, ни docker volume prune, ни down -v.
#
#   ./ops/cleanup.sh                 — то же, что --dry-run: показать, что занимает место и что можно
#   ./ops/cleanup.sh --dry-run         освободить. Ничего не удаляет.
#   ./ops/cleanup.sh --images        — удалить висячие образы Docker: без тега и не нужные ни одному
#                                      контейнеру (остаются после обновлений и сборок). docker rmi по
#                                      одному; образ, который нужен контейнеру, Docker сам не удалит.
#                                      Это не данные: нужный образ ./ops/up.sh скачает или соберёт снова.
#   ./ops/cleanup.sh --backups [--keep N]
#                                    — удалить резервные копии сверх N последних в local/backups — целиком,
#                                      по метке времени: архив data-*.tar.gz, выгрузка db-*.dump и опись
#                                      meta-*.txt (по умолчанию N = SHTURMAN_BACKUP_KEEP, иначе 5 — как
#                                      хранит ./ops/backup.sh; правило одно — bk_extra в ops/lib-backup.sh;
#                                      N не меньше 2) — и недописанные копии (*.part). Внешний каталог
#                                      копий (SHTURMAN_BACKUP_DIR) не трогает.
#                                      Удалённую копию не вернуть: скрипт показывает список и шлёт владельцу
#                                      запрос в бот согласований (сколько копий, какие, сколько места);
#                                      удаляет только после нажатия «Разрешить» (Р-100) и только тот же список.
#                                      Бот недоступен — запасной путь: фраза человека в терминале.
#
# Агент: --dry-run можно всегда; --images — сказав владельцу; --backups — по просьбе владельца: решает
# он сам, кнопкой в боте.
# Печатаются только размеры, число файлов и имена копий (в них только дата и время).
#   -h, --help — эта справка; ничего не удаляется.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

action=show; keep=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) [ "$action" = show ] || ops_unknown "$1" ;;
    --images) [ "$action" = show ] || ops_unknown "$1"; action=images ;;
    --backups) [ "$action" = show ] || ops_unknown "$1"; action=backups ;;
    --keep)
      [ $# -ge 2 ] || { echo "после --keep нужно число" >&2; exit 2; }
      printf '%s' "$2" | grep -Eq '^[0-9]{1,3}$' && [ "$2" -ge 2 ] || { echo "--keep — число от 2: последние копии оставляются всегда" >&2; exit 2; }
      keep="$2"; shift ;;
    *) ops_unknown "$1" ;;
  esac
  shift
done

. ops/lib-backup.sh
dir="$BK_LOCAL"
[ -n "$keep" ] || keep="$(bk_keep)"

# Копии сверх ротации — тем же правилом, что ротация ./ops/backup.sh (bk_extra), и недописанные.
extra_backups() {
  [ -d "$dir" ] || return 0
  bk_extra "$dir" "$keep"
  find "$dir" -maxdepth 1 -type f -name '*.part' 2>/dev/null
}

files_mb() {
  local total=0 f s
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    s="$(du -sm "$f" 2>/dev/null | awk '{print $1}')"; total=$((total + ${s:-0}))
  done
  echo "$total"
}

dangling() { docker images -q -f dangling=true 2>/dev/null | sort -u; }

case "$action" in
  show)
    free_mb="$(df -Pm . | awk 'NR==2 {print $4}')"
    echo "Свободно на диске: $(human_mb "${free_mb:-0}")."
    du_line="$(disk_usage)"; [ -z "$du_line" ] || echo "Занято: $du_line."
    echo
    extra="$(extra_backups)"
    if [ -n "$extra" ]; then
      n="$(printf '%s\n' "$extra" | grep -c .)"
      echo "Копии сверх $keep последних и недописанные — $n шт., $(human_mb "$(printf '%s\n' "$extra" | files_mb)"):"
      printf '%s\n' "$extra" | sed 's/^/  /'
      echo "  удалить — ./ops/cleanup.sh --backups${keep:+ --keep $keep} (спросит разрешение владельца в боте согласований)"
    else
      echo "Копий сверх $keep последних нет."
      echo "  Если места мало, а копий много: ./ops/cleanup.sh --dry-run --keep 2 покажет, что останется при двух."
    fi
    ids="$(dangling)"
    if [ -n "$ids" ]; then
      echo "Висячие образы Docker (без тега, не нужны контейнерам) — $(printf '%s\n' "$ids" | grep -c .) шт.; удалить — ./ops/cleanup.sh --images"
    else
      echo "Висячих образов Docker нет."
    fi
    echo
    echo "Не удаляется этим скриптом: данные экземпляра (data/), тома, база, модели дополнений. Кеш сборки"
    echo "Docker (docker builder prune) и неиспользуемые образы с тегом (docker image prune -a) — стоп-точка"
    echo "«prune» (AGENTS.md): только с согласия владельца. Журналы контейнеров ограничены (10 МБ × 3 на контейнер)."
    ;;
  images)
    ids="$(dangling)"
    if [ -z "$ids" ]; then echo "Висячих образов Docker нет — удалять нечего."; exit 0; fi
    removed=0; kept=0
    while IFS= read -r id; do
      [ -n "$id" ] || continue
      if docker rmi "$id" > /dev/null 2>&1; then removed=$((removed + 1)); else kept=$((kept + 1)); fi
    done <<EOF
$ids
EOF
    if [ "$kept" -gt 0 ]; then echo "Висячих образов удалено: $removed; Docker не удалил (нужны контейнеру): $kept."
    else echo "Висячих образов удалено: $removed."; fi
    ;;
  backups)
    extra="$(extra_backups)"
    if [ -z "$extra" ]; then echo "Копий сверх $keep последних нет — удалять нечего."; exit 0; fi
    echo "Будут удалены (останутся $keep последних копий):"
    printf '%s\n' "$extra" | sed 's/^/  /'
    size="$(human_mb "$(printf '%s\n' "$extra" | files_mb)")"
    echo "Освободится: $size. Удалённую копию не вернуть. Спрашиваю разрешение владельца."
    stamps_of="$(printf '%s\n' "$extra" | sed -n 's/.*-\([0-9]\{8\}T[0-9]\{6\}Z\)\..*/\1/p' | sort -u)"
    target="$keep:$(short_hash "$extra")"
    if ! confirm_owner backups_delete "$target" "УДАЛИТЬ" "./ops/cleanup.sh --backups --keep $keep" \
         "count=$(printf '%s\n' "$extra" | grep -c .)" "size=$size" \
         "oldest=$(printf '%s\n' "$stamps_of" | head -n 1)" "newest=$(printf '%s\n' "$stamps_of" | tail -n 1)"; then
      echo "Ничего не удалено." >&2; exit "$OPS_NEED_HUMAN"
    fi
    # Под общим замком ops/: не удалять копии, пока идёт копия, восстановление или обновление.
    ops_lock "удаление копий (./ops/cleanup.sh --backups)"
    # Разрешение — на этот самый список: если он изменился, пока владелец решал, ничего не удаляем.
    if [ "$(short_hash "$(extra_backups)")" != "${target#*:}" ]; then
      echo "Список копий изменился, пока ждали решения владельца — ничего не удалено. Посмотрите ./ops/cleanup.sh --dry-run и повторите." >&2
      exit 1
    fi
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      case "$f" in "$dir"/data-*.tar.gz|"$dir"/db-*.dump|"$dir"/meta-*.txt|"$dir"/*.part) rm -f -- "$f" ;; esac
    done <<EOF
$extra
EOF
    echo "Удалено. Осталось: $(find "$dir" -maxdepth 1 -type f | wc -l) файлов, $(human_mb "$(dir_mb "$dir")")."
    ;;
esac
