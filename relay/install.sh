#!/usr/bin/env bash
# Установка и обновление посредника «Штурмана» на его сервере (docs/relay.md, relay/README.md).
# Запускает держатель посредника на самом посреднике (Ubuntu 24.04), под root:
#   sudo ./relay/install.sh                      — поставить или обновить; повторный запуск безопасен
#   sudo RELAY_DOMAIN=sh.example.org ./relay/install.sh   — другая зона (по умолчанию sh.namre.app)
#   ./relay/install.sh --download-only КАТАЛОГ   — только скачать и сверить frp и Caddy в новый каталог
#   ./relay/install.sh --render КАТАЛОГ          — только собрать файлы настройки для зоны в новый каталог
#   -h, --help                                   — эта справка; ничего не меняется
#
# Что ставится (всё — в /opt/shturman-relay, /etc/frp, /etc/shturman-relay, /etc/systemd/system):
#   * frp 0.71.0 (frps и frpc) и Caddy 2.11.7 — официальные выпуски с GitHub, суммы sha256 (frp)
#     и sha512 (Caddy) закреплены ниже и сверяются до установки; не совпали — ничего не ставится;
#   * relayd.py (служба: плагин frps и выдача имён) и команда relayctl;
#   * службы systemd: frps, shturman-relayd, shturman-relay-caddy, shturman-relay-frpc.
# Что создаётся, если ещё нет (существующее не перезаписывается):
#   * /etc/frp/token.env — общий токен frps (root, 600);
#   * /etc/frp/frps.crt и frps.key — постоянный сертификат frps для TLS к экземплярам (ключ — root, 600).
#     Экземпляр получает сертификат при подключении и по нему проверяет посредника;
#   * /etc/shturman-relay/operator.env — ключ служебного экземпляра посредника (root, 600);
#   * /var/lib/shturman-relay/relay.db — база экземпляров и приглашений (пользователь shturman-relay, 600).
# Файлы настройки frps, Caddy и служебного frpc перезаписываются из репозитория; прежний
# /etc/frp/frps.toml, если отличался, сохраняется рядом с пометкой .bak-ДАТА.
#
# Службы прототипа relay-test-*.service (тестовый «экземпляр» test.<зона>) установщик останавливает
# и убирает: имя test больше не выдаётся, а плагин frps без ключа его не пустит.
#
# Секретов скрипт не печатает. Новый сертификат frps (если создан) меняет то, чем экземпляры
# проверяют посредника: уже подключённым экземплярам нужно ./ops/relay.sh on (он заберёт сертификат
# заново). На первой установке подключённых экземпляров ещё нет.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
DOMAIN="${RELAY_DOMAIN:-sh.namre.app}"
PREFIX=/opt/shturman-relay

FRP_VERSION=0.71.0
FRP_SHA256_amd64=84f27e39f11169f7adcef8e8b70c9329de17747b1f14dad9fb95eef5682ea716
FRP_SHA256_arm64=f33c293c275d8fc68c654b6fba8f10b2551d6463d09a9fc9cffb7227eae82266
CADDY_VERSION=2.11.7
CADDY_SHA512_amd64=a7a433a1b133efc3c8d10eb0b99d52a24b5ef5c322dc77f5282182b1c0402139ab83f3a99f0c52409df77d20123fb0b523edad8a66d8f5e49136197bf61ef0e7
CADDY_SHA512_arm64=3db36ba90c7a6e8dda40ee3dd71fa08844c76b5fb08f61b31e5e78d2ed38e71c51dc7baed875e50d1ca1279196e84302967237386ae87c91ae9f2aaceada682e

usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; }
die() { echo "ОШИБКА: $*" >&2; exit 1; }

if ! printf '%s' "$DOMAIN" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'; then
  die "RELAY_DOMAIN — имя зоны строчными буквами, например sh.namre.app"
fi

arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    *) die "процессор $(uname -m) не поддерживается: выпуски закреплены для amd64 и arm64" ;;
  esac
}

