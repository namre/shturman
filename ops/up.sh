#!/usr/bin/env bash
# Поднимает стек (или применяет изменения). Повторный запуск безопасен.
#   ./ops/up.sh          — скачать образы при необходимости и запустить
#   ./ops/up.sh --pull   — сначала обновить образы указанных версий
#   ./ops/up.sh --help   — эта справка; ничего не запускается
# Что запускается, зависит от режима установки (SHTURMAN_MODE в .env, ./ops/mode.sh):
#   hermes      — Hermes, сервис переписки, база; плагин и архив подключаются к Hermes: запись
#                 mcp_servers.shturman в настройках Hermes и токен архива в .env профиля Hermes
#                 (data/hermes/.env — штатной функцией Hermes внутри контейнера, значение
#                 не печатается; при изменении Hermes перезапускается); часовой пояс владельца
#                 (HERMES_TIMEZONE контейнера) — в timezone config.yaml профиля командой
#                 `hermes config set timezone` (Р-90: многопрофильный шлюз переменную не читает;
#                 при изменении Hermes перезапускается), а задачи shturman:*, посчитанные
#                 по другому поясу, пересчитываются `hermes cron edit <id> --schedule <та же запись>`;
#   standalone — только сервис переписки и база. Шаги Hermes пропускаются, его образ
#                 не скачивается, а оставшийся от прежнего режима контейнер убирается.
# В режиме с Hermes, если адрес дашборда задан, а адреса страницы настройки переписки в .env ещё
# нет, скрипт дописывает адрес по умолчанию: то же имя, порт 8443 (./ops/set-setup-url.sh --help).
# Прокси, firewall и каталог сокета сервиса для прокси (/run/shturman-service, Р-76; его создаёт
# systemd-tmpfiles по config/shturman-service-socket.conf под sudo) скрипт не трогает; если каталог
# готов, а сервис переписки запущен до его появления, скрипт перезапускает сервис, чтобы тот открыл
# сокет. Если экземпляр подключён к посреднику (./ops/relay.sh join,
# профиль relay), скрипт собирает и запускает и контейнер frpc — только в режиме с Hermes.
# Если адрес дашборда или адрес страницы настройки записан в .env не как адрес (например, без
# https://) или как https-адрес с IP закрытой сети либо в необычной записи (docs/deployment.md,
# «Без домена»), скрипт останавливается до перезапуска и называет строку; так же — при неизвестном часовом
# поясе SHTURMAN_TIMEZONE. Из .env читаются только несекретные строки: режим, профили, пояс, эти два
# адреса и — только на «задана ли и пуста ли» — строка адреса API своей модели.
# Свежая копия перед миграциями и сменой Hermes (стоп-точки «миграции базы» и «обновление без свежей
# проверенной копии»): если в коде есть миграции, которых нет в базе (сверка имён: файлы
# service/src/shturman/migrations против таблицы schema_migrations; для этого контейнер базы
# запускается первым), или образ Hermes сменится (HERMES_VERSION, новый compose.yaml), скрипт
# требует копию моложе SHTURMAN_BACKUP_FRESH_MINUTES минут (по умолчанию 60; ./ops/backup.sh) либо
# только что выполненное восстановление (./ops/restore.sh). Нет — остановка до перезапуска.
#   ./ops/up.sh --skip-backup-check — без свежей копии, осознанно: только человек в своём терминале
#                                     (фраза с одноразовым числом); без терминала — отказ, код 4.
# Непроверенный образ Hermes (нет в config/hermes-versions.txt) — предупреждение; подтверждение
# человека спрашивает ./ops/update.sh, копию требует и этот скрипт.
# Работает под общим замком ops/: второй одновременный запуск отказывает с кодом 75.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"
pull=no
skip_check=no
for arg in "$@"; do
  case "$arg" in
    --pull) pull=yes ;;
    --skip-backup-check) skip_check=yes ;;
    *) ops_unknown "$arg" ;;
  esac
