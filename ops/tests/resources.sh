#!/usr/bin/env bash
# ./ops/resources.sh (Д-26) с подставным Docker: память и процессор контейнеров печатаются, а docker
# inspect зовётся только с шаблоном из перечня полей — без окружения (Config.Env) и без шаблона вовсе.
# И во всех скриптах ops/ — ни одного шаблона inspect с окружением контейнера (Д-26).
# Запуск из корня репозитория: bash ops/tests/resources.sh.
set -eu
src="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/repo" "$tmp/stub"
cp -r "$src/ops" "$tmp/repo/"
cat > "$tmp/stub/docker" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$tmp/calls"
case "\$1" in
  ps) printf 'shturman-service\nshturman-embeddings\nshturman-asr\nother-app\n' ;;
  stats) printf 'shturman-service|312.5MiB / 7.66GiB|3.98%%|0.50%%\nshturman-embeddings|1.42GiB / 1.5GiB|94.67%%|0.10%%\nother-app|1GiB / 7.66GiB|13%%|1%%\n' ;;
  inspect)
    case "\$*" in
      *shturman-service) echo 'running|0|0|0|false' ;;
      *shturman-embeddings) echo 'running|1610612736|0|2|false' ;;
      *shturman-asr) echo 'exited|2147483648|2000000000|5|true' ;;
      *) exit 1 ;;
    esac ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$tmp/stub/docker"
export PATH="$tmp/stub:$PATH"
cd "$tmp/repo"
bad() { echo "ОШИБКА: $*" >&2; cat "$tmp/out.txt" >&2; exit 1; }

./ops/resources.sh > "$tmp/out.txt" 2>&1 || bad "код не 0"
grep -q '^Сервер: память ' "$tmp/out.txt" || bad "нет строки сервера"
grep -Eq '^shturman-service +работает  память 312.5MiB, предела нет \(до памяти сервера\)  процессор 0.50%  перезапусков: 0$' "$tmp/out.txt" || bad "нет строки сервиса"
grep -Eq '^shturman-embeddings +работает  память 1.42GiB из 1.5 ГБ \(94.67%\)  процессор 0.10%  перезапусков: 2  ВНИМАНИЕ: у предела памяти$' "$tmp/out.txt" || bad "нет строки эмбеддингов"
grep -Eq '^shturman-asr +остановлен  предел 2.0 ГБ, предел 2 проц.  перезапусков: 5  ВНИМАНИЕ: последний раз убит за нехватку памяти \(OOM\)$' "$tmp/out.txt" || bad "нет строки asr"
grep -q 'other-app' "$tmp/out.txt" && bad "чужой контейнер в выводе"
# docker inspect — только с шаблоном, и в шаблоне нет окружения, команды и каталогов.
grep '^inspect' "$tmp/calls" > "$tmp/inspect" || bad "inspect не звался"
while IFS= read -r call; do
  case "$call" in "inspect --format "*) ;; *) bad "inspect без шаблона: $call" ;; esac
  case "$call" in *Env*|*.Config*|*Mounts*|*Args*|*Cmd*|*json*) bad "лишнее поле в шаблоне: $call" ;; esac
done < "$tmp/inspect"
# Справка не зовёт Docker.
: > "$tmp/calls"; ./ops/resources.sh --help > /dev/null
[ -s "$tmp/calls" ] && bad "справка позвала Docker"
code=0; ./ops/resources.sh x > /dev/null 2>&1 || code=$?; [ "$code" -eq 2 ] || bad "лишний параметр: код $code вместо 2"
# Ни один скрипт ops/ не читает окружение контейнера (Д-26): ошибка в шаблоне или лишняя команда напечатала бы
# токены. Нельзя: шаблон inspect с окружением ({{range .Config.Env}}, {{json .Config}}, {{.Config}} целиком),
# docker inspect без шаблона (кроме проверки «есть ли контейнер» с выводом в /dev/null), `docker exec … env`
# и printenv без имени переменной. Можно: `docker exec <контейнер> printenv ИМЯ` — одна несекретная переменная;
# коммит работающего кода — из GET /api/status (Д-58). Комментарии не в счёт.
rules='
  /^[[:space:]]*#/ { next }
  /\.Config\.Env|\{\{[^}]*[^A-Za-z_]Env[^A-Za-z_]|json[[:space:]]+\.Config|\{\{[[:space:]]*\.Config[[:space:]]*\}\}|\{\{[[:space:]]*json[[:space:]]+\.[[:space:]]*\}\}/ { print FILENAME ":" FNR ": " $0; next }
  /docker[[:space:]]+inspect/ && !/(-f|--format)[[:space:]=]/ && !/docker[[:space:]]+inspect[[:space:]]+[^|;&]*>[[:space:]]*\/dev\/null/ { print FILENAME ":" FNR ": " $0; next }
  /docker[[:space:]]+exec[^|;&]*[[:space:]]env([[:space:]]|$|\)|"|;)/ { print FILENAME ":" FNR ": " $0; next }
  /printenv([[:space:]]*($|\)|"|\||;|&|[0-9]?>))/ { print FILENAME ":" FNR ": " $0; next }'
envuse="$(awk "$rules" "$src"/ops/*.sh "$src"/ops/tests/stub/docker)"
[ -z "$envuse" ] || { echo "ОШИБКА: скрипт ops/ читает окружение контейнера:" >&2; printf '%s\n' "$envuse" >&2; exit 1; }
# Сама проверка ловит то, что должна, и не мешает разрешённому: образцы — по одному на правило.
probe="$tmp/probe.sh"
for bad in "docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' c" "docker inspect -f '{{json .Config}}' c" \
           "docker inspect --format '{{.Config}}' c" "x=\"\$(docker inspect shturman-service)\"" "docker exec c env" \
           "docker exec c printenv | grep X" "docker exec c printenv"; do
  printf '%s\n' "$bad" > "$probe"
  [ -n "$(awk "$rules" "$probe")" ] || bad "проверка окружения пропустила: $bad"
done
for good in "docker exec \"\$c\" printenv HERMES_DASHBOARD_PUBLIC_URL 2>/dev/null" "if docker inspect shturman-asr >/dev/null 2>&1; then :; fi" \
            "docker inspect -f '{{.State.Status}}' c" "# docker inspect без шаблона печатает переменные"; do
  printf '%s\n' "$good" > "$probe"
  [ -z "$(awk "$rules" "$probe")" ] || bad "проверка окружения ложно сработала: $good"
done
echo "resources: ok"
