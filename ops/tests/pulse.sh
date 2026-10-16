#!/usr/bin/env bash
# Проверка ./ops/pulse.sh (Р-98): адрес внешнего сторожа принимается только https, пишется одной строкой
# .env и нигде не печатается целиком. Без сети: test проверяется с подставным curl, который записывает,
# как его вызвали. Запуск из корня репозитория: bash ops/tests/pulse.sh.
set -eu
src="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/repo" "$tmp/stub"
cp -r "$src/ops" "$tmp/repo/"
# Подставные docker и curl: docker не отвечает, curl записывает аргументы и ввод и отвечает 200.
printf '#!/bin/sh\nexit 1\n' > "$tmp/stub/docker"
printf '#!/bin/sh\necho "args: $*" >> "%s/curl.log"\ncat >> "%s/curl.log"\nprintf 200\n' "$tmp" "$tmp" > "$tmp/stub/curl"
chmod +x "$tmp/stub/docker" "$tmp/stub/curl"
export PATH="$tmp/stub:$PATH"
cd "$tmp/repo"
printf 'SHTURMAN_MODE=standalone\nSHTURMAN_MCP_TOKEN=keep-me\n' > .env
bad() { echo "ОШИБКА: $*" >&2; exit 1; }
uuid="0f0e0d0c-aaaa-bbbb-cccc-1234567890ab"

for wrong in "http://hc-ping.com/$uuid" "hc-ping.com/$uuid" "https://user:pw@hc-ping.com/x" \
             "https://hc-ping.com/a b" "https://hc-ping.com/a\"b" "https://hc-ping.com/a'b" "https://hc-ping.com/a\`b"; do
  code=0; ./ops/pulse.sh set "$wrong" > out.txt 2>&1 || code=$?
  [ "$code" -eq 2 ] || bad "адрес «$wrong» принят (код $code)"
done
grep -q SHTURMAN_PULSE_URL .env && bad "неверный адрес попал в .env"

./ops/pulse.sh set "https://hc-ping.com/$uuid" > out.txt
grep -qx "SHTURMAN_PULSE_URL=https://hc-ping.com/$uuid" .env || bad "адрес не записан"
grep -qx "SHTURMAN_MCP_TOKEN=keep-me" .env || bad "другие строки .env задеты"
[ "$(stat -c '%a' .env)" = 600 ] || bad ".env не 600"
grep -q "$uuid" out.txt && bad "set напечатал адрес целиком"
grep -q "hc-ping.com" out.txt || bad "set не назвал узел"

./ops/pulse.sh status > out.txt 2>&1
grep -q "$uuid" out.txt && bad "status напечатал адрес целиком"
grep -q "адрес задан (hc-ping.com" out.txt || bad "status не сказал, что адрес задан"

./ops/pulse.sh test > out.txt 2>&1 || bad "test с ответом 200 не прошёл"
grep -q "$uuid" out.txt && bad "test напечатал адрес целиком"
grep -q "^args: .*$uuid" "$tmp/curl.log" && bad "адрес попал в командную строку curl (виден в списке процессов)"
grep -q "url = \"https://hc-ping.com/$uuid\"" "$tmp/curl.log" || bad "адрес не передан curl через ввод"

# Ввод из канала («-»): адрес не в командной строке.
./ops/pulse.sh off > /dev/null
echo "https://hc-ping.com/second" | ./ops/pulse.sh set - > /dev/null 2>&1
grep -qx "SHTURMAN_PULSE_URL=https://hc-ping.com/second" .env || bad "set - не записал адрес"
./ops/pulse.sh off > /dev/null
grep -q SHTURMAN_PULSE_URL .env && bad "off не убрал адрес"
./ops/pulse.sh off | grep -q "не задан" || bad "повторный off должен быть спокойным"
echo "pulse: ok"
