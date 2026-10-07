#!/usr/bin/env bash
# Копия данных экземпляра перед обновлением или рискованным действием.
# Складывает в local/backups (вне git, права 600) архив каталога data/ и выгрузку базы архива.
# Вместе с data/ в архив кладётся файл .env. Работает одинаково в обоих режимах установки:
# без Hermes каталога data/hermes просто нет, а токен бота согласований и ключ модели лежат
# как раз в .env.
# В копии ключи, настройки и переписка владельца: агент её не открывает и никуда не передаёт.
# Файлы базы и кэш модели в архив каталога не входят: база сохраняется выгрузкой (pg_dump),
# модель скачивается заново.
#   ./ops/backup.sh            — сделать копию и проверить, что она читается
#   ./ops/backup.sh --list     — показать имеющиеся копии
#   ./ops/backup.sh --help     — эта справка; ничего не создаётся
# Восстановление поверх данных — стоп-точка, отдельной процедурой.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"
case "${1:-}" in
  ""|--list) ;;
  *) ops_unknown "$1" ;;
esac
[ $# -le 1 ] || ops_unknown "$2"

dir="local/backups"
mkdir -p "$dir"; chmod 700 local "$dir"

if [ "${1:-}" = "--list" ]; then
  # shellcheck disable=SC2012
  ls -lh "$dir" 2>/dev/null | awk 'NR>1 {print $5, $6, $7, $8, $9}'
  exit 0
fi

[ -d data ] || { echo "нет каталога data — копировать нечего" >&2; exit 1; }
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="$dir/data-$stamp.tar.gz"
# Файлы могут меняться на ходу (журналы Hermes): для tar это предупреждение, а не провал.
# .env кладётся в архив целиком, не читая: в нём режим установки и значения, введённые владельцем.
members="data"
[ -f .env ] && members="data .env"
# shellcheck disable=SC2086
tar -czf "$name" --warning=no-file-changed \
  --exclude='data/hermes/logs' --exclude='data/postgres' --exclude='data/embeddings' --exclude='data/guard' \
  --exclude='data/shturman/uploads' $members || [ $? -eq 1 ]
count="$(tar -tzf "$name" | wc -l)"
[ "$count" -gt 0 ] || { echo "копия пустая: $name" >&2; exit 1; }
echo "Копия готова: $name ($(du -h "$name" | cut -f1), файлов: $count)"

# База архива: выгрузка средствами Postgres — согласованная, в отличие от копии файлов на ходу.
pg=shturman-postgres
if [ "$(docker inspect -f '{{.State.Status}}' "$pg" 2>/dev/null || true)" = "running" ]; then
  dump="$dir/db-$stamp.dump"
  if docker exec "$pg" pg_dump -U shturman -d shturman -Fc > "$dump.part" && [ -s "$dump.part" ]; then
    mv "$dump.part" "$dump"
    # Проверка, что выгрузка читается: оглавление должно содержать таблицу сообщений.
    if docker exec -i "$pg" pg_restore --list < "$dump" | grep -q 'TABLE public messages'; then
      echo "Копия базы готова: $dump ($(du -h "$dump" | cut -f1))"
    else
      echo "выгрузка базы не читается: $dump" >&2; exit 1
    fi
  else
    rm -f "$dump.part"
    echo "не удалось выгрузить базу архива" >&2; exit 1
  fi
elif [ -d data/postgres ]; then
  echo "контейнер базы не запущен — копия базы НЕ сделана (./ops/up.sh и повторите)" >&2; exit 1
fi

# Храним пять последних копий каждого вида.
for pattern in 'data-*.tar.gz' 'db-*.dump'; do
  # shellcheck disable=SC2086
  ls -1t "$dir"/$pattern 2>/dev/null | tail -n +6 | while IFS= read -r old; do rm -f -- "$old"; done
done