done

[ -f .env ] || { echo "нет .env — сначала ./ops/init-env.sh --auto" >&2; exit 1; }
ops_lock "запуск стека (./ops/up.sh)"

# Служебные значения, появившиеся в новой версии (токены сервиса переписки, режим установки),
# дописываются сами. Уже заданные значения скрипт не трогает и не печатает.
./ops/init-env.sh --auto > /dev/null
load_mode
# Адрес страницы настройки переписки по умолчанию (то же имя, что у дашборда, порт 8443) —
# экземпляру, у которого этой строки ещё нет. Ничего другого в .env при этом не меняется.
ensure_setup_url "$MODE"

# Оба адреса compose.yaml передаёт сервису переписки (SHTURMAN_DASHBOARD_ORIGIN и
# SHTURMAN_SETUP_ORIGIN), и сервис разбирает их при запуске: с неразборчивым адресом он не
# запускается вовсе, а контейнер уходит в цикл перезапусков. Поэтому очевидно неверную запись
# останавливаем здесь, до перезапуска, пока прежний сервис ещё работает. У адреса дашборда путь
# и косая черта на конце допустимы (сервис берёт из него только схему, имя и порт), у адреса
# страницы настройки — нет. Проверяется только вид записи; неверный порт и подобное сервис назовёт
# сам, в журнале контейнера. Обе строки не секретные; значения не печатаются.
addr_bad() {
  local v
  v="$(env_get "$1")"
  [ -n "$v" ] || return 1
  if printf '%s' "$v" | grep -Eq "$2"; then return 1; fi
  return 0
}
if addr_bad SHTURMAN_PUBLIC_URL '^https?://[^/?#@[:space:]]+([/?#].*)?$'; then
  echo "SHTURMAN_PUBLIC_URL в .env записан не как адрес вида https://имя[:порт] — с ним сервис переписки не запустится." >&2
  echo "Запишите адрес заново: ./ops/set-public-url.sh https://адрес — и повторите ./ops/up.sh. Ничего не перезапущено." >&2
  exit 1
fi
if addr_bad SHTURMAN_SETUP_URL '^https?://[^/?#@[:space:]]+/?$'; then
  echo "SHTURMAN_SETUP_URL в .env записан не как https://имя[:порт] без пути — с ним сервис переписки не запустится." >&2
  echo "Запишите адрес заново: ./ops/set-setup-url.sh https://имя:порт (или --clear) — и повторите ./ops/up.sh. Ничего не перезапущено." >&2
  exit 1
fi
# IP-адрес вместо имени (экземпляр без домена). Необычную запись (010, 0x…, три числа) браузер
# прочёл бы как другой адрес, и с ней сервис переписки не запускается — по любой схеме. Адрес
# https:// — ещё и только внешний IPv4: на адрес закрытой сети, самого сервера или служебный
# сертификат не выдаётся. Адрес http:// с обычным IP закрытой сети принимается, как и раньше.
# Такую строку вписали вручную — скрипты адресов её не пишут.
for name in SHTURMAN_PUBLIC_URL SHTURMAN_SETUP_URL; do
  v="$(env_get "$name")"
  h="$(url_host "$v")"
  [ -n "$h" ] || continue
  bad=no
  if ip_odd "$h"; then bad=yes; fi
  case "$v" in https://*) if [ "$(ip_kind "$h")" = private ]; then bad=yes; fi ;; esac
  if [ "$bad" = yes ]; then
    echo "$name в .env записан не как внешний IP-адрес сервера: это IP закрытой сети, самого сервера, служебный или в необычной записи." >&2
    echo "По https снаружи такой адрес не откроется (docs/deployment.md, «Без домена»). Запишите адрес заново: ./ops/set-public-url.sh или ./ops/set-setup-url.sh — и повторите ./ops/up.sh. Ничего не перезапущено." >&2
    exit 1
  fi