# Скачать и сверить frp и Caddy в новый пустой каталог $1. Не совпала сумма — файл удаляется, код 1.
fetch() {
  local dir="$1" a frp_sum caddy_sum frp_tgz caddy_tgz
  a="$(arch)"
  [ ! -e "$dir" ] || [ -z "$(ls -A "$dir")" ] || die "$dir не пустой — нужен новый каталог"
  mkdir -p "$dir"
  case "$a" in
    amd64) frp_sum="$FRP_SHA256_amd64"; caddy_sum="$CADDY_SHA512_amd64" ;;
    arm64) frp_sum="$FRP_SHA256_arm64"; caddy_sum="$CADDY_SHA512_arm64" ;;
  esac
  frp_tgz="frp_${FRP_VERSION}_linux_${a}.tar.gz"
  caddy_tgz="caddy_${CADDY_VERSION}_linux_${a}.tar.gz"
  echo "скачиваю frp $FRP_VERSION ($a)…"
  curl -fsSL --proto '=https' --retry 3 -m 300 -o "$dir/$frp_tgz" \
    "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/${frp_tgz}"
  printf '%s  %s\n' "$frp_sum" "$dir/$frp_tgz" | sha256sum -c --quiet - \
    || { rm -f "$dir/$frp_tgz"; die "контрольная сумма frp не совпала — ничего не установлено"; }
  echo "скачиваю Caddy $CADDY_VERSION ($a)…"
  curl -fsSL --proto '=https' --retry 3 -m 300 -o "$dir/$caddy_tgz" \
    "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/${caddy_tgz}"
  printf '%s  %s\n' "$caddy_sum" "$dir/$caddy_tgz" | sha512sum -c --quiet - \
    || { rm -f "$dir/$caddy_tgz"; die "контрольная сумма Caddy не совпала — ничего не установлено"; }
  tar --no-same-owner -xzf "$dir/$frp_tgz" -C "$dir" --strip-components=1 "frp_${FRP_VERSION}_linux_${a}/frps" "frp_${FRP_VERSION}_linux_${a}/frpc"
  tar --no-same-owner -xzf "$dir/$caddy_tgz" -C "$dir" caddy
  echo "суммы совпали: frp $FRP_VERSION, Caddy $CADDY_VERSION"
}

