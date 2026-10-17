#!/usr/bin/env bash
# Проверка выпуска перед тегом и в CI на теге (docs/releases.md):
#   bash ops/tests/release_check.sh vX.Y.Z        — выпуск
#   bash ops/tests/release_check.sh vX.Y.Z-rc.N   — кандидат для пилота
# Проверяет: VERSION = X.Y.Z; в CHANGELOG.md есть раздел «## X.Y.Z»; в UPGRADING.md раздел «… → X.Y.Z»
# есть и не помечен «(не выпущено)» (или раздела нет — выпуск без действий для владельца, тогда в
# CHANGELOG об этом сказано); образ Hermes по умолчанию из compose.yaml — в config/hermes-versions.txt;
# в списке выпусков releases.json есть запись этого тега с версией VERSION, форма списка верна, его срок
# годности не вышел, а образы Hermes в нём — те же, что в config/hermes-versions.txt (Р-101; иначе
# экземпляры о выпуске не узнают); версия пакета сервиса (service/pyproject.toml) — VERSION;
# если тег уже стоит — он указывает на проверяемый коммит. Код 0 — можно ставить тег.
set -eu
cd "$(dirname "$0")/../.." || exit 1
tag="${1:-}"
printf '%s' "$tag" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$' || { echo "нужен тег вида v0.0.8 или v0.0.8-rc.1" >&2; exit 2; }
ver="${tag#v}"; ver="${ver%%-rc.*}"
bad=0
say_bad() { echo "НЕТ: $1"; bad=1; }
[ "$(tr -d '[:space:]' < VERSION)" = "$ver" ] && echo "да: VERSION = $ver" || say_bad "VERSION = $(cat VERSION), а тег — $tag"
grep -Eq "^## $ver( |$)" CHANGELOG.md && echo "да: в CHANGELOG.md раздел «## $ver»" || say_bad "в CHANGELOG.md нет раздела «## $ver …» (раздел «Не выпущено» переименовать в «## $ver — ГГГГ-ММ-ДД»)"
head_line="$(grep -E "^## .*→ $ver( |$)" UPGRADING.md | head -n 1 || true)"
if [ -z "$head_line" ]; then
  echo "да: раздела «→ $ver» в UPGRADING.md нет — выпуск без действий сверх ./ops/update.sh"
elif printf '%s' "$head_line" | grep -q 'не выпущено'; then
  say_bad "в UPGRADING.md раздел помечен «не выпущено»: $head_line"
else
  echo "да: в UPGRADING.md раздел «${head_line#\#\# }»"
fi
def="$(sed -n 's/.*hermes-agent:\${HERMES_VERSION:-\([^}]*\)}.*/\1/p' compose.yaml | head -n 1)"
if awk -F'\t' -v t="$def" '!/^#/ && $1 == t { f = 1 } END { exit f ? 0 : 1 }' config/hermes-versions.txt; then
  echo "да: Hermes по умолчанию ($def) — в списке проверенных"
else
  say_bad "Hermes по умолчанию ($def) нет в config/hermes-versions.txt"
fi
if out="$(python3 -I ops/release_manifest.py check releases.json --hermes-file config/hermes-versions.txt \
           --tag "$tag" --version "$ver" --fresh 2>&1)"; then
  echo "$out"
else
  say_bad "список выпусков releases.json: $(printf '%s' "$out" | sed 's/^НЕТ: //' | paste -sd';' -)"
fi
pv="$(sed -n 's/^version = "\([^"]*\)"/\1/p' service/pyproject.toml | head -n 1)"
iv="$(sed -n 's/^__version__ = "\([^"]*\)"/\1/p' service/src/shturman/__init__.py | head -n 1)"
if [ "$pv" = "$ver" ] && [ "$iv" = "$ver" ]; then echo "да: версия пакета сервиса — $ver"
else say_bad "версия пакета сервиса: pyproject.toml — $pv, __init__.py — $iv, а VERSION — $ver"; fi
if git rev-parse -q --verify "refs/tags/$tag" > /dev/null 2>&1; then
  [ "$(git rev-parse "$tag^{commit}")" = "$(git rev-parse HEAD)" ] && echo "да: тег $tag стоит на этом коммите" || say_bad "тег $tag уже стоит на другом коммите"
fi
[ "$bad" -eq 0 ] || exit 1
echo "Выпуск $tag готов к тегу."
