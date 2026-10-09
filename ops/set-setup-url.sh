#!/usr/bin/env bash
# Показывает или записывает в .env адрес страницы настройки переписки (SHTURMAN_SETUP_URL).
#
# Страницу настройки переписки (/shturman-setup/) отдаёт сам сервис переписки, мимо Hermes: на ней
# владелец вводит ключи приложения Telegram, входит в свой аккаунт Telegram и выбирает чаты.
# У неё должен быть СВОЙ адрес, не тот, по которому открывается ассистент. Причина простая:
# ассистент может добавлять свои страницы на адрес дашборда, а браузер считает все страницы одного
# адреса «своими» друг для друга. На общем адресе ассистент мог бы нажимать на странице настройки
# от имени вошедшего владельца и перехватывать то, что тот вводит. Когда адреса разные — пусть
# даже отличаются только портом, — браузер этого не даёт.
#
#   ./ops/set-setup-url.sh                                     — показать текущее значение
#   ./ops/set-setup-url.sh https://assistant.example.com:8443  — то же имя, другой порт (так по умолчанию)
#   ./ops/set-setup-url.sh https://setup.example.com           — отдельное имя, обычный порт
#   ./ops/set-setup-url.sh https://203.0.113.10:8443           — домена нет: IP-адрес сервера, другой порт
#   ./ops/set-setup-url.sh --clear                             — убрать адрес: страница останется
#                                                                доступна только через туннель SSH
#   -h, --help — эта справка; .env не меняется.
#
# Обычно запускать скрипт не нужно: адрес по умолчанию (то же имя, порт 8443) записывают
# ./ops/set-public-url.sh и ./ops/up.sh. Скрипт нужен, чтобы выбрать другой порт или отдельное имя.
#
# Адрес — только https://, имя узла и, если нужно, порт: без пути и без имени пользователя.
# Вместо имени — внешний IPv4-адрес сервера, если домена нет (docs/deployment.md, «Без домена»):
# адрес закрытой сети или самого сервера скрипт не принимает, IPv6 тоже.
# Совпадает с адресом дашборда (SHTURMAN_PUBLIC_URL) — скрипт отвечает отказом. Значение не
# секретное. Из .env читаются три несекретные строки: SHTURMAN_MODE, SHTURMAN_PUBLIC_URL
# и SHTURMAN_SETUP_URL; остальное скрипт не читает и не печатает.
#
# Что ещё нужно, кроме этой строки (скрипт этого не делает):
#   1. блок сайта для этого адреса в обратном прокси по образцу config/Caddyfile.example — правка
#      прокси, стоп-точка (sudo);
#   2. если это другой порт — открыть его в firewall (стоп-точка); если отдельное имя — запись DNS
#      для него, направленная на этот сервер (делает владелец);
#   3. ./ops/up.sh — сервис переписки узнаёт адрес при запуске; затем ./ops/doctor.sh.
#
# Режим без Hermes: внешний адрес странице не нужен и не задаётся — она открывается с самого
# сервера или через туннель SSH (./ops/setup-link.sh); наружу экземпляр ничего не публикует.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

clear=no; url=""; have_url=no
for arg in "$@"; do
  case "$arg" in
    --clear) clear=yes ;;
    -*) ops_unknown "$arg" ;;
    *)
      if [ "$have_url" = yes ]; then ops_unknown "$arg"; fi
      url="$arg"; have_url=yes ;;
  esac
done
if [ "$clear" = yes ] && [ "$have_url" = yes ]; then
  echo "--clear не сочетается с адресом — справка: ./ops/set-setup-url.sh --help" >&2; exit 2
fi

MODE="$(shturman_mode)" || exit 2
public="$(env_get SHTURMAN_PUBLIC_URL)"
public_origin="$(url_origin "$public")"

same_origin_why() {
  echo "Почему нельзя: ассистент может добавлять свои страницы на адрес дашборда, а браузер разрешает" >&2
  echo "страницам одного адреса действовать друг за друга. На общем адресе ассистент мог бы нажимать" >&2
  echo "на странице настройки от имени вошедшего владельца и перехватывать то, что тот вводит: ключи" >&2
  echo "и вход в Telegram. Когда адреса разные (хотя бы порт), браузер этого не даёт." >&2
}

# --- показать ---
if [ "$have_url" = no ] && [ "$clear" = no ]; then
  cur="$(env_get SHTURMAN_SETUP_URL)"
  if [ -n "$cur" ]; then
    echo "SHTURMAN_SETUP_URL: $cur"
    if [ -n "$public_origin" ] && [ "$(url_origin "$cur")" = "$public_origin" ]; then
      echo "ВНИМАНИЕ: это тот же адрес, что у дашборда, — по нему сервис страницу настройки переписки" >&2
      echo "не отдаёт (через туннель SSH она открывается: ./ops/setup-link.sh --local)." >&2
      def="$(setup_url_default "$public")"
      echo "Задайте другой: ./ops/set-setup-url.sh ${def:-https://имя:порт}" >&2
    elif [ "$MODE" != hermes ]; then
      echo "ВНИМАНИЕ: режим без Hermes — внешний адрес странице не нужен. Убрать: ./ops/set-setup-url.sh --clear" >&2
    fi
  elif [ "$MODE" != hermes ]; then
    echo "SHTURMAN_SETUP_URL не задан. В режиме без Hermes он и не нужен: страница настройки"
    echo "переписки открывается с самого сервера или через туннель SSH (./ops/setup-link.sh)."
  elif env_has SHTURMAN_SETUP_URL; then
    echo "SHTURMAN_SETUP_URL убран: страница настройки переписки открывается только с самого сервера"
    echo "или через туннель SSH (./ops/setup-link.sh). Вернуть: ./ops/set-setup-url.sh https://имя:порт"
  else
    echo "SHTURMAN_SETUP_URL не задан: страница настройки переписки открывается только с самого"
    echo "сервера или через туннель SSH (./ops/setup-link.sh). Адрес по умолчанию запишут"
    echo "./ops/set-public-url.sh и ./ops/up.sh, когда будет задан адрес дашборда."
  fi
  exit 0
