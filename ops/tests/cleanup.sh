#!/usr/bin/env bash
# Проверка ./ops/cleanup.sh (Р-98): по умолчанию ничего не удаляет; копии сверх ротации удаляются только
# после «да» человека в терминале; data/ и самые новые копии не трогаются никогда. Docker подставной:
# отдаёт два висячих образа и записывает вызовы. Запуск из корня репозитория: bash ops/tests/cleanup.sh.
set -eu
src="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/repo/local/backups" "$tmp/repo/data/shturman" "$tmp/stub"
cp -r "$src/ops" "$tmp/repo/"
cat > "$tmp/stub/docker" <<EOF
#!/bin/sh
echo "\$*" >> "$tmp/docker.log"
case "\$1" in
  images) printf 'sha256:aaa\nsha256:bbb\n' ;;
  rmi) exit 0 ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$tmp/stub/docker"
export PATH="$tmp/stub:$PATH"
cd "$tmp/repo"
bad() { echo "ОШИБКА: $*" >&2; exit 1; }
echo "переписка" > data/shturman/keep.txt
for i in 1 2 3 4 5 6 7; do
  touch -d "2026-10-0$i 03:00" "local/backups/data-2026100${i}T030000Z.tar.gz" "local/backups/db-2026100${i}T030000Z.dump"
done
touch local/backups/db-20261008T030000Z.dump.part
listing() { find . -type f | sort; }
before="$(listing)"

# По умолчанию и с --dry-run — только показ.
for args in "" "--dry-run" "--dry-run --keep 2"; do
  # shellcheck disable=SC2086
  ./ops/cleanup.sh $args > "$tmp/out.txt" 2>&1 || bad "cleanup.sh $args: код $?"
  [ "$(listing)" = "$before" ] || bad "cleanup.sh $args что-то удалил"
done
grep -q "data-20261001T030000Z.tar.gz" "$tmp/out.txt" || bad "dry-run не показал старые копии"
grep -q "\.part" "$tmp/out.txt" || bad "dry-run не показал недописанную копию"
grep -q "data-20261007T030000Z" "$tmp/out.txt" && bad "dry-run предлагает удалить самую новую копию"
grep -q "rmi" "$tmp/docker.log" && bad "dry-run удалял образы"

# --keep меньше 2 — отказ.
code=0; ./ops/cleanup.sh --backups --keep 1 > /dev/null 2>&1 || code=$?
[ "$code" -eq 2 ] || bad "--keep 1 принят"

# --backups без терминала (как агент) — отказ, ничего не удалено.
code=0; setsid ./ops/cleanup.sh --backups < /dev/null > "$tmp/out.txt" 2>&1 || code=$?
[ "$code" -eq 2 ] || bad "--backups без терминала: код $code вместо 2"
[ "$(listing)" = "$before" ] || bad "--backups без терминала что-то удалил"

# --images — только docker rmi висячих, без prune.
./ops/cleanup.sh --images > "$tmp/out.txt"
grep -q "удалено: 2" "$tmp/out.txt" || bad "--images не удалил висячие образы"
grep -q "prune" "$tmp/docker.log" && bad "cleanup.sh звал prune"

if command -v script > /dev/null 2>&1; then
  # Человек в терминале отказался — ничего не удалено.
  echo нет | script -qec "./ops/cleanup.sh --backups" /dev/null > /dev/null 2>&1 || true
  [ "$(listing)" = "$before" ] || bad "--backups удалил без «да»"
  # Человек сказал «да» — удалены 2 + 2 старые и недописанная; 5 новых каждого вида и data/ на месте.
  printf 'да\n' | script -qec "./ops/cleanup.sh --backups" /dev/null > "$tmp/out.txt" 2>&1 || bad "--backups с «да» не прошёл"
  [ "$(find local/backups -name 'data-*' | wc -l)" -eq 5 ] || bad "осталось не 5 копий data"
  [ "$(find local/backups -name 'db-*.dump' | wc -l)" -eq 5 ] || bad "осталось не 5 копий базы"
  [ ! -e local/backups/db-20261008T030000Z.dump.part ] || bad "недописанная копия не удалена"
  [ -e local/backups/data-20261007T030000Z.tar.gz ] && [ -e local/backups/data-20261003T030000Z.tar.gz ] || bad "удалены новые копии"
  [ -e data/shturman/keep.txt ] || bad "тронут каталог data"
fi
echo "cleanup: ok"
