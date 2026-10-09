#!/usr/bin/env bash
# Записывает в .env адрес, по которому владелец открывает ассистента (SHTURMAN_PUBLIC_URL),
# и вместе с ним — адрес страницы настройки переписки по умолчанию (SHTURMAN_SETUP_URL).
#   ./ops/set-public-url.sh https://assistant.example.com
#   ./ops/set-public-url.sh https://203.0.113.10        — домена нет: IP-адрес сервера (только IPv4)
# После изменения — ./ops/up.sh, чтобы Hermes и сервис переписки подхватили адреса.
#
# Без домена. Вместо имени можно записать внешний IPv4-адрес сервера: Let's Encrypt выдаёт
# сертификаты и на IP-адрес (короткие, на 6 дней; Caddy продлевает их сам). Для этого нужен
# Caddy 2.11 или новее и вариант «без домена» из config/Caddyfile.example, а порт 80 должен быть
# открыт из интернета — подробно docs/deployment.md, «Без домена». Адрес https:// с IP закрытой
# сети (10.x, 172.16–31.x, 192.168.x, 100.64–127.x), самого сервера (127.x) и подобными скрипт не
# принимает: снаружи их не открыть, и сертификат на них не выдаётся. IPv6 не поддерживается.
# Адрес http:// принимается, как и раньше, с предупреждением — в том числе с адресом закрытой сети.
#
# Адрес страницы настройки переписки по умолчанию — то же имя, но порт 8443:
# https://assistant.example.com:8443. Владельцу этот порт ни знать, ни набирать не нужно: он
# переходит по кнопке в мастере и по ссылке из ./ops/setup-link.sh. Порт другой потому, что
# странице нужен свой адрес, отдельный от дашборда (почему — ./ops/set-setup-url.sh --help).
# Если адрес страницы уже задан (или убран владельцем), скрипт его не трогает; исключение — он
# был адресом по умолчанию для прежнего имени: тогда он переезжает на новое имя.
# Другой порт или отдельное имя — ./ops/set-setup-url.sh.
#
# Оба значения не секретные. Из .env читаются три несекретные строки: SHTURMAN_MODE,
# SHTURMAN_PUBLIC_URL и SHTURMAN_SETUP_URL; остальное скрипт не читает и не печатает.
# Только для режима с Hermes: без него у экземпляра нет ни дашборда, ни внешнего адреса.
#   -h, --help — эта справка; .env не меняется.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"
case "${1:-}" in -*) ops_unknown "$1" ;; esac
[ $# -le 1 ] || ops_unknown "$2"
if [ -f .env ]; then require_hermes "дашборда и внешнего адреса"; fi

url="${1:-}"
case "$url" in
  https://*|http://*) ;;
  *) echo "нужен адрес с https://, например https://assistant.example.com" >&2; exit 2 ;;
esac
url="${url%/}"
if ! printf '%s' "$url" | grep -Eq '^https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?$'; then
  echo "адрес содержит недопустимые символы" >&2; exit 2
fi
if [ -z "$(url_origin "$url")" ]; then
  echo "адрес не разобран: проверьте имя и порт" >&2; exit 2
fi
ip="$(ip_kind "$(url_host "$url")")"
# Требование внешнего IPv4 — только для https: сертификат на IP-адрес выдаётся лишь на такой.
# Адрес http:// принимается как раньше, с предупреждением ниже.
case "$url" in http://*) ip="" ;; esac
# Необычная запись IP (010, 0x…, три числа) — отказ и для http: с ней сервис переписки не запустится.
if ip_odd "$(url_host "$url")"; then ip=private; fi
if [ "$ip" = private ]; then
  echo "ОТКАЗ: $(url_host "$url") — не внешний IP-адрес сервера (или записан необычно): это адрес закрытой сети," >&2
  echo "самого сервера или служебный. Снаружи по нему ассистента не открыть, и сертификат на него не выдаётся." >&2
  echo "Нужен IP-адрес, по которому сервер виден из интернета (обычно тот, по которому вы входите по SSH)," >&2
  echo "в обычной записи — четыре числа через точку, например https://203.0.113.10, — либо имя. Ничего не записано." >&2
  exit 2