fi

[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }
umask 077

# --- убрать ---
if [ "$clear" = yes ]; then
  # Строка остаётся пустой: так ./ops/up.sh видит, что адрес убран намеренно, и не возвращает
  # адрес по умолчанию.
  env_put SHTURMAN_SETUP_URL ""
  echo "SHTURMAN_SETUP_URL убран: страница настройки переписки — только через туннель SSH (./ops/setup-link.sh)."
  echo "Дальше: ./ops/up.sh — сервис переписки узнаёт адрес при запуске. Блок этой страницы в обратном"
  echo "прокси и открытый для неё порт больше не нужны (правка прокси и firewall — стоп-точки)."
  exit 0
fi

# --- записать ---
if [ "$MODE" != hermes ]; then
  {
    echo "Режим без Hermes: внешний адрес странице настройки переписки не задаётся. Она открывается"
    echo "с самого сервера или через туннель SSH: ./ops/setup-link.sh даёт ссылку и подсказывает, как"
    echo "открыть её со своего компьютера. Наружу экземпляр в этом режиме ничего не публикует."
    echo "Ничего не записано."
  } >&2
  exit 2
fi

case "$url" in
  https://*) ;;
  http://*)
    echo "нужен адрес с https://: на этой странице вводятся ключи и идёт вход в Telegram," >&2
    echo "по открытому соединению её наружу отдавать нельзя. Ничего не записано." >&2; exit 2 ;;
  *) echo "нужен адрес вида https://assistant.example.com:8443 или https://setup.example.com" >&2; exit 2 ;;
esac
url="${url%/}"
rest="${url#https://}"
case "$rest" in
  */*|*\?*|*\#*)
    echo "нужны только https://, имя узла и порт, без пути: например https://assistant.example.com:8443" >&2
    echo "(путь у страницы всегда один — /shturman-setup/, его дописывать не нужно). Ничего не записано." >&2
    exit 2 ;;
  *@*) echo "в адресе не должно быть имени пользователя и пароля. Ничего не записано." >&2; exit 2 ;;
esac
origin="$(url_origin "$url")"
host="$(url_host "$url")"
if [ -z "$origin" ] || ! printf '%s' "$host" | grep -Eq '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$'; then
  echo "адрес не разобран: нужно имя узла с точкой из латинских букв, цифр и дефисов (или IPv4-адрес) и," >&2
  echo "если нужно, порт от 1 до 65535 — например https://assistant.example.com:8443. Ничего не записано." >&2
  exit 2
fi
ip="$(ip_kind "$host")"
if [ "$ip" = private ]; then
  echo "ОТКАЗ: $host — не внешний IP-адрес сервера (или записан необычно): это адрес закрытой сети, самого" >&2
  echo "сервера или служебный. Снаружи страницу по нему не открыть, и сертификат на него не выдаётся. Нужен" >&2
  echo "IP-адрес, по которому сервер виден из интернета, в обычной записи — например https://203.0.113.10:8443, —" >&2
  echo "либо имя. Ничего не записано." >&2
  exit 2
fi
port="$(url_port "$origin")"

if [ -n "$public_origin" ] && [ "$origin" = "$public_origin" ]; then
  echo "ОТКАЗ: $origin — это адрес, по которому открывается сам ассистент (SHTURMAN_PUBLIC_URL)." >&2
  echo "Странице настройки переписки нужен ДРУГОЙ адрес." >&2
  same_origin_why
  def="$(setup_url_default "$public")"
  echo "Проще всего — то же имя на другом порту: ./ops/set-setup-url.sh ${def:-https://$host:9443}" >&2
  echo "Ничего не записано." >&2
  exit 2
fi

env_put SHTURMAN_SETUP_URL "$origin"
echo "SHTURMAN_SETUP_URL записан: $origin"
public_host="$(url_host "$public")"
if [ -z "$public_origin" ]; then
  echo "Адрес дашборда (SHTURMAN_PUBLIC_URL) ещё не задан: когда будете задавать, он должен быть другим."
fi
echo "Дальше:"
echo "  1. блок сайта для $origin в обратном прокси по образцу config/Caddyfile.example —"
echo "     стоп-точка (sudo): показать настройку владельцу и получить согласие;"
if [ -n "$public_host" ] && [ "$host" = "$public_host" ]; then
  echo "  2. открыть порт $port в firewall — стоп-точка (sudo); если перед сервером стоит внешняя"
  echo "     защита (Cloudflare и подобные) — убедиться, что она пропускает HTTPS на этот порт;"
elif [ "$ip" = public ]; then
  echo "  2. $host должен быть IP-адресом этого же сервера; открыть порт $port в firewall — стоп-точка (sudo);"
else
  echo "  2. запись DNS для $host, направленная на этот сервер, — делает владелец;"
  if [ "$port" != 443 ]; then echo "     и открыть порт $port в firewall — стоп-точка (sudo);"; fi
fi
echo "  3. ./ops/up.sh, затем ./ops/doctor.sh — строки setup-origin, setup-page-public, setup-isolation."
if [ "$ip" = public ]; then
  echo
  ip_note "порт $port"
fi
