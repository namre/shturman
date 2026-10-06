#!/usr/bin/env bash
# Завершает все сессии дашборда (например, потеряно устройство, с которого входили).
# После этого вход — заново по коду от бота или по ссылке из ./ops/activation-link.sh.
set -eu
cd "$(dirname "$0")/.." || exit 1
docker exec -u "$(id -u):$(id -g)" shturman-hermes /opt/hermes/.venv/bin/python \
  /opt/data/plugins/shturman/cli.py logout-all
