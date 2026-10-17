#!/usr/bin/env bash
# Проверка ./ops/report.sh (Р-102): подставной doctor с заведомо личными и секретными строками, итог обновления,
# свободный текст агента и строки дефектов, в которые вписаны ВСЕ строки fixtures/secrets.txt, — ни одна из них
# не попадает ни в показанный отчёт, ни в сохранённый JSON, ни в то, что передано сервису. Отчёт уходит
# сервису (подставной docker exec … ops-report submit) и сохраняется с правами 600; без сервиса — сохраняется
# и называется --send. Запуск из корня репозитория: bash ops/tests/report.sh.
set -eu
src="$(cd "$(dirname "$0")/../.." && pwd)"
fx="$src/ops/tests/fixtures"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/repo/local/update" "$tmp/stub" "$tmp/repo/docs"
cp -r "$src/ops" "$src/AGENTS.md" "$src/prompts" "$tmp/repo/"
cp -r "$src/docs/runbooks" "$src/docs/pilot" "$src/docs/releases.md" "$src/docs/deployment.md" "$tmp/repo/docs/"
rm -rf "$tmp/repo/ops/tests"
cd "$tmp/repo"
git init -q . && git -c user.name=t -c user.email=t@example.com commit -q --allow-empty -m x
git tag v0.0.10-rc.1
printf '0.0.10\n' > VERSION
printf 'SHTURMAN_MODE=hermes\nSHTURMAN_BOT_TOKEN=123456789:AAH-secret-bot-token-ABCDEFGHIJKLMNOP\n' > .env
cp "$fx/doctor.txt" "$tmp/doctor.txt"
printf '#!/usr/bin/env bash\ncat "%s/doctor.txt"\n' "$tmp" > ops/doctor.sh
cat > "$tmp/stub/docker" <<EOF
#!/bin/sh
echo "docker \$*" >> "$tmp/docker.log"
case "\$*" in
  "inspect -f {{.State.Status}} shturman-service") [ -f "$tmp/service-up" ] && echo running || exit 1 ;;
  "exec -i shturman-service shturman ops-report submit") cat > "$tmp/submitted.json"; echo "Карточка отчёта отправлена владельцу"; exit 0 ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$tmp/stub/docker" ops/doctor.sh
export PATH="$tmp/stub:$PATH"
bad() { echo "ОШИБКА: $*" >&2; cat "$tmp/out.txt" >&2 2> /dev/null || true; exit 1; }
secrets=()
while IFS= read -r line; do [ -n "$line" ] && secrets+=("$line"); done < "$fx/secrets.txt"
all="$(printf '%s ' "${secrets[@]}")"

./ops/report.sh --start > "$tmp/out.txt" 2>&1 || bad "--start"
[ -f local/report/session.json ] && [ "$(stat -c '%a' local/report/session.json)" = 600 ] || bad "снимок «до» не сохранён с правами 600"
printf 'run=20261020T090000Z\nfrom_name=v0.0.9-rc.2\nto_name=v0.0.10-rc.1\nresult=ok\nbackup=20261020T085900Z\n' > local/update/last.env
touch local/update/last.env
touch "$tmp/service-up"
./ops/report.sh --scenario О2 --result clean --deviation "Иван Петров попросил: $all" --helped "$all ./ops/update.sh --all помогло" \
  --defect "| Д-? | 2026-10-20 | $all — смета по фасадам | О2 | мешает | сопровождение | Codex | не исправлено | нет |" \
  --defect "не строка дефекта" > "$tmp/out.txt" 2>&1 || bad "сборка отчёта"
saved="$(find local/report -name 'r-*.json' | head -n 1)"
[ -n "$saved" ] && [ "$(stat -c '%a' "$saved")" = 600 ] || bad "отчёт не сохранён с правами 600"
[ -s "$tmp/submitted.json" ] || bad "отчёт не передан сервису"
cmp -s "$saved" "$tmp/submitted.json" || bad "сервису передано не то, что сохранено"
for s in "${secrets[@]}"; do
  if grep -qF -- "$s" "$tmp/out.txt" "$saved"; then bad "в отчёт попало: $s"; fi
done
grep -qF ivanpetrov "$saved" && bad "в отчёт попало имя у посредника"
python3 - "$saved" <<'PY' || bad "форма отчёта"
import json, sys
r = json.load(open(sys.argv[1], encoding="utf-8"))
assert r["shturman_report"] == 1 and r["scenario"] == "О2" and r["result"] == "без вмешательства", r
assert r["scenario_title"] == "Обновление «Штурмана»", r["scenario_title"]
assert r["before"]["tag"] == r["after"]["tag"] == "v0.0.10-rc.1" and r["after"]["version"] == "0.0.10"
d = r["after"]["doctor"]
assert d["total"] == "FAIL (1)" and d["counts"]["FAIL"] == 1 and d["counts"]["WARN"] == 2, d
assert [x["name"] for x in d["rows"]] == ["tg-session", "hermes-tz", "stopped"], d["rows"]
assert r["update"] == {"from": "v0.0.9-rc.2", "to": "v0.0.10-rc.1", "result": "ok", "backup": True}, r["update"]
assert len(r["defects"]) == 1 and r["defects"][0]["function"] == "О2" and r["defects"][0]["severity"] == "мешает"
assert "./ops/update.sh --all" in r["helped"] and "<имя>" in r["deviations"]
PY
grep -q "Отчёт сессии сопровождения «Штурмана»" "$tmp/out.txt" || bad "отчёт не показан"
grep -q "нажмите «Отправить»" "$tmp/out.txt" || bad "нет подсказки для владельца"
[ ! -f local/report/session.json ] || bad "снимок «до» не убран после отчёта"
[ -z "$(find local/report -name 'work.*')" ] || bad "рабочий каталог не убран"
echo "report: отчёт без секретов, передан сервису"

rm -f "$tmp/service-up" "$tmp/submitted.json"
./ops/report.sh --scenario У17 --result failed > "$tmp/out.txt" 2>&1 || bad "отчёт без сервиса"
grep -q -- "--send" "$tmp/out.txt" || bad "без сервиса не названо, как отправить позже"
[ ! -f "$tmp/submitted.json" ] || bad "без сервиса что-то передано"
grep -q "Проверка до: не снималась" "$tmp/out.txt" || bad "без --start снимок «до» должен называться неснятым"
touch "$tmp/service-up"
./ops/report.sh --send > "$tmp/out.txt" 2>&1 || bad "--send"
[ -s "$tmp/submitted.json" ] || bad "--send не передал отчёт"
echo "report: без сервиса — сохранён, --send отправляет"

for args in "--scenario Q1 --result clean" "--scenario О2 --result maybe" "--start --scenario О2" "--notes x --scenario О2"; do
  code=0
  # shellcheck disable=SC2086
  ./ops/report.sh $args > "$tmp/out.txt" 2>&1 || code=$?
  [ "$code" -eq 2 ] || bad "$args: код $code вместо 2"
done
printf 'отступления: перезапуск руками Иван Петров\n| Д-? | 2026-10-20 | бот молчал | О8 | мешает | эксплуатация | Codex | не исправлено | нет |\n' \
  | ./ops/report.sh --scenario О8 --result intervened --notes - --no-send > "$tmp/out.txt" 2>&1 || bad "--notes -"
grep -q "Где отступили от runbook: перезапуск руками <имя>" "$tmp/out.txt" || bad "--notes: отступления"
grep -q "| Д-? | 2026-10-20 | бот молчал | О8 |" "$tmp/out.txt" || bad "--notes: строка дефекта"
echo "report: неверные параметры — код 2; --notes со стандартного ввода"
