#!/usr/bin/env bash
# Создаёт .env из .env.example.
#
#   ./ops/init-env.sh --auto   — только служебные значения, которые генерируются сами
#                                (пароль базы и т.п.). Ничего не спрашивает и не печатает
#                                значений. Этот режим может запускать агент.
#   ./ops/init-env.sh          — запасной путь без веб-интерфейса: спрашивает все значения
#                                у ЧЕЛОВЕКА в его терминале, секреты — скрытым вводом.
#
# Основной путь ввода токенов и ключей — веб-интерфейс (docs/setup.md), а не этот скрипт.
# Читать .env агенту запрещено в любом режиме.
set -eu
cd "$(dirname "$0")/.." || exit 1

auto=0
[ "${1:-}" = "--auto" ] && auto=1

if [ "$auto" -eq 0 ] && [ ! -t 0 ]; then
  echo "Без --auto этот скрипт запускает человек в интерактивном терминале." >&2
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
        if [ "$auto" -eq 1 ] && [ "$kind" != "auto" ]; then kind=""; desc=""; continue; fi
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
if [ "$auto" -eq 0 ]; then echo "Сообщите агенту слово «готово». Содержимое файла ему не показывайте."; fi
