#!/usr/bin/env bash
# Создаёт .env из .env.example и запоминает режим установки.
#
#   ./ops/init-env.sh --auto   — только служебные значения, которые генерируются сами
#                                (пароль базы и т.п.). Ничего не спрашивает и не печатает
#                                значений. Этот режим может запускать агент.
#   ./ops/init-env.sh          — спрашивает значения у ЧЕЛОВЕКА в его терминале, секреты —
#                                скрытым вводом: набранное не показывается на экране и не попадает
#                                ни в историю команд, ни в журналы. Агент так скрипт не запускает.
#
#   --mode hermes|standalone   — вариант установки, записывается при первом запуске:
#                                  hermes      — «Ассистент в Telegram (Hermes)»;
#                                  standalone  — «Только архив и согласования», без Hermes.
#                                Уже записанный режим этот ключ не меняет: смена — ./ops/mode.sh.
#
# Вариант установки выбирает владелец. При первой установке без --mode скрипт спрашивает его сам,
# а с --auto останавливается и просит назвать вариант: агент не выбирает за владельца. Экземпляр,
# развёрнутый до появления вариантов (в .env уже есть значения, но нет SHTURMAN_MODE), остаётся
# установкой с Hermes — его ни о чём не спрашивают.
#
# В режиме с Hermes токены и ключи владелец вводит в веб-интерфейсе (docs/setup.md), а запуск
# без --auto — запасной путь. В режиме без Hermes веб-интерфейса нет: токен бота согласований
# и ключ модели владелец вводит здесь (docs/standalone.md).
# Читать .env агенту запрещено в любом режиме.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh

auto=0; want=""
while [ $# -gt 0 ]; do
  case "$1" in
    --auto) auto=1 ;;
    --mode)
      [ $# -ge 2 ] || { echo "после --mode нужен режим: hermes или standalone" >&2; exit 2; }
      want="$2"; shift ;;
    --mode=*) want="${1#--mode=}" ;;
    -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "неизвестный параметр: $1 (см. $0 --help)" >&2; exit 2 ;;
  esac
  shift
done
if [ -n "$want" ] && ! valid_mode "$want"; then
  echo "неизвестный режим: $want — допустимы hermes и standalone" >&2; exit 2
fi

if [ "$auto" -eq 0 ] && [ ! -t 0 ]; then
  echo "Без --auto этот скрипт запускает человек в интерактивном терминале." >&2
  exit 2
fi

src=".env.example"; dst=".env"
[ -f "$src" ] || { echo "нет $src" >&2; exit 1; }

# --- вариант установки: его выбирает владелец ---
# Первая установка — файла .env ещё нет (или он пуст) и вариант не назван ключом --mode.
if [ ! -s "$dst" ] && [ -z "$want" ]; then
  if [ "$auto" -eq 1 ]; then
    echo "Вариант установки ещё не выбран, а выбирает его владелец. Спросите его, какой из двух ставим:" >&2
    echo "  1) «Ассистент в Telegram (Hermes)» — ассистент отвечает вам в Telegram, ведёт архив переписки," >&2
    echo "     готовит ответы и сводки. Запуск: $0 --auto --mode hermes" >&2
    echo "  2) «Только архив и согласования» — без ассистента в Telegram: архив переписки с поиском" >&2
    echo "     и список обязательств; вопросы вы задаёте из своего Codex CLI или Claude Code." >&2
    echo "     Запуск: $0 --auto --mode standalone" >&2
    echo "Чем варианты отличаются — README.md, «Как развернуть», и docs/standalone.md." >&2
    exit 2
  fi
  echo "Какой вариант ставим?"
  echo "  1) Ассистент в Telegram (Hermes) — ассистент отвечает вам в Telegram, ведёт архив переписки,"
  echo "     готовит ответы и сводки."
  echo "  2) Только архив и согласования — без ассистента в Telegram: архив переписки с поиском"
  echo "     и список обязательств; вопросы вы задаёте из своего Codex CLI или Claude Code."
  while [ -z "$want" ]; do
    printf 'Введите 1 или 2: '
    IFS= read -r answer || { echo; echo "вариант не выбран" >&2; exit 2; }
    case "$answer" in
      1) want=hermes ;;
      2) want=standalone ;;
      *) echo "  нужно 1 или 2" ;;
    esac
  done
fi

umask 077
touch "$dst"; chmod 600 "$dst"

