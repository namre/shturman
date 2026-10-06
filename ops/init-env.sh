#!/usr/bin/env bash
# Создаёт .env из .env.example, спрашивая значения у человека.
# Запускает ЧЕЛОВЕК в своём терминале. Агенту запускать и читать .env запрещено.
# Секреты вводятся скрыто, на экран и в журналы не попадают.
set -eu
cd "$(dirname "$0")/.."

if [ ! -t 0 ]; then
  echo "Этот скрипт нужно запускать в интерактивном терминале человеком." >&2
  exit 2
fi

src=".env.example"; dst=".env"
[ -f "$src" ] || { echo "нет $src" >&2; exit 1; }

umask 077
touch "$dst"; chmod 600 "$dst"

current() { grep -E "^$1=" "$dst" | tail -n 1 | cut -d= -f2- || true; }
put() {
  local tmp; tmp="$(mktemp .env.XXXXXX)"
  grep -Ev "^$1=" "$dst" > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  chmod 600 "$tmp"; mv "$tmp" "$dst"
}

kind=""; desc=""
while IFS= read -r line <&3; do
  case "$line" in
    "# [secret]"*) kind="secret"; desc="${line#\# \[secret\] }" ;;
    "# [value]"*)  kind="value";  desc="${line#\# \[value\] }" ;;
    "# [auto]"*)   kind="auto";   desc="${line#\# \[auto\] }" ;;
    [A-Z]*=*)
      key="${line%%=*}"; default="${line#*=}"
      have="$(current "$key")"
      if [ -n "$have" ]; then
        echo "[$key] уже задано — пропускаю (чтобы изменить, удалите строку из .env)"
      else
        case "$kind" in
          auto)
            put "$key" "$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)"
            echo "[$key] сгенерировано" ;;
          secret)
            printf '%s\n[%s] (ввод скрыт, Enter — пропустить): ' "$desc" "$key"
            IFS= read -rs val; echo
            if [ -n "$val" ]; then put "$key" "$val"; echo "  записано"; else echo "  пропущено"; fi
            val="" ;;
          *)
            printf '%s\n[%s]%s: ' "$desc" "$key" "${default:+ (по умолчанию: $default)}"
            IFS= read -r val
            val="${val:-$default}"
            if [ -n "$val" ]; then put "$key" "$val"; echo "  записано"; else echo "  пропущено"; fi ;;
        esac
      fi
      kind=""; desc="" ;;
  esac
done 3< "$src"

echo
echo "Готово: $dst (права $(stat -c '%a' "$dst")). Заполнено переменных: $(grep -Ec '^[A-Z_]+=.+' "$dst")."
echo "Сообщите агенту слово «готово». Содержимое файла ему не показывайте."
