#!/usr/bin/env bash
# Записывает в .env адрес, по которому владелец открывает ассистента (SHTURMAN_PUBLIC_URL).
# Значение не секретное. Остальное содержимое .env скрипт не читает и не печатает.
#   ./ops/set-public-url.sh https://assistant.example.com
# После изменения — ./ops/up.sh, чтобы Hermes подхватил адрес.
set -eu
cd "$(dirname "$0")/.." || exit 1

url="${1:-}"
case "$url" in
  https://*|http://*) ;;
  *) echo "нужен адрес с https://, например https://assistant.example.com" >&2; exit 2 ;;
esac
url="${url%/}"
if ! printf '%s' "$url" | grep -Eq '^https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?$'; then
  echo "адрес содержит недопустимые символы" >&2; exit 2
fi
case "$url" in
  http://*) echo "ВНИМАНИЕ: адрес без https — вход будет работать, но данные пойдут по сети открыто." >&2 ;;
esac

[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }
umask 077
tmp="$(mktemp .env.XXXXXX)"
grep -Ev '^SHTURMAN_PUBLIC_URL=' .env > "$tmp" || true
printf 'SHTURMAN_PUBLIC_URL=%s\n' "$url" >> "$tmp"
chmod 600 "$tmp"; mv "$tmp" .env
echo "SHTURMAN_PUBLIC_URL записан: $url"