done

# Часовой пояс владельца (Р-90): с неизвестным именем сервис переписки не запускается, а Hermes считает
# по UTC. Строка не секретная; её пишет ./ops/init-env.sh --auto --timezone, но могли вписать вручную.
owner_tz="$(env_get SHTURMAN_TIMEZONE)"
if [ -n "$owner_tz" ] && ! tz_valid "$owner_tz"; then
  echo "SHTURMAN_TIMEZONE в .env — неизвестный часовой пояс «$owner_tz»: с ним сервис переписки не запустится." >&2
  echo "Запишите пояс заново: ./ops/init-env.sh --auto --timezone Europe/Moscow (или свой, например Asia/Yekaterinburg) — и повторите ./ops/up.sh. Ничего не перезапущено." >&2
  exit 1
fi

# Адрес API своей модели контейнер получает, только когда строка SHTURMAN_LLM_BASE_URL есть в .env
# (compose.yaml): адрес из окружения страница настройки менять не даёт. Строка без значения значит
# «адрес по умолчанию», но сервис принял бы её за пустой адрес — на время запуска подставляем
# адрес по умолчанию. Файл .env при этом не меняется.
if env_has SHTURMAN_LLM_BASE_URL && [ -z "$(env_get SHTURMAN_LLM_BASE_URL)" ]; then
  export SHTURMAN_LLM_BASE_URL="https://api.openai.com/v1"
fi

# Каталог данных принадлежит тому, кто запускает стек; Hermes и сервис переписки в контейнерах
# работают под ним же. Точки подключения создаём сами: иначе Docker создаст их от имени root.
mkdir -p data/shturman data/embeddings/model
if [ "$MODE" = hermes ]; then mkdir -p data/hermes/plugins/shturman; fi
chmod 700 data data/shturman
HERMES_UID="$(id -u)"
HERMES_GID="$(id -g)"
export HERMES_UID HERMES_GID

