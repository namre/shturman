#!/usr/bin/env bash
# Переход экземпляра через сброс истории репозитория (docs/releases.md, «Сброс истории и закрытый репозиторий»).
# Когда историю репозитория на GitHub начинают заново (новый первый коммит вместо прежних), прежний
# коммит экземпляра — не предок новых выпусков, и ./ops/update.sh («только вперёд») их не берёт.
# Новая история называет в файле ops/history-reset прежний коммит, который она продолжает. Скрипт
# проверяет выпуски (теги vX.Y.Z, vX.Y.Z-rc.N) и origin/main, уже полученные git fetch: если ветка
# не связана с нынешним коммитом, а её первый коммит называет в ops/history-reset прежний коммит, до
# которого нынешний код доходит по истории, — записывает это локально: git replace --graft <первый
# коммит новой истории> <прежний коммит>. Дальше ./ops/update.sh обновляет как обычно, по одному
# выпуску. Отслеживаемые файлы, .env, данные и Docker не трогаются; в сеть скрипт не ходит.
#   ./ops/history-reset.sh            — проверить и, если нужно и можно, записать переход
#   ./ops/history-reset.sh --dry-run  — только сказать, что было бы сделано
#   -h, --help                        — эта справка; ничего не делается
# Его зовёт ./ops/update.sh после git fetch. Экземпляру, чей ./ops/update.sh этого скрипта ещё не знает
# (0.0.13), его запускают один раз из новой истории — UPGRADING.md, «0.0.13 → 0.0.14»:
#   git show origin/main:ops/history-reset.sh > local/history-reset.sh && bash local/history-reset.sh
# Отменить переход: git replace -d <первый коммит новой истории> (пока экземпляр на прежнем коде).
# Коды: 0 — переход не нужен или записан (в том числе раньше); 2 — неверные параметры; 3 — есть ветка
# другой истории, а перейти на неё нельзя (нет ops/history-reset, нынешний коммит в нём не назван или
# прежнего коммита на экземпляре нет): стоп, сказать разработчику.
set -eu
cd "$(dirname "$0")/.." || exit 1
. ops/lib.sh
ops_help "$@"

MARK="ops/history-reset"
dry=no
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry=yes ;;
    *) ops_unknown "$1" ;;
  esac
  shift
done
git rev-parse --git-dir > /dev/null 2>&1 || { echo "каталог не репозиторий git — переходить нечему" >&2; exit 2; }

# Полные SHA из файла-отметки коммита $1 (по одному в строке; комментарии и подписи — мимо).
marked() {
  git show "$1:$MARK" 2> /dev/null | sed 's/#.*//' | awk '$1 ~ /^[0-9a-f]{40}$/ { print $1 }'
}

head="$(git rev-parse HEAD)"
ver="$(tr -d '[:space:]' < VERSION 2> /dev/null || true)"
# Кандидаты: выпуски не старше нынешней версии (прежние теги на экземпляре остаются и целями не бывают)
# и origin/main.
refs=""
for t in $(git tag -l 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$' || true); do
  v="${t#v}"; v="${v%%-rc.*}"
  if [ -n "$ver" ] && [ "$v" != "$ver" ] && [ "$(printf '%s\n%s\n' "$v" "$ver" | sort -V | head -n 1)" = "$v" ]; then continue; fi
  refs="$refs$t"$'\n'
done
if git rev-parse -q --verify 'refs/remotes/origin/main^{commit}' > /dev/null; then refs="${refs}origin/main"$'\n'; fi

# Не связана ли ветка $1 с нынешним кодом (в том числе через уже записанный переход).
unrelated() { ! git merge-base "$head" "$1" > /dev/null 2>&1; }

done_roots=" " fails=""
while IFS= read -r ref; do
  [ -n "$ref" ] || continue
  c="$(git rev-parse -q --verify "$ref^{commit}")" || continue
  unrelated "$c" || continue
  roots="$(git rev-list --max-parents=0 "$c")"
  if [ "$(printf '%s\n' "$roots" | grep -c .)" -ne 1 ]; then
    fails="$fails$ref"$'\t'"у новой истории не один первый коммит — переход не записывается"$'\n'; continue
  fi
  root="$roots"
  case "$done_roots" in *" $root "*) continue ;; esac
  if [ -z "$(marked "$root")" ]; then
    fails="$fails$ref"$'\t'"в первом коммите новой истории (${root:0:12}) нет файла $MARK с прежним коммитом"$'\n'; continue
  fi
  # Прежний коммит, который продолжает новая история (назван в её первом коммите): есть на экземпляре,
  # и нынешний код до него доходит (тот же коммит или раньше в прежней истории).
  base="" known=""
  for x in $(marked "$root"); do
    git cat-file -e "$x^{commit}" 2> /dev/null || continue
    known="$known ${x:0:12}"
    if git merge-base --is-ancestor "$head" "$x" 2> /dev/null; then base="$x"; break; fi
  done
  if [ -z "$base" ]; then
    if [ -z "$known" ]; then
      fails="$fails$ref"$'\t'"новая история продолжает коммит $(marked "$root" | head -n 1 | cut -c1-12), которого на этом экземпляре нет"$'\n'
    else
      fails="$fails$ref"$'\t'"новая история продолжает коммит$known, а нынешний код (${head:0:12}) к нему не ведёт"$'\n'
    fi
    continue
  fi
  done_roots="$done_roots$root "
  label="$(git show "$root:$MARK" 2> /dev/null | sed 's/#.*//' | awk -v x="$base" '$1 == x { $1 = ""; sub(/^ /, ""); print; exit }')"
  if [ "$dry" = yes ]; then
    echo "Сброс истории: записал бы переход — первый коммит новой истории ${root:0:12} продолжает прежний ${base:0:12}${label:+ ($label)}."
    continue
  fi
  if git rev-parse -q --verify "refs/replace/$root" > /dev/null; then
    fails="$fails$ref"$'\t'"для ${root:0:12} уже записана другая замена (git replace -l) — не трогаю"$'\n'; continue
  fi
  if ! git replace --graft "$root" "$base" 2> /dev/null || ! git merge-base --is-ancestor "$head" "$c"; then
    git replace -d "$root" > /dev/null 2>&1 || true
    fails="$fails$ref"$'\t'"переход не записался (git replace --graft) — снят"$'\n'; continue
  fi
  echo "Сброс истории: первый коммит новой истории ${root:0:12} продолжает прежний ${base:0:12}${label:+ ($label)} — записано локально (git replace). Дальше обновление идёт как обычно, по одному выпуску."
done <<< "$refs"

# Ветка, которую связал переход, записанный уже после неё в этом же проходе, — не неудача.
left=""
while IFS="$(printf '\t')" read -r ref why; do
  [ -n "$ref" ] || continue
  c="$(git rev-parse -q --verify "$ref^{commit}")" || continue
  if [ "$dry" = no ] && ! unrelated "$c"; then continue; fi
  left="$left  $ref: $why"$'\n'
done <<< "$fails"
if [ -n "$left" ]; then
  printf 'На GitHub ветка другой истории репозитория, а перейти на неё нельзя:\n%s' "$left" >&2
  echo "Ничего не изменено. Стоп: скажите разработчику (docs/releases.md, «Сброс истории и закрытый репозиторий»); git reset и git pull руками не делать." >&2
  exit 3
fi
exit 0
