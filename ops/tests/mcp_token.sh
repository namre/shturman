#!/usr/bin/env bash
# Проверка ./ops/mcp-token.sh (Р-98, дефект Д-15): токен архива не выдаётся команде, которую выполняет
# агент с перехватом вывода, и выдаётся человеку в терминале. Терминал человека изображает программа
# script (util-linux): она даёт процессу псевдотерминал, как его даёт окно терминала.
# Запуск из корня репозитория: bash ops/tests/mcp_token.sh. Ничего вне временного каталога не меняет.
set -eu
src="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/repo" "$tmp/home"
cp -r "$src/ops" "$tmp/repo/"
token="mcp-token-SECRET-0123456789abcdef0123456789"
printf 'SHTURMAN_MODE=standalone\nSHTURMAN_MCP_TOKEN=%s\n' "$token" > "$tmp/repo/.env"
cd "$tmp/repo"
bad() { echo "ОШИБКА: $*" >&2; exit 1; }

# 1. Как агент: без терминала, вывод перехвачен. Отказ с кодом 2, токена нет ни в выводе, ни в ошибках.
for args in "--pipe" "" "--out $tmp/home/t"; do
  code=0
  # shellcheck disable=SC2086
  out="$(setsid ./ops/mcp-token.sh $args < /dev/null 2>&1)" || code=$?
  [ "$code" -eq 2 ] || bad "mcp-token.sh $args без терминала: код $code вместо 2"
  case "$out" in *"$token"*) bad "mcp-token.sh $args без терминала напечатал токен" ;; esac
  case "$out" in *"терминал"*) ;; *) bad "mcp-token.sh $args: нет объяснения отказа" ;; esac
done
[ ! -e "$tmp/home/t" ] || bad "--out без терминала создал файл"
# stdin из канала, а не с терминала, — тоже отказ (агент может подать «да» в канал).
code=0; out="$(echo да | setsid ./ops/mcp-token.sh 2>&1)" || code=$?
[ "$code" -eq 2 ] || bad "показ с вводом из канала: код $code вместо 2"
case "$out" in *"$token"*) bad "показ с вводом из канала напечатал токен" ;; esac

if ! command -v script > /dev/null 2>&1; then
  echo "нет программы script — проверка терминала человека пропущена"; echo "mcp-token: ok (без терминала)"; exit 0
fi

# 2. Как человек: свой терминал. --pipe в подстановку — токен в файле вывода, на «экран» не попал.
script -qec "./ops/mcp-token.sh --pipe > $tmp/home/piped" /dev/null > "$tmp/screen" 2>&1 || bad "--pipe в терминале не сработал"
[ "$(cat "$tmp/home/piped")" = "$token" ] || bad "--pipe в терминале выдал не тот токен"
grep -q "$token" "$tmp/screen" && bad "--pipe показал токен на экране"
# --pipe прямо на экран — отказ.
code=0; script -qec "./ops/mcp-token.sh --pipe" /dev/null > "$tmp/screen" 2>&1 || code=$?
grep -q "$token" "$tmp/screen" && bad "--pipe на экран напечатал токен"

# 3. --out: файл с правами 600, на экране только путь; в каталог репозитория и по ссылке — нет.
script -qec "./ops/mcp-token.sh --out $tmp/home/token-file" /dev/null > "$tmp/screen" 2>&1 || bad "--out в терминале не сработал"
[ "$(cat "$tmp/home/token-file")" = "$token" ] || bad "--out записал не тот токен"
[ "$(stat -c '%a' "$tmp/home/token-file")" = 600 ] || bad "--out: права не 600"
grep -q "$token" "$tmp/screen" && bad "--out показал токен на экране"
grep -q "$tmp/home/token-file" "$tmp/screen" || bad "--out не назвал путь"
script -qec "./ops/mcp-token.sh --out $tmp/repo/inside" /dev/null > /dev/null 2>&1 || true
[ ! -e "$tmp/repo/inside" ] || bad "--out записал токен в каталог репозитория"
ln -s "$tmp/home/elsewhere" "$tmp/home/link"
script -qec "./ops/mcp-token.sh --out $tmp/home/link" /dev/null > /dev/null 2>&1 || true
[ ! -e "$tmp/home/elsewhere" ] || bad "--out прошёл по ссылке"

echo "mcp-token: ok"
