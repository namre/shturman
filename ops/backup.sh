#!/usr/bin/env bash
# Копия данных экземпляра перед обновлением или рискованным действием.
# Складывает архив каталога data/ в local/backups (вне git), права 600.
# В архиве ключи и настройки владельца: агент его не открывает и никуда не передаёт.
#   ./ops/backup.sh            — сделать копию и проверить, что она читается
#   ./ops/backup.sh --list     — показать имеющиеся копии
# Восстановление поверх данных — стоп-точка, отдельной процедурой.
set -eu
cd "$(dirname "$0")/.." || exit 1

dir="local/backups"
mkdir -p "$dir"; chmod 700 local "$dir"

if [ "${1:-}" = "--list" ]; then
  ls -lh "$dir" 2>/dev/null | awk 'NR>1 {print $5, $6, $7, $8, $9}'
  exit 0
fi

[ -d data ] || { echo "нет каталога data — копировать нечего" >&2; exit 1; }
umask 077
name="$dir/data-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
# Файлы могут меняться на ходу (журналы Hermes): для tar это предупреждение, а не провал.
tar -czf "$name" --warning=no-file-changed --exclude='data/hermes/logs' data || [ $? -eq 1 ]
count="$(tar -tzf "$name" | wc -l)"
[ "$count" -gt 0 ] || { echo "копия пустая: $name" >&2; exit 1; }
echo "Копия готова: $name ($(du -h "$name" | cut -f1), файлов: $count)"
# Храним пять последних копий.
ls -1t "$dir"/data-*.tar.gz 2>/dev/null | tail -n +6 | while IFS= read -r old; do rm -f -- "$old"; done