fi
case "$url" in
  http://*) echo "ВНИМАНИЕ: адрес без https — вход будет работать, но данные пойдут по сети открыто." >&2 ;;
esac

[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }

# Что делать с адресом страницы настройки переписки.
old_public="$(env_get SHTURMAN_PUBLIC_URL)"
setup="$(env_get SHTURMAN_SETUP_URL)"
want="$(setup_url_default "$url")"
action=keep
if ! env_has SHTURMAN_SETUP_URL; then
  action=default                       # строки ещё нет: записать адрес по умолчанию
elif [ -n "$setup" ] && [ -n "$old_public" ] && [ "$(url_origin "$setup")" = "$(setup_url_default "$old_public")" ]; then
  action=default                       # был адресом по умолчанию для прежнего имени: переезжает
fi
if [ "$action" = keep ] && [ -n "$setup" ] && [ "$(url_origin "$setup")" = "$(url_origin "$url")" ]; then
  echo "ОТКАЗ: этот адрес уже занят страницей настройки переписки (SHTURMAN_SETUP_URL)." >&2
  echo "Дашборду ассистента и странице настройки нужны разные адреса: на общем адресе ассистент мог бы" >&2
  echo "действовать на странице настройки от имени вошедшего владельца. Ничего не записано." >&2
  echo "Сначала смените адрес страницы настройки: ./ops/set-setup-url.sh https://имя:порт" >&2
  exit 2
fi

umask 077
env_put SHTURMAN_PUBLIC_URL "$url"
echo "SHTURMAN_PUBLIC_URL записан: $url"
case "$action" in
  default)
    if [ -n "$want" ]; then
      env_put SHTURMAN_SETUP_URL "$want"
      echo "SHTURMAN_SETUP_URL записан: $want — адрес страницы настройки переписки (по умолчанию: то же имя, порт $SETUP_PORT_DEFAULT)"
      echo "Что ещё нужно для этого адреса: второй блок сайта в обратном прокси (config/Caddyfile.example)"
      echo "и открытый порт $SETUP_PORT_DEFAULT в firewall — обе правки стоп-точки (sudo). Другой адрес: ./ops/set-setup-url.sh"
    else
      if env_has SHTURMAN_SETUP_URL; then env_del SHTURMAN_SETUP_URL; fi
      echo "SHTURMAN_SETUP_URL не записан: адрес по умолчанию для такого адреса дашборда предложить нельзя"
      echo "(нужен https, и порт $SETUP_PORT_DEFAULT не должен быть занят самим дашбордом). Страница настройки переписки"
      echo "открывается через туннель SSH (./ops/setup-link.sh); свой адрес — ./ops/set-setup-url.sh https://имя:порт"
    fi ;;
  keep)
    if [ -n "$setup" ]; then
      echo "SHTURMAN_SETUP_URL не менялся: $setup — адрес страницы настройки переписки"
    else
      echo "SHTURMAN_SETUP_URL не менялся: адрес страницы настройки переписки убран (./ops/set-setup-url.sh --clear);"
      echo "страница открывается только через туннель SSH. Вернуть: ./ops/set-setup-url.sh https://имя:порт"
    fi ;;
esac

# Адрес из IP: что ещё для него нужно (сертификат на IP-адрес, порт 80, версия Caddy).
if [ "$ip" = public ]; then
  case "$url" in
    https://*)
      ports="порт $(url_port "$url")"
      setup_now="$(env_get SHTURMAN_SETUP_URL)"
      if [ -n "$setup_now" ] && [ "$(url_host "$setup_now")" = "$(url_host "$url")" ]; then
        ports="порты $(url_port "$url") и $(url_port "$setup_now")"
      fi
      echo
      ip_note "$ports" ;;
  esac
fi
