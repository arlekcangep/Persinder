#!/usr/bin/env bash
# deploy.sh — обёртка над deploy/deploy.py (цели test/build см. там).
#
#   ./deploy.sh test     — все тесты (pytest + vitest)
#   ./deploy.sh build    — сборка веб-клиента и wheel в dist/
#
# Просто запускает python-скрипт тем python'ом, который выбран общим
# способом (scripts/py.sh) и передаёт его deploy.py.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Общий выбор python (см. scripts/py.sh): задаёт PY и PY_FROM_VENV.
# shellcheck source=scripts/py.sh
source "$REPO/scripts/py.sh"

export PERSINDER_DEPLOY_PYTHON="$PY"
exec "$PY" "$REPO/deploy/deploy.py" "$@"