c=shturman-hermes
hx() { docker exec -u "$HERMES_UID:$HERMES_GID" "$c" "$@"; }
plugin_enabled() {
  hx /opt/hermes/.venv/bin/python -c '
from hermes_cli.config import load_config
p = (load_config() or {}).get("plugins") or {}
ok = "shturman" in (p.get("enabled") or []) and "shturman" not in (p.get("disabled") or [])
print("yes" if ok else "no")' 2>/dev/null | tail -n 1
}
# Запись об архиве в настройках Hermes: адрес сервиса, токен по имени переменной (само значение
# в настройки не попадает), перечень инструментов — все только на чтение.
mcp_register() {
  hx /opt/hermes/.venv/bin/python -c '
from hermes_cli.config import load_config, save_config
want = {
    "url": "http://127.0.0.1:8765/mcp",
    "headers": {"Authorization": "Bearer ${MCP_SHTURMAN_API_KEY}"},
    "timeout": 60,
    "connect_timeout": 15,
    "skip_preflight": True,
    "tools": {
        "include": ["search_messages", "get_context", "list_chats", "get_chat_history", "find_person",
                    "list_commitments", "get_commitment", "get_person_page", "search_pages",
                    "list_projects", "get_project_page", "get_owner_profile", "list_spheres",
                    "list_awaiting_reply"],
        "resources": False,
        "prompts": False,
    },
}
cfg = load_config() or {}
servers = cfg.get("mcp_servers")
if not isinstance(servers, dict):
    servers = {}
# load_config отдаёт настройки с уже подставленными переменными окружения: на месте имени
# переменной в заголовке стоит её значение, и сравнение с образцом никогда бы не сошлось —
# запись переписывалась бы, а Hermes перезапускался бы при каждом запуске скрипта. Поэтому
# сравниваем запись как она лежит в файле, а если такой функции в этой версии Hermes нет —
# с образцом, в который переменная подставлена так же. Значение переменной не печатается.
try:
    from hermes_cli.config import read_raw_config
    current = ((read_raw_config() or {}).get("mcp_servers") or {}).get("shturman")
except Exception:
    current = None
import copy, os
filled = copy.deepcopy(want)
token = os.environ.get("MCP_SHTURMAN_API_KEY")
if token is not None:
    filled["headers"]["Authorization"] = "Bearer " + token
if current == want or (current is None and servers.get("shturman") in (want, filled)):
    print("same")
else:
    servers["shturman"] = want
    cfg["mcp_servers"] = servers
    save_config(cfg)
    print("changed")' 2>/dev/null | tail -n 1
}
# Токен архива — в .env профиля Hermes (/opt/data/.env, Р-83). Запись в настройках ссылается на
# переменную MCP_SHTURMAN_API_KEY; контейнеру её передаёт compose.yaml, но этого мало: когда шлюз
# обслуживает несколько профилей, Hermes подставляет переменные только из .env профиля, а значение
# из окружения контейнера не видит — и архив отвечает шлюзу 401. Значение берётся из окружения
# контейнера и записывается штатной функцией Hermes (hermes_cli.config.save_env_value — так же
# токен сохраняет `hermes mcp add`). Сравнение — внутри контейнера; значение не печатается
# и сюда не попадает. Смена токена (./ops/mcp-token.sh) доходит сюда при следующем ./ops/up.sh:
# Compose пересоздаёт контейнер с новым значением, а эта функция обновляет .env профиля.
# Вывод: same | changed | missing (в окружении контейнера нет токена) | failed.
mcp_key_sync() {
  hx /opt/hermes/.venv/bin/python -c '
import os
from hermes_cli.config import load_env, save_env_value
name = "MCP_SHTURMAN_API_KEY"
want = (os.environ.get(name) or "").strip()
if not want:
    print("missing")
elif load_env().get(name) == want:
    print("same")
else:
    save_env_value(name, want)
    from hermes_cli.config import invalidate_env_cache
    invalidate_env_cache()
    print("changed" if load_env().get(name) == want else "failed")' 2>/dev/null | tail -n 1
}
# Часовой пояс шлюза — в config.yaml профиля (/opt/data/config.yaml, Р-90). Пояс владельца контейнер
# получает переменной HERMES_TIMEZONE (из SHTURMAN_TIMEZONE, compose.yaml), но шлюз Hermes, когда
# обслуживает несколько профилей, переменную не читает (hermes_time._env_timezone) и считает
# расписание по `timezone` из config.yaml, а без него — по часам сервера (UTC): утренняя сводка
# на 08:27 приходила в 11:27 по Москве. Пояс записывается штатной командой `hermes config set
# timezone …`; сравнение — внутри контейнера, функциями плагина (shturman_core/cron_jobs.py).
# Пояса в окружении нет — ничего не делаем: пусть решает Hermes. Пояс может записать и сам плагин,
# когда заводит задачи (отметка в его хранилище, cron_jobs.TZ_STATE); шлюз читает пояс только при
# запуске, поэтому запись новее запуска контейнера — тоже повод перезапустить Hermes (pending).
# Вывод: none | same <пояс> | pending <пояс> | changed <пояс> | invalid <пояс> | failed. Пояс не секретный.
tz_py='
import os, sys
sys.path.insert(0, "/opt/data/plugins/shturman")
from shturman_core import cron_jobs
from hermes_cli.config import read_raw_config
want = (os.environ.get("HERMES_TIMEZONE") or "").strip()
have = cron_jobs.config_zone(read_raw_config())
started = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
try:
    from shturman_core.state import Store
    written = Store().read(cron_jobs.TZ_STATE).get("written_at")
except Exception:
    written = None