# Собрать файлы настройки для зоны в новый каталог $1.
render() {
  local dir="$1" f
  [ ! -e "$dir" ] || [ -z "$(ls -A "$dir")" ] || die "$dir не пустой — нужен новый каталог"
  mkdir -p "$dir/systemd"
  sed "s/@DOMAIN@/$DOMAIN/g" "$here/frps.toml.example" > "$dir/frps.toml"
  sed "s/@DOMAIN@/$DOMAIN/g" "$here/Caddyfile" > "$dir/Caddyfile"
  sed "s/@DOMAIN@/$DOMAIN/g" "$here/frpc-operator.toml" > "$dir/frpc.toml"
  for f in "$here"/systemd/*.service; do
    sed "s/@DOMAIN@/$DOMAIN/g" "$f" > "$dir/systemd/$(basename "$f")"
  done
  if grep -rq '@DOMAIN@' "$dir"; then die "в файлах настройки осталась незаполненная зона"; fi
}

# Постоянный сертификат frps: ECDSA P-256, на имя relay.<зона>, 10 лет. Экземпляры доверяют именно ему.
make_frps_cert() {
  local crt="$1" key="$2"
  ( umask 077
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -sha256 -days 3650 -nodes \
      -subj "/CN=relay.$DOMAIN" -addext "subjectAltName=DNS:relay.$DOMAIN" \
      -addext "keyUsage=critical,digitalSignature" -addext "extendedKeyUsage=serverAuth" \
      -keyout "$key" -out "$crt" 2>/dev/null )
  chmod 600 "$key"; chmod 644 "$crt"
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --download-only) [ $# -eq 2 ] || die "нужен каталог: --download-only КАТАЛОГ"; fetch "$2"; exit 0 ;;
  --render) [ $# -eq 2 ] || die "нужен каталог: --render КАТАЛОГ"; render "$2"; exit 0 ;;
  --make-cert) [ $# -eq 3 ] || die "использование: --make-cert ФАЙЛ.crt ФАЙЛ.key"; make_frps_cert "$2" "$3"; exit 0 ;;
  "") ;;
  *) echo "неизвестный параметр: $1 — справка: $0 --help" >&2; exit 2 ;;
esac

[ "$(id -u)" -eq 0 ] || die "запускается под root: sudo $0"
for tool in curl openssl systemctl sha256sum sha512sum python3; do
  command -v "$tool" >/dev/null || die "нет программы $tool"
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || die "нужен python3 версии 3.12 или новее"
if ! grep -q 'VERSION_ID="24.04"' /etc/os-release 2>/dev/null; then
  echo "ВНИМАНИЕ: установщик проверялся только на Ubuntu 24.04" >&2
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fetch "$tmp/dl"
render "$tmp/conf"

echo "ставлю программы в $PREFIX…"
install -d -m 755 "$PREFIX" "$PREFIX/bin"
install -m 755 "$tmp/dl/frps" "$tmp/dl/frpc" "$tmp/dl/caddy" "$PREFIX/bin/"
install -m 644 "$here/relayd.py" "$PREFIX/relayd.py"
printf '#!/bin/sh\n# Команды держателя посредника «Штурмана»: relayctl --help\nexec /usr/bin/python3 -I %s/relayd.py "$@"\n' "$PREFIX" > "$tmp/relayctl"
install -m 755 "$tmp/relayctl" /usr/local/sbin/relayctl

id shturman-relay >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin shturman-relay
install -d -m 755 /etc/frp
install -d -m 755 /etc/shturman-relay
install -d -m 700 -o shturman-relay -g shturman-relay /var/lib/shturman-relay

if [ ! -s /etc/frp/token.env ]; then
  ( umask 077; printf 'FRP_AUTH_TOKEN=%s\n' "$(openssl rand -hex 32)" > /etc/frp/token.env )
  echo "создан общий токен frps: /etc/frp/token.env"
fi
grep -q '^FRP_AUTH_TOKEN=.' /etc/frp/token.env || die "в /etc/frp/token.env нет строки FRP_AUTH_TOKEN="
chown root:root /etc/frp/token.env; chmod 600 /etc/frp/token.env

new_cert=no
if [ ! -s /etc/frp/frps.crt ] || [ ! -s /etc/frp/frps.key ]; then
  make_frps_cert /etc/frp/frps.crt /etc/frp/frps.key
  new_cert=yes
  echo "создан сертификат frps: /etc/frp/frps.crt (ключ — /etc/frp/frps.key, только root)"
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
if [ -f /etc/frp/frps.toml ] && ! cmp -s /etc/frp/frps.toml "$tmp/conf/frps.toml"; then
  cp -p /etc/frp/frps.toml "/etc/frp/frps.toml.bak-$stamp"
  echo "прежняя настройка frps сохранена: /etc/frp/frps.toml.bak-$stamp"
fi
install -m 644 "$tmp/conf/frps.toml" /etc/frp/frps.toml
install -m 644 "$tmp/conf/Caddyfile" /etc/shturman-relay/Caddyfile
install -m 644 "$tmp/conf/frpc.toml" /etc/shturman-relay/frpc.toml
for f in "$tmp"/conf/systemd/*.service; do
  dst="/etc/systemd/system/$(basename "$f")"
  if [ -f "$dst" ] && ! cmp -s "$dst" "$f"; then cp -p "$dst" "$dst.bak-$stamp"; fi
  install -m 644 "$f" "$dst"
done

# Службы прототипа: тестовый «экземпляр» test.<зона>.
for unit in /etc/systemd/system/relay-test-*.service; do
  [ -e "$unit" ] || continue
  name="$(basename "$unit")"
  echo "убираю службу прототипа $name…"
  systemctl disable --now "$name" >/dev/null 2>&1 || true
  rm -f "$unit"
done
systemctl daemon-reload

# База и ключ служебного экземпляра (relayctl сам работает от имени владельца базы).
relayctl --db /var/lib/shturman-relay/relay.db init
relayctl --db /var/lib/shturman-relay/relay.db operator --key-file /etc/shturman-relay/operator.env
chown root:root /etc/shturman-relay/operator.env; chmod 600 /etc/shturman-relay/operator.env

# Проверка настроек до перезапуска служб: frps — с теми же окружением и сертификатом, что у службы.
"$PREFIX/bin/caddy" validate --config /etc/shturman-relay/Caddyfile --adapter caddyfile >/dev/null 2>&1 \
  || die "Caddy не принял /etc/shturman-relay/Caddyfile: $PREFIX/bin/caddy validate --config /etc/shturman-relay/Caddyfile"
( set -a; . /etc/frp/token.env; set +a
  CREDENTIALS_DIRECTORY=/etc/frp "$PREFIX/bin/frps" verify -c /etc/frp/frps.toml >/dev/null ) \
  || die "frps не принял /etc/frp/frps.toml: проверьте файл (frps verify -c /etc/frp/frps.toml)"

echo "запускаю службы…"
systemctl enable shturman-relayd frps shturman-relay-caddy shturman-relay-frpc >/dev/null 2>&1
systemctl restart shturman-relayd
systemctl restart frps
systemctl restart shturman-relay-caddy shturman-relay-frpc
for unit in shturman-relayd frps shturman-relay-caddy shturman-relay-frpc; do
  if systemctl is-active --quiet "$unit"; then echo "  $unit: работает"; else echo "  $unit: НЕ РАБОТАЕТ — journalctl -u $unit -n 50" >&2; fi
done

echo "жду https://relay.$DOMAIN/api/health (сертификат Let's Encrypt при первом запуске — до пары минут)…"
ok=no
for _ in $(seq 1 30); do
  if curl -fsS -m 10 -o /dev/null "https://relay.$DOMAIN/api/health" 2>/dev/null; then ok=yes; break; fi
  sleep 5
done
if [ "$ok" = yes ]; then
  echo "ГОТОВО: https://relay.$DOMAIN/api/health отвечает."
else
  echo "https://relay.$DOMAIN/api/health не ответил за 150 секунд — journalctl -u shturman-relay-caddy -u shturman-relay-frpc -u frps -n 80" >&2
fi
if [ "$new_cert" = yes ]; then
  echo "Сертификат frps новый: экземпляры, подключённые раньше, заберут его командой ./ops/relay.sh on."
fi
echo "Код приглашения для владельца экземпляра: sudo relayctl invite --note 'кому'. Список: sudo relayctl list."
[ "$ok" = yes ]
