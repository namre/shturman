#!/usr/bin/env bash
# Проверка ./ops/cleanup.sh (Р-98, Р-100): по умолчанию ничего не удаляет; копии сверх ротации удаляются только
# после «Разрешить» владельца в боте согласований (бот недоступен — фразой человека в терминале); data/ и самые
# новые копии не трогаются никогда. Docker подставной: отдаёт два висячих образа, отвечает за сервис по
# STUB_SERVICE (running — запущен) и за решение владельца по STUB_CONSENT (код выхода `shturman ops-consent`:
# 0 — разрешил, 4 — ещё не ответил, 5 — отклонил, 6 — бота нет) и записывает вызовы.
# Запуск из корня репозитория: bash ops/tests/cleanup.sh.
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
  inspect) if [ "\${STUB_SERVICE:-}" = running ] && [ "\${2:-}" = -f ]; then echo running; exit 0; fi; exit 1 ;;
  exec) case "\$*" in *"ops-consent ask"*) echo "подставной бот: \${STUB_CONSENT:-6}"; exit "\${STUB_CONSENT:-6}" ;; esac; exit 1 ;;
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

# --backups: бота нет (сервис не запущен, образа нет) и терминала нет — отказ (код 4), ничего не удалено.
code=0; setsid ./ops/cleanup.sh --backups < /dev/null > "$tmp/out.txt" 2>&1 || code=$?
[ "$code" -eq 4 ] || bad "--backups без бота и терминала: код $code вместо 4"
grep -q "ssh -t" "$tmp/out.txt" || bad "нет запасного пути для человека"
[ "$(listing)" = "$before" ] || bad "--backups без бота и терминала что-то удалил"
# Сервис работает: запрос в бот; владелец ещё не ответил или отклонил — ничего не удалено.
for answer in 4 5; do
  code=0; STUB_SERVICE=running STUB_CONSENT=$answer setsid ./ops/cleanup.sh --backups < /dev/null > "$tmp/out.txt" 2>&1 || code=$?
  [ "$code" -eq 4 ] || bad "--backups, решение $answer: код $code вместо 4"
  [ "$(listing)" = "$before" ] || bad "--backups без разрешения владельца что-то удалил"
done
grep -q "ops-consent ask --action backups_delete --target 5:[0-9a-f]\{16\}" "$tmp/docker.log" || bad "запрос в бот без вида и цели"
grep -q -- "--detail count=5" "$tmp/docker.log" || bad "в карточке нет числа копий"

# --images — только docker rmi висячих, без prune.
./ops/cleanup.sh --images > "$tmp/out.txt"
grep -q "удалено: 2" "$tmp/out.txt" || bad "--images не удалил висячие образы"
grep -q "prune" "$tmp/docker.log" && bad "cleanup.sh звал prune"

if command -v python3 > /dev/null 2>&1; then
  # Бота нет, человек в терминале набрал не ту фразу — ничего не удалено.
  python3 "$src/ops/tests/pty_confirm.py" wrong -- ./ops/cleanup.sh --backups > "$tmp/out.txt" 2>&1 || true
  grep -q "Запасной путь" "$tmp/out.txt" || bad "запасной путь не назван"
  [ "$(listing)" = "$before" ] || bad "--backups удалил без верной фразы"
fi
# Владелец нажал «Разрешить» — удалены 2 + 2 старые и недописанная; 5 новых каждого вида и data/ на месте.
STUB_SERVICE=running STUB_CONSENT=0 setsid ./ops/cleanup.sh --backups < /dev/null > "$tmp/out.txt" 2>&1 || bad "--backups с разрешением не прошёл"
[ "$(find local/backups -name 'data-*' | wc -l)" -eq 5 ] || bad "осталось не 5 копий data"
[ "$(find local/backups -name 'db-*.dump' | wc -l)" -eq 5 ] || bad "осталось не 5 копий базы"
[ ! -e local/backups/db-20261008T030000Z.dump.part ] || bad "недописанная копия не удалена"
[ -e local/backups/data-20261007T030000Z.tar.gz ] && [ -e local/backups/data-20261003T030000Z.tar.gz ] || bad "удалены новые копии"
[ -e data/shturman/keep.txt ] || bad "тронут каталог data"
echo "cleanup: ok"