pending = "yes" if cron_jobs.restart_pending(written, started) else "no"
print(cron_jobs.zone_state(want, have), want or "-", pending)'
tz_sync() {
  local st zone pending started
  started="$(date -d "$(hermes_started)" +%s 2>/dev/null || true)"
  read -r st zone pending <<EOF_TZ
$(hx /opt/hermes/.venv/bin/python -c "$tz_py" "$started" 2>/dev/null | tail -n 1)
EOF_TZ
  case "$st" in
    none) echo none ;;
    same) if [ "$pending" = yes ]; then echo "pending $zone"; else echo "same $zone"; fi ;;
    invalid) echo "invalid $zone" ;;
    missing|differs)
      hx hermes config set timezone "$zone" > /dev/null 2>&1 || true
      read -r st zone pending <<EOF_TZ
$(hx /opt/hermes/.venv/bin/python -c "$tz_py" "$started" 2>/dev/null | tail -n 1)
EOF_TZ
      if [ "$st" = same ]; then echo "changed $zone"; else echo failed; fi ;;
    *) echo failed ;;
  esac
}
# Задачи «Штурмана» (shturman:*), у которых следующий запуск посчитан не по поясу владельца (обычно по UTC,
# до записи пояса; без пояса в окружении — по поясу шлюза) или не лежит на их расписании, пересчитываются штатной командой
# `hermes cron edit <id> --schedule <та же запись>`: Hermes заново считает next_run_at. Остальные
# задачи и выключенные не трогаются. Печатаются только имена задач. Вывод — строки «id<TAB>запись<TAB>имя».
cron_drifted() {
  hx /opt/hermes/.venv/bin/python -c '
import sys
sys.path.insert(0, "/opt/data/plugins/shturman")
from shturman_core import cron_jobs
from hermes_cli.config import read_raw_config
import os
from cron.jobs import list_jobs
zone = cron_jobs.expected_zone(os.environ.get("HERMES_TIMEZONE"), cron_jobs.config_zone(read_raw_config()))
for job in cron_jobs.ours(list_jobs(include_disabled=True)):
    if cron_jobs.schedule_drift(job, zone) in ("offset", "off-schedule"):
        expr = str((job.get("schedule") or {}).get("expr") or "").strip()
        if expr and job.get("id"):
            print(job["id"], expr, job.get("name"), sep="\t")' 2>/dev/null
}
cron_resync() {
  local id expr name bad=0
  while IFS="$(printf '\t')" read -r id expr name; do
    [ -n "$id" ] || continue
    if hx hermes cron edit "$id" --schedule "$expr" > /dev/null 2>&1; then
      echo "Расписание задачи $name пересчитано по поясу Hermes."
    else
      echo "Не удалось пересчитать расписание задачи $name — hermes cron edit $id --schedule \"$expr\"" >&2; bad=$((bad + 1))
    fi
  done <<EOF_CRON
$(cron_drifted)
EOF_CRON
  return "$bad"
}
wait_service() {
  for _ in $(seq 1 45); do
    if curl -fsS -o /dev/null -m 3 http://127.0.0.1:8765/health 2>/dev/null; then return 0; fi
    sleep 2
  done
  return 1
}
# Проверка здоровья и, если она прошла, одна строка о том, что дальше. Код возврата — её.
finish() {
  local rc=0
  ./ops/doctor.sh || rc=$?
  if [ "$rc" -eq 0 ] && [ "$MODE" = hermes ]; then
    echo "Дальше — настройка переписки: владелец открывает её кнопкой в мастере, а входит по одноразовой ссылке; ссылку выдаёт ./ops/setup-link.sh — только по его просьбе."
  elif [ "$rc" -eq 0 ]; then
    echo "Дальше — настройка переписки: владелец открывает страницу через туннель SSH по одноразовой ссылке; ссылку и команду туннеля выдаёт ./ops/setup-link.sh — только по его просьбе."
  fi
  exit "$rc"
}
wait_dashboard() {
  for _ in $(seq 1 45); do
    if curl -fsS -o /dev/null -m 3 http://127.0.0.1:9119/api/status 2>/dev/null; then return 0; fi
    sleep 2
  done
  return 1
}

