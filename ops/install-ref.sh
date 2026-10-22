#!/usr/bin/env bash
# Новая установка — с последнего выпуска, а не с основной ветки: переключает свежий клон на тег.
# Запускается сразу после git clone, до ./ops/init-env.sh. Основная ветка (main) может быть впереди
# выпуска — там изменения, которые ещё не прошли обкатку; ставить их новому владельцу нельзя.
#
#   ./ops/install-ref.sh          — выбрать и переключить: последний выпуск vX.Y.Z; выпусков ещё нет —
#                                   последний кандидат vX.Y.Z-rc.N (тогда ./ops/init-env.sh --auto сам
#                                   запишет канал обновлений pilot: кандидатам — следующие кандидаты).
#                                   Тег, у которого файл VERSION не совпадает с номером, — не выпуск: мимо
#   ./ops/install-ref.sh --check  — только назвать, что было бы выбрано; меняет лишь ссылки git (git fetch
#                                   тегов), рабочую копию не трогает
#
# Годится и для клона на компьютере агента, который ставит сервер по SSH: документы он читает того же
# выпуска, что ставит на сервер (AGENTS.md, «Перед любыми действиями»). После переключения ветки нет
# (HEAD отделён) — так и должно быть: дальше код меняет ./ops/update.sh.
#
# Отказывает (код 2), если экземпляр уже развёрнут (.env не пуст) — его код меняет только
# ./ops/update.sh, — и если отслеживаемые файлы изменены. Тегов выпусков нет — код 1, ничего не изменено.
# Меняет только рабочую копию git (git fetch тегов, git checkout --detach); .env, данные и Docker не трогает.
# -h, --help — эта справка.
set -u
. "$(dirname "$0")/lib.sh"
ops_help "$@"
check=no
while [ $# -gt 0 ]; do
  case "$1" in
    --check) check=yes ;;
    *) ops_unknown "$1" ;;
  esac
  shift
done
cd "$(dirname "$0")/.." || exit 1

REL_RE='^v[0-9]+\.[0-9]+\.[0-9]+$'
RC_RE='^v[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9]+$'

git rev-parse --git-dir > /dev/null 2>&1 || { echo "каталог не репозиторий git — нужен клон: git clone https://github.com/namre/shturman.git" >&2; exit 2; }
if [ -s .env ]; then
  echo "Экземпляр уже развёрнут (.env есть): его код меняет только ./ops/update.sh (docs/runbooks/update.md). Ничего не изменено." >&2
  exit 2
fi
if ! git diff --quiet HEAD -- 2> /dev/null; then
  echo "Отслеживаемые файлы изменены (git status) — переключать нельзя. Ничего не изменено; скажите владельцу." >&2
  exit 2
fi
[ "$check" = yes ] || ops_lock "выбор версии для новой установки (./ops/install-ref.sh)"

git fetch --quiet --tags origin 2> /dev/null || echo "Теги с GitHub получить не удалось — выбираю из тех, что есть в клоне." >&2

# «ключ<TAB>тег» подходящего вида, у которого VERSION совпадает с номером. $1 — шаблон тега.
tags_of() {
  local re="$1" t c v
  git tag -l 'v*' | while IFS= read -r t; do
    printf '%s' "$t" | grep -Eq "$re" || continue
    c="$(git rev-parse -q --verify "$t^{commit}")" || continue
    v="${t#v}"; v="${v%%-rc.*}"
    [ "$(git show "$c:VERSION" 2> /dev/null | tr -d '[:space:]')" = "$v" ] || continue
    printf '%s\t%s\n' "$(tag_key "$t")" "$t"
  done | sort -V | tail -n 1 | cut -f2
}
tag="$(tags_of "$REL_RE")"; kind="выпуск"
if [ -z "$tag" ]; then tag="$(tags_of "$RC_RE")"; kind="кандидат"; fi
if [ -z "$tag" ]; then
  echo "Тегов выпусков (vX.Y.Z или vX.Y.Z-rc.N) в репозитории нет — ставить не с чего. Ничего не изменено; стоп, сказать разработчику." >&2
  exit 1
fi

if [ "$check" = yes ]; then
  echo "Новая установка встанет на $kind $tag (коммит $(git rev-parse --short=12 "$tag^{commit}"))."
  exit 0
fi
git checkout -q --detach "$tag" || { echo "git checkout $tag не прошёл. Ничего не изменено." >&2; exit 1; }
echo "Код переключён на $kind $tag: VERSION $(tr -d '[:space:]' < VERSION), коммит $(git rev-parse --short=12 HEAD)."
[ "$kind" = выпуск ] || echo "Выпусков ещё нет — это кандидат; ./ops/init-env.sh --auto запишет канал обновлений pilot (docs/releases.md)."