# --- режим установки ---
have_mode="$(env_get SHTURMAN_MODE)"
if [ -z "$have_mode" ]; then
  # Экземпляр, развёрнутый до появления режимов, — это режим с Hermes.
  mode="${want:-hermes}"
  env_put SHTURMAN_MODE "$mode"
  echo "[SHTURMAN_MODE] записан режим: $mode"
else
  mode="$(shturman_mode)" || exit 2
  if [ -n "$want" ] && [ "$want" != "$mode" ]; then
    echo "Режим уже выбран: $mode. Этот скрипт его не меняет: смена режима — ./ops/mode.sh set $want" >&2
    exit 2
  fi
  echo "[SHTURMAN_MODE] режим: $mode"
fi
# Профили Compose следуют за режимом; включённый поиск по смыслу сохраняется.
profiles="$(compose_profiles "$mode" keep)"
if ! grep -q '^COMPOSE_PROFILES=' "$dst" || [ "$(env_get COMPOSE_PROFILES)" != "$profiles" ]; then
  env_put COMPOSE_PROFILES "$profiles"
fi

current() { grep -E "^$1=" "$dst" | tail -n 1 | cut -d= -f2- || true; }
put() { env_put "$1" "$2"; }

# Значение, которое .env не передаст контейнеру как есть. О самом значении ничего не печатаем.
unsafe() {
  case "$1" in
    *[[:space:]]*|*\$*|*\"*|*\'*|*\\*|*\#*) return 0 ;;
    *) return 1 ;;
  esac
}
# Очевидная опечатка в секрете известного вида. Проверяется только форма, не само значение.
malformed() {
  case "$1" in
    SHTURMAN_BOT_TOKEN|TELEGRAM_BOT_TOKEN)
      printf '%s' "$2" | grep -Eq '^[0-9]{3,20}:[A-Za-z0-9_-]{20,128}$' && return 1
      return 0 ;;
    *) return 1 ;;
  esac
}

kind=""; desc=""; scope=""
while IFS= read -r line <&3; do
  case "$line" in
    "# ["*"]"*)
      tag="${line#\# \[}"; tag="${tag%%\]*}"
      desc="${line#*\] }"
      kind="${tag%%:*}"; scope=""
      case "$tag" in *:*) scope="${tag#*:}" ;; esac
      case "$kind" in secret|value|auto) ;; *) kind=""; desc=""; scope="" ;; esac ;;
    [A-Z]*=*)
      key="${line%%=*}"; default="${line#*=}"
      have="$(current "$key")"
      if [ -n "$have" ]; then
        echo "[$key] уже задано — пропускаю (чтобы изменить, удалите строку из .env)"
      elif [ -n "$scope" ] && [ "$scope" != "$mode" ]; then
        : # значение другого режима: не спрашиваем
      else
        if [ "$auto" -eq 1 ] && [ "$kind" != "auto" ]; then kind=""; desc=""; scope=""; continue; fi
        case "$kind" in
          auto)
            put "$key" "$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)"
            echo "[$key] сгенерировано" ;;
          secret)
            printf '%s\n[%s] (ввод скрыт, Enter — пропустить): ' "$desc" "$key"
            IFS= read -rs val; echo
            if [ -z "$val" ]; then echo "  пропущено"
            elif unsafe "$val"; then echo "  НЕ записано: в значении пробел, кавычка или один из знаков \$ # \\ — проверьте, что скопировали его целиком и без лишнего"
            elif malformed "$key" "$val"; then echo "  НЕ записано: это не похоже на токен бота (ожидается вид 123456789:буквы-и-цифры) — запустите скрипт ещё раз"
            else put "$key" "$val"; echo "  записано"; fi
            val="" ;;
          *)
            printf '%s\n[%s]%s: ' "$desc" "$key" "${default:+ (по умолчанию: $default)}"
            IFS= read -r val
            val="${val:-$default}"
            if [ -z "$val" ]; then echo "  пропущено"
            elif unsafe "$val"; then echo "  НЕ записано: в значении пробел, кавычка или один из знаков \$ # \\"
            else put "$key" "$val"; echo "  записано"; fi ;;
        esac
      fi
      kind=""; desc=""; scope="" ;;
  esac
done 3< "$src"

echo
echo "Готово: $dst (права $(stat -c '%a' "$dst")), режим: $mode. Заполнено переменных: $(grep -Ec '^[A-Z_]+=.+' "$dst")."
if [ "$auto" -eq 0 ]; then
  echo "Чтобы сервис подхватил новые значения — ./ops/up.sh."
  echo "Сообщите агенту слово «готово». Содержимое файла ему не показывайте."
fi