# Свежая копия перед миграциями базы и сменой образа Hermes. Новая установка (каталога базы ещё нет
# или база пуста) — проверять нечего: терять нечего. Печатаются только имена миграций и образов.
. ops/lib-backup.sh
need=""
if [ -d data/postgres ]; then
  if ! pg_running; then
    echo "Запускаю базу первой — сверить миграции…"
    pg_start || { echo "База не запустилась: docker logs --tail 50 $PG. Ничего не перезапущено." >&2; exit 1; }
  fi
  if applied="$(db_migrations shturman 2> /dev/null)"; then
    if [ -n "$applied" ]; then
      pending="$(list_minus "$(repo_migrations)" "$applied")"
      [ -z "$pending" ] || need="миграции базы: $(list_count "$pending") ($pending)"
    fi
  else
    need="миграции базы: какие применены, узнать не удалось"
  fi
fi
if [ "$MODE" = hermes ]; then
  h_was="$(hermes_image_running)"; h_new="$(hermes_image_planned)"
  if [ "$h_was" != "-" ] && [ "$h_new" != "?" ] && [ "$h_was" != "$h_new" ]; then
    need="${need:+$need; }смена образа Hermes: $h_was → $h_new"
  fi
  if [ "$h_new" != "?" ] && [ "$h_new" != "-" ] && ! hermes_verified "$h_new"; then
    echo "ВНИМАНИЕ: образа $h_new нет в списке проверенных версий Hermes ($HERMES_VERSIONS_FILE): плагин с ним не проверен; откат Hermes — только восстановлением копии data/hermes (./ops/restore.sh)." >&2
  fi
fi
if [ -n "$need" ]; then
  fresh_min="$(bk_fresh_minutes)"
  if fresh="$(bk_fresh "$fresh_min")"; then
    echo "Ждут: $need. Свежая копия есть: $fresh — продолжаю."
  elif fresh="$(recently_restored "$fresh_min")"; then
    echo "Ждут: $need. Данные только что восстановлены из копии $fresh — продолжаю."
  elif [ "$skip_check" = yes ]; then
    if ! has_tty; then need_human "./ops/up.sh --skip-backup-check"; exit "$OPS_NEED_HUMAN"; fi
    echo "Ждут: $need. Свежей копии нет: если что-то пойдёт не так, вернуть базу и данные будет не из чего, кроме старых копий." > /dev/tty
    if ! human_confirm "БЕЗ-КОПИИ"; then echo "Не подтверждено — ничего не перезапущено." >&2; exit "$OPS_NEED_HUMAN"; fi
    ./ops/state.sh log "up.sh без свежей копии по подтверждению человека: $need" > /dev/null 2>&1 || true
  else
    echo "Ждут: $need." >&2
    echo "Свежей проверенной копии (моложе $fresh_min минут) нет — это стоп-точка «обновление без свежей копии». Ничего не перезапущено." >&2
    echo "Сделайте ./ops/backup.sh и повторите ./ops/up.sh — или обновляйтесь через ./ops/update.sh: он делает копию сам и откатывает при провале." >&2
    echo "Осознанно без копии — только человек в своём терминале: ./ops/up.sh --skip-backup-check" >&2
    exit 1
  fi
fi

[ "$pull" = yes ] && docker compose pull --quiet --ignore-buildable

docker compose config --quiet
# Образ сервиса переписки собирается здесь же, из каталога service/.
docker compose build --quiet shturman
# Контейнер распознавания речи — тоже из этого репозитория (каталог asr/), если он включён.
if profile_on asr; then docker compose build --quiet asr; fi
# Подключение к посреднику (./ops/relay.sh) — тоже образ из этого репозитория (каталог frpc/). Только
# в режиме с Hermes: без него профиль relay в COMPOSE_PROFILES не попадает (compose_profiles в lib.sh).
case ",$COMPOSE_PROFILES," in
  *,relay,*)
    mkdir -p data/relay; chmod 700 data/relay
    docker compose build --quiet relay ;;
  *)
    if [ "$MODE" != hermes ] && { profile_on relay || docker inspect shturman-relay >/dev/null 2>&1; }; then
      echo "Режим без Hermes: подключение к посреднику не запускается — наружу в этом режиме ничего не публикуется."
      if docker inspect shturman-relay >/dev/null 2>&1; then
        docker compose --profile relay rm --stop --force relay >/dev/null 2>&1 || true
        if docker inspect shturman-relay >/dev/null 2>&1; then docker rm --force shturman-relay >/dev/null; fi
      fi
    fi ;;
esac
if [ "$MODE" != hermes ] && docker inspect "$c" >/dev/null 2>&1; then
  # Остался от режима с Hermes. Убирается только контейнер: данные Hermes (data/hermes) не трогаем.
  echo "Режим без Hermes: останавливаю и убираю контейнер $c (его данные остаются в data/hermes)…"
  docker compose --profile hermes rm --stop --force hermes || true
  # Если контейнер создавали не этим файлом Compose, первая команда его не увидит.
  if docker inspect "$c" >/dev/null 2>&1; then docker rm --force "$c" >/dev/null; fi
fi
# Когда контейнер Hermes запущен в последний раз (пусто — контейнера нет): по этому ниже видно,
# перезапустил ли его сам Compose.
hermes_started() { docker inspect -f '{{.State.StartedAt}}' "$c" 2>/dev/null || true; }
started_before="$(hermes_started)"
docker compose up -d --remove-orphans

echo "Жду запуска сервиса переписки…"
if ! wait_service; then
  echo "Сервис переписки не ответил за 90 секунд. Смотрите: docker logs --tail 50 shturman-service" >&2
  exit 1
fi

# Сокет для обратного прокси (Р-76). Сервис проверяет каталог /run/shturman-service только при
# запуске: если каталог создали или исправили позже (config/shturman-service-socket.conf), а сервис
# с тех пор не перезапускался, он так и слушает только порт. Тогда перезапускаем его один раз.
# Из состояния сервиса берётся только код состояния сокета.
sock_dir=/run/shturman-service
case "$(stat -c '%u %a' "$sock_dir" 2>/dev/null || true)" in
  "$(id -u) 2710"|"$(id -u) 2750")
    sock_state="$(docker exec shturman-service shturman call GET /api/status 2>/dev/null | docker exec -i shturman-service python -c '
import json, sys
try:
    print((json.load(sys.stdin).get("setup") or {}).get("socket") or "")
except Exception:
    pass' 2>/dev/null || true)"
    case "$sock_state" in
      dir_missing|dir_owner|dir_mode)
        echo "Каталог сокета для прокси готов, а сервис переписки запущен раньше — перезапускаю его, чтобы он открыл сокет…"
        docker compose restart shturman
        if ! wait_service; then
          echo "Сервис переписки не ответил за 90 секунд. Смотрите: docker logs --tail 50 shturman-service" >&2
          exit 1
        fi ;;
    esac ;;
esac

if [ "$MODE" != hermes ]; then
  echo "пропущено: режим без Hermes — включение плагина shturman в Hermes"
  echo "пропущено: режим без Hermes — запись архива (MCP-сервер shturman) в настройки Hermes"
  echo "пропущено: режим без Hermes — ожидание дашборда Hermes"
  echo "Архив к своему Codex CLI или Claude Code владелец подключает по ./ops/connect.sh."
  finish
fi

echo "Жду запуска контейнера…"
for _ in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)" = "running" ] && hx true 2>/dev/null && break
  sleep 2
done

# Плагин «Штурмана» включается штатной командой Hermes. Без него дашборд с заданным внешним
# адресом не запустится: Hermes требует хотя бы один способ входа.
restart=no
if [ "$(plugin_enabled)" != "yes" ]; then
  echo "Включаю плагин shturman…"
  hx hermes plugins enable shturman
  [ "$(plugin_enabled)" = "yes" ] || { echo "плагин shturman не включился" >&2; exit 1; }
  restart=yes
fi
case "$(mcp_register)" in
  changed) echo "Архив переписки подключён к Hermes (MCP-сервер shturman)."; restart=yes ;;
  same) ;;
  *) echo "не удалось записать MCP-сервер shturman в настройки Hermes" >&2; exit 1 ;;
esac
case "$(mcp_key_sync)" in
  changed) echo "Токен архива записан в .env профиля Hermes — Hermes перезапустится и подключится к архиву."; restart=yes ;;
  same) ;;
  missing) echo "В окружении контейнера Hermes нет токена архива (SHTURMAN_MCP_TOKEN в .env пуст?) — архив не будет доступен ассистенту. ./ops/init-env.sh --auto, затем ./ops/up.sh" >&2 ;;
  *) echo "не удалось записать токен архива в .env профиля Hermes" >&2; exit 1 ;;
esac
tz_result="$(tz_sync)"
case "$tz_result" in
  none) ;;
  "same "*) ;;
  "pending "*) echo "Часовой пояс ${tz_result#pending } записан в настройки Hermes после его запуска — перезапускаю Hermes, чтобы шлюз считал сводки по нему."; restart=yes ;;
  "changed "*) echo "Часовой пояс ${tz_result#changed } записан в настройки Hermes (timezone в config.yaml) — Hermes перезапустится, и сводки пойдут по этому поясу."; restart=yes ;;
  "invalid "*) echo "В окружении Hermes неизвестный часовой пояс (SHTURMAN_TIMEZONE в .env) — в настройки Hermes он не записан. Исправьте строку на имя вида Europe/Moscow и повторите ./ops/up.sh" >&2 ;;
  *) echo "не удалось записать часовой пояс в настройки Hermes (hermes config set timezone …) — сводки пойдут по часам сервера; смотрите ./ops/doctor.sh, строка hermes-tz" >&2; exit 1 ;;
esac
# Hermes читает плагин при запуске и держит его в памяти. Если файлы плагина изменились с прошлого
# запуска (обновление репозитория), а контейнер Hermes при этом не перезапускался, он продолжает
# исполнять прежний код: прежний мастер и прежний перечень маршрутов прохода к сервису — в том
# числе тех, что в новой версии закрыты. Поэтому при изменившемся плагине Hermes перезапускается.
plugin_now="$(plugin_print)"
plugin_was=""
if [ -f "$PLUGIN_MARK" ]; then plugin_was="$(head -n 1 "$PLUGIN_MARK")"; fi
if [ "$plugin_now" != "$plugin_was" ] && [ -n "$started_before" ] && [ "$(hermes_started)" = "$started_before" ]; then
  echo "Файлы плагина shturman изменились с прошлого запуска — перезапускаю Hermes, чтобы он загрузил новый код плагина…"
  restart=yes
fi
[ "$restart" = "yes" ] && docker compose restart hermes

echo "Жду запуска Hermes…"
if wait_dashboard; then
  echo "Дашборд Hermes отвечает на локальном адресе."
  # Отпечаток кода плагина, с которым Hermes сейчас работает (секретов в нём нет).
  mkdir -p local
  printf '%s\n' "$plugin_now" > "$PLUGIN_MARK"
  # После перезапуска шлюз считает по записанному поясу; задачи, посчитанные по прежнему, — заново.
  cron_resync || echo "Часть задач по расписанию не пересчитана — проверьте строку cron-schedule ниже." >&2
  finish
fi
echo "Дашборд не ответил за 90 секунд. Смотрите: ./ops/diag.sh --print (журнал Hermes — только счётчики ошибок; сам журнал — только с разрешения владельца: в нём начало текста его сообщений)" >&2
exit 1
